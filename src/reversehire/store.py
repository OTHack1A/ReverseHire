"""Local SQLite store: cache, search history and results.

Threads (the GUI must never wait behind a worker):

- every thread gets its OWN sqlite3 connection (created on first use, closed when
  the thread is gone or the store is closed). There is no process-wide lock around
  reads, so a GUI-thread read never queues behind a worker's statement or
  transaction; SQLite's own file locking arbitrates;
- WAL journal where the folder allows it (local, not cloud-synced, not inside a
  packaged app): readers and the writer never block each other. Elsewhere the
  classic DELETE journal stays (see below), where a reader waits at most for one
  short commit;
- writes run in `BEGIN IMMEDIATE` transactions (no lock-upgrade deadlock between
  two connections);
- the GUI thread (declared with `mark_gui_thread()` by the window) uses a short busy
  timeout (GUI_BUSY_TIMEOUT_MS) and acquires the read-modify-write lock (`locked()`)
  with GUI_LOCK_TIMEOUT_S: in the worst case the window waits ~2 s and gets
  StoreBusyError (logged, shown as a notice), never hangs;
- ":memory:" stores (tests) keep one shared connection behind a lock: separate
  connections would each see an empty database.

Robustness of the database file (it may live in the program folder, see
config.data_folder):

- journal mode DELETE where WAL is not possible: WAL needs a shared-memory "-shm"
  file and keeps committed data in a separate "-wal" file: it does not work on
  network folders, cloud-sync clients upload the three files independently (a copy
  elsewhere can miss the last commits), and a Windows process started from a
  packaged app may see the new side files in its redirected view only. With DELETE
  the database is one self-contained file between transactions;
- busy timeout (BUSY_TIMEOUT_MS): a writer waits for the other process's lock
  instead of failing at once with "database is locked";
- a folder that cannot be written (read-only media, permissions, full disk) raises
  StoreUnavailableError with the path and what to do, instead of a bare
  sqlite3.OperationalError.
"""

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

from .models import Company, CVProfile, Assessment

log = logging.getLogger(__name__)

BUSY_TIMEOUT_MS = 10_000
GUI_BUSY_TIMEOUT_MS = 2_000   # the GUI thread never waits longer for the file lock
GUI_LOCK_TIMEOUT_S = 2.0      # ... nor for the read-modify-write lock
JOURNAL_MODE = "DELETE"       # fallback where WAL is not possible
WAL_MODE = "WAL"


class StoreUnavailableError(sqlite3.OperationalError):
    """The database file cannot be opened or written; the message says why and what to do.
    Subclass of sqlite3.OperationalError, so existing handlers keep working."""

    def __init__(self, message: str, path: str, cause: str):
        super().__init__(message)
        self.path = path
        self.cause = cause  # "read_only", "cannot_open", "locked", "disk_full"


def explain_error(error: BaseException, path: str) -> StoreUnavailableError | None:
    """A StoreUnavailableError for the SQLite/OS errors that mean "the folder or file
    cannot be used", or None for other errors (bugs, constraint violations...)."""
    text = str(error).lower()
    if isinstance(error, PermissionError) or "readonly" in text or "read-only" in text:
        cause, advice = "read_only", ("the file or its folder is read-only. Check the folder "
                                      "permissions, or move the data folder (REVERSEHIRE_HOME)")
    elif "unable to open database" in text or "cannot open" in text or isinstance(error, OSError):
        cause, advice = "cannot_open", ("the folder is missing, not writable or on a disconnected "
                                        "drive. Check it, or move the data folder (REVERSEHIRE_HOME)")
    elif "database is locked" in text or "database table is locked" in text:
        cause, advice = "locked", ("another program keeps the database busy (a second copy of "
                                   "ReverseHire, the MCP server or a cloud-sync client). Close it "
                                   "and try again")
    elif "database or disk is full" in text or "disk i/o error" in text:
        cause, advice = "disk_full", "the disk is full or failing. Free some space and try again"
    else:
        return None
    return StoreUnavailableError(f"The ReverseHire database {path} cannot be used: {advice}. "
                                 f"({type(error).__name__}: {error})", path, cause)


class StoreBusyError(StoreUnavailableError):
    """The GUI thread gave up waiting for the database (another thread or process keeps
    it busy): the operation is skipped, the window stays responsive."""

    def __init__(self, path: str):
        super().__init__(f"The ReverseHire database {path} is busy: try again in a moment.", path, "locked")


_gui_thread: int | None = None  # set by the window: other programs (MCP server, tools) wait normally


def mark_gui_thread(ident: int | None = -1) -> None:
    """Declares the calling thread (or `ident`; None = none) as the GUI thread, whose
    waits for the database are kept short (see the module docstring)."""
    global _gui_thread
    _gui_thread = threading.get_ident() if ident == -1 else ident


def _on_gui_thread() -> bool:
    return _gui_thread is not None and threading.get_ident() == _gui_thread


def wal_allowed(path: str) -> bool:
    """WAL only on a local folder that is neither cloud-synced nor seen through a
    packaged app's redirection (see the module docstring)."""
    if path == ":memory:":
        return False
    text = str(path)
    if text.startswith(("\\\\", "//")):  # UNC network path
        return False
    try:
        from . import config

        if config.cloud_sync_provider(Path(text).parent) or config.packaged_app_name():
            return False
        if os.name == "nt":
            import ctypes

            drive = os.path.splitdrive(os.path.abspath(text))[0]
            if drive and ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4:  # DRIVE_REMOTE
                return False
    except Exception:  # noqa: BLE001 - in doubt, keep the safe journal
        return False
    return True

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY (namespace, key)
);
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL,
    cv_path TEXT,
    place TEXT,
    lat REAL,
    lon REAL,
    radius_km REAL,
    profile TEXT,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    company TEXT NOT NULL,
    assessment TEXT
);
CREATE INDEX IF NOT EXISTS idx_results_search ON results(search_id);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    site TEXT,
    address TEXT NOT NULL DEFAULT '',
    lat REAL,
    lon REAL,
    distance_km REAL,
    osm_tags TEXT NOT NULL DEFAULT '{}',
    sectors TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    assessment TEXT,
    status TEXT NOT NULL DEFAULT 'interesting',
    favourite INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    contacts TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    source_search_id INTEGER,
    origin TEXT NOT NULL DEFAULT 'osm',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_companies_favourite ON companies(favourite);
-- The user's own title and note of a previous search ("Rename / note"): kind "companies" for
-- the company searches (searches.id), "jobs" for the job searches (job_searches.id). Created
-- here (not in a migration) so it reaches every existing database on open.
CREATE TABLE IF NOT EXISTS search_notes (
    kind TEXT NOT NULL,
    search_id INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    updated REAL NOT NULL,
    PRIMARY KEY (kind, search_id)
);
"""

# Schema version of the tables above. Every table is created with
# CREATE TABLE IF NOT EXISTS, so new tables reach existing databases on open.
# To add columns later: bump SCHEMA_VERSION and add the statements under the
# new number in _MIGRATIONS (applied once, in order, inside a transaction).
SCHEMA_VERSION = 5

# Version 2: the company card (see companies.py). Header identifiers
# as plain columns (searchable), the other sections as JSON, one "*_checked" time per
# section (epoch seconds, NULL = never checked). The user's fields are untouched.
_CARD_V2_COLUMNS = (
    ("legal_name", "TEXT NOT NULL DEFAULT ''"),
    ("vat", "TEXT NOT NULL DEFAULT ''"),        # partita IVA (11 digits for Italy)
    ("tax_code", "TEXT NOT NULL DEFAULT ''"),   # codice fiscale (= Registro Imprese number)
    ("rea", "TEXT NOT NULL DEFAULT ''"),        # "BO-472965"
    ("ateco", "TEXT NOT NULL DEFAULT ''"),      # only from a register (import / visura)
    ("website", "TEXT NOT NULL DEFAULT ''"),    # the company's own website, as identified
    ("registry", "TEXT NOT NULL DEFAULT '{}'"),  # anagrafica: legal form, offices, PEC...
    ("products", "TEXT NOT NULL DEFAULT '{}'"),  # products and services from the site
    ("hiring", "TEXT NOT NULL DEFAULT '{}'"),    # profiles sought (careers snapshot)
    ("ats", "TEXT NOT NULL DEFAULT '{}'"),       # selection system and platform offer
    ("screening", "TEXT NOT NULL DEFAULT '{}'"),  # CV screening indicators and statements
    ("identity_status", "TEXT NOT NULL DEFAULT 'unverified'"),
    ("identity", "TEXT NOT NULL DEFAULT '{}'"),  # agreement table, conflicts, missing fields
    ("provenance", "TEXT NOT NULL DEFAULT '[]'"),  # [{"field","value","source","url","date"}]
    ("identity_checked", "REAL"),
    ("registry_checked", "REAL"),
    ("hiring_checked", "REAL"),
    ("products_checked", "REAL"),
    ("ats_checked", "REAL"),
    ("screening_checked", "REAL"),
)
# Version 3: presentation and history from the website, the people/contacts list (local
# only, deletable) and the financial series (financials/model.py JSON) of the card.
_CARD_V3_COLUMNS = (
    ("profile", "TEXT NOT NULL DEFAULT '{}'"),     # {"presentation": {...}, "history": {...}}
    ("people", "TEXT NOT NULL DEFAULT '{}'"),      # {"items": [...], "deleted": [ids]}
    ("financials", "TEXT NOT NULL DEFAULT '{}'"),  # {"series": {...}, "reports": [...]}
    ("profile_checked", "REAL"),
    ("people_checked", "REAL"),
    ("financials_checked", "REAL"),
)
# Version 4: the job search (jobsearch package). A posting is stored once (`key`: its URL,
# else its de-duplication key) as the jobsearch Posting JSON; a search keeps the typed query,
# how it was understood and the per-source reports, and links its postings in rank order.
# "origin" of a posting: "search" (found by a job search) or "pasted" (pasted and reviewed
# by the user). Names of people are never stored (the paste parser drops them).
_JOB_TABLES = [
    """CREATE TABLE IF NOT EXISTS job_searches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created REAL NOT NULL,
        query TEXT NOT NULL DEFAULT '',
        understood TEXT NOT NULL DEFAULT '{}',
        place TEXT NOT NULL DEFAULT '',
        lat REAL,
        lon REAL,
        radius_km REAL,
        status TEXT NOT NULL DEFAULT 'running',
        found INTEGER NOT NULL DEFAULT 0,
        reports TEXT NOT NULL DEFAULT '[]',
        stats TEXT NOT NULL DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS job_postings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT NOT NULL UNIQUE,
        source TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        company TEXT NOT NULL DEFAULT '',
        origin TEXT NOT NULL DEFAULT 'search',
        favourite INTEGER NOT NULL DEFAULT 0,
        data TEXT NOT NULL DEFAULT '{}',
        created REAL NOT NULL,
        updated REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS job_search_postings (
        search_id INTEGER NOT NULL REFERENCES job_searches(id) ON DELETE CASCADE,
        posting_id INTEGER NOT NULL REFERENCES job_postings(id) ON DELETE CASCADE,
        rank INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (search_id, posting_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_job_search_postings ON job_search_postings(search_id, rank)",
    "CREATE INDEX IF NOT EXISTS idx_job_postings_origin ON job_postings(origin)",
]
# Version 5: the affinity between the CV and each company (affinity package). One result per
# (company key, CV fingerprint, settings fingerprint = weights version + preferences
# fingerprint); `data_fp` is the fingerprint of the company data it was computed from (a
# result whose data changed is recomputed). `company_key` is "card:<card key>" for a saved
# card, "result:<card key>" for a search result row and "company:<card key>" for a map /
# register company that is not a card (gui/affinity_service.key_of). The feedback keeps the judged result (JSON) so that calibration can re-judge it.
_AFFINITY_TABLES = [
    """CREATE TABLE IF NOT EXISTS affinity_results (
        company_key TEXT NOT NULL,
        cv_fp TEXT NOT NULL,
        settings_fp TEXT NOT NULL,
        data_fp TEXT NOT NULL DEFAULT '',
        verdict TEXT NOT NULL DEFAULT '',
        affinity REAL,
        result TEXT NOT NULL,
        created REAL NOT NULL,
        PRIMARY KEY (company_key, cv_fp, settings_fp)
    )""",
    """CREATE TABLE IF NOT EXISTS affinity_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_key TEXT NOT NULL,
        agrees INTEGER NOT NULL,
        user_verdict TEXT,
        verdict TEXT NOT NULL DEFAULT '',
        cv_fp TEXT NOT NULL DEFAULT '',
        result TEXT NOT NULL DEFAULT '{}',
        created REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_affinity_feedback_key ON affinity_feedback(company_key)",
]
_MIGRATIONS: dict[int, list[str]] = {
    2: [f"ALTER TABLE companies ADD COLUMN {name} {kind}" for name, kind in _CARD_V2_COLUMNS]
    + ["CREATE INDEX IF NOT EXISTS idx_companies_vat ON companies(vat)",
       "CREATE INDEX IF NOT EXISTS idx_companies_tax_code ON companies(tax_code)"],
    3: [f"ALTER TABLE companies ADD COLUMN {name} {kind}" for name, kind in _CARD_V3_COLUMNS],
    4: list(_JOB_TABLES),
    5: list(_AFFINITY_TABLES),
}
JOB_SEARCH_STATUSES = ("running", "completed", "cancelled", "failed")
POSTING_ORIGINS = ("search", "pasted")

SEARCH_STATUSES = ("running", "completed", "cancelled")

# Columns of the "companies" table that hold JSON values.
_CARD_JSON_COLUMNS = {"osm_tags": {}, "sectors": [], "tags": [], "assessment": None,
                      "registry": {}, "products": {}, "hiring": {}, "ats": {}, "screening": {},
                      "identity": {}, "provenance": [], "profile": {}, "people": {}, "financials": {}}
CARD_COLUMNS = (
    "key", "name", "site", "address", "lat", "lon", "distance_km", "osm_tags", "sectors",
    "description", "assessment", "status", "favourite", "notes", "contacts", "tags",
    "source_search_id", "origin", "created", "updated",
) + tuple(name for name, _kind in _CARD_V2_COLUMNS) + tuple(name for name, _kind in _CARD_V3_COLUMNS)


def _now() -> float:
    return time.time()


class _GuardedLock:
    """The store's read-modify-write lock: re-entrant; on the GUI thread acquired with a
    timeout (StoreBusyError instead of a frozen window)."""

    def __init__(self, path: str):
        self._lock = threading.RLock()
        self._path = path

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        if _on_gui_thread():
            if not self._lock.acquire(timeout=GUI_LOCK_TIMEOUT_S):
                log.warning("GUI thread: database lock not free after %.1f s", GUI_LOCK_TIMEOUT_S)
                raise StoreBusyError(self._path)
        else:
            self._lock.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self._lock.release()


class Store:
    """SQLite store; accepts a path or ":memory:". Thread-safe (see the module docstring)."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._shared = self.path == ":memory:"
        # Read-modify-write sequences of the same process (companies.py) and, for the
        # shared ":memory:" connection, every statement.
        self._lock = _GuardedLock(self.path)
        self._local = threading.local()
        self._connections: list[tuple[threading.Thread, sqlite3.Connection]] = []
        self._connections_lock = threading.Lock()
        self._closed = False
        self._mode = JOURNAL_MODE
        try:
            if not self._shared:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            conn = self._open_connection()
            if not self._shared:
                self._mode = self._choose_journal(conn)
        except (sqlite3.Error, OSError) as error:
            raise (explain_error(error, self.path) or error) from error
        self._create_schema()
        self._migrate()

    # --- connections ----------------------------------------------------------

    def _choose_journal(self, conn: sqlite3.Connection) -> str:
        """WAL where the folder allows it, else DELETE (also when WAL is refused)."""
        if wal_allowed(self.path):
            try:
                mode = str(conn.execute(f"PRAGMA journal_mode = {WAL_MODE}").fetchone()[0]).upper()
                if mode == WAL_MODE:
                    conn.execute("PRAGMA synchronous = NORMAL")
                    return WAL_MODE
            except sqlite3.Error:
                log.info("WAL journal not possible for %s: classic journal used", self.path)
        conn.execute(f"PRAGMA journal_mode = {JOURNAL_MODE}")
        return JOURNAL_MODE

    def _open_connection(self) -> sqlite3.Connection:
        timeout_ms = GUI_BUSY_TIMEOUT_MS if _on_gui_thread() and not self._shared else BUSY_TIMEOUT_MS
        # autocommit (isolation_level None): transactions are opened by _writing()
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=timeout_ms / 1000,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {timeout_ms}")
        if not self._shared and getattr(self, "_mode", JOURNAL_MODE) == WAL_MODE:
            conn.execute("PRAGMA synchronous = NORMAL")
        current = threading.current_thread()
        with self._connections_lock:
            # connections of threads that ended (short-lived workers) are closed here
            alive = []
            for thread, other in self._connections:
                if thread.is_alive():
                    alive.append((thread, other))
                else:
                    with contextlib.suppress(sqlite3.Error):
                        other.close()
            alive.append((current, conn))
            self._connections = alive
        if self._shared:
            self._shared_conn = conn
        else:
            self._local.conn = conn
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        """The connection of the calling thread (one shared connection for ":memory:")."""
        if self._shared:
            conn = getattr(self, "_shared_conn", None)
            if conn is None:
                raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
            return conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if self._closed:
                raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
            try:
                conn = self._open_connection()
            except (sqlite3.Error, OSError) as error:
                raise (explain_error(error, self.path) or error) from error
        return conn

    @contextlib.contextmanager
    def _reading(self):
        """The calling thread's connection; only the shared ":memory:" one needs the lock."""
        if self._shared:
            with self._lock:
                yield self._conn
        else:
            yield self._conn

    @contextlib.contextmanager
    def _writing(self):
        """A `BEGIN IMMEDIATE` transaction on the calling thread's connection (nested
        calls join the outer one); unusable-folder errors become StoreUnavailableError."""
        guard = self._lock if self._shared else contextlib.nullcontext()
        try:
            with guard:
                conn = self._conn
                if conn.in_transaction:
                    yield
                    return
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield
                except BaseException:
                    with contextlib.suppress(sqlite3.Error):
                        conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
        except StoreUnavailableError:
            raise
        except (sqlite3.OperationalError, OSError) as error:
            explained = explain_error(error, self.path)
            if explained is None:
                raise
            if _on_gui_thread() and explained.cause == "locked":
                raise StoreBusyError(self.path) from error
            raise explained from error

    def _create_schema(self) -> None:
        # executescript commits on its own: not inside _writing()
        guard = self._lock if self._shared else contextlib.nullcontext()
        try:
            with guard:
                self._conn.executescript(_SCHEMA)
        except (sqlite3.OperationalError, OSError) as error:
            raise (explain_error(error, self.path) or error) from error

    @property
    def journal_mode(self) -> str:
        with self._reading() as conn:
            return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).upper()

    def _migrate(self) -> None:
        """Bring an existing database up to SCHEMA_VERSION (idempotent)."""
        with self._writing():
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            current = int(row["value"]) if row else 0
            for version in range(current + 1, SCHEMA_VERSION + 1):
                for statement in _MIGRATIONS.get(version, []):
                    self._conn.execute(statement)
            if current != SCHEMA_VERSION:
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )

    @property
    def schema_version(self) -> int:
        with self._reading():
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        return int(row["value"]) if row else 0

    def locked(self) -> _GuardedLock:
        """The store's re-entrant lock, for read-modify-write sequences (time-limited on
        the GUI thread: StoreBusyError)."""
        return self._lock

    def close(self) -> None:
        """Closes every thread's connection; later use raises sqlite3.ProgrammingError."""
        self._closed = True
        with self._connections_lock:
            connections, self._connections = self._connections, []
        for _thread, conn in connections:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        self._shared_conn = None
        self._local = threading.local()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # --- cache ---------------------------------------------------------

    def cache_read(self, namespace: str, key: str, max_age_s: float | None = None):
        """Cached JSON value, or None if missing or expired."""
        # Each thread reads on its own connection: parallel site analyses, scoring and
        # the window never share one (a shared connection gave "InterfaceError: bad
        # parameter or other API misuse" and made every reader queue behind the others).
        with self._reading():
            row = self._conn.execute(
                "SELECT value, created FROM cache WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()
        if row is None:
            return None
        if max_age_s is not None and _now() - row["created"] > max_age_s:
            return None
        return json.loads(row["value"])

    def cache_write(self, namespace: str, key: str, value) -> None:
        with self._writing():
            self._conn.execute(
                "INSERT OR REPLACE INTO cache (namespace, key, value, created) "
                "VALUES (?, ?, ?, ?)",
                (namespace, key, json.dumps(value, ensure_ascii=False), _now()),
            )

    def cache_clear(self, namespace: str | None = None) -> None:
        with self._writing():
            if namespace is None:
                self._conn.execute("DELETE FROM cache")
            else:
                self._conn.execute("DELETE FROM cache WHERE namespace = ?", (namespace,))

    # --- searches ------------------------------------------------------

    def create_search(
        self,
        cv_path: str,
        place: str,
        lat: float | None,
        lon: float | None,
        radius_km: float,
        profile: CVProfile | None,
    ) -> int:
        profile_text = (
            json.dumps(profile.to_dict(), ensure_ascii=False) if profile else None
        )
        with self._writing():
            cur = self._conn.execute(
                "INSERT INTO searches "
                "(created, cv_path, place, lat, lon, radius_km, profile, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'running')",
                (_now(), cv_path, place, lat, lon, radius_km, profile_text),
            )
        return cur.lastrowid

    def update_search_status(self, id: int, status: str) -> None:
        if status not in SEARCH_STATUSES:
            raise ValueError(f"Invalid search status: {status!r}")
        with self._writing():
            self._conn.execute("UPDATE searches SET status = ? WHERE id = ?", (status, id))

    @staticmethod
    def _search_row(row: sqlite3.Row) -> dict:
        search = dict(row)
        search["profile"] = (
            CVProfile.from_dict(json.loads(row["profile"])) if row["profile"] else None
        )
        return search

    def list_searches(self) -> list[dict]:
        """Searches, most recent first; 'profile' is a CVProfile or None."""
        with self._reading():
            rows = self._conn.execute(
                "SELECT * FROM searches ORDER BY created DESC, id DESC"
            ).fetchall()
        return [self._search_row(r) for r in rows]

    def read_search(self, id: int) -> dict | None:
        with self._reading():
            row = self._conn.execute("SELECT * FROM searches WHERE id = ?", (id,)).fetchone()
        return None if row is None else self._search_row(row)

    def delete_search(self, id: int) -> None:
        """Deletes the search and, by cascade, its results (and its title/note)."""
        with self._writing():
            self._conn.execute("DELETE FROM searches WHERE id = ?", (id,))
            self._conn.execute("DELETE FROM search_notes WHERE kind = 'companies' AND search_id = ?", (id,))

    # --- titles and notes of the previous searches (schema 5) --------------

    SEARCH_NOTE_KINDS = ("companies", "jobs")

    def search_note_set(self, kind: str, search_id: int, title: str = "", note: str = "") -> None:
        """Saves the user's title and note of a search ("" and "" removes them)."""
        if kind not in self.SEARCH_NOTE_KINDS:
            raise ValueError(f"unknown search kind {kind!r}")
        title, note = (title or "").strip()[:200], (note or "").strip()[:4000]
        with self._writing():
            if not title and not note:
                self._conn.execute("DELETE FROM search_notes WHERE kind = ? AND search_id = ?", (kind, search_id))
                return
            self._conn.execute(
                "INSERT OR REPLACE INTO search_notes (kind, search_id, title, note, updated) VALUES (?, ?, ?, ?, ?)",
                (kind, int(search_id), title, note, _now()))

    def search_note_get(self, kind: str, search_id: int) -> dict:
        """{"title", "note"} of a search ("" when none)."""
        with self._reading():
            row = self._conn.execute("SELECT title, note FROM search_notes WHERE kind = ? AND search_id = ?",
                                     (kind, int(search_id))).fetchone()
        return {"title": row["title"], "note": row["note"]} if row is not None else {"title": "", "note": ""}

    def search_notes(self, kind: str) -> dict[int, dict]:
        """Every title/note of one kind: {search_id: {"title", "note"}}."""
        with self._reading():
            rows = self._conn.execute("SELECT search_id, title, note FROM search_notes WHERE kind = ?",
                                      (kind,)).fetchall()
        return {int(r["search_id"]): {"title": r["title"], "note": r["note"]} for r in rows}

    # --- results -----------------------------------------------------

    def save_result(
        self, search_id: int, company: Company, assessment: Assessment | None
    ) -> int:
        with self._writing():
            cur = self._conn.execute(
                "INSERT INTO results (search_id, company, assessment) VALUES (?, ?, ?)",
                (
                    search_id,
                    json.dumps(company.to_dict(), ensure_ascii=False),
                    json.dumps(assessment.to_dict(), ensure_ascii=False)
                    if assessment
                    else None,
                ),
            )
        return cur.lastrowid

    def results_of(self, search_id: int) -> list[tuple[Company, Assessment | None]]:
        with self._reading():
            rows = self._conn.execute(
                "SELECT company, assessment FROM results WHERE search_id = ? ORDER BY id",
                (search_id,),
            ).fetchall()
        return [
            (
                Company.from_dict(json.loads(r["company"])),
                Assessment.from_dict(json.loads(r["assessment"]))
                if r["assessment"]
                else None,
            )
            for r in rows
        ]

    # --- company cards ("My companies") --------------------------------
    # Plain dict rows; the CompanyCard model and the rules live in companies.py.

    @staticmethod
    def _card_row(row: sqlite3.Row) -> dict:
        card = dict(row)
        for column, empty in _CARD_JSON_COLUMNS.items():
            text = card.get(column)
            card[column] = json.loads(text) if text else (empty.copy() if empty is not None else None)
        card["favourite"] = bool(card["favourite"])
        return card

    @staticmethod
    def _card_values(fields: dict) -> dict:
        unknown = set(fields) - set(CARD_COLUMNS)
        if unknown:
            raise ValueError(f"Unknown card columns: {sorted(unknown)}")
        values = {}
        for column, value in fields.items():
            if column in _CARD_JSON_COLUMNS:
                value = None if value is None else json.dumps(value, ensure_ascii=False)
            elif column == "favourite":
                value = 1 if value else 0
            values[column] = value
        return values

    def card_insert(self, fields: dict) -> int:
        """Insert a card row; 'created'/'updated' default to now. Returns the id."""
        now = _now()
        values = self._card_values({"created": now, "updated": now, **fields})
        columns = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        with self._writing():
            cur = self._conn.execute(
                f"INSERT INTO companies ({columns}) VALUES ({marks})", tuple(values.values())
            )
        return cur.lastrowid

    def card_update(self, id: int, fields: dict) -> None:
        """Update the given columns of a card; 'updated' is set to now."""
        values = self._card_values({**fields, "updated": _now()})
        assignments = ", ".join(f"{c} = ?" for c in values)
        with self._writing():
            self._conn.execute(
                f"UPDATE companies SET {assignments} WHERE id = ?", (*values.values(), id)
            )

    def card_read(self, id: int) -> dict | None:
        with self._reading():
            row = self._conn.execute("SELECT * FROM companies WHERE id = ?", (id,)).fetchone()
        return None if row is None else self._card_row(row)

    def card_by_key(self, key: str) -> dict | None:
        with self._reading():
            row = self._conn.execute("SELECT * FROM companies WHERE key = ?", (key,)).fetchone()
        return None if row is None else self._card_row(row)

    def card_rows(self) -> list[dict]:
        """All cards, most recently updated first."""
        with self._reading():
            rows = self._conn.execute(
                "SELECT * FROM companies ORDER BY updated DESC, id DESC"
            ).fetchall()
        return [self._card_row(r) for r in rows]

    def card_delete(self, id: int) -> None:
        with self._writing():
            self._conn.execute("DELETE FROM companies WHERE id = ?", (id,))

    def card_by_identifier(self, column: str, value: str) -> dict | None:
        """First card whose "vat" or "tax_code" column equals `value` (schema 2)."""
        if column not in ("vat", "tax_code") or not value:
            return None
        with self._reading():
            row = self._conn.execute(
                f"SELECT * FROM companies WHERE {column} = ? ORDER BY id LIMIT 1", (value,)
            ).fetchone()
        return None if row is None else self._card_row(row)

    def favourite_card_keys(self) -> set[str]:
        with self._reading():
            rows = self._conn.execute(
                "SELECT key FROM companies WHERE favourite = 1"
            ).fetchall()
        return {r["key"] for r in rows}

    # --- job search (schema 4) -----------------------------------------------
    # Postings are plain dicts (jobsearch.models.Posting.to_dict()); the model lives in
    # the jobsearch package, which this module does not import.

    @staticmethod
    def posting_key(posting: dict) -> str:
        """The identity of a stored posting: its URL, else its de-duplication key."""
        url = str(posting.get("url") or "").strip()
        if url:
            return "url:" + url
        return "dedup:" + str(posting.get("dedup_key") or posting.get("title") or "").strip()

    def create_job_search(self, query: str, understood: dict | None = None, *, place: str = "",
                          lat: float | None = None, lon: float | None = None,
                          radius_km: float | None = None) -> int:
        with self._writing():
            cur = self._conn.execute(
                "INSERT INTO job_searches (created, query, understood, place, lat, lon, radius_km, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'running')",
                (_now(), query or "", json.dumps(understood or {}, ensure_ascii=False), place or "", lat, lon,
                 radius_km))
        return cur.lastrowid

    def finish_job_search(self, search_id: int, status: str, postings: list[dict], *,
                          reports: list[dict] | None = None, stats: dict | None = None) -> list[int]:
        """Stores the ranked postings of a search (upserted by key) and its outcome; returns
        the posting ids in rank order."""
        if status not in JOB_SEARCH_STATUSES:
            raise ValueError(f"Invalid job search status: {status!r}")
        ids: list[int] = []
        with self._writing():
            self._conn.execute("DELETE FROM job_search_postings WHERE search_id = ?", (search_id,))
            for rank, posting in enumerate(postings):
                posting_id = self._upsert_posting(posting, "search")
                ids.append(posting_id)
                self._conn.execute(
                    "INSERT OR REPLACE INTO job_search_postings (search_id, posting_id, rank) VALUES (?, ?, ?)",
                    (search_id, posting_id, rank))
            self._conn.execute(
                "UPDATE job_searches SET status = ?, found = ?, reports = ?, stats = ? WHERE id = ?",
                (status, len(postings), json.dumps(reports or [], ensure_ascii=False),
                 json.dumps(stats or {}, ensure_ascii=False, default=str), search_id))
        return ids

    def _upsert_posting(self, posting: dict, origin: str) -> int:
        """Inside a write transaction: insert or update one posting; the favourite flag
        and the "pasted" origin of an existing row are kept."""
        key = self.posting_key(posting)
        now = _now()
        row = self._conn.execute("SELECT id, origin FROM job_postings WHERE key = ?", (key,)).fetchone()
        values = (str(posting.get("source") or ""), str(posting.get("url") or ""),
                  str(posting.get("title") or ""), str(posting.get("company") or ""),
                  json.dumps(posting, ensure_ascii=False, default=str))
        if row is None:
            cur = self._conn.execute(
                "INSERT INTO job_postings (key, source, url, title, company, data, origin, created, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (key, *values, origin, now, now))
            return cur.lastrowid
        kept_origin = "pasted" if "pasted" in (row["origin"], origin) else origin
        self._conn.execute(
            "UPDATE job_postings SET source = ?, url = ?, title = ?, company = ?, data = ?, origin = ?, updated = ? "
            "WHERE id = ?", (*values, kept_origin, now, row["id"]))
        return row["id"]

    def save_posting(self, posting: dict, *, origin: str = "pasted") -> int:
        """Stores one posting (e.g. a pasted ad confirmed by the user); returns its id."""
        if origin not in POSTING_ORIGINS:
            raise ValueError(f"Invalid posting origin: {origin!r}")
        with self._writing():
            return self._upsert_posting(posting, origin)

    @staticmethod
    def _posting_row(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["data"] = json.loads(row["data"]) if row["data"] else {}
        item["favourite"] = bool(row["favourite"])
        return item

    def read_posting(self, posting_id: int) -> dict | None:
        with self._reading():
            row = self._conn.execute("SELECT * FROM job_postings WHERE id = ?", (posting_id,)).fetchone()
        return None if row is None else self._posting_row(row)

    def list_postings(self, *, origin: str | None = None, favourite: bool | None = None) -> list[dict]:
        """Stored postings, most recently updated first."""
        clauses, params = [], []
        if origin is not None:
            clauses.append("origin = ?")
            params.append(origin)
        if favourite is not None:
            clauses.append("favourite = ?")
            params.append(1 if favourite else 0)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._reading():
            rows = self._conn.execute(f"SELECT * FROM job_postings{where} ORDER BY updated DESC, id DESC",
                                      params).fetchall()
        return [self._posting_row(r) for r in rows]

    def set_posting_favourite(self, posting_id: int, on: bool) -> None:
        with self._writing():
            self._conn.execute("UPDATE job_postings SET favourite = ?, updated = ? WHERE id = ?",
                               (1 if on else 0, _now(), posting_id))

    def delete_posting(self, posting_id: int) -> None:
        with self._writing():
            self._conn.execute("DELETE FROM job_postings WHERE id = ?", (posting_id,))

    @staticmethod
    def _job_search_row(row: sqlite3.Row) -> dict:
        item = dict(row)
        for name, empty in (("understood", {}), ("reports", []), ("stats", {})):
            item[name] = json.loads(row[name]) if row[name] else empty
        return item

    def list_job_searches(self) -> list[dict]:
        """Job searches, most recent first."""
        with self._reading():
            rows = self._conn.execute("SELECT * FROM job_searches ORDER BY created DESC, id DESC").fetchall()
        return [self._job_search_row(r) for r in rows]

    def read_job_search(self, search_id: int) -> dict | None:
        with self._reading():
            row = self._conn.execute("SELECT * FROM job_searches WHERE id = ?", (search_id,)).fetchone()
        return None if row is None else self._job_search_row(row)

    def postings_of_job_search(self, search_id: int) -> list[dict]:
        """The stored postings of a search, in rank order."""
        with self._reading():
            rows = self._conn.execute(
                "SELECT p.* FROM job_search_postings l JOIN job_postings p ON p.id = l.posting_id "
                "WHERE l.search_id = ? ORDER BY l.rank", (search_id,)).fetchall()
        return [self._posting_row(r) for r in rows]

    def delete_job_search(self, search_id: int) -> None:
        """Deletes the search and its links; postings no other search, favourite or paste
        keeps are deleted too."""
        with self._writing():
            self._conn.execute("DELETE FROM job_searches WHERE id = ?", (search_id,))
            self._conn.execute("DELETE FROM search_notes WHERE kind = 'jobs' AND search_id = ?", (search_id,))
            self._conn.execute(
                "DELETE FROM job_postings WHERE origin = 'search' AND favourite = 0 AND id NOT IN "
                "(SELECT posting_id FROM job_search_postings)")

    # --- affinity (schema 5) --------------------------------------------------

    def affinity_save(self, company_key: str, cv_fp: str, settings_fp: str, data_fp: str, result_json: str,
                      *, verdict: str = "", affinity: float | None = None) -> None:
        """Stores (or replaces) the affinity result of a company for a CV and settings."""
        with self._writing():
            self._conn.execute(
                "INSERT OR REPLACE INTO affinity_results (company_key, cv_fp, settings_fp, data_fp, verdict, "
                "affinity, result, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (company_key, cv_fp, settings_fp, data_fp or "", verdict or "", affinity, result_json, _now()))

    def affinity_read(self, company_key: str, cv_fp: str, settings_fp: str) -> dict | None:
        """{"data_fp", "result" (JSON text), "verdict", "affinity", "created"} or None."""
        with self._reading():
            row = self._conn.execute(
                "SELECT * FROM affinity_results WHERE company_key = ? AND cv_fp = ? AND settings_fp = ?",
                (company_key, cv_fp, settings_fp)).fetchone()
        return None if row is None else dict(row)

    def affinity_read_many(self, company_keys, cv_fp: str, settings_fp: str) -> dict[str, dict]:
        """company_key -> stored row (as affinity_read) for the keys that have one."""
        keys = list(dict.fromkeys(company_keys))
        found: dict[str, dict] = {}
        for start in range(0, len(keys), 400):  # SQLite's parameter limit
            chunk = keys[start:start + 400]
            marks = ", ".join("?" for _ in chunk)
            with self._reading():
                rows = self._conn.execute(
                    f"SELECT * FROM affinity_results WHERE cv_fp = ? AND settings_fp = ? "
                    f"AND company_key IN ({marks})", (cv_fp, settings_fp, *chunk)).fetchall()
            found.update({r["company_key"]: dict(r) for r in rows})
        return found

    def affinity_prune(self, keep_cv_fp: str, keep_settings_fp: str) -> int:
        """Deletes the results of other CVs / settings; returns how many."""
        with self._writing():
            cur = self._conn.execute(
                "DELETE FROM affinity_results WHERE cv_fp != ? OR settings_fp != ?", (keep_cv_fp, keep_settings_fp))
        return cur.rowcount

    def affinity_count(self) -> int:
        with self._reading():
            return int(self._conn.execute("SELECT COUNT(*) FROM affinity_results").fetchone()[0])

    def feedback_add(self, company_key: str, agrees: bool, *, verdict: str = "", user_verdict: str | None = None,
                     cv_fp: str = "", result_json: str = "{}", when: float | None = None) -> int:
        """Records one judgement of a verdict (the latest per company wins in calibration)."""
        with self._writing():
            cur = self._conn.execute(
                "INSERT INTO affinity_feedback (company_key, agrees, user_verdict, verdict, cv_fp, result, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (company_key, 1 if agrees else 0, user_verdict, verdict or "", cv_fp or "", result_json or "{}",
                 _now() if when is None else when))
        return cur.lastrowid

    @staticmethod
    def _feedback_row(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["agrees"] = bool(item["agrees"])
        return item

    def feedback_rows(self) -> list[dict]:
        """Every judgement, oldest first."""
        with self._reading():
            rows = self._conn.execute("SELECT * FROM affinity_feedback ORDER BY created, id").fetchall()
        return [self._feedback_row(r) for r in rows]

    def feedback_latest(self, company_key: str) -> dict | None:
        with self._reading():
            row = self._conn.execute(
                "SELECT * FROM affinity_feedback WHERE company_key = ? ORDER BY created DESC, id DESC LIMIT 1",
                (company_key,)).fetchone()
        return None if row is None else self._feedback_row(row)

    def feedback_count(self) -> int:
        """Number of companies judged (the latest judgement per company counts once)."""
        with self._reading():
            return int(self._conn.execute(
                "SELECT COUNT(DISTINCT company_key) FROM affinity_feedback").fetchone()[0])
