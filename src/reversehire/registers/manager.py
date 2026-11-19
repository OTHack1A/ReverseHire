"""Orchestration of the regional register: sources, downloads, refresh, reminder, disk.

Folder layout (inside the app's data folder):
    registers/register.sqlite          the register (RegisterDB)
    registers/raw/<source>/            downloaded files + manifest.json

Settings read (all optional, with defaults; the integrator adds them to config.py):
    register_regions         list or "a,b" of region keys      default ["emilia-romagna"]
    register_rna_months      months of RNA files (0 = archive) default 24
    register_auto_check      monthly reminder / check          default True
    register_sources         enabled source ids                default all four
    request_pause_s          the gateway's pause (already in the settings)

Nothing here blocks the GUI by itself: run `refresh`, `download`, `check_updates` in a
worker (`RegisterJob` does it), pass a `threading.Event` to cancel and a progress
callback. No public method raises: results carry states and error keys.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from ..numbers import format_int, format_number  # noqa: F401 - marks of the interface language
from . import texts
from .agcm import AgcmSource
from .base import Estimate, RegisterSource, UpdateCheck
from .bought import read_bought_list
from .db import LoadReport, RegisterDB
from .download import DownloadResult, folder_bytes
from .gleif import GleifSource
from .italy_codes import region_key
from .model import (AGCM, DOWNLOADABLE, GLEIF, OPENCOESIONE, RI_BOUGHT, RNA, RegisterProgress, RegisterRow, notify,
                    now_iso)
from .opencoesione import OpenCoesioneSource
from .rna import RnaSource

log = logging.getLogger(__name__)

FOLDER = "registers"
DB_NAME = "register.sqlite"
RAW = "raw"
DEFAULT_REGIONS = ("emilia-romagna",)
REMINDER_DAYS = 30
MAX_RNA_MONTHS = 120     # register_rna_months: 0 (the whole archive) .. 10 years

SOURCE_CLASSES: dict[str, type[RegisterSource]] = {
    RNA: RnaSource, GLEIF: GleifSource, OPENCOESIONE: OpenCoesioneSource, AGCM: AgcmSource,
}


def _month_text(period: str) -> str:
    """"2025-03" -> "03/2025"."""
    year, _, month = period.partition("-")
    return f"{month}/{year}" if month else period


def periods_note(names: list[str], download: DownloadResult | None) -> str:
    """" Months loaded: 12/2024–11/2026 (24)." for a monthly source (RNA), plus the months
    that could not be downloaded (e.g. not published yet: HTTP 404) and were skipped;
    "" for a source without periods."""
    from .rna import period_of

    loaded = sorted({p for p in (period_of(n) for n in names) if p})
    if not loaded:
        return ""
    note = " " + texts.text("registers.result.periods", first=_month_text(loaded[0]), last=_month_text(loaded[-1]),
                            n=len(loaded))
    failed = sorted({p for p in (period_of(f.name) for f in (download.files if download else [])
                                  if f.state == "failed") if p})
    if failed:
        note += " " + texts.text("registers.result.skipped", n=len(failed),
                                 months=", ".join(_month_text(p) for p in failed))
    return note


@dataclass
class SourceRefresh:
    source: str
    state: str = "ok"                  # ok | unchanged | skipped | failed | cancelled | not_downloaded
    download: DownloadResult | None = None
    load: LoadReport | None = None
    rows: int = 0
    parse_errors: list[dict] = field(default_factory=list)
    parse_stats: dict = field(default_factory=dict)
    seconds: float = 0.0
    message: str = ""


@dataclass
class RefreshReport:
    regions: list[str]
    started: str = ""
    finished: str = ""
    seconds: float = 0.0
    state: str = "ok"                  # ok | partial | failed | cancelled
    results: list[SourceRefresh] = field(default_factory=list)

    def messages(self) -> list[str]:
        return [r.message for r in self.results if r.message]


def _setting(settings, name: str, default):
    value = getattr(settings, name, None) if settings is not None else None
    return default if value in (None, "") else value


class RegisterManager:
    def __init__(self, folder: Path | str | None = None, settings=None, *, gateway_factory=None,
                 db: RegisterDB | None = None):
        self._folder = Path(folder) if folder is not None else None
        self.settings = settings
        self._gateway_factory = gateway_factory
        self._db = db
        self._db_lock = threading.Lock()
        self._gateways: dict[str, object] = {}

    # --- locations

    @property
    def root(self) -> Path:
        if self._folder is None:
            from ..config import data_folder
            self._folder = data_folder() / FOLDER
        return self._folder

    @property
    def raw_root(self) -> Path:
        return self.root / RAW

    def raw_folder(self, source_id: str) -> Path:
        return self.raw_root / source_id

    @property
    def db(self) -> RegisterDB:
        with self._db_lock:
            if self._db is None:
                self._db = RegisterDB(self.root / DB_NAME)
            return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
        for gateway in self._gateways.values():
            try:
                gateway.close()
            except Exception:  # noqa: BLE001
                pass
        self._gateways.clear()

    # --- settings

    def regions(self, regions=None) -> list[str]:
        raw = regions if regions is not None else _setting(self.settings, "register_regions", DEFAULT_REGIONS)
        if isinstance(raw, str):
            raw = raw.split(",")
        keys = []
        for item in raw or ():
            key = region_key(str(item)) or str(item).strip().lower()
            if key and key not in keys:
                keys.append(key)
        return keys or list(DEFAULT_REGIONS)

    def enabled_sources(self) -> list[str]:
        raw = _setting(self.settings, "register_sources", DOWNLOADABLE)
        if isinstance(raw, str):
            raw = raw.split(",")
        names = [str(n).strip().lower() for n in raw]
        return [n for n in DOWNLOADABLE if n in names] or list(DOWNLOADABLE)

    def options(self) -> dict:
        try:
            months = int(_setting(self.settings, "register_rna_months", 24))
        except (TypeError, ValueError):
            months = 24
        return {"rna_months": max(0, min(MAX_RNA_MONTHS, months))}

    # --- sources

    def gateway_for(self, source_id: str):
        if source_id not in self._gateways:
            cls = SOURCE_CLASSES[source_id]
            if self._gateway_factory is not None:
                self._gateways[source_id] = self._gateway_factory(cls)
            else:
                from ..discovery.gateway import DEFAULT_PAUSE_S, PoliteGateway
                pause = float(_setting(self.settings, "request_pause_s", DEFAULT_PAUSE_S))
                self._gateways[source_id] = PoliteGateway(self.settings, None, pause_s=max(pause, cls.min_pause_s))
        return self._gateways[source_id]

    def source(self, source_id: str) -> RegisterSource:
        cls = SOURCE_CLASSES[source_id]
        return cls(self.gateway_for(source_id), self.options())

    def sources_info(self) -> list[dict]:
        """Metadata, licence, last refresh and local size of every downloadable source,
        plus the bought-list source (for the manager window)."""
        last = self.db.last_refresh()
        result = []
        for source_id in DOWNLOADABLE:
            info = SOURCE_CLASSES[source_id](None, self.options()).info()
            info.update({"enabled": source_id in self.enabled_sources(), "last_refresh": last.get(source_id),
                         "raw_bytes": folder_bytes(self.raw_folder(source_id)),
                         "downloaded": bool(SOURCE_CLASSES[source_id](None).local_files(self.raw_folder(source_id)))})
            result.append(info)
        result.append({"id": RI_BOUGHT, "title": texts.source_title(RI_BOUGHT),
                       "licence": texts.text("registers.source.ri_bought.licence"),
                       "attribution": texts.text("registers.source.ri_bought.attribution"), "terms_url": "",
                       "update_frequency": "manual", "update_text": texts.text("registers.update.manual"),
                       "note": texts.text("registers.source.ri_bought.note"), "last_refresh": last.get(RI_BOUGHT),
                       "enabled": True, "raw_bytes": 0, "downloaded": RI_BOUGHT in last})
        return result

    def attributions(self, used_only: bool = True) -> list[str]:
        """ATTRIBUTIONS lines for the sources whose data is in the register."""
        present = set(self.db.stats().get("per_source", {})) if used_only else set(DOWNLOADABLE)
        lines = []
        for source_id in DOWNLOADABLE:
            if source_id in present:
                cls = SOURCE_CLASSES[source_id]
                lines.append(f"{cls.attribution} – {cls.homepage}")
        return lines

    # --- network operations

    def estimate(self, source_ids=None, cancel=None) -> dict[str, Estimate]:
        result = {}
        for source_id in source_ids or self.enabled_sources():
            from .download import Manifest
            result[source_id] = self.source(source_id).estimate(Manifest(self.raw_folder(source_id)), cancel)
        return result

    def check_updates(self, source_ids=None, cancel=None, progress=None) -> list[UpdateCheck]:
        """Cheap checks (HEAD / one listing page / one API call per source)."""
        checks = []
        for source_id in source_ids or self.enabled_sources():
            if cancel is not None and cancel.is_set():
                checks.append(UpdateCheck(source_id, "unknown", detail="cancelled"))
                continue
            notify(progress, RegisterProgress(source_id, "checking"))
            checks.append(self.source(source_id).check_update(self.raw_folder(source_id), cancel))
        try:
            self.db.set_meta("last_check", now_iso())
        except Exception:  # noqa: BLE001
            log.warning("Register: last check time not stored", exc_info=True)
        return checks

    def download(self, source_ids=None, progress=None, cancel=None, force: bool = False) -> dict[str, DownloadResult]:
        result = {}
        for source_id in source_ids or self.enabled_sources():
            if cancel is not None and cancel.is_set():
                result[source_id] = DownloadResult(source_id, "cancelled")
                continue
            result[source_id] = self.source(source_id).download(self.raw_folder(source_id), progress, cancel, force)
        return result

    # --- refresh

    def refresh(self, sources=None, regions=None, progress=None, cancel=None, *, download: bool = True,
                force: bool = False) -> RefreshReport:
        """Download what changed (unless `download` is False: only the local files are
        read), parse for `regions`, merge into the register. Never raises."""
        region_list = self.regions(regions)
        report = RefreshReport(region_list, started=now_iso())
        started = time.monotonic()
        wanted = set(region_list)
        last = self.db.last_refresh()
        for source_id in [s for s in (sources or self.enabled_sources()) if s in SOURCE_CLASSES]:
            if cancel is not None and cancel.is_set():
                report.results.append(SourceRefresh(source_id, "cancelled"))
                continue
            report.results.append(self._refresh_one(source_id, wanted, last.get(source_id), progress, cancel,
                                                    download, force))
        if not (cancel is not None and cancel.is_set()):
            try:
                self.db.derive_municipality_names()
            except Exception:  # noqa: BLE001
                log.warning("Register: municipality names not derived", exc_info=True)
        states = {r.state for r in report.results}
        if "cancelled" in states:
            report.state = "cancelled"
        elif states and states <= {"failed", "not_downloaded"}:
            report.state = "failed"
        elif states & {"failed", "not_downloaded"}:
            report.state = "partial"
        report.finished = now_iso()
        report.seconds = round(time.monotonic() - started, 1)
        try:
            self.db.set_meta("last_refresh", report.finished)
        except Exception:  # noqa: BLE001
            pass
        return report

    def _refresh_one(self, source_id, wanted: set[str], last: dict | None, progress, cancel, download: bool,
                     force: bool) -> SourceRefresh:
        title = texts.source_title(source_id)
        outcome = SourceRefresh(source_id)
        began = time.monotonic()
        source = self.source(source_id)
        folder = self.raw_folder(source_id)
        try:
            if download:
                outcome.download = source.download(folder, progress, cancel, force)
                if outcome.download.state == "cancelled":
                    outcome.state = "cancelled"
                    outcome.message = texts.text("registers.progress.cancelled", source=title)
                    return outcome
            files = source.local_files(folder)
            if not files:
                outcome.state = "not_downloaded"
                detail = outcome.download.detail if outcome.download else ""
                key = "registers.error.network" if detail else "registers.error.not_downloaded"
                outcome.message = texts.text(key, source=title, detail=detail)
                return outcome
            names = [p.name for p in files]
            same = (last and not force and sorted((last.get("regions") or "").split(",")) == sorted(wanted)
                    and outcome.download is not None and outcome.download.state == "unchanged")
            if same:
                outcome.state = "unchanged"
                outcome.message = texts.text("registers.result.unchanged", source=title)
                return outcome
            rows: list[RegisterRow] = list(source.parse(files, wanted, progress, cancel))
            outcome.parse_errors = [{"file": e.file, "detail": e.detail} for e in source.errors]
            outcome.parse_stats = dict(source.stats)
            if source.errors and not rows:
                outcome.state = "failed"
                outcome.message = texts.text("registers.error.parse", source=title, file=source.errors[0].file,
                                             detail=source.errors[0].detail)
                return outcome   # the previous data of the source stay untouched
            notify(progress, RegisterProgress(source_id, "merge", rows=len(rows), rows_estimated=len(rows)))
            outcome.load = self.db.load_source(source_id, rows, wanted, progress=progress, cancel=cancel, files=names)
            outcome.rows = outcome.load.rows
            outcome.state = outcome.load.state
            if outcome.state == "ok":
                outcome.message = texts.text("registers.progress.done", source=title, rows=format_int(outcome.rows))
                outcome.message += periods_note(names, outcome.download)
                if source.errors:
                    outcome.state = "partial"
                    outcome.message += " " + texts.text("registers.error.parse", source=title,
                                                        file=source.errors[0].file, detail=source.errors[0].detail)
            elif outcome.state == "cancelled":
                outcome.message = texts.text("registers.progress.cancelled", source=title)
            else:
                outcome.message = texts.text("registers.error.parse", source=title, file="", detail=outcome.load.detail)
            if outcome.download is not None and outcome.download.state in ("failed", "partial"):
                key = "registers.error.checksum" if outcome.download.error == "checksum" else "registers.error.network"
                outcome.message += " " + texts.text(key, source=title, file="", detail=outcome.download.detail)
        except Exception as error:  # noqa: BLE001 - CancelledError from a parser included
            if type(error).__name__ == "CancelledError":
                outcome.state = "cancelled"
                outcome.message = texts.text("registers.progress.cancelled", source=title)
            else:
                log.warning("Register: refresh of %s failed", source_id, exc_info=True)
                outcome.state = "failed"
                outcome.message = texts.text("registers.error.parse", source=title, file="",
                                             detail=f"{type(error).__name__}: {error}")
        finally:
            outcome.seconds = round(time.monotonic() - began, 1)
        return outcome

    # --- bought list

    def import_bought_list(self, path: Path | str, mapping: dict[str, int] | None = None, source_date: str = "",
                           progress=None, cancel=None) -> dict:
        """Import a Registro Imprese list the user bought (CSV/XLSX). Rows are added or
        updated by tax code; earlier bought lists are kept. Never raises."""
        bought = read_bought_list(path, mapping, source_date)
        if bought.error:
            return {"state": "failed", "error": bought.error,
                    "message": texts.text("registers.error.import", detail=bought.error)}
        regions = {r.fields.get("region") for r in bought.rows if r.fields.get("region")}
        load = self.db.load_source(RI_BOUGHT, bought.rows, regions or None, replace=False, progress=progress,
                                   cancel=cancel, files=[Path(path).name])
        return {"state": load.state, "rows": len(bought.rows), "total": bought.total, "new": load.new,
                "skipped_no_code": bought.skipped_no_code, "skipped_persons": bought.skipped_persons,
                "kind": bought.kind, "warnings": bought.warnings,
                "error": load.detail if load.state != "ok" else "",
                "message": texts.text("registers.result.updated", source=texts.source_title(RI_BOUGHT),
                                      rows=format_int(len(bought.rows)))}

    # --- reminder, disk

    def reminder(self, today: date | None = None) -> dict:
        """Data for the monthly reminder of the GUI."""
        today = today or date.today()
        auto = bool(_setting(self.settings, "register_auto_check", True))
        last = self.db.last_refresh()
        dates = [v.get("finished", "")[:10] for k, v in last.items() if k in DOWNLOADABLE and v.get("finished")]
        if not dates:
            return {"due": auto, "never": True, "days": None, "last_refresh": "", "auto_check": auto,
                    "text": texts.text("registers.reminder.never")}
        oldest = min(dates)
        try:
            days = (today - date.fromisoformat(oldest)).days
        except ValueError:
            days = REMINDER_DAYS
        last_check = self.db.meta("last_check", "")[:10]
        try:
            checked_days = (today - date.fromisoformat(last_check)).days if last_check else None
        except ValueError:
            checked_days = None
        due = auto and days >= REMINDER_DAYS and (checked_days is None or checked_days >= REMINDER_DAYS)
        return {"due": due, "never": False, "days": days, "last_refresh": oldest, "last_check": last_check,
                "auto_check": auto, "text": texts.text("registers.reminder.due", days=days)}

    def disk_usage(self) -> dict:
        raw = {source_id: folder_bytes(self.raw_folder(source_id)) for source_id in DOWNLOADABLE}
        db_bytes = self.db.size_bytes()
        total_raw = sum(raw.values())
        return {"raw": raw, "raw_total": total_raw, "db": db_bytes, "total": total_raw + db_bytes,
                "text": texts.text("registers.disk.usage", raw=format_number(total_raw / 1e6), db=format_number(db_bytes / 1e6, 1))}

    def purge_raw(self, source_ids=None) -> int:
        """Delete the downloaded files (the register stays). Returns the bytes freed."""
        freed = 0
        for source_id in source_ids or DOWNLOADABLE:
            folder = self.raw_folder(source_id)
            size = folder_bytes(folder)
            try:
                if folder.exists():
                    shutil.rmtree(folder)
                freed += size
            except OSError:
                log.warning("Register: %s could not be removed", folder, exc_info=True)
        return freed

    # --- queries (shortcuts)

    def find(self, **kwargs):
        return self.db.find(**kwargs)

    def stats(self) -> dict:
        return self.db.stats()

    def coverage_note(self, province: str, ateco_division: str, municipality: str | None = None) -> dict:
        return self.db.coverage_note(province, ateco_division, municipality)


_SHARED: dict[str, RegisterManager] = {}
_SHARED_LOCK = threading.Lock()


def shared_manager(settings=None, folder: Path | str | None = None) -> RegisterManager:
    """The one RegisterManager of a data folder (default `<data folder>/registers`),
    shared by the window and the discovery sources (one database connection set per
    thread). Passing `settings` updates the manager's settings."""
    if folder is None:
        from ..config import data_folder
        folder = data_folder() / FOLDER
    key = str(Path(folder).resolve())
    with _SHARED_LOCK:
        manager = _SHARED.get(key)
        if manager is None:
            manager = _SHARED[key] = RegisterManager(folder, settings)
        elif settings is not None:
            manager.settings = settings
    return manager


def close_shared() -> None:
    """Close every shared manager (end of the program, tests)."""
    with _SHARED_LOCK:
        managers = list(_SHARED.values())
        _SHARED.clear()
    for manager in managers:
        try:
            manager.close()
        except Exception:  # noqa: BLE001
            log.debug("Register manager not closed", exc_info=True)


class RegisterJob:
    """Runs one manager call in a daemon thread: `cancel()`, `done`, `result`, `wait()`,
    and the latest progress event in `last_progress` (for a GUI timer to poll)."""

    def __init__(self, target, *args, progress=None, **kwargs):
        self.cancel_event = threading.Event()
        self.result = None
        self.error: BaseException | None = None
        self.last_progress: RegisterProgress | None = None
        self._user_progress = progress
        self._done = threading.Event()

        def on_progress(event: RegisterProgress) -> None:
            self.last_progress = event
            notify(self._user_progress, event)

        def run() -> None:
            try:
                self.result = target(*args, progress=on_progress, cancel=self.cancel_event, **kwargs)
            except BaseException as error:  # noqa: BLE001 - reported through `error`
                log.warning("Register job failed", exc_info=True)
                self.error = error
            finally:
                self._done.set()

        self.thread = threading.Thread(target=run, name="register-job", daemon=True)
        self.thread.start()

    def cancel(self) -> None:
        self.cancel_event.set()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)


__all__ = ["DEFAULT_REGIONS", "RefreshReport", "RegisterJob", "RegisterManager", "SOURCE_CLASSES", "SourceRefresh",
           "close_shared", "shared_manager"]
