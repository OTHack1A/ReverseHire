"""Log file in the data folder: one line per entry, rolling, capped, never raising.

Line format (every physical line, continuation lines of a traceback included):

    [YYYY-MM-DD][HH:MM:SS][COMPUTER-NAME]: LEVEL module [thread]: message

The level, the module (without the "reversehire." prefix) and the thread name are
part of the message: the thread tells the GUI thread ("MainThread") from the
workers, which is what a freeze diagnosis needs first.

Size: `reversehire.log` plus at most BACKUP_COUNT older files (`.1` = newest
backup), each at most MAX_SIZE_BYTES; when the current file is full the oldest file
is discarded (rolling), so the total on disk never exceeds MAX_TOTAL_BYTES (100 MB).
When the file cannot be renamed (another process keeps it open on Windows) the
oldest half of the current file is cut away in place instead, so the cap holds.

Robustness: the handler opens the file for each entry and closes it again (no
handle kept open, other processes can rotate it), every error (full disk, locked
file, missing folder) is swallowed and counted in `dropped()`: logging never breaks
the app. The handler has its own lock, so threads never interleave lines.

Privacy rules:
- secrets (API keys) never appear: `register_secret` adds them to a filter that
  replaces them with "***" in every line; `mask()` is for values logged on purpose
  (e.g. "setting changed");
- the CV text and e-mail bodies are never passed to the log (no module does it).
"""

import logging
import os
import platform
import threading
import time
from pathlib import Path

from .config import data_folder

LOG_FILE_NAME = "reversehire.log"
MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB per file
BACKUP_COUNT = 9                   # reversehire.log.1 ... .9
MAX_TOTAL_BYTES = MAX_SIZE_BYTES * (BACKUP_COUNT + 1)  # 100 MB on disk at most
PLACEHOLDER = "***"
MODULE_PREFIX = "reversehire."
CONTINUATION = "    "  # after the prefix, on the continuation lines of one entry

_secrets: set[str] = set()
_lock = threading.Lock()
_handle: "RollingFileHandler | None" = None
_computer: str | None = None


def safe_console() -> None:
    """stdout / stderr never raise on text: under pythonw (no console) they are None and
    become a null sink; on a Windows console with a legacy code page (cp1252/cp850) a
    character such as "✓" is replaced instead of raising UnicodeEncodeError."""
    import sys

    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115 - process-wide sink
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):  # a closed or detached stream: leave it
                pass


def register_secret(value: str | None) -> None:
    """Value to hide in every message (e.g. the Firecrawl key)."""
    if value and len(value.strip()) >= 4:
        with _lock:
            _secrets.add(value.strip())


def redact(text: str) -> str:
    with _lock:
        secret_list = sorted(_secrets, key=len, reverse=True)
    for secret in secret_list:
        text = text.replace(secret, PLACEHOLDER)
    return text


def mask(value) -> str:
    """What a log line shows of a secret value: "***" when set, "" when empty."""
    return PLACEHOLDER if value else ""


def set_computer_name(name: str | None) -> None:
    """Overrides the computer name written in every line (tests); None = the real one."""
    global _computer
    _computer = name


def computer_name() -> str:
    """The name of this computer (the third field of every line)."""
    if _computer is not None:
        return _computer
    name = os.environ.get("COMPUTERNAME") or ""
    if not name:
        try:
            name = platform.node()
        except Exception:  # noqa: BLE001
            name = ""
    return (name or "unknown").strip().replace("]", ")") or "unknown"


class SecretsFilter(logging.Filter):
    """Replaces the registered secrets in the already formatted message."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:  # arguments that cannot be formatted: leave as is
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        return True


def prefix(created: float | None = None) -> str:
    """"[YYYY-MM-DD][HH:MM:SS][COMPUTER-NAME]: " for the time `created` (now by default)."""
    moment = time.localtime(created if created is not None else time.time())
    return f"[{time.strftime('%Y-%m-%d', moment)}][{time.strftime('%H:%M:%S', moment)}][{computer_name()}]: "


class LineFormatter(logging.Formatter):
    """`[date][time][computer]: LEVEL module [thread]: message`, one prefix per physical line."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not lose the entry
            message = f"{record.msg!r} {record.args!r}"
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            message = f"{message}\n{record.exc_text}"
        if record.stack_info:
            message = f"{message}\n{self.formatStack(record.stack_info)}"
        module = record.name[len(MODULE_PREFIX):] if record.name.startswith(MODULE_PREFIX) else record.name
        head = prefix(record.created)
        lines = redact(message).replace("\r\n", "\n").replace("\r", "\n").split("\n")
        first = f"{head}{record.levelname} {module} [{record.threadName}]: {lines[0]}"
        rest = [f"{head}{CONTINUATION}{line}" for line in lines[1:] if line.strip()]
        return "\n".join([first, *rest])


class RollingFileHandler(logging.Handler):
    """Appends each entry to `path`; rolls to `path.1`..`path.N` when full (oldest
    dropped). Never raises; see the module docstring."""

    def __init__(self, path: Path, max_bytes: int = MAX_SIZE_BYTES, backups: int = BACKUP_COUNT):
        super().__init__()
        self.baseFilename = str(Path(path).resolve())
        self.path = Path(self.baseFilename)
        self.max_bytes = max(256, int(max_bytes))
        self.backups = max(0, int(backups))
        self._dropped = 0
        self.last_error = ""

    @property
    def dropped(self) -> int:
        return self._dropped

    def backup_path(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")

    def files(self) -> list[Path]:
        """The log files on disk, newest first."""
        found = [self.path] if self.path.exists() else []
        return found + [self.backup_path(i) for i in range(1, self.backups + 1) if self.backup_path(i).exists()]

    def emit(self, record: logging.LogRecord) -> None:
        try:
            data = (self.format(record) + "\n").encode("utf-8", errors="replace")
            if len(data) > self.max_bytes:  # one huge entry: keep its beginning
                data = data[: self.max_bytes - 16] + b"\n[...]\n"
            try:
                size = self.path.stat().st_size
            except OSError:
                size = 0
            if size + len(data) > self.max_bytes:
                self._roll()
            with open(self.path, "ab") as stream:
                stream.write(data)
        except Exception as error:  # noqa: BLE001 - logging must never break the app
            self._dropped += 1
            self.last_error = f"{type(error).__name__}: {error}"

    def _roll(self) -> None:
        """Make room in the current file: rotate the backups (the oldest is discarded),
        or, when the file cannot be renamed, cut its oldest half in place."""
        try:
            if self.backups == 0:
                raise OSError("no backups: trim in place")
            oldest = self.backup_path(self.backups)
            if oldest.exists():
                oldest.unlink()
            for index in range(self.backups - 1, 0, -1):
                source = self.backup_path(index)
                if source.exists():
                    os.replace(source, self.backup_path(index + 1))
            os.replace(self.path, self.backup_path(1))
        except OSError:
            self._trim_in_place()

    def _trim_in_place(self) -> None:
        try:
            with open(self.path, "r+b") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                keep = self.max_bytes // 2
                if size <= keep:
                    return
                stream.seek(size - keep)
                tail = stream.read()
                cut = tail.find(b"\n")
                tail = tail[cut + 1:] if cut >= 0 else tail
                stream.seek(0)
                stream.write(tail)
                stream.truncate()
        except OSError as error:
            self.last_error = f"{type(error).__name__}: {error}"

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 (logging API)
        self._dropped += 1


def log_file_path(folder: Path | None = None) -> Path:
    return (folder or data_folder()) / LOG_FILE_NAME


def handler() -> "RollingFileHandler | None":
    """The active file handler (None before `configure`)."""
    return _handle


def configure(folder: Path | None = None, level: int = logging.INFO, *, computer: str | None = None,
              max_bytes: int = MAX_SIZE_BYTES, backups: int = BACKUP_COUNT) -> Path:
    """Enables the log file (once per process) and returns its path. `computer`
    overrides the computer name of the lines (tests); `max_bytes` and `backups` the
    rolling limits (tests)."""
    global _handle
    if computer is not None:
        set_computer_name(computer)
    path = log_file_path(folder)
    root = logging.getLogger()
    logging.raiseExceptions = False  # a failing handler never prints nor raises
    with _lock:
        if _handle is not None:
            same = (Path(_handle.baseFilename) == path.resolve() and _handle.max_bytes == max(256, int(max_bytes))
                    and _handle.backups == max(0, int(backups)))
            if same:
                return path
            root.removeHandler(_handle)
            _handle.close()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # the handler counts the lost lines; the app keeps working
        handle = RollingFileHandler(path, max_bytes=max_bytes, backups=backups)
        handle.setFormatter(LineFormatter())
        handle.addFilter(SecretsFilter())
        root.addHandler(handle)
        _handle = handle
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    # httpx logs every request at INFO level: too noisy.
    for name in ("httpx", "httpcore", "urllib3", "fastembed"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # trafilatura reports pages it cannot parse (empty or broken HTML of a company site)
    # as WARNING/ERROR: a property of that page, already handled by the app, not an error.
    logging.getLogger("trafilatura").setLevel(logging.CRITICAL)
    return path


def dropped() -> int:
    """How many entries could not be written since `configure` (full disk, locked file)."""
    return _handle.dropped if _handle is not None else 0


def open_folder(folder: Path, platform: str | None = None, runner=None) -> bool:
    """Shows `folder` in the system file manager: Explorer on Windows, Finder on macOS
    (`open`), the desktop's default on Linux and BSD (`xdg-open`). False on failure."""
    import subprocess
    import sys

    platform = platform or sys.platform
    target = str(Path(folder))
    try:
        if platform == "win32" and runner is None:
            os.startfile(target)  # noqa: S606 - a folder path, never user text
            return True
        command = (["explorer", target] if platform == "win32"
                   else ["open", target] if platform == "darwin" else ["xdg-open", target])
        (runner or (lambda cmd: subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.DEVNULL)))(command)
        return True
    except (OSError, ValueError, AttributeError):
        logging.getLogger(__name__).warning("Folder cannot be opened: %s", target, exc_info=True)
        return False


def close() -> None:
    """Removes the file handler (used by the tests)."""
    global _handle
    with _lock:
        if _handle is not None:
            logging.getLogger().removeHandler(_handle)
            _handle.close()
            _handle = None
