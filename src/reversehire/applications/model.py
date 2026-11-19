"""Data model of the "Applications sent" tracker: plain dataclasses and the allowed values.

Dates are ISO strings in the database (``YYYY-MM-DD`` for a day, ``YYYY-MM-DDTHH:MM:SS+HH:MM``
for an appointment, so the UTC offset travels with the instant); the helpers below
convert them. Nothing here touches Qt or the disk.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta, timezone

KIND_POSTING = "posting"          # reply to a job posting
KIND_SPONTANEOUS = "spontaneous"  # spontaneous application
KINDS = (KIND_POSTING, KIND_SPONTANEOUS)

CHANNELS = ("email", "site", "agency", "linkedin", "other")  # site = company site / ATS form

STATUS_DRAFT = "draft"
STATUS_SENT = "sent"
STATUS_ACKNOWLEDGED = "acknowledged"
STATUS_INTERVIEW = "interview"
STATUS_OFFER = "offer"
STATUS_REJECTED = "rejected"
STATUS_NO_ANSWER = "no_answer"
STATUS_WITHDRAWN = "withdrawn"
STATUSES = (STATUS_DRAFT, STATUS_SENT, STATUS_ACKNOWLEDGED, STATUS_INTERVIEW, STATUS_OFFER,
            STATUS_REJECTED, STATUS_NO_ANSWER, STATUS_WITHDRAWN)
ANSWERED_STATUSES = (STATUS_ACKNOWLEDGED, STATUS_INTERVIEW, STATUS_OFFER, STATUS_REJECTED)
OPEN_STATUSES = (STATUS_SENT, STATUS_ACKNOWLEDGED, STATUS_INTERVIEW)  # still waiting for something
FINAL_STATUSES = (STATUS_OFFER, STATUS_REJECTED, STATUS_NO_ANSWER, STATUS_WITHDRAWN)

SOURCES = ("manual", "mail_draft", "export")

APPOINTMENT_KINDS = ("phone", "video", "onsite", "test", "other")
OUTCOMES = ("pending", "positive", "neutral", "negative", "cancelled")

# Fields whose changes are written to the history (the long texts are recorded as
# "changed" only, without copying the text).
TRACKED_FIELDS = ("status", "sent_date", "kind", "channel", "follow_up_date", "cv_hash", "cover_letter")
LONG_FIELDS = ("cover_letter",)


def choice(value, allowed: tuple, default: str) -> str:
    """`value` if it is one of `allowed`, else `default`."""
    return value if value in allowed else default


# --- dates -------------------------------------------------------------------------------


def parse_day(value) -> date | None:
    """A date from a ``date``, a ``datetime`` or an ISO string (``None`` when empty/invalid)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def day_text(value) -> str | None:
    day = parse_day(value)
    return day.isoformat() if day else None


def parse_moment(value) -> datetime | None:
    """An aware datetime from an ISO string or a datetime (naive = local time)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).strip())
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.astimezone()  # a naive value is the local wall-clock time
    return moment


def moment_text(value) -> str:
    moment = parse_moment(value)
    return moment.isoformat(timespec="seconds") if moment else ""


def is_working_day(day: date) -> bool:
    return day.weekday() < 5


def working_days_between(start: date, end: date) -> int:
    """Working days (Monday-Friday, no holidays) in ``(start, end]``; negative if end < start."""
    if end < start:
        return -working_days_between(end, start)
    count, day = 0, start
    while day < end:
        day += timedelta(days=1)
        count += is_working_day(day)
    return count


def add_working_days(start: date, days: int) -> date:
    day, left = start, days
    while left > 0:
        day += timedelta(days=1)
        left -= is_working_day(day)
    return day


# --- records -----------------------------------------------------------------------------


@dataclass
class Application:
    """One CV sent (or about to be sent) to a company."""

    id: int = 0
    company_id: int | None = None   # card id in "My companies", None when typed by hand
    company_name: str = ""          # snapshot of the name when the record was made
    role: str = ""
    posting_url: str = ""
    kind: str = KIND_POSTING
    channel: str = "email"
    recipient_email: str = ""       # as typed by the user
    contact_name: str = ""          # as typed by the user
    sent_date: str | None = None
    status: str = STATUS_DRAFT
    cover_subject: str = ""
    cover_letter: str = ""          # exactly as sent
    cv_hash: str = ""               # SHA-256 of the archived PDF ("" = none)
    cv_name: str = ""               # original file name of the PDF sent
    cv_language: str = ""
    notes: str = ""
    follow_up_date: str | None = None
    source: str = "manual"
    created: float = 0.0
    updated: float = 0.0

    @property
    def sent_on(self) -> date | None:
        return parse_day(self.sent_date)

    @property
    def follow_up_on(self) -> date | None:
        return parse_day(self.follow_up_date)

    @property
    def display_title(self) -> str:
        return " - ".join(part for part in (self.company_name, self.role) if part)

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_row(cls, row) -> "Application":
        data = dict(row)
        return cls(**{f.name: data[f.name] for f in fields(cls) if f.name in data})


# Columns written by the service (everything except the id).
APPLICATION_COLUMNS = tuple(f.name for f in fields(Application) if f.name != "id")
# Fields a caller may change with ``update`` (timestamps and the id are managed).
EDITABLE_FIELDS = tuple(c for c in APPLICATION_COLUMNS if c not in ("created", "updated"))


@dataclass
class Appointment:
    """An interview, call or test linked to an application."""

    id: int = 0
    application_id: int = 0
    kind: str = "phone"
    start: str = ""                 # ISO date-time with the UTC offset
    duration_min: int = 60
    place: str = ""                 # address, room or meeting link
    people: str = ""                # people met, free text
    notes: str = ""
    outcome: str = "pending"
    reminder_min: int | None = None  # alarm this many minutes before the start
    uid: str = ""
    created: float = 0.0
    updated: float = 0.0

    @property
    def start_dt(self) -> datetime | None:
        return parse_moment(self.start)

    @property
    def end_dt(self) -> datetime | None:
        start = self.start_dt
        return start + timedelta(minutes=max(0, int(self.duration_min or 0))) if start else None

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_row(cls, row) -> "Appointment":
        data = dict(row)
        return cls(**{f.name: data[f.name] for f in fields(cls) if f.name in data})


APPOINTMENT_COLUMNS = tuple(f.name for f in fields(Appointment) if f.name != "id")
APPOINTMENT_EDITABLE = ("kind", "start", "duration_min", "place", "people", "notes", "outcome", "reminder_min")


@dataclass
class Attachment:
    """A further file archived with an application (e.g. a certificate or a portfolio)."""

    id: int = 0
    application_id: int = 0
    hash: str = ""
    name: str = ""
    size: int = 0
    added: float = 0.0

    @classmethod
    def from_row(cls, row) -> "Attachment":
        data = dict(row)
        return cls(**{f.name: data[f.name] for f in fields(cls) if f.name in data})


@dataclass
class HistoryEntry:
    """One change of a tracked field: who, when, what."""

    id: int = 0
    application_id: int = 0
    at: float = 0.0
    who: str = "user"     # user | system | import | mail
    field: str = ""
    old: str = ""
    new: str = ""

    @property
    def moment(self) -> datetime:
        return datetime.fromtimestamp(self.at, tz=timezone.utc).astimezone()

    @classmethod
    def from_row(cls, row) -> "HistoryEntry":
        data = dict(row)
        return cls(**{f.name: data[f.name] for f in fields(cls) if f.name in data})


@dataclass
class FollowUp:
    """An application that needs a nudge."""

    application: Application
    days_waiting: int           # working or calendar days since it was sent
    reason: str                 # "no_answer" | "scheduled"


@dataclass
class Statistics:
    total: int = 0
    sent: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    sent_per_month: list[tuple[str, int]] = field(default_factory=list)
    answered: int = 0
    answer_rate: float = 0.0
    interviews: int = 0
    offers: int = 0
    median_days_to_answer: float | None = None


@dataclass
class ImportReport:
    created: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
