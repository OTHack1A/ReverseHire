"""The tracker's logic: create, update, delete and list applications, follow-ups,
duplicates, statistics, appointments, CSV and folder export. No Qt.

Thread rules: every method may be called from any thread (the database keeps one
connection per thread). Methods that copy or hash files (``create`` with a CV path,
``set_cv``, ``add_attachment``, ``delete``, ``export_application``, ``import_csv``)
touch the disk: the widgets call them on a worker. Errors are raised to the caller,
who logs them and shows a notice (the widgets do); ``ValueError`` means invalid input.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import statistics as stats_module
import threading
import time
import unicodedata
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

from . import ics as ics_module
from .archive import Archive, ArchivedFile
from .db import Database
from .model import (
    ANSWERED_STATUSES, APPLICATION_COLUMNS, APPOINTMENT_COLUMNS, APPOINTMENT_EDITABLE,
    APPOINTMENT_KINDS, CHANNELS, EDITABLE_FIELDS, KIND_POSTING, KIND_SPONTANEOUS, KINDS,
    LONG_FIELDS, OPEN_STATUSES, OUTCOMES, SOURCES, STATUS_DRAFT, STATUS_INTERVIEW, STATUS_OFFER,
    STATUS_SENT, STATUSES, TRACKED_FIELDS, Application, Appointment, Attachment, FollowUp,
    HistoryEntry, ImportReport, Statistics, choice, day_text, moment_text, parse_day, parse_moment,
    working_days_between,
)

log = logging.getLogger("reversehire.applications")

DEFAULT_FOLLOW_UP_DAYS = 10     # working days without an answer
DUPLICATE_WINDOW_DAYS = 60
FOLDER_NAME = "applications"
DATABASE_NAME = "applications.sqlite"
ARCHIVE_NAME = "files"

CSV_COLUMNS = ("id", "company_id", "company_name", "role", "posting_url", "kind", "channel",
               "recipient_email", "contact_name", "sent_date", "status", "follow_up_date", "source",
               "cv_name", "cv_language", "cover_subject", "cover_letter", "notes",
               "next_appointment", "created")
_TEXT_COLUMNS = ("company_name", "role", "posting_url", "recipient_email", "contact_name", "cv_name",
                 "cv_language", "cover_subject", "cover_letter", "notes")
_FORMULA_START = "=+-@\t\r"


def default_folder() -> Path:
    """``<data folder>/applications`` (the data folder rules live in ``config``)."""
    from .. import config

    return config.data_folder() / FOLDER_NAME


def fold_text(text: str) -> str:
    """Case- and accent-insensitive form used for searching and duplicate checks."""
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold())
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).replace(".", " ")
                    .replace(",", " ").split())


def company_ref(company) -> tuple[int | None, str]:
    """(id, name) from a company card, a dict, a plain name or None."""
    if company is None:
        return None, ""
    if isinstance(company, str):
        return None, company.strip()
    if isinstance(company, dict):
        ident, name = company.get("id"), company.get("name", "")
    else:
        ident, name = getattr(company, "id", None), getattr(company, "name", "")
    try:
        ident = int(ident) if ident not in (None, "", 0) else None
    except (TypeError, ValueError):
        ident = None
    return ident, str(name or "").strip()


def _safe_name(text: str, limit: int = 60) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text or "").strip(" ._")
    return cleaned[:limit] or "application"


def _csv_safe(value: str) -> str:
    return "'" + value if value and value[0] in _FORMULA_START else value


def _csv_unsafe(value: str) -> str:
    return value[1:] if len(value) > 1 and value[0] == "'" and value[1] in _FORMULA_START else value


class DeleteResult:
    def __init__(self, deleted: bool, files_removed: int):
        self.deleted = deleted
        self.files_removed = files_removed


class ApplicationService:
    """Facade over the tracker's database and archive."""

    def __init__(self, folder: Path | str | None = None, *, clock: Callable[[], float] = time.time,
                 tr: Callable | None = None):
        self.folder = Path(folder) if folder is not None else default_folder()
        self.db = Database(self.folder / DATABASE_NAME)
        self.archive = Archive(self.folder / ARCHIVE_NAME)
        self._clock = clock
        self._tr = tr  # only used for the texts of the .ics files and the exported notes
        self._lock = threading.RLock()

    def close(self) -> None:
        self.db.close()

    # --- helpers -----------------------------------------------------------------------

    def _now(self) -> float:
        return float(self._clock())

    def today(self) -> date:
        return date.fromtimestamp(self._now())

    # --- reading -----------------------------------------------------------------------

    def get(self, application_id: int) -> Application | None:
        with self.db.reading() as conn:
            row = conn.execute("SELECT * FROM applications WHERE id = ?", (application_id,)).fetchone()
        return Application.from_row(row) if row else None

    def list(self, *, status=None, kind: str | None = None, company_id: int | None = None,
             company_name: str | None = None, sent_from=None, sent_to=None, text: str | None = None
             ) -> list[Application]:
        """Applications, newest first. `status` is one value or several; `sent_from`/`sent_to`
        (inclusive) exclude records without a sent date; `text` matches company, role,
        recipient, contact, subject, letter and notes (case- and accent-insensitive)."""
        where, params = [], []
        if status:
            values = [status] if isinstance(status, str) else list(status)
            where.append(f"status IN ({','.join('?' * len(values))})")
            params += values
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if company_id is not None:
            where.append("company_id = ?")
            params.append(company_id)
        first, last = day_text(sent_from), day_text(sent_to)
        if first:
            where.append("sent_date >= ?")
            params.append(first)
        if last:
            where.append("sent_date <= ?")
            params.append(last)
        sql = "SELECT * FROM applications" + (" WHERE " + " AND ".join(where) if where else "")
        with self.db.reading() as conn:
            rows = [Application.from_row(r) for r in conn.execute(sql, params).fetchall()]
        if company_name:
            wanted = fold_text(company_name)
            rows = [a for a in rows if fold_text(a.company_name) == wanted]
        if text and text.strip():
            needles = fold_text(text).split()
            rows = [a for a in rows if all(n in self._haystack(a) for n in needles)]
        rows.sort(key=lambda a: (a.sent_date or date.fromtimestamp(a.created).isoformat(), a.id), reverse=True)
        return rows

    @staticmethod
    def _haystack(app: Application) -> str:
        return fold_text(" ".join((app.company_name, app.role, app.recipient_email, app.contact_name,
                                   app.cover_subject, app.cover_letter, app.notes)))

    def count(self) -> int:
        with self.db.reading() as conn:
            return conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]

    def applications_for_company(self, company_id: int | None, company_name: str | None = None
                                 ) -> list[Application]:
        """The timeline of one company, newest first: by card id, plus records typed by name only."""
        rows = self.list(company_id=company_id) if company_id is not None else []
        if company_name:
            wanted = fold_text(company_name)
            known = {a.id for a in rows}
            rows += [a for a in self.list() if a.id not in known and a.company_id is None
                     and fold_text(a.company_name) == wanted]
            rows.sort(key=lambda a: (a.sent_date or date.fromtimestamp(a.created).isoformat(), a.id),
                      reverse=True)
        return rows

    # --- writing -----------------------------------------------------------------------

    @staticmethod
    def _validated(values: dict) -> dict:
        """Cleans the values of an application (enum fields are checked, dates normalised)."""
        clean = dict(values)
        for name, allowed in (("kind", KINDS), ("channel", CHANNELS), ("status", STATUSES),
                              ("source", SOURCES)):
            if name in clean and clean[name] not in allowed:
                raise ValueError(f"Invalid {name}: {clean[name]!r}")
        for name in ("sent_date", "follow_up_date"):
            if name in clean:
                raw = clean[name]
                clean[name] = day_text(raw)
                if raw not in (None, "") and clean[name] is None:
                    raise ValueError(f"Invalid date in {name}: {raw!r}")
        for name in ("company_name", "role", "posting_url", "recipient_email", "contact_name",
                     "cover_subject", "cv_language", "cv_name"):
            if name in clean:
                clean[name] = str(clean[name] or "").strip()
        for name in ("cover_letter", "notes"):
            if name in clean:
                clean[name] = str(clean[name] or "")
        if "company_id" in clean:
            ident = clean["company_id"]
            clean["company_id"] = int(ident) if ident not in (None, "") else None
        return clean

    def create(self, *, company_name: str = "", company_id: int | None = None, role: str = "",
               posting_url: str = "", kind: str | None = None, channel: str = "email",
               recipient_email: str = "", contact_name: str = "", sent_date=None,
               status: str | None = None, cover_subject: str = "", cover_letter: str = "",
               cv_path: Path | str | None = None, cv_language: str = "", notes: str = "",
               follow_up_date=None, source: str = "manual", who: str = "user") -> Application:
        """Creates a record. `kind` defaults to a reply when there is a posting URL, else
        spontaneous; `status` defaults to "sent" when a sent date is given, else "draft".
        `cv_path` is copied into the archive (call from a worker)."""
        if not str(company_name or "").strip():
            raise ValueError("The company name is required")
        kind = kind or (KIND_POSTING if posting_url else KIND_SPONTANEOUS)
        sent = day_text(sent_date)
        status = status or (STATUS_SENT if sent else STATUS_DRAFT)
        values = self._validated({
            "company_id": company_id, "company_name": company_name, "role": role,
            "posting_url": posting_url, "kind": kind, "channel": channel,
            "recipient_email": recipient_email, "contact_name": contact_name, "sent_date": sent,
            "status": status, "cover_subject": cover_subject, "cover_letter": cover_letter,
            "cv_language": cv_language, "notes": notes, "follow_up_date": follow_up_date,
            "source": source})
        archived: ArchivedFile | None = self.archive.store(cv_path) if cv_path else None
        if archived is not None:
            values["cv_hash"], values["cv_name"] = archived.hash, archived.name
        now = self._now()
        values.update(created=now, updated=now)
        columns = [c for c in APPLICATION_COLUMNS if c in values]
        with self.db.writing() as conn:
            cursor = conn.execute(
                f"INSERT INTO applications ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
                [values[c] for c in columns])
            application_id = cursor.lastrowid
            self._log(conn, application_id, who, "status", "", status)
        return self.get(application_id)

    def update(self, application_id: int, *, who: str = "user", cv_path: Path | str | None = None,
               **fields) -> Application:
        """Changes the given fields (see ``model.EDITABLE_FIELDS``) and logs tracked changes.
        Moving to "sent" without a sent date sets it to today. ``cv_path`` replaces the CV."""
        unknown = set(fields) - set(EDITABLE_FIELDS)
        if unknown:
            raise ValueError(f"Unknown fields: {sorted(unknown)}")
        archived = self.archive.store(cv_path) if cv_path else None
        with self._lock:
            current = self.get(application_id)
            if current is None:
                raise KeyError(application_id)
            changes = self._validated(fields)
            if archived is not None:
                changes["cv_hash"], changes["cv_name"] = archived.hash, archived.name
            if changes.get("status") == STATUS_SENT and not (changes.get("sent_date") or current.sent_date) \
                    and "sent_date" not in changes:
                changes["sent_date"] = self.today().isoformat()
            changes = {k: v for k, v in changes.items() if getattr(current, k) != v}
            if not changes:
                return current
            changes["updated"] = self._now()
            with self.db.writing() as conn:
                conn.execute(f"UPDATE applications SET {','.join(f'{k} = ?' for k in changes)} WHERE id = ?",
                             [*changes.values(), application_id])
                for name in TRACKED_FIELDS:
                    if name in changes:
                        old, new = getattr(current, name), changes[name]
                        if name in LONG_FIELDS:
                            old, new = f"({len(old or '')} chars)", f"({len(new or '')} chars)"
                        self._log(conn, application_id, who, name, old, new)
            updated = self.get(application_id)
        if archived is not None and current.cv_hash and current.cv_hash != updated.cv_hash:
            self._discard_if_unreferenced(current.cv_hash)
        return updated

    def set_status(self, application_id: int, status: str, *, who: str = "user") -> Application:
        return self.update(application_id, status=status, who=who)

    def mark_sent(self, application_id: int, when=None, *, who: str = "user") -> Application:
        """Marks the application as sent on `when` (default today). The app cannot know that the
        user pressed Send: this is the user's own statement."""
        day = day_text(when) or self.today().isoformat()
        return self.update(application_id, status=STATUS_SENT, sent_date=day, who=who)

    def set_cv(self, application_id: int, path: Path | str, *, who: str = "user") -> Application:
        return self.update(application_id, cv_path=path, who=who)

    def delete(self, application_id: int) -> DeleteResult:
        """Deletes a record with its appointments, history and attachments, then removes the
        archived files nobody else uses."""
        with self._lock:
            with self.db.writing() as conn:
                cursor = conn.execute("DELETE FROM applications WHERE id = ?", (application_id,))
                deleted = cursor.rowcount > 0
            removed = self.gc_archive() if deleted else 0
        return DeleteResult(deleted, removed)

    def _log(self, conn, application_id: int, who: str, name: str, old, new) -> None:
        conn.execute("INSERT INTO status_history (application_id, at, who, field, old, new) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (application_id, self._now(), who, name, "" if old is None else str(old),
                      "" if new is None else str(new)))

    def history(self, application_id: int) -> list[HistoryEntry]:
        with self.db.reading() as conn:
            rows = conn.execute("SELECT * FROM status_history WHERE application_id = ? ORDER BY at, id",
                                (application_id,)).fetchall()
        return [HistoryEntry.from_row(r) for r in rows]

    # --- archive -----------------------------------------------------------------------

    def referenced_hashes(self) -> set[str]:
        with self.db.reading() as conn:
            hashes = {r[0] for r in conn.execute("SELECT cv_hash FROM applications WHERE cv_hash != ''")}
            hashes |= {r[0] for r in conn.execute("SELECT hash FROM attachments")}
        return hashes

    def gc_archive(self) -> int:
        """Removes the archived files no record references; returns how many."""
        with self._lock:
            return self.archive.collect_garbage(self.referenced_hashes())

    def _discard_if_unreferenced(self, digest: str) -> None:
        with self._lock:
            if digest and digest not in self.referenced_hashes():
                self.archive.remove(digest)

    def cv_file(self, application: Application) -> Path | None:
        """Path of the archived CV (do not modify it), or None."""
        return self.archive.find(application.cv_hash) if application.cv_hash else None

    def read_cv(self, application: Application) -> bytes | None:
        return self.archive.read_bytes(application.cv_hash) if application.cv_hash else None

    def materialize_cv(self, application: Application, folder: Path | str) -> Path | None:
        """Copies the archived CV into `folder` under its original name (to open it externally)."""
        source = self.cv_file(application)
        if source is None:
            return None
        target_dir = Path(folder)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / (_safe_name(Path(application.cv_name).stem or "cv", 80)
                               + (Path(application.cv_name).suffix or source.suffix or ".pdf"))
        target.write_bytes(source.read_bytes())
        return target

    def add_attachment(self, application_id: int, path: Path | str) -> Attachment:
        if self.get(application_id) is None:
            raise KeyError(application_id)
        archived = self.archive.store(path)
        with self.db.writing() as conn:
            cursor = conn.execute("INSERT INTO attachments (application_id, hash, name, size, added) "
                                  "VALUES (?, ?, ?, ?, ?)",
                                  (application_id, archived.hash, archived.name, archived.size, self._now()))
            attachment_id = cursor.lastrowid
        return self.attachments(application_id, attachment_id)[0]

    def attachments(self, application_id: int, attachment_id: int | None = None) -> list[Attachment]:
        sql, params = "SELECT * FROM attachments WHERE application_id = ?", [application_id]
        if attachment_id is not None:
            sql += " AND id = ?"
            params.append(attachment_id)
        with self.db.reading() as conn:
            rows = conn.execute(sql + " ORDER BY id", params).fetchall()
        return [Attachment.from_row(r) for r in rows]

    def remove_attachment(self, attachment_id: int) -> None:
        with self.db.writing() as conn:
            conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
        self.gc_archive()

    # --- recording from the other parts of the program -----------------------------------

    def record_from_mail_draft(self, draft, outcome=None, company=None, *, role: str = "",
                               posting_url: str = "", kind: str | None = None, cv_language: str = "",
                               ) -> Application | None:
        """A DRAFT record for an e-mail draft that the program has just opened (nothing is
        sent by the program: the user marks it sent later). Returns None when the draft was
        not opened (`outcome.ok` false). Opening the same draft again updates the record; any
        other record for the same company and role within ``DUPLICATE_WINDOW_DAYS`` is returned
        unchanged instead of creating a second one.

        `draft` is a ``mail.compose.Draft`` (to, subject, body, attachment); `company` a
        company card (id, name), a dict or a name."""
        if outcome is not None and not getattr(outcome, "ok", True):
            return None
        ident, name = company_ref(company)
        if not name:
            raise ValueError("The company name is required")
        attachment = getattr(draft, "attachment", None)
        cv_path = Path(attachment) if attachment and Path(attachment).is_file() else None
        recipient, subject, body = (getattr(draft, "to", "") or ""), getattr(draft, "subject", ""), \
            getattr(draft, "body", "")
        with self._lock:
            for existing in self.list(status=STATUS_DRAFT):
                if (existing.source == "mail_draft" and existing.recipient_email == recipient.strip()
                        and existing.cover_subject == subject.strip() and
                        (existing.company_id == ident if ident is not None
                         else fold_text(existing.company_name) == fold_text(name))):
                    return self.update(existing.id, cover_letter=body, who="mail",
                                       **({"cv_path": cv_path} if cv_path else {}))
            duplicates = self.find_duplicates(company_name=name, company_id=ident, role=role)
            if duplicates:  # already recorded for this company and role within the window
                return duplicates[0]
            return self.create(company_name=name, company_id=ident, role=role, posting_url=posting_url,
                               kind=kind, channel="email", recipient_email=recipient,
                               cover_subject=subject, cover_letter=body, cv_path=cv_path,
                               cv_language=cv_language, status=STATUS_DRAFT, source="mail_draft",
                               who="mail")

    def record_from_export(self, *, company=None, role: str = "", posting_url: str = "",
                           kind: str | None = None, cover_letter: str = "", cover_subject: str = "",
                           cv_path: Path | str | None = None, cv_language: str = "",
                           channel: str = "other", recipient_email: str = "") -> Application:
        """A DRAFT record for an application document exported by the program ("Export application").
        When the same company and role were already recorded within ``DUPLICATE_WINDOW_DAYS``,
        that record is returned and nothing is created (saving the same export as PDF and Word,
        or printing it again, is one application)."""
        ident, name = company_ref(company)
        if not name:
            raise ValueError("The company name is required")
        with self._lock:
            duplicates = self.find_duplicates(company_name=name, company_id=ident, role=role)
            if duplicates:
                return duplicates[0]
            return self.create(company_name=name, company_id=ident, role=role, posting_url=posting_url,
                               kind=kind, channel=channel, recipient_email=recipient_email,
                               cover_subject=cover_subject, cover_letter=cover_letter, cv_path=cv_path,
                               cv_language=cv_language, status=STATUS_DRAFT, source="export", who="system")

    # --- rules: follow-up, duplicates, statistics ----------------------------------------

    def due_follow_ups(self, today=None, after_days: int = DEFAULT_FOLLOW_UP_DAYS, *,
                       working_days: bool = True) -> list[FollowUp]:
        """Applications to chase: sent with no answer for `after_days` (working days by default;
        not when the user set a later follow-up date), or with a follow-up date reached.
        Oldest first."""
        day = parse_day(today) or self.today()
        found: list[FollowUp] = []
        for app in self.list(status=OPEN_STATUSES):
            planned = app.follow_up_on
            sent = app.sent_on
            if planned is not None:
                if planned <= day:
                    waiting = (day - sent).days if sent else 0
                    found.append(FollowUp(app, waiting, "scheduled"))
                continue
            if app.status == STATUS_SENT and sent is not None:
                waiting = working_days_between(sent, day) if working_days else (day - sent).days
                if waiting >= after_days:
                    found.append(FollowUp(app, waiting, "no_answer"))
        found.sort(key=lambda f: (f.application.sent_date or "", f.application.id))
        return found

    def find_duplicates(self, *, company_name: str = "", company_id: int | None = None, role: str = "",
                        when=None, days: int = DUPLICATE_WINDOW_DAYS, exclude_id: int | None = None
                        ) -> list[Application]:
        """Records for the same company and role within `days` of `when` (default today)."""
        reference = parse_day(when) or self.today()
        wanted_company, wanted_role = fold_text(company_name), fold_text(role)
        found = []
        for app in self.list():
            if app.id == exclude_id:
                continue
            same_company = (app.company_id == company_id if app.company_id is not None and company_id is not None
                            else fold_text(app.company_name) == wanted_company)
            if not same_company or fold_text(app.role) != wanted_role:
                continue
            their_day = app.sent_on or date.fromtimestamp(app.created)
            if abs((their_day - reference).days) <= days:
                found.append(app)
        return found

    def statistics(self, today=None) -> Statistics:
        apps = self.list()
        result = Statistics(total=len(apps))
        for app in apps:
            result.by_status[app.status] = result.by_status.get(app.status, 0) + 1
        sent = [a for a in apps if a.sent_on and a.status != STATUS_DRAFT]
        result.sent = len(sent)
        months: dict[str, int] = {}
        for app in sent:
            months[app.sent_date[:7]] = months.get(app.sent_date[:7], 0) + 1
        result.sent_per_month = sorted(months.items())
        sent_ids = {a.id for a in sent}
        # first answer = first change to an "answered" status
        first_answer: dict[int, float] = {}
        interviewed: set[int] = set()
        with self.db.reading() as conn:
            for row in conn.execute("SELECT application_id, at, new FROM status_history "
                                    "WHERE field = 'status' ORDER BY at, id"):
                if row["new"] in ANSWERED_STATUSES:
                    first_answer.setdefault(row["application_id"], row["at"])
                if row["new"] in (STATUS_INTERVIEW, STATUS_OFFER):
                    interviewed.add(row["application_id"])
            interviewed |= {r[0] for r in conn.execute("SELECT DISTINCT application_id FROM appointments")}
        answered = [a for a in sent if a.status in ANSWERED_STATUSES or a.id in first_answer]
        result.answered = len(answered)
        result.answer_rate = result.answered / result.sent if result.sent else 0.0
        result.interviews = len({a.id for a in sent if a.status in (STATUS_INTERVIEW, STATUS_OFFER)
                                 or a.id in interviewed} & sent_ids)
        result.offers = sum(1 for a in sent if a.status == STATUS_OFFER)
        waits = []
        for app in answered:
            at = first_answer.get(app.id)
            if at is not None:
                days = (date.fromtimestamp(at) - app.sent_on).days
                if days >= 0:
                    waits.append(days)
        result.median_days_to_answer = float(stats_module.median(waits)) if waits else None
        return result

    # --- appointments --------------------------------------------------------------------

    @staticmethod
    def _validated_appointment(values: dict) -> dict:
        clean = dict(values)
        if "kind" in clean and clean["kind"] not in APPOINTMENT_KINDS:
            raise ValueError(f"Invalid appointment kind: {clean['kind']!r}")
        if "outcome" in clean and clean["outcome"] not in OUTCOMES:
            raise ValueError(f"Invalid outcome: {clean['outcome']!r}")
        if "start" in clean:
            text = moment_text(clean["start"])
            if not text:
                raise ValueError(f"Invalid start: {clean['start']!r}")
            clean["start"] = text
        if "duration_min" in clean:
            clean["duration_min"] = max(0, int(clean["duration_min"] or 0))
        if "reminder_min" in clean:
            value = clean["reminder_min"]
            clean["reminder_min"] = None if value in (None, "") or int(value) < 0 else int(value)
        for name in ("place", "people", "notes"):
            if name in clean:
                clean[name] = str(clean[name] or "")
        return clean

    def add_appointment(self, application_id: int, *, start, kind: str = "phone", duration_min: int = 60,
                        place: str = "", people: str = "", notes: str = "", outcome: str = "pending",
                        reminder_min: int | None = None) -> Appointment:
        if self.get(application_id) is None:
            raise KeyError(application_id)
        values = self._validated_appointment({
            "kind": kind, "start": start, "duration_min": duration_min, "place": place, "people": people,
            "notes": notes, "outcome": outcome, "reminder_min": reminder_min})
        now = self._now()
        values.update(application_id=application_id, uid=uuid.uuid4().hex, created=now, updated=now)
        columns = [c for c in APPOINTMENT_COLUMNS if c in values]
        with self.db.writing() as conn:
            cursor = conn.execute(
                f"INSERT INTO appointments ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
                [values[c] for c in columns])
            return self._appointment(conn, cursor.lastrowid)

    @staticmethod
    def _appointment(conn, appointment_id: int) -> Appointment | None:
        row = conn.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
        return Appointment.from_row(row) if row else None

    def get_appointment(self, appointment_id: int) -> Appointment | None:
        with self.db.reading() as conn:
            return self._appointment(conn, appointment_id)

    def update_appointment(self, appointment_id: int, **fields) -> Appointment:
        unknown = set(fields) - set(APPOINTMENT_EDITABLE)
        if unknown:
            raise ValueError(f"Unknown fields: {sorted(unknown)}")
        changes = self._validated_appointment(fields)
        if not changes:
            return self.get_appointment(appointment_id)
        changes["updated"] = self._now()
        with self.db.writing() as conn:
            conn.execute(f"UPDATE appointments SET {','.join(f'{k} = ?' for k in changes)} WHERE id = ?",
                         [*changes.values(), appointment_id])
            result = self._appointment(conn, appointment_id)
        if result is None:
            raise KeyError(appointment_id)
        return result

    def delete_appointment(self, appointment_id: int) -> bool:
        with self.db.writing() as conn:
            return conn.execute("DELETE FROM appointments WHERE id = ?", (appointment_id,)).rowcount > 0

    def appointments_for(self, application_id: int) -> list[Appointment]:
        with self.db.reading() as conn:
            rows = conn.execute("SELECT * FROM appointments WHERE application_id = ?", (application_id,)).fetchall()
        return sorted((Appointment.from_row(r) for r in rows), key=lambda a: (a.start_dt or datetime.max.astimezone(), a.id))

    def _all_appointments(self) -> list[Appointment]:
        with self.db.reading() as conn:
            rows = conn.execute("SELECT * FROM appointments").fetchall()
        return [Appointment.from_row(r) for r in rows]

    def upcoming_appointments(self, now: datetime | None = None, *, days: int | None = None,
                              limit: int | None = 10) -> list[tuple[Appointment, Application]]:
        """Future appointments (not cancelled; not of a rejected or withdrawn application), soonest first."""
        moment = parse_moment(now) or datetime.now().astimezone()
        horizon = moment + timedelta(days=days) if days is not None else None
        applications = {a.id: a for a in self.list()}
        found = []
        for item in self._all_appointments():
            start = item.start_dt
            app = applications.get(item.application_id)
            if start is None or app is None or start < moment or item.outcome == "cancelled":
                continue
            if app.status in ("rejected", "withdrawn"):
                continue
            if horizon is not None and start > horizon:
                continue
            found.append((item, app))
        found.sort(key=lambda pair: (pair[0].start_dt, pair[0].id))
        return found[:limit] if limit else found

    def next_appointments(self, now: datetime | None = None) -> dict[int, Appointment]:
        """For each application, its next future appointment (for the table column)."""
        result: dict[int, Appointment] = {}
        for item, app in self.upcoming_appointments(now, limit=None):
            result.setdefault(app.id, item)
        return result

    def export_ics(self, appointment_ids: Iterable[int] | None = None, *, application_id: int | None = None
                   ) -> str:
        """iCalendar text for the chosen appointments (all, or those of one application)."""
        items = self._all_appointments()
        if application_id is not None:
            items = [i for i in items if i.application_id == application_id]
        if appointment_ids is not None:
            wanted = set(appointment_ids)
            items = [i for i in items if i.id in wanted]
        applications = {a.id: a for a in self.list()}
        return ics_module.export_ics(sorted(items, key=lambda i: i.start), applications, tr=self._tr)

    def appointment_ics(self, appointment_id: int) -> str:
        item = self.get_appointment(appointment_id)
        if item is None:
            raise KeyError(appointment_id)
        return ics_module.to_ics(item, self.get(item.application_id), tr=self._tr)

    # --- CSV and folder export -----------------------------------------------------------

    def export_csv(self, path: Path | str | None = None) -> str:
        """The whole tracker as CSV text (UTF-8; also written to `path` when given, with a BOM so
        Excel reads the accents). Letters are included, files are not."""
        nexts = self.next_appointments()
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=CSV_COLUMNS, lineterminator="\r\n")
        writer.writeheader()
        for app in sorted(self.list(), key=lambda a: a.id):
            row = {name: getattr(app, name, "") for name in CSV_COLUMNS if hasattr(app, name)}
            for name in _TEXT_COLUMNS:
                row[name] = _csv_safe(row.get(name) or "")
            row["company_id"] = "" if app.company_id is None else app.company_id
            row["created"] = datetime.fromtimestamp(app.created).astimezone().isoformat(timespec="seconds")
            upcoming = nexts.get(app.id)
            row["next_appointment"] = upcoming.start if upcoming else ""
            writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
        text = out.getvalue()
        if path is not None:
            Path(path).write_text(text, encoding="utf-8-sig", newline="")
        return text

    def import_csv(self, source: str | Path) -> ImportReport:
        """Adds the rows of a CSV made by ``export_csv`` (or by hand with the same headers).
        Rows without a company, or identical to an existing record (same company, role, sent
        date and recipient), are skipped; bad values are reported and replaced by defaults."""
        text = Path(source).read_text(encoding="utf-8-sig") if isinstance(source, Path) else str(source)
        text = text.lstrip("﻿")
        first_line = text.splitlines()[0] if text.strip() else ""
        delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
        report = ImportReport()
        reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
        existing = {(fold_text(a.company_name), fold_text(a.role), a.sent_date or "", a.recipient_email.lower())
                    for a in self.list()}
        for number, row in enumerate(reader, start=2):
            try:
                values = {k.strip().lower(): (v or "") for k, v in row.items() if k}
                name = _csv_unsafe(values.get("company_name", "")).strip()
                if not name:
                    report.skipped += 1
                    report.errors.append(f"line {number}: no company name")
                    continue
                kind = values.get("kind", "").strip()
                channel = values.get("channel", "").strip()
                status = values.get("status", "").strip()
                for label, value, allowed in (("kind", kind, KINDS), ("channel", channel, CHANNELS),
                                              ("status", status, STATUSES)):
                    if value and value not in allowed:
                        report.errors.append(f"line {number}: unknown {label} {value!r}, default used")
                sent = parse_day(values.get("sent_date"))
                follow = parse_day(values.get("follow_up_date"))
                for label in ("sent_date", "follow_up_date"):
                    if values.get(label, "").strip() and parse_day(values[label]) is None:
                        report.errors.append(f"line {number}: invalid date in {label}, ignored")
                key = (fold_text(name), fold_text(_csv_unsafe(values.get("role", ""))),
                       sent.isoformat() if sent else "", values.get("recipient_email", "").strip().lower())
                if key in existing:
                    report.skipped += 1
                    continue
                existing.add(key)
                cid = values.get("company_id", "").strip()
                self.create(
                    company_name=name, company_id=int(cid) if cid.isdigit() else None,
                    role=_csv_unsafe(values.get("role", "")), posting_url=_csv_unsafe(values.get("posting_url", "")),
                    kind=choice(kind, KINDS, KIND_POSTING if values.get("posting_url") else KIND_SPONTANEOUS),
                    channel=choice(channel, CHANNELS, "other"),
                    recipient_email=_csv_unsafe(values.get("recipient_email", "")),
                    contact_name=_csv_unsafe(values.get("contact_name", "")), sent_date=sent,
                    status=choice(status, STATUSES, STATUS_SENT if sent else STATUS_DRAFT),
                    cover_subject=_csv_unsafe(values.get("cover_subject", "")),
                    cover_letter=_csv_unsafe(values.get("cover_letter", "")),
                    cv_language=_csv_unsafe(values.get("cv_language", "")),
                    notes=_csv_unsafe(values.get("notes", "")), follow_up_date=follow,
                    source=choice(values.get("source", "").strip(), SOURCES, "manual"), who="import")
                report.created += 1
            except Exception as error:  # noqa: BLE001 - one bad row never stops the import
                log.warning("CSV row %d not imported", number, exc_info=True)
                report.skipped += 1
                report.errors.append(f"line {number}: {error}")
        return report

    def export_application(self, application_id: int, destination: Path | str) -> Path:
        """Writes one application into a new sub-folder of `destination`: the letter (.txt), the
        CV PDF as sent, the notes and, when there are appointments, a .ics file. Returns the folder."""
        app = self.get(application_id)
        if app is None:
            raise KeyError(application_id)
        base = _safe_name(" ".join(p for p in (app.company_name, app.role, app.sent_date or "") if p), 90)
        folder = Path(destination) / base
        counter = 2
        while folder.exists():
            folder = Path(destination) / f"{base} ({counter})"
            counter += 1
        folder.mkdir(parents=True)
        subject = f"{app.cover_subject}\n\n" if app.cover_subject else ""
        (folder / "cover_letter.txt").write_text(subject + app.cover_letter, encoding="utf-8")
        lines = [f"Company: {app.company_name}", f"Role: {app.role}", f"Posting: {app.posting_url}",
                 f"Kind: {app.kind}", f"Channel: {app.channel}", f"Recipient: {app.recipient_email}",
                 f"Contact: {app.contact_name}", f"Sent: {app.sent_date or ''}", f"Status: {app.status}",
                 f"Follow-up: {app.follow_up_date or ''}", "", "Notes:", app.notes]
        for item in self.appointments_for(application_id):
            lines.append(f"Appointment: {item.start} ({item.kind}, {item.duration_min} min) {item.place}")
        (folder / "notes.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if self.materialize_cv(app, folder) is None and app.cv_hash:
            log.warning("Archived CV of application %s is missing", application_id)
        for attachment in self.attachments(application_id):
            source = self.archive.find(attachment.hash)
            if source is not None:
                (folder / _safe_name(attachment.name, 80)).write_bytes(source.read_bytes())
        if self.appointments_for(application_id):
            (folder / "appointments.ics").write_text(self.export_ics(application_id=application_id),
                                                     encoding="utf-8", newline="")
        return folder
