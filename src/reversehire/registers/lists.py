"""Company lists the user imports (CSV/XLSX) as sources of the regional register, and the
one-time migration of the lists that older versions kept in the Store.

ONE register for the user
-------------------------
Older versions kept imported lists (Registro Imprese "Elenco", start-up register, any
list with a name column) in the Store, viewed in a separate "Local register" dialog and
searched by the discovery source "imports". The regional register (this package) does the
same job better: provenance per field, locked hand edits, hide/undo, export. So every
imported list is now ONE SOURCE of the register, id ``list:<id>``:

- `import_list` stores a list (rows matched by tax code; a row without one gets a stable
  key ``L-<hash of name + town>``, the same identity the old dialog used), `refresh_list`
  replaces its rows with a newer file (the register's `load_source` semantics: hand
  edits stay locked, rows the user added stay, companies no source lists any more go),
  `delete_list` drops it, `lists` describes them (metadata in the register's meta table);
- `migrate_imported_lists(store, db)` copies the Store's lists once: list rows become
  rows of their ``list:`` source, fields the user corrected by hand ("_edited") become
  locked edits, rows added by hand ("_manual") become companies added by the user. It is
  idempotent (migrated list ids are recorded; running it again changes nothing) and never
  deletes anything: the Store keeps the old lists as a backup.

Imported values rank after the official sources and the user's own edits; between two
lists the newest value wins (see `db.RegisterDB._compute`). Nothing here raises: results
carry a state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .model import RegisterRow, company_cf

log = logging.getLogger(__name__)

LIST_PREFIX = "list:"
META_LISTS = "imported_lists"              # {source_id: {name, kind, created, source, source_date, path, ...}}
META_MIGRATED = "legacy_lists_migrated"    # {old Store list id: source_id}
MANUAL_KIND = "manual"
# Sources whose companies without any ATECO code are kept by an ATECO-filtered search:
# a list the user imported (or a company added by hand) cannot be judged by a code.
UNCODED_KEPT = (LIST_PREFIX, "user")

# Field of an imported list (discovery.imports.FIELDS) -> field of the register.
FIELD_MAP = {
    "name": "name", "site": "website", "address": "address", "town": "municipality", "province": "province",
    "postcode": "postcode", "vat": "vat", "rea": "rea", "ateco": "ateco", "ateco_text": "ateco_text",
    "pec": "pec", "email": "email", "phone": "phone", "legal_form": "legal_form", "employees": "employees_class",
    "start_date": "founded", "lat": "lat", "lon": "lon",
}


def is_list_source(source: str) -> bool:
    return str(source or "").startswith(LIST_PREFIX)


def row_key(record: dict) -> tuple[str, str]:
    """(register key, company tax code) of an imported row: the 11-digit tax code (or
    VAT number) when there is one, else "L-" + a hash of the old dialog's identity
    (name + municipality, or a personal tax code, which is never stored itself)."""
    cf = company_cf(record.get("cf")) or company_cf(record.get("vat"))
    if cf:
        return cf, cf
    from ..discovery.imports import dedup_key

    identity = dedup_key(record)
    if not identity:
        return "", ""
    return "L-" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16], ""


def register_fields(record: dict) -> dict[str, str]:
    """The register fields of an imported row (empty values left out)."""
    result = {}
    for name, target in FIELD_MAP.items():
        value = record.get(name)
        if value in (None, ""):
            continue
        result[target] = str(value).strip()
    return {k: v for k, v in result.items() if v}


def to_row(record: dict, source_id: str, source_date: str = "") -> RegisterRow | None:
    key, cf = row_key(record)
    if not key or not str(record.get("name") or "").strip():
        return None
    return RegisterRow(source_id, cf or key, register_fields(record), source_date=source_date)


# --- metadata --------------------------------------------------------------------------------


def _read_meta(db, key: str) -> dict:
    try:
        value = json.loads(db.meta(key, "") or "{}")
    except (ValueError, TypeError):
        log.warning("Register: metadata %s not readable", key)
        return {}
    return value if isinstance(value, dict) else {}


def _write_meta(db, key: str, value: dict) -> None:
    db.set_meta(key, json.dumps(value, ensure_ascii=False))


def lists(db) -> list[dict]:
    """[{"id", "name", "kind", "created", "count", "source", "source_date", "refreshed",
    "path"}] of the imported lists, the most recently imported or refreshed first."""
    result = []
    for source_id, info in _read_meta(db, META_LISTS).items():
        if isinstance(info, dict):
            result.append({"id": source_id, "name": str(info.get("name") or ""), "kind": str(info.get("kind") or ""),
                           "created": info.get("created") or 0, "count": int(info.get("count") or 0),
                           "source": str(info.get("source") or info.get("name") or ""),
                           "source_date": str(info.get("source_date") or ""),
                           "refreshed": info.get("refreshed") or 0, "path": str(info.get("path") or "")})
    result.sort(key=lambda item: -float(item.get("refreshed") or item.get("created") or 0))
    return result


def list_info(db, source_id: str) -> dict | None:
    return next((i for i in lists(db) if i["id"] == source_id), None)


def list_title(db, source_id: str) -> str:
    """The name the user knows a list source by ("" when it is not a list)."""
    info = list_info(db, source_id) if is_list_source(source_id) else None
    return (info.get("source") or info.get("name")) if info else ""


# --- import / refresh / delete -----------------------------------------------------------------


@dataclass
class ListReport:
    state: str = "ok"            # ok | failed | cancelled
    source_id: str = ""
    rows: int = 0                # rows given
    added: int = 0               # companies new to the register
    removed: int = 0             # companies the list no longer gives (refresh)
    edits: int = 0               # hand corrections kept as locked edits (migration)
    manual: int = 0              # companies added by hand (migration)
    skipped: int = 0             # rows without a name
    detail: str = ""


def _date_of(value) -> str:
    if isinstance(value, (int, float)) and value:
        try:
            return date.fromtimestamp(float(value)).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    return str(value or "")[:10]


def _store(db, source_id: str, records: list[dict], source_date: str, path: str, progress, cancel) -> ListReport:
    report = ListReport(source_id=source_id)
    rows: list[RegisterRow] = []
    manual: list[dict] = []
    for record in records or []:
        if not isinstance(record, dict) or not str(record.get("name") or "").strip():
            report.skipped += 1
            continue
        if record.get("_manual"):
            manual.append(record)
            continue
        row = to_row(record, source_id, source_date)
        if row is None:
            report.skipped += 1
        else:
            rows.append(row)
    report.rows = len(rows) + len(manual)
    load = db.load_source(source_id, rows, None, replace=True, progress=progress, cancel=cancel,
                          files=[Path(path).name] if path else [])
    if load.state != "ok":
        report.state, report.detail = load.state, load.detail
        return report
    report.added, report.removed = load.new, load.removed
    for record in records or []:
        if not isinstance(record, dict) or record.get("_manual") or not record.get("_edited"):
            continue
        key, cf = row_key(record)
        edited = {FIELD_MAP[f]: record.get(f, "") for f in record.get("_edited") or [] if f in FIELD_MAP}
        if key and edited and db.set_user_values(key, edited, cf=cf):
            report.edits += 1
    for record in manual:
        key, cf = row_key(record)
        if not cf:
            rid = str(record.get("_rid") or "") or hashlib.sha1(json.dumps(
                record, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
            key = "U-" + rid
        if db.set_user_values(key, register_fields(record), user_added=True, cf=cf):
            report.manual += 1
    return report


def import_list(db, name: str, kind: str, records: list[dict], *, source: str = "", source_date: str = "",
                path: str = "", list_id: str | None = None, created: float | None = None, progress=None,
                cancel=None) -> ListReport:
    """Store a list the user imported (rows as `discovery.imports.records` gives them) as
    a new source of the register. Never raises."""
    source_id = LIST_PREFIX + (list_id or f"{int(time.time())}-{uuid.uuid4().hex[:8]}")
    source_date = source_date or _date_of(created or time.time())
    try:
        report = _store(db, source_id, records, source_date, path, progress, cancel)
    except Exception as error:  # noqa: BLE001 - reported in the result
        log.warning("Register: list import failed", exc_info=True)
        return ListReport("failed", source_id, detail=f"{type(error).__name__}: {error}")
    if report.state == "ok":
        meta = _read_meta(db, META_LISTS)
        meta[source_id] = {"name": name, "kind": kind, "created": created or time.time(),
                           "source": source or name, "source_date": source_date, "path": str(path or ""),
                           "refreshed": 0, "count": report.rows}
        _write_meta(db, META_LISTS, meta)
        log.info("Register: list imported (%d rows, %d new companies)", report.rows, report.added)
    return report


def refresh_list(db, source_id: str, records: list[dict], *, path: str = "", source_date: str = "",
                 progress=None, cancel=None) -> ListReport:
    """Replace a list's rows with a newer file of the same list: hand edits stay locked,
    companies the user added stay, companies no source lists any more disappear."""
    meta = _read_meta(db, META_LISTS)
    if source_id not in meta:
        return ListReport("failed", source_id, detail="unknown list")
    source_date = source_date or date.today().isoformat()
    try:
        report = _store(db, source_id, records, source_date, path, progress, cancel)
    except Exception as error:  # noqa: BLE001
        log.warning("Register: list refresh failed", exc_info=True)
        return ListReport("failed", source_id, detail=f"{type(error).__name__}: {error}")
    if report.state == "ok":
        meta = _read_meta(db, META_LISTS)
        info = dict(meta.get(source_id) or {})
        info.update({"refreshed": time.time(), "source_date": source_date, "count": report.rows})
        if path:
            info["path"] = str(path)
        meta[source_id] = info
        _write_meta(db, META_LISTS, meta)
    return report


def delete_list(db, source_id: str) -> int:
    """Drop an imported list (its companies stay when another source, a hand edit or
    the user's own addition keeps them). Returns the companies it listed."""
    count = db.remove_source(source_id)
    meta = _read_meta(db, META_LISTS)
    if meta.pop(source_id, None) is not None:
        _write_meta(db, META_LISTS, meta)
    return count


# --- migration from the Store ------------------------------------------------------------------


@dataclass
class MigrationReport:
    lists: int = 0               # lists migrated now
    already: int = 0             # lists migrated by an earlier run
    rows: int = 0
    edits: int = 0
    manual: int = 0
    failed: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.lists)


def pending_lists(store) -> list[str]:
    """Ids of the Store's lists (the old "imports" namespace)."""
    from ..discovery import imports

    try:
        return [info["id"] for info in imports.lists(store)]
    except Exception:  # noqa: BLE001
        log.warning("Register: the old imported lists cannot be read", exc_info=True)
        return []


def migrate_imported_lists(store, db, cancel=None) -> MigrationReport:
    """Copy the lists older versions kept in the Store into the register, once (see the
    module docstring). Oldest list first, so a newer list's values win as before."""
    from ..discovery import imports

    report = MigrationReport()
    if store is None:
        return report
    try:
        index = [i for i in (store.cache_read(imports.NAMESPACE, imports.INDEX_KEY) or []) if isinstance(i, str)]
        if not index:
            return report
        done = _read_meta(db, META_MIGRATED)
        if all(list_id in done for list_id in index):   # the usual case: one small read, nothing to do
            report.already = len(index)
            return report
        infos = imports.lists(store)
    except Exception as error:  # noqa: BLE001 - e.g. the Store was closed meanwhile: tried again later
        log.info("Register: the old imported lists cannot be read now (%s)", type(error).__name__)
        return report
    for info in reversed(infos):     # lists() gives the newest first
        if cancel is not None and cancel.is_set():
            break
        if info["id"] in done:
            report.already += 1
            continue
        value = imports.read_list(store, info["id"])
        if value is None:
            continue
        created = float(info.get("refreshed") or info.get("created") or 0) or None
        result = import_list(db, info["name"], info["kind"], list(value.get("rows") or []),
                             source=info.get("source") or info["name"],
                             source_date=str(info.get("source_date") or "") or _date_of(created),
                             path=info.get("path") or "", list_id=info["id"], created=created)
        if result.state != "ok":
            report.failed.append(info["id"])
            log.warning("Register: old list not migrated (%s)", result.detail)
            continue
        if info.get("kind") == MANUAL_KIND:
            # the list that only held companies added by hand: they are the user's now
            meta = _read_meta(db, META_LISTS)
            meta.pop(result.source_id, None)
            _write_meta(db, META_LISTS, meta)
        done[info["id"]] = result.source_id
        _write_meta(db, META_MIGRATED, done)
        report.lists += 1
        report.rows += result.rows
        report.edits += result.edits
        report.manual += result.manual
    if report.lists:
        log.info("Register: %d imported lists of an older version moved into the register (%d rows, "
                 "%d hand corrections, %d companies added by hand); the old copies are kept",
                 report.lists, report.rows, report.edits, report.manual)
    return report


__all__ = [
    "FIELD_MAP", "LIST_PREFIX", "ListReport", "MigrationReport", "UNCODED_KEPT", "delete_list", "import_list",
    "is_list_source", "list_info", "list_title", "lists", "migrate_imported_lists", "pending_lists",
    "refresh_list", "register_fields", "row_key", "to_row",
]
