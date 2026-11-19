"""The regional register: its own SQLite file (`<data>/registers/register.sqlite`).

Tables
- `company`: one row per company (key = 11-digit tax code; "U-…" for a company the user
  added without one) with the RESOLVED values used by queries, plus flags;
- `field_value`: every source's value of every field, with its date (provenance);
- `user_edit`: the user's values; an edited field is LOCKED (a refresh never changes it);
- `hidden`: companies the user deleted: kept hidden, a refresh does not bring them back;
- `signal`: dated facts (hiring incentives from RNA);
- `undo_log`: the user's actions, newest last (undo restores the state before);
- `import_log`: one line per source load; `meta`: schema version and settings.

Merging is by tax code only: a company with the same name but another tax code stays a
separate row (`possible_duplicates` lets the GUI ask the user). For each field the value
of the first source in `model.PRIORITY` wins; the province falls back to the tax-office
digits (flagged "vat_office", or a list of candidates when the office code is shared).

Threads: one sqlite3 connection per thread (WAL where the folder allows it, like the
main store); writes are serialised by a process lock plus `BEGIN IMMEDIATE`.
"""

from __future__ import annotations

import contextlib
import csv
import json
import logging
import sqlite3
import threading
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..numbers import format_int
from . import texts
from .italy_codes import region_of_province, tax_office_provinces
from .model import (CSV_IMPORT, DERIVED, EDITABLE, FIELDS, USER, CancelledError, RegisterProgress, RegisterRow,
                    check_cancel, company_cf, division, fold, name_key, notify, now_iso, priority, today_iso)
from .record import RegisterCompany

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 15000
CHUNK = 500
UNDO_LIMIT = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS company (
    key TEXT PRIMARY KEY, cf TEXT,
    name TEXT, name_norm TEXT, name_key TEXT, vat TEXT, legal_form TEXT, address TEXT, postcode TEXT,
    municipality TEXT, municipality_norm TEXT, municipality_istat TEXT,
    province TEXT, province_src TEXT, province_guess TEXT, region TEXT,
    ateco TEXT, ateco_text TEXT, nace_hints TEXT, sector_codes TEXT,
    size_class TEXT, employees_class TEXT, founded TEXT, lei TEXT, website TEXT, status TEXT,
    legality_rating TEXT, legality_until TEXT, legality_state TEXT, lat REAL, lon REAL, pec TEXT, rea TEXT,
    aid_count INTEGER, last_aid_date TEXT, aid_measures TEXT, hiring_count INTEGER, last_hiring_date TEXT,
    note TEXT, provenance TEXT, sources TEXT, locked TEXT,
    user_added INTEGER NOT NULL DEFAULT 0, first_seen TEXT, last_seen TEXT, updated TEXT
);
CREATE INDEX IF NOT EXISTS company_province ON company(province);
CREATE INDEX IF NOT EXISTS company_region ON company(region);
CREATE INDEX IF NOT EXISTS company_municipality ON company(municipality_norm);
CREATE INDEX IF NOT EXISTS company_ateco ON company(ateco);
CREATE INDEX IF NOT EXISTS company_name ON company(name_norm);
CREATE INDEX IF NOT EXISTS company_name_key ON company(name_key);
CREATE INDEX IF NOT EXISTS company_cf ON company(cf);
CREATE TABLE IF NOT EXISTS field_value (
    key TEXT NOT NULL, field TEXT NOT NULL, source TEXT NOT NULL, value TEXT, source_date TEXT,
    PRIMARY KEY (key, field, source)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS field_value_region ON field_value(source, value) WHERE field = 'region';
CREATE TABLE IF NOT EXISTS user_edit (
    key TEXT NOT NULL, field TEXT NOT NULL, value TEXT, edited_at TEXT, PRIMARY KEY (key, field)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hidden (key TEXT PRIMARY KEY, name TEXT, hidden_at TEXT);
CREATE TABLE IF NOT EXISTS signal (
    key TEXT NOT NULL, kind TEXT NOT NULL, date TEXT NOT NULL, title TEXT NOT NULL, source TEXT,
    PRIMARY KEY (key, kind, date, title)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS signal_source ON signal(source);
CREATE TABLE IF NOT EXISTS undo_log (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT, key TEXT,
    payload TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS import_log (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, regions TEXT,
    files TEXT, rows_kept INTEGER, rows_removed INTEGER, source_date TEXT, started TEXT, finished TEXT,
    state TEXT, detail TEXT);
"""
# version -> statements (columns added later; never in _SCHEMA above, which stays version 1)
_MIGRATIONS: dict[int, list[str]] = {
    2: ["ALTER TABLE company ADD COLUMN email TEXT", "ALTER TABLE company ADD COLUMN phone TEXT"],
}

_INT_FIELDS = ("aid_count",)
_REAL_FIELDS = ("lat", "lon")

# Official counts (coverage denominators), study A.1.5. key: (kind, place, ATECO code)
# -> (low, high, basis). "estimate" when derived, else the published count.
OFFICIAL_COUNTS: dict[tuple[str, str, str], tuple[int, int, str]] = {
    ("province", "FC", "28"): (250, 300, "estimate from the Chamber of Commerce counts of Cesena, 2025"),
    ("province", "BO", "28"): (800, 1000, "estimate from the counts of the Comune di Bologna, 2025"),
    ("municipality", "cesena", "26"): (11, 11, "Chamber of Commerce, active companies 2025"),
    ("municipality", "cesena", "27"): (17, 17, "Chamber of Commerce, active companies 2025"),
    ("municipality", "cesena", "28"): (59, 59, "Chamber of Commerce, active companies 2025"),
    ("municipality", "cesena", "33.2"): (21, 21, "Chamber of Commerce, active companies 2025"),
    ("municipality", "cesena", "62"): (110, 110, "Chamber of Commerce, active companies 2025"),
    ("municipality", "cesena", "71.1"): (34, 34, "Chamber of Commerce, active companies 2025"),
    ("municipality", "bologna", "26"): (178, 178, "Comune di Bologna open data, active companies"),
    ("municipality", "bologna", "27"): (87, 87, "Comune di Bologna open data, active companies"),
    ("municipality", "bologna", "28"): (214, 214, "Comune di Bologna open data, active companies"),
    ("municipality", "bologna", "33.2"): (180, 180, "Comune di Bologna open data, active companies"),
    ("municipality", "bologna", "62"): (1120, 1120, "Comune di Bologna open data, active companies"),
    ("municipality", "bologna", "71.1"): (294, 294, "Comune di Bologna open data, active companies"),
}


@dataclass
class LoadReport:
    source: str
    rows: int = 0          # rows given by the source
    new: int = 0           # companies not in the register before
    removed: int = 0       # companies this source no longer lists
    kept_locked: int = 0   # companies with locked (user-edited) fields among the loaded ones
    hidden_skipped: int = 0
    state: str = "ok"      # ok | cancelled | failed
    detail: str = ""


@dataclass
class ImportCsvReport:
    rows: int = 0
    added: int = 0
    updated: int = 0
    locked_fields: int = 0
    hidden: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _chunks(items: list, size: int = CHUNK):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _pack_provenance(provenance: dict[str, list[str]]) -> str:
    """{field: [source, date]} stored compactly as {"source|date": [fields]}."""
    grouped: dict[str, list[str]] = defaultdict(list)
    for name, (source, day) in provenance.items():
        grouped[f"{source}|{day or ''}"].append(name)
    return json.dumps(grouped, ensure_ascii=False, separators=(",", ":"))


def _unpack_provenance(text) -> dict[str, tuple[str, str]]:
    try:
        grouped = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    result = {}
    for marker, names in grouped.items():
        source, _, day = str(marker).partition("|")
        for name in names or ():
            result[name] = (source, day)
    return result


def _wal_allowed(path: str) -> bool:
    try:
        from ..store import wal_allowed
        return bool(wal_allowed(path))
    except Exception:  # noqa: BLE001 - the helper is optional
        return True


class RegisterDB:
    """See the module docstring. Every public method catches database errors that are
    not the caller's fault and logs them; queries return empty results then."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._shared = self.path == ":memory:"
        if not self._shared:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._lock = threading.RLock()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._wal = False
        conn = self._open()
        if not self._shared and _wal_allowed(self.path):
            try:
                mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).upper()
                self._wal = mode == "WAL"
                if self._wal:
                    conn.execute("PRAGMA synchronous = NORMAL")
            except sqlite3.Error:
                log.info("WAL not possible for %s", self.path)
        with self._lock:
            conn.executescript(_SCHEMA)
        self._migrate()

    # --- connections

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if self._wal:
            conn.execute("PRAGMA synchronous = NORMAL")
        with self._connections_lock:
            self._connections.append(conn)
        if self._shared:
            self._shared_conn = conn
        else:
            self._local.conn = conn
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._shared:
            return self._shared_conn
        conn = getattr(self._local, "conn", None)
        return conn if conn is not None else self._open()

    @contextlib.contextmanager
    def _write(self):
        with self._lock:
            conn = self.conn
            if conn.in_transaction:
                yield conn
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    @contextlib.contextmanager
    def _read(self):
        if self._shared:
            with self._lock:
                yield self.conn
        else:
            yield self.conn

    def _migrate(self) -> None:
        with self._write() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            current = int(row["value"]) if row else 0
            for version in range(current + 1, SCHEMA_VERSION + 1):
                for statement in _MIGRATIONS.get(version, []):
                    conn.execute(statement)
            if current != SCHEMA_VERSION:
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                             (str(SCHEMA_VERSION),))

    @property
    def schema_version(self) -> int:
        with self._read() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return int(row["value"]) if row else 0

    @property
    def journal_mode(self) -> str:
        with self._read() as conn:
            return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).upper()

    def close(self) -> None:
        with self._connections_lock:
            connections, self._connections = self._connections, []
        for conn in connections:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        self._local = threading.local()

    def meta(self, key: str, default: str = "") -> str:
        with self._read() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._write() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))

    # --- loading sources

    def _keys_of_source(self, conn, source: str, regions: set[str] | None) -> set[str]:
        if regions:
            marks = ",".join("?" * len(regions))
            rows = conn.execute(f"SELECT key FROM field_value WHERE source = ? AND field = 'region' "
                                f"AND value IN ({marks})", (source, *sorted(regions))).fetchall()
        else:
            rows = conn.execute("SELECT DISTINCT key FROM field_value WHERE source = ?", (source,)).fetchall()
        return {r["key"] for r in rows}

    def load_source(self, source: str, rows: Iterable[RegisterRow], regions: set[str] | None = None, *,
                    replace: bool = True, progress=None, cancel=None, files: list[str] | None = None) -> LoadReport:
        """Store the rows of one source. With `replace`, the source's previous values for
        the companies of `regions` are dropped first (a company it no longer lists loses
        that source; a company no source lists any more disappears unless the user added
        or edited it). Atomic: a cancel or an error leaves the register as it was.
        Never raises (the report says what happened)."""
        report = LoadReport(source)
        started = now_iso()
        today = today_iso()
        source_date = ""
        try:
            with self._write() as conn:
                old = self._keys_of_source(conn, source, regions) if replace else set()
                if old:
                    for chunk in _chunks(sorted(old)):
                        marks = ",".join("?" * len(chunk))
                        conn.execute(f"DELETE FROM field_value WHERE source = ? AND key IN ({marks})", (source, *chunk))
                        conn.execute(f"DELETE FROM signal WHERE source = ? AND key IN ({marks})", (source, *chunk))
                existing = {r["key"] for r in conn.execute("SELECT key FROM company")}
                hidden = {r["key"] for r in conn.execute("SELECT key FROM hidden")}
                seen: set[str] = set()
                values, signals, companies = [], [], []
                for row in rows:
                    report.rows += 1
                    if report.rows % 2000 == 0:
                        check_cancel(cancel)
                        notify(progress, RegisterProgress(source, "merge", rows=report.rows))
                    cf = company_cf(row.cf)
                    key = cf or row.cf    # a list row without tax code: its stable "L-…" key
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    if key in hidden:
                        report.hidden_skipped += 1
                    source_date = max(source_date, row.source_date or "")
                    if key not in existing:
                        companies.append((key, cf, today, today))
                        existing.add(key)
                        report.new += 1
                    for name, value in row.fields.items():
                        if value not in (None, ""):
                            values.append((key, name, source, str(value), row.source_date or today))
                    for sig in row.signals:
                        signals.append((key, sig.kind, sig.date or "", sig.title or "", source))
                    if len(values) > 20000:
                        self._flush(conn, companies, values, signals)
                self._flush(conn, companies, values, signals)
                check_cancel(cancel)
                removed = old - seen
                report.removed = len(removed)
                if seen:
                    for chunk in _chunks(sorted(seen)):
                        marks = ",".join("?" * len(chunk))
                        conn.execute(f"UPDATE company SET last_seen = ? WHERE key IN ({marks})", (today, *chunk))
                notify(progress, RegisterProgress(source, "merge", rows=report.rows, message="resolve"))
                self._resolve(conn, sorted(seen | removed), cancel)
                self._drop_orphans(conn, sorted(removed))
                edited = {r["key"] for r in conn.execute("SELECT DISTINCT key FROM user_edit")}
                report.kept_locked = len(edited & seen)
                conn.execute("INSERT INTO import_log (source, regions, files, rows_kept, rows_removed, source_date, "
                             "started, finished, state, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (source, ",".join(sorted(regions or [])), json.dumps(files or []), len(seen),
                              report.removed, source_date, started, now_iso(), "ok", ""))
        except CancelledError:
            report.state, report.detail = "cancelled", "cancelled"
        except sqlite3.Error as error:
            log.warning("Register: loading %s failed", source, exc_info=True)
            report.state, report.detail = "failed", f"{type(error).__name__}: {error}"
        except Exception as error:  # noqa: BLE001 - a broken parser never breaks the register
            log.warning("Register: loading %s failed", source, exc_info=True)
            report.state, report.detail = "failed", f"{type(error).__name__}: {error}"
        self.checkpoint()
        notify(progress, RegisterProgress(source, "done" if report.state == "ok" else report.state, rows=report.rows))
        return report

    def checkpoint(self) -> None:
        """Fold the WAL back into the file (a big load would otherwise leave it large)."""
        if not self._wal:
            return
        try:
            with self._lock:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            log.debug("Register: WAL checkpoint skipped", exc_info=True)

    def compact(self) -> None:
        """VACUUM (after a purge of many rows); may take a few seconds."""
        try:
            with self._lock:
                self.conn.execute("VACUUM")
            self.checkpoint()
        except sqlite3.Error:
            log.warning("Register: VACUUM failed", exc_info=True)

    @staticmethod
    def _flush(conn, companies: list, values: list, signals: list) -> None:
        if companies:
            conn.executemany("INSERT OR IGNORE INTO company (key, cf, first_seen, last_seen) VALUES (?,?,?,?)",
                             companies)
            companies.clear()
        if values:
            conn.executemany("INSERT OR REPLACE INTO field_value (key, field, source, value, source_date) "
                             "VALUES (?,?,?,?,?)", values)
            values.clear()
        if signals:
            conn.executemany("INSERT OR IGNORE INTO signal (key, kind, date, title, source) VALUES (?,?,?,?,?)",
                             signals)
            signals.clear()

    def _drop_orphans(self, conn, keys: list[str]) -> None:
        for chunk in _chunks(keys):
            marks = ",".join("?" * len(chunk))
            conn.execute(f"DELETE FROM company WHERE key IN ({marks}) AND user_added = 0 "
                         f"AND NOT EXISTS (SELECT 1 FROM field_value f WHERE f.key = company.key) "
                         f"AND NOT EXISTS (SELECT 1 FROM user_edit u WHERE u.key = company.key)", chunk)

    # --- resolution

    def _resolve(self, conn, keys: list[str], cancel=None) -> None:
        for chunk in _chunks(keys):
            check_cancel(cancel)
            marks = ",".join("?" * len(chunk))
            by_key: dict[str, dict] = defaultdict(lambda: defaultdict(dict))
            for r in conn.execute(f"SELECT key, field, source, value, source_date FROM field_value "
                                  f"WHERE key IN ({marks})", chunk):
                by_key[r["key"]][r["field"]][r["source"]] = (r["value"], r["source_date"] or "")
            edits: dict[str, dict] = defaultdict(dict)
            for r in conn.execute(f"SELECT key, field, value, edited_at FROM user_edit WHERE key IN ({marks})", chunk):
                edits[r["key"]][r["field"]] = (r["value"] or "", r["edited_at"] or "")
            hiring: dict[str, tuple[int, str]] = {}
            for r in conn.execute(f"SELECT key, COUNT(*) AS n, MAX(date) AS last FROM signal WHERE kind = 'hiring' "
                                  f"AND key IN ({marks}) GROUP BY key", chunk):
                hiring[r["key"]] = (r["n"], r["last"] or "")
            meta = {r["key"]: r for r in conn.execute(f"SELECT key, cf, user_added FROM company WHERE key IN ({marks})",
                                                      chunk)}
            updates = []
            for key in chunk:
                if key not in meta:
                    continue
                cf = meta[key]["cf"] or ""
                values = self._compute(cf, by_key.get(key, {}), edits.get(key, {}), hiring.get(key))
                updates.append(tuple(values[c] for c in _UPDATE_COLUMNS) + (key,))
            if updates:
                sets = ", ".join(f"{c} = ?" for c in _UPDATE_COLUMNS)
                conn.executemany(f"UPDATE company SET {sets} WHERE key = ?", updates)

    @staticmethod
    def _compute(cf: str, by_field: dict, edits: dict, hiring: tuple[int, str] | None) -> dict:
        resolved: dict[str, object] = {}
        provenance: dict[str, list[str]] = {}
        for name in FIELDS:
            if name in edits:
                resolved[name] = edits[name][0]
                provenance[name] = [USER, edits[name][1]]
                continue
            candidates = by_field.get(name) or {}
            for source in priority(name):
                if source in candidates and candidates[source][0] not in (None, ""):
                    resolved[name] = candidates[source][0]
                    provenance[name] = [source, candidates[source][1]]
                    break
            else:
                # sources missing from the order (the user's imported lists): the newest value
                for source, (value, day) in sorted(candidates.items(), key=lambda item: (item[1][1] or "", item[0]),
                                                   reverse=True):
                    if value not in (None, "") and source != USER:
                        resolved[name] = value
                        provenance[name] = [source, day]
                        break
        province = str(resolved.get("province") or "").upper()
        region = str(resolved.get("region") or "")
        province_src = provenance.get("province", [""])[0]
        guess = ""
        if not province and "province" not in edits:
            candidates = [p for p in tax_office_provinces(cf) if not region or region_of_province(p) == region]
            if len(candidates) == 1:
                province, province_src = candidates[0], "vat_office"
                provenance["province"] = ["vat_office", ""]
            elif candidates:
                guess = "," + ",".join(candidates) + ","
                province_src = "vat_office"
        if not region and province:
            region = region_of_province(province)
            provenance.setdefault("region", [DERIVED, ""])
        ateco = str(resolved.get("ateco") or "")
        hints = [h for h in str(resolved.get("nace_hints") or "").split(",") if h]
        codes = []
        for code in [ateco] + hints:
            if code and code not in codes:
                codes.append(code)
        sources = sorted({s for per in by_field.values() for s in per} | ({USER} if edits else set()))
        name = str(resolved.get("name") or "")
        municipality = str(resolved.get("municipality") or "")
        out = {c: resolved.get(c) for c in FIELDS}
        for c in _INT_FIELDS:
            try:
                out[c] = int(out[c]) if out[c] not in (None, "") else None
            except (TypeError, ValueError):
                out[c] = None
        for c in _REAL_FIELDS:
            try:
                out[c] = float(out[c]) if out[c] not in (None, "") else None
            except (TypeError, ValueError):
                out[c] = None
        out.update({
            "name": name, "name_norm": fold(name), "name_key": name_key(name), "province": province,
            "province_src": province_src, "province_guess": guess, "region": region,
            "municipality_norm": fold(municipality), "sector_codes": ("," + ",".join(codes) + ",") if codes else "",
            "hiring_count": hiring[0] if hiring else 0, "last_hiring_date": hiring[1] if hiring else "",
            "provenance": _pack_provenance(provenance),
            "sources": ",".join(sources), "locked": ",".join(sorted(edits)), "updated": now_iso(),
        })
        return out

    def resolve_all(self, cancel=None) -> int:
        with self._write() as conn:
            keys = [r["key"] for r in conn.execute("SELECT key FROM company")]
            self._resolve(conn, keys, cancel)
        return len(keys)

    def derive_municipality_names(self) -> int:
        """Companies known only by an ISTAT municipality code (OpenCoesione) get the
        municipality name most other sources give for that code (source "derived")."""
        with self._write() as conn:
            votes: dict[str, Counter] = defaultdict(Counter)
            for r in conn.execute(
                    "SELECT i.value AS code, m.value AS name FROM field_value i JOIN field_value m "
                    "ON m.key = i.key AND m.field = 'municipality' AND m.source != ? "
                    "WHERE i.field = 'municipality_istat'", (DERIVED,)):
                if r["name"]:
                    votes[r["code"]][r["name"].upper()] += 1
            names = {code: counter.most_common(1)[0][0] for code, counter in votes.items() if counter}
            rows = conn.execute("SELECT key, municipality_istat FROM company WHERE (municipality IS NULL OR "
                                "municipality = '') AND municipality_istat IS NOT NULL AND municipality_istat != ''"
                                ).fetchall()
            today = today_iso()
            values = [(r["key"], "municipality", DERIVED, names[r["municipality_istat"]], today)
                      for r in rows if r["municipality_istat"] in names]
            conn.executemany("INSERT OR REPLACE INTO field_value (key, field, source, value, source_date) "
                             "VALUES (?,?,?,?,?)", values)
            self._resolve(conn, [v[0] for v in values])
        return len(values)

    # --- reading

    def _record(self, row: sqlite3.Row, hiring: list[str] | None = None, hidden: bool = False) -> RegisterCompany:
        values = {name: row[name] for name in FIELDS if name in row.keys()}
        prov = _unpack_provenance(row["provenance"])
        guess = tuple(p for p in (row["province_guess"] or "").split(",") if p)
        return RegisterCompany(row["key"], row["cf"] or "", values, prov,
                               tuple(f for f in (row["locked"] or "").split(",") if f),
                               tuple(s for s in (row["sources"] or "").split(",") if s), bool(row["user_added"]),
                               hidden, row["province_src"] or "", guess, tuple(hiring or ()))

    def _hiring_for(self, conn, keys: list[str]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = defaultdict(list)
        for chunk in _chunks(keys):
            marks = ",".join("?" * len(chunk))
            for r in conn.execute(f"SELECT key, date FROM signal WHERE kind = 'hiring' AND key IN ({marks}) "
                                  f"ORDER BY date", chunk):
                if r["date"] and r["date"] not in result[r["key"]]:
                    result[r["key"]].append(r["date"])
        return result

    def get(self, key: str) -> RegisterCompany | None:
        key = company_cf(key) or key
        try:
            with self._read() as conn:
                row = conn.execute("SELECT * FROM company WHERE key = ?", (key,)).fetchone()
                if row is None:
                    return None
                hidden = conn.execute("SELECT 1 FROM hidden WHERE key = ?", (key,)).fetchone() is not None
                hiring = self._hiring_for(conn, [key]).get(key, [])
            return self._record(row, hiring, hidden)
        except sqlite3.Error:
            log.warning("Register: reading %s failed", key, exc_info=True)
            return None

    def provenance_of(self, key: str) -> dict[str, list[dict]]:
        """{field: [{"source", "value", "date", "chosen"}]} every source's value (hover text)."""
        record = self.get(key)
        if record is None:
            return {}
        result: dict[str, list[dict]] = defaultdict(list)
        with self._read() as conn:
            for r in conn.execute("SELECT field, source, value, source_date FROM field_value WHERE key = ?",
                                  (record.key,)):
                chosen = record.provenance.get(r["field"], ("",))[0] == r["source"]
                result[r["field"]].append({"source": r["source"], "value": r["value"], "date": r["source_date"],
                                           "chosen": chosen})
            for r in conn.execute("SELECT field, value, edited_at FROM user_edit WHERE key = ?", (record.key,)):
                result[r["field"]].insert(0, {"source": USER, "value": r["value"], "date": r["edited_at"],
                                              "chosen": True})
        return dict(result)

    def find(self, region: str | None = None, province: str | None = None, municipality: str | None = None,
             ateco_prefixes=(), text: str | None = None, only_active: bool = True, limit: int | None = None, *,
             include_hints: bool = True, include_presumed_province: bool = True, include_hidden: bool = False,
             offset: int = 0, include_unplaced: bool = False,
             keep_uncoded: tuple[str, ...] = ()) -> list[RegisterCompany]:
        """Companies of the register matching every given filter (indexed columns).

        `ateco_prefixes`: codes such as "28", "26.5", "33.2"; a company matches by its
        exact ATECO code and, with `include_hints`, by the RNA sector hints too.
        `province`: also companies whose province is only presumed from the tax office
        when `include_presumed_province`. `text`: words of the name, or a tax code.
        `include_unplaced`: with `region`, also the companies without any region (rows of
        an imported list that give only a town). `keep_uncoded`: with `ateco_prefixes`,
        also the companies without any code that one of these sources lists (source-id
        prefixes, e.g. "list:", "user": an imported list cannot be judged by a code)."""
        sql, params = self._where(region, province, municipality, ateco_prefixes, text, only_active,
                                  include_hints, include_presumed_province, include_hidden, include_unplaced,
                                  keep_uncoded)
        sql = f"SELECT * FROM company c WHERE {sql} ORDER BY c.name_norm"
        if limit:
            sql += " LIMIT ? OFFSET ?"
            params += [int(limit), int(offset)]
        try:
            with self._read() as conn:
                rows = conn.execute(sql, params).fetchall()
                hidden = {r["key"] for r in conn.execute("SELECT key FROM hidden")} if include_hidden else set()
                hiring = self._hiring_for(conn, [r["key"] for r in rows if r["hiring_count"]])
        except sqlite3.Error:
            log.warning("Register: query failed", exc_info=True)
            return []
        return [self._record(r, hiring.get(r["key"]), r["key"] in hidden) for r in rows]

    def count(self, **filters) -> int:
        allowed = ("region", "province", "municipality", "ateco_prefixes", "text", "only_active", "include_hints",
                   "include_presumed_province", "include_hidden", "include_unplaced", "keep_uncoded")
        args = {k: v for k, v in filters.items() if k in allowed}
        sql, params = self._where(args.get("region"), args.get("province"), args.get("municipality"),
                                  args.get("ateco_prefixes", ()), args.get("text"), args.get("only_active", True),
                                  args.get("include_hints", True), args.get("include_presumed_province", True),
                                  args.get("include_hidden", False), args.get("include_unplaced", False),
                                  args.get("keep_uncoded", ()))
        try:
            with self._read() as conn:
                return int(conn.execute(f"SELECT COUNT(*) FROM company c WHERE {sql}", params).fetchone()[0])
        except sqlite3.Error:
            log.warning("Register: count failed", exc_info=True)
            return 0

    @staticmethod
    def _where(region, province, municipality, ateco_prefixes, text, only_active, include_hints,
               include_presumed, include_hidden, include_unplaced=False, keep_uncoded=()) -> tuple[str, list]:
        from ..discovery.ateco import normalize_code
        from .italy_codes import region_key

        clauses, params = ["1 = 1"], []
        if not include_hidden:
            clauses.append("NOT EXISTS (SELECT 1 FROM hidden h WHERE h.key = c.key)")
        if region:
            if include_unplaced:
                clauses.append("(c.region = ? OR COALESCE(c.region, '') = '')")
            else:
                clauses.append("c.region = ?")
            params.append(region_key(region) or region)
        if province:
            code = province.strip().upper()
            if include_presumed:
                clauses.append("(c.province = ? OR (c.province = '' AND c.province_guess LIKE ?))")
                params += [code, f"%,{code},%"]
            else:
                clauses.append("c.province = ? AND c.province_src != 'vat_office'")
                params.append(code)
        if municipality:
            clauses.append("c.municipality_norm = ?")
            params.append(fold(municipality))
        codes = [normalize_code(p) or str(p).strip() for p in (ateco_prefixes or ()) if str(p).strip()]
        codes = [c for c in codes if c]
        if codes:
            column = "c.sector_codes" if include_hints else "(',' || COALESCE(c.ateco, '') || ',')"
            alternatives = [f"{column} LIKE ?" for _ in codes]
            params += [f"%,{c}%" for c in codes]
            if keep_uncoded:
                kept = " OR ".join("(',' || COALESCE(c.sources, '')) LIKE ?" for _ in keep_uncoded)
                alternatives.append(f"(COALESCE(c.sector_codes, '') = '' AND ({kept}))")
                params += [f"%,{prefix}%" for prefix in keep_uncoded]
            clauses.append("(" + " OR ".join(alternatives) + ")")
        if text:
            cf = company_cf(text)
            words = fold(text).split()
            if cf:
                clauses.append("(c.cf = ? OR c.vat = ?)")
                params += [cf, cf]
            elif words:
                for word in words:
                    clauses.append("c.name_norm LIKE ?")
                    params.append(f"%{word}%")
        if only_active:
            clauses.append("COALESCE(c.status, '') != 'inactive'")
        return " AND ".join(clauses), params

    def possible_duplicates(self, key_or_name: str, municipality: str = "", limit: int = 10) -> list[RegisterCompany]:
        """Rows with the same name (legal form ignored) but another key: SUGGESTIONS for
        the user, never merged automatically."""
        record = self.get(key_or_name)
        nkey = name_key(record.name if record else key_or_name)
        place = fold(municipality or (record.get("municipality") if record else ""))
        if not nkey:
            return []
        sql = "SELECT * FROM company WHERE name_key = ?"
        params: list = [nkey]
        if record is not None:
            sql += " AND key != ?"
            params.append(record.key)
        if place:
            sql += " AND (municipality_norm = ? OR municipality_norm = '' OR municipality_norm IS NULL)"
            params.append(place)
        sql += " LIMIT ?"
        params.append(limit)
        with self._read() as conn:
            return [self._record(r) for r in conn.execute(sql, params)]

    # --- user edits

    def _log_undo(self, conn, action: str, key: str, payload: dict) -> None:
        conn.execute("INSERT INTO undo_log (action, key, payload, at) VALUES (?,?,?,?)",
                     (action, key, json.dumps(payload, ensure_ascii=False), now_iso()))
        conn.execute("DELETE FROM undo_log WHERE id <= (SELECT MAX(id) FROM undo_log) - ?", (UNDO_LIMIT,))

    def edit(self, key: str, fields: dict[str, object]) -> bool:
        """The user changes fields of a company: each becomes LOCKED (a refresh never
        changes it; an empty value is a locked empty field). One undo step."""
        key = company_cf(key) or key
        changes = {k: ("" if v is None else str(v).strip()) for k, v in (fields or {}).items() if k in EDITABLE}
        if not changes:
            return False
        try:
            with self._write() as conn:
                if conn.execute("SELECT 1 FROM company WHERE key = ?", (key,)).fetchone() is None:
                    return False
                before = {}
                for name in changes:
                    row = conn.execute("SELECT value, edited_at FROM user_edit WHERE key = ? AND field = ?",
                                       (key, name)).fetchone()
                    before[name] = [row["value"], row["edited_at"]] if row else None
                stamp = now_iso()
                conn.executemany("INSERT OR REPLACE INTO user_edit (key, field, value, edited_at) VALUES (?,?,?,?)",
                                 [(key, n, v, stamp) for n, v in changes.items()])
                self._log_undo(conn, "edit", key, {"before": before})
                self._resolve(conn, [key])
            return True
        except sqlite3.Error:
            log.warning("Register: edit of %s failed", key, exc_info=True)
            return False

    def unlock(self, key: str, field_name: str) -> bool:
        """Forget the user's value of a field: the sources' value is used again."""
        key = company_cf(key) or key
        try:
            with self._write() as conn:
                row = conn.execute("SELECT value, edited_at FROM user_edit WHERE key = ? AND field = ?",
                                   (key, field_name)).fetchone()
                if row is None:
                    return False
                conn.execute("DELETE FROM user_edit WHERE key = ? AND field = ?", (key, field_name))
                self._log_undo(conn, "edit", key, {"before": {field_name: [row["value"], row["edited_at"]]}})
                self._resolve(conn, [key])
            return True
        except sqlite3.Error:
            log.warning("Register: unlock failed", exc_info=True)
            return False

    def add_company(self, fields: dict[str, object], cf: str = "") -> str | None:
        """A company added by hand (all its fields locked). With a valid tax code that is
        already in the register, the values are applied as edits of that company (and it
        is shown again if hidden). Returns the key, None without a name."""
        cf = company_cf(cf or fields.get("cf", ""))
        clean = {k: str(v).strip() for k, v in fields.items() if k in EDITABLE and v not in (None, "")}
        try:
            with self._write() as conn:
                exists = cf and conn.execute("SELECT 1 FROM company WHERE key = ?", (cf,)).fetchone()
                if exists:
                    was_hidden = conn.execute("SELECT name, hidden_at FROM hidden WHERE key = ?", (cf,)).fetchone()
                    if was_hidden:
                        conn.execute("DELETE FROM hidden WHERE key = ?", (cf,))
                    before = {}
                    for name in clean:
                        row = conn.execute("SELECT value, edited_at FROM user_edit WHERE key = ? AND field = ?",
                                           (cf, name)).fetchone()
                        before[name] = [row["value"], row["edited_at"]] if row else None
                    stamp = now_iso()
                    conn.executemany("INSERT OR REPLACE INTO user_edit (key, field, value, edited_at) "
                                     "VALUES (?,?,?,?)", [(cf, n, v, stamp) for n, v in clean.items()])
                    self._log_undo(conn, "edit", cf, {"before": before,
                                                      "hidden": dict(was_hidden) if was_hidden else None})
                    self._resolve(conn, [cf])
                    return cf
                if not clean.get("name"):
                    return None
                key = cf or "U-" + uuid.uuid4().hex[:12]
                today = today_iso()
                conn.execute("INSERT INTO company (key, cf, user_added, first_seen, last_seen) VALUES (?,?,1,?,?)",
                             (key, cf, today, today))
                stamp = now_iso()
                conn.executemany("INSERT OR REPLACE INTO user_edit (key, field, value, edited_at) VALUES (?,?,?,?)",
                                 [(key, n, v, stamp) for n, v in clean.items()])
                self._log_undo(conn, "add", key, {})
                self._resolve(conn, [key])
                return key
        except sqlite3.Error:
            log.warning("Register: adding a company failed", exc_info=True)
            return None

    def hide(self, key: str) -> bool:
        """The user deletes a company that a source lists: it is hidden and stays hidden
        after every refresh (undo or `unhide` shows it again)."""
        key = company_cf(key) or key
        try:
            with self._write() as conn:
                row = conn.execute("SELECT name FROM company WHERE key = ?", (key,)).fetchone()
                if row is None or conn.execute("SELECT 1 FROM hidden WHERE key = ?", (key,)).fetchone():
                    return False
                conn.execute("INSERT INTO hidden (key, name, hidden_at) VALUES (?,?,?)", (key, row["name"], now_iso()))
                self._log_undo(conn, "hide", key, {})
            return True
        except sqlite3.Error:
            log.warning("Register: hide failed", exc_info=True)
            return False

    def unhide(self, key: str) -> bool:
        key = company_cf(key) or key
        try:
            with self._write() as conn:
                row = conn.execute("SELECT name, hidden_at FROM hidden WHERE key = ?", (key,)).fetchone()
                if row is None:
                    return False
                conn.execute("DELETE FROM hidden WHERE key = ?", (key,))
                self._log_undo(conn, "unhide", key, {"name": row["name"], "hidden_at": row["hidden_at"]})
            return True
        except sqlite3.Error:
            log.warning("Register: unhide failed", exc_info=True)
            return False

    def delete(self, key: str) -> bool:
        """Delete: a company the user added is removed (undoable); one a source lists is
        hidden (see `hide`)."""
        key = company_cf(key) or key
        try:
            with self._write() as conn:
                row = conn.execute("SELECT * FROM company WHERE key = ?", (key,)).fetchone()
                if row is None:
                    return False
                has_sources = conn.execute("SELECT 1 FROM field_value WHERE key = ? LIMIT 1", (key,)).fetchone()
                if row["user_added"] and not has_sources:
                    edits = [dict(r) for r in conn.execute("SELECT * FROM user_edit WHERE key = ?", (key,))]
                    conn.execute("DELETE FROM user_edit WHERE key = ?", (key,))
                    conn.execute("DELETE FROM company WHERE key = ?", (key,))
                    self._log_undo(conn, "remove", key, {"company": {"key": key, "cf": row["cf"],
                                                                     "first_seen": row["first_seen"]},
                                                         "edits": edits})
                    return True
        except sqlite3.Error:
            log.warning("Register: delete failed", exc_info=True)
            return False
        return self.hide(key)

    def can_undo(self) -> bool:
        with self._read() as conn:
            return conn.execute("SELECT 1 FROM undo_log LIMIT 1").fetchone() is not None

    def undo(self) -> str | None:
        """Revert the user's last action; returns its description, None when nothing to undo."""
        try:
            with self._write() as conn:
                row = conn.execute("SELECT * FROM undo_log ORDER BY id DESC LIMIT 1").fetchone()
                if row is None:
                    return None
                conn.execute("DELETE FROM undo_log WHERE id = ?", (row["id"],))
                key, action = row["key"], row["action"]
                payload = json.loads(row["payload"] or "{}")
                if action == "edit":
                    for name, before in (payload.get("before") or {}).items():
                        if before is None:
                            conn.execute("DELETE FROM user_edit WHERE key = ? AND field = ?", (key, name))
                        else:
                            conn.execute("INSERT OR REPLACE INTO user_edit (key, field, value, edited_at) "
                                         "VALUES (?,?,?,?)", (key, name, before[0], before[1]))
                    if payload.get("hidden"):
                        conn.execute("INSERT OR REPLACE INTO hidden (key, name, hidden_at) VALUES (?,?,?)",
                                     (key, payload["hidden"].get("name"), payload["hidden"].get("hidden_at")))
                    self._resolve(conn, [key])
                elif action == "add":
                    conn.execute("DELETE FROM user_edit WHERE key = ?", (key,))
                    conn.execute("DELETE FROM company WHERE key = ? AND user_added = 1", (key,))
                elif action == "hide":
                    conn.execute("DELETE FROM hidden WHERE key = ?", (key,))
                elif action == "unhide":
                    conn.execute("INSERT OR REPLACE INTO hidden (key, name, hidden_at) VALUES (?,?,?)",
                                 (key, payload.get("name"), payload.get("hidden_at")))
                elif action == "remove":
                    company = payload.get("company") or {}
                    conn.execute("INSERT OR IGNORE INTO company (key, cf, user_added, first_seen, last_seen) "
                                 "VALUES (?,?,1,?,?)", (key, company.get("cf"), company.get("first_seen"),
                                                        today_iso()))
                    conn.executemany("INSERT OR REPLACE INTO user_edit (key, field, value, edited_at) "
                                     "VALUES (?,?,?,?)", [(e["key"], e["field"], e["value"], e["edited_at"])
                                                          for e in payload.get("edits") or []])
                    self._resolve(conn, [key])
                return f"{action} {key}"
        except (sqlite3.Error, ValueError):
            log.warning("Register: undo failed", exc_info=True)
            return None

    def hidden_rows(self) -> list[dict]:
        with self._read() as conn:
            return [dict(r) for r in conn.execute("SELECT key, name, hidden_at FROM hidden ORDER BY hidden_at DESC")]

    # --- statistics and coverage

    def last_refresh(self) -> dict[str, dict]:
        """{source: {"finished", "rows_kept", "source_date", "regions"}} of the last good load."""
        result = {}
        with self._read() as conn:
            for r in conn.execute("SELECT * FROM import_log WHERE state = 'ok' ORDER BY id"):
                result[r["source"]] = {"finished": r["finished"], "rows_kept": r["rows_kept"],
                                       "source_date": r["source_date"], "regions": r["regions"],
                                       "rows_removed": r["rows_removed"]}
        return result

    def stats(self) -> dict:
        """Rows per source, per province, per ATECO division (exact codes and hints apart),
        totals and the last refresh of each source."""
        try:
            with self._read() as conn:
                visible = "NOT EXISTS (SELECT 1 FROM hidden h WHERE h.key = c.key)"
                total = conn.execute(f"SELECT COUNT(*) FROM company c WHERE {visible}").fetchone()[0]
                per_source = {r["source"]: r["n"] for r in conn.execute(
                    "SELECT source, COUNT(DISTINCT key) AS n FROM field_value GROUP BY source")}
                per_province = {(r["province"] or "?"): r["n"] for r in conn.execute(
                    f"SELECT province, COUNT(*) AS n FROM company c WHERE {visible} GROUP BY province ORDER BY n DESC")}
                presumed = {(r["province"] or "?"): r["n"] for r in conn.execute(
                    f"SELECT province, COUNT(*) AS n FROM company c WHERE {visible} AND province_src = 'vat_office' "
                    f"GROUP BY province")}
                exact, hints = Counter(), Counter()
                for r in conn.execute(f"SELECT ateco, nace_hints FROM company c WHERE {visible}"):
                    if r["ateco"]:
                        exact[division(r["ateco"])] += 1
                    for d in {division(h) for h in (r["nace_hints"] or "").split(",") if h}:
                        hints[d] += 1
                hidden = conn.execute("SELECT COUNT(*) FROM hidden").fetchone()[0]
                user_added = conn.execute("SELECT COUNT(*) FROM company WHERE user_added = 1").fetchone()[0]
                locked = conn.execute("SELECT COUNT(DISTINCT key) FROM user_edit").fetchone()[0]
                active = conn.execute(f"SELECT COUNT(*) FROM company c WHERE {visible} AND "
                                      f"COALESCE(status,'') != 'inactive'").fetchone()[0]
            return {"total": total, "active": active, "per_source": per_source, "per_province": per_province,
                    "presumed_province": presumed, "per_division_exact": dict(exact.most_common()),
                    "per_division_hint": dict(hints.most_common()), "hidden": hidden, "user_added": user_added,
                    "with_user_edits": locked, "last_refresh": self.last_refresh()}
        except sqlite3.Error:
            log.warning("Register: stats failed", exc_info=True)
            return {"total": 0, "per_source": {}, "per_province": {}, "last_refresh": {}}

    def coverage_note(self, province: str, ateco_division: str, municipality: str | None = None) -> dict:
        """How much of the real number of companies the free registers cover, stated
        honestly: official counts when the study has them, else "unknown". Always names
        the paid Registro Imprese list as the complete alternative."""
        code = str(ateco_division or "").strip()
        count = self.count(province=province or None, municipality=municipality or None,
                           ateco_prefixes=(code,) if code else (), only_active=True)
        official = None
        if municipality:
            official = OFFICIAL_COUNTS.get(("municipality", fold(municipality), code))
        if official is None and province:
            official = OFFICIAL_COUNTS.get(("province", province.strip().upper(), code))
        if official:
            low, high, basis = official
            pct_low = round(100 * count / high) if high else 0
            pct_high = round(100 * count / low) if low else 0
            text = texts.text("registers.coverage.known", count=format_int(count), low=format_int(low),
                              high=format_int(high), basis=basis,
                              pct_low=min(pct_low, 100), pct_high=min(pct_high, 100))
            return {"kind": "known", "count": count, "official_low": low, "official_high": high, "basis": basis,
                    "text": text}
        return {"kind": "unknown", "count": count, "official_low": None, "official_high": None, "basis": "",
                "text": texts.text("registers.coverage.unknown", count=format_int(count))}

    def size_bytes(self) -> int:
        if self._shared:
            return 0
        total = 0
        for suffix in ("", "-wal", "-shm"):
            path = Path(self.path + suffix)
            if path.exists():
                total += path.stat().st_size
        return total

    # --- export / import

    EXPORT_COLUMNS = ("key", "cf") + FIELDS + ("province_src", "province_guess", "sources", "locked", "user_added",
                                               "hidden", "hiring_dates")

    def export_csv(self, path: Path | str) -> int:
        """The whole register (hidden rows included, flagged) as a UTF-8 CSV with `;`."""
        rows = self.find(only_active=False, include_hidden=True, include_presumed_province=True)
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(self.EXPORT_COLUMNS)
            for record in rows:
                data = record.to_dict()
                data["province_guess"] = ",".join(record.province_guess)
                data["sources"] = ",".join(record.sources)
                data["locked"] = ",".join(record.locked)
                data["user_added"] = "1" if record.user_added else ""
                data["hidden"] = "1" if record.hidden else ""
                data["hiring_dates"] = ",".join(record.hiring_dates)
                writer.writerow(["" if data.get(c) is None else data.get(c) for c in self.EXPORT_COLUMNS])
        return len(rows)

    def import_csv(self, path: Path | str) -> ImportCsvReport:
        """Read a file written by `export_csv` (or edited in a spreadsheet): locked fields
        come back as the user's edits, user-added companies are recreated, hidden ones
        hidden again; other values fill fields no source gives (source "csv_import")."""
        report = ImportCsvReport()
        try:
            with open(path, encoding="utf-8-sig", newline="") as handle:
                sample = handle.read(4096)
                handle.seek(0)
                delimiter = ";" if sample.count(";") >= sample.count(",") else ","
                rows = list(csv.DictReader(handle, delimiter=delimiter))
        except (OSError, csv.Error, UnicodeDecodeError) as error:
            report.errors.append(f"{type(error).__name__}: {error}")
            return report
        today, stamp = today_iso(), now_iso()
        try:
            with self._write() as conn:
                touched = []
                for data in rows:
                    report.rows += 1
                    cf = company_cf(data.get("cf", ""))
                    key = (data.get("key") or "").strip() or cf
                    if key and not key.startswith("U-"):
                        key = company_cf(key) or key
                    if not key or not (data.get("name") or "").strip():
                        report.skipped += 1
                        continue
                    locked = [f for f in (data.get("locked") or "").split(",") if f in EDITABLE]
                    user_added = (data.get("user_added") or "").strip() in ("1", "true", "yes")
                    exists = conn.execute("SELECT 1 FROM company WHERE key = ?", (key,)).fetchone()
                    if not exists:
                        conn.execute("INSERT INTO company (key, cf, user_added, first_seen, last_seen) "
                                     "VALUES (?,?,?,?,?)", (key, cf, 1 if user_added else 0, today, today))
                        report.added += 1
                    else:
                        report.updated += 1
                    if user_added:
                        locked = [f for f in EDITABLE if (data.get(f) or "").strip()]
                    for name in locked:
                        conn.execute("INSERT OR REPLACE INTO user_edit (key, field, value, edited_at) VALUES (?,?,?,?)",
                                     (key, name, (data.get(name) or "").strip(), stamp))
                        report.locked_fields += 1
                    for name in FIELDS:
                        value = (data.get(name) or "").strip()
                        if value and name not in locked:
                            conn.execute("INSERT OR REPLACE INTO field_value (key, field, source, value, source_date) "
                                         "VALUES (?,?,?,?,?)", (key, name, CSV_IMPORT, value, today))
                    if (data.get("hidden") or "").strip() in ("1", "true", "yes"):
                        conn.execute("INSERT OR IGNORE INTO hidden (key, name, hidden_at) VALUES (?,?,?)",
                                     (key, data.get("name"), stamp))
                        report.hidden += 1
                    touched.append(key)
                self._resolve(conn, touched)
        except sqlite3.Error as error:
            log.warning("Register: CSV import failed", exc_info=True)
            report.errors.append(f"{type(error).__name__}: {error}")
        return report

    # --- whole sources and values kept from elsewhere

    def remove_source(self, source: str) -> int:
        """Drop every value one source gave (e.g. an imported list the user deletes): a
        company no other source lists disappears, unless the user added or edited it.
        Returns the number of companies the source listed (0 on an error)."""
        try:
            with self._write() as conn:
                keys = [r["key"] for r in conn.execute("SELECT DISTINCT key FROM field_value WHERE source = ?",
                                                       (source,))]
                conn.execute("DELETE FROM field_value WHERE source = ?", (source,))
                conn.execute("DELETE FROM signal WHERE source = ?", (source,))
                self._resolve(conn, keys)
                self._drop_orphans(conn, keys)
            return len(keys)
        except (sqlite3.Error, CancelledError):
            log.warning("Register: removing %s failed", source, exc_info=True)
            return 0

    def set_user_values(self, key: str, fields: dict[str, object], *, user_added: bool = False,
                        cf: str = "", edited_at: str = "") -> bool:
        """The user's values of a company, LOCKED like `edit` but without an undo step
        (values the user already entered elsewhere, e.g. hand corrections of a list of an
        older version). The company is created when missing (`user_added` marks one the
        user added by hand). Empty values are locked empty fields."""
        changes = {k: ("" if v is None else str(v).strip()) for k, v in (fields or {}).items() if k in EDITABLE}
        if not key or (not changes and not user_added):
            return False
        try:
            with self._write() as conn:
                if conn.execute("SELECT 1 FROM company WHERE key = ?", (key,)).fetchone() is None:
                    today = today_iso()
                    conn.execute("INSERT INTO company (key, cf, user_added, first_seen, last_seen) VALUES (?,?,?,?,?)",
                                 (key, company_cf(cf), 1 if user_added else 0, today, today))
                stamp = edited_at or now_iso()
                conn.executemany("INSERT OR REPLACE INTO user_edit (key, field, value, edited_at) VALUES (?,?,?,?)",
                                 [(key, n, v, stamp) for n, v in changes.items()])
                self._resolve(conn, [key])
            return True
        except sqlite3.Error:
            log.warning("Register: storing values of %s failed", key, exc_info=True)
            return False

    def keys_of_source(self, source: str) -> set[str]:
        """Keys of the companies one source lists."""
        with self._read() as conn:
            return self._keys_of_source(conn, source, None)


_UPDATE_COLUMNS = tuple(dict.fromkeys(
    [c for c in FIELDS] + ["name_norm", "name_key", "province_src", "province_guess", "municipality_norm",
                           "sector_codes", "hiring_count", "last_hiring_date", "provenance", "sources", "locked",
                           "updated"]))


__all__ = ["ImportCsvReport", "LoadReport", "OFFICIAL_COUNTS", "RegisterDB", "SCHEMA_VERSION"]
