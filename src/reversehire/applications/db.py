"""SQLite file of the tracker (``<data folder>/applications/applications.sqlite``).

Same threading rules as ``reversehire.store`` (read there for the reasons):

- every thread has its own connection, created on first use; no process-wide lock around reads;
- WAL journal where the folder allows it (``store.wal_allowed``), the classic journal elsewhere;
- writes run in ``BEGIN IMMEDIATE`` transactions; nested calls join the outer one;
- the GUI thread (``store.mark_gui_thread``) uses a short busy timeout and gets ``StoreBusyError``
  instead of a frozen window;
- ``":memory:"`` keeps one shared connection behind a lock (tests).

Schema versions live in the ``meta`` table; ``_MIGRATIONS[n]`` brings a version n-1 file to n.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from pathlib import Path

from .. import store as main_store

log = logging.getLogger("reversehire.applications")

BUSY_TIMEOUT_MS = 10_000
GUI_BUSY_TIMEOUT_MS = 2_000

_SCHEMA_V1 = [
    """CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER,
        company_name TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT '',
        posting_url TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'posting',
        channel TEXT NOT NULL DEFAULT 'email',
        recipient_email TEXT NOT NULL DEFAULT '',
        contact_name TEXT NOT NULL DEFAULT '',
        sent_date TEXT,
        status TEXT NOT NULL DEFAULT 'draft',
        cover_subject TEXT NOT NULL DEFAULT '',
        cover_letter TEXT NOT NULL DEFAULT '',
        cv_hash TEXT NOT NULL DEFAULT '',
        cv_name TEXT NOT NULL DEFAULT '',
        cv_language TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        follow_up_date TEXT,
        source TEXT NOT NULL DEFAULT 'manual',
        created REAL NOT NULL,
        updated REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS status_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
        at REAL NOT NULL,
        who TEXT NOT NULL DEFAULT 'user',
        field TEXT NOT NULL DEFAULT 'status',
        old TEXT NOT NULL DEFAULT '',
        new TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
        kind TEXT NOT NULL DEFAULT 'phone',
        start TEXT NOT NULL DEFAULT '',
        duration_min INTEGER NOT NULL DEFAULT 60,
        place TEXT NOT NULL DEFAULT '',
        people TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        outcome TEXT NOT NULL DEFAULT 'pending',
        reminder_min INTEGER,
        uid TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL,
        updated REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS attachments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
        hash TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        size INTEGER NOT NULL DEFAULT 0,
        added REAL NOT NULL
    )""",
]

# Version 2: indexes for the lists (by company, by date) and the appointment queries.
_MIGRATIONS: dict[int, list[str]] = {
    2: [
        "CREATE INDEX IF NOT EXISTS idx_applications_company ON applications(company_id)",
        "CREATE INDEX IF NOT EXISTS idx_applications_sent ON applications(sent_date)",
        "CREATE INDEX IF NOT EXISTS idx_history_application ON status_history(application_id)",
        "CREATE INDEX IF NOT EXISTS idx_appointments_application ON appointments(application_id)",
        "CREATE INDEX IF NOT EXISTS idx_attachments_application ON attachments(application_id)",
    ],
}
SCHEMA_VERSION = 2


class Database:
    """Thread-safe SQLite access (see the module docstring)."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._shared = self.path == ":memory:"
        self._lock = threading.RLock()
        self._local = threading.local()
        self._connections: list[tuple[threading.Thread, sqlite3.Connection]] = []
        self._connections_lock = threading.Lock()
        self._closed = False
        self._mode = "DELETE"
        try:
            if not self._shared:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            conn = self._open()
            if not self._shared:
                self._mode = self._choose_journal(conn)
        except (sqlite3.Error, OSError) as error:
            raise (main_store.explain_error(error, self.path) or error) from error
        self._create_and_migrate()

    # --- connections -------------------------------------------------------------------

    def _choose_journal(self, conn: sqlite3.Connection) -> str:
        if main_store.wal_allowed(self.path):
            try:
                if str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).upper() == "WAL":
                    conn.execute("PRAGMA synchronous = NORMAL")
                    return "WAL"
            except sqlite3.Error:
                log.info("WAL journal not possible for %s: classic journal used", self.path)
        conn.execute("PRAGMA journal_mode = DELETE")
        return "DELETE"

    def _open(self) -> sqlite3.Connection:
        on_gui = main_store._on_gui_thread() and not self._shared
        timeout_ms = GUI_BUSY_TIMEOUT_MS if on_gui else BUSY_TIMEOUT_MS
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=timeout_ms / 1000,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {timeout_ms}")
        if not self._shared and self._mode == "WAL":
            conn.execute("PRAGMA synchronous = NORMAL")
        current = threading.current_thread()
        with self._connections_lock:
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
    def conn(self) -> sqlite3.Connection:
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
                conn = self._open()
            except (sqlite3.Error, OSError) as error:
                raise (main_store.explain_error(error, self.path) or error) from error
        return conn

    @contextlib.contextmanager
    def reading(self):
        if self._shared:
            with self._lock:
                yield self.conn
        else:
            yield self.conn

    @contextlib.contextmanager
    def writing(self):
        """A ``BEGIN IMMEDIATE`` transaction; nested calls join the outer one."""
        guard = self._lock if self._shared else contextlib.nullcontext()
        try:
            with guard:
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
        except main_store.StoreUnavailableError:
            raise
        except (sqlite3.OperationalError, OSError) as error:
            explained = main_store.explain_error(error, self.path)
            if explained is None:
                raise
            if main_store._on_gui_thread() and explained.cause == "locked":
                raise main_store.StoreBusyError(self.path) from error
            raise explained from error

    # --- schema ------------------------------------------------------------------------

    def _create_and_migrate(self) -> None:
        with self.writing() as conn:
            for statement in _SCHEMA_V1:
                conn.execute(statement)
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            current = int(row["value"]) if row else 1
            if current > SCHEMA_VERSION:
                log.warning("Applications database %s is newer (schema %d) than this program (%d)",
                            self.path, current, SCHEMA_VERSION)
                return
            for version in range(current + 1, SCHEMA_VERSION + 1):
                for statement in _MIGRATIONS.get(version, []):
                    conn.execute(statement)
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                         (str(max(current, SCHEMA_VERSION)),))

    @property
    def schema_version(self) -> int:
        with self.reading() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return int(row["value"]) if row else 0

    @property
    def journal_mode(self) -> str:
        with self.reading() as conn:
            return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).upper()

    def locked(self) -> threading.RLock:
        return self._lock

    def close(self) -> None:
        self._closed = True
        with self._connections_lock:
            connections, self._connections = self._connections, []
        for _thread, conn in connections:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        self._shared_conn = None
        self._local = threading.local()
