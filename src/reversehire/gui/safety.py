"""Safety net of the window: an error in any handler, thread or Qt itself is logged and
shown as a non-blocking status-bar notice (never a crash, never a modal box opened
from a worker), and a watchdog writes the GUI thread's stack to the log when the event
loop stops answering, so every future freeze can be diagnosed.

- `install()`: `sys.excepthook` (exceptions in Qt slots end there in PySide6),
  `threading.excepthook` and a Qt message handler;
- `safe_slot(function)`: wrapper for callbacks wired by hand (menu actions, queue
  callbacks): the exception is reported and the call returns None;
- `set_notice_handler(function)`: what shows the notice (the main window's status
  bar); called on the GUI thread through a queued Qt signal, from any thread;
- `Watchdog`: a QTimer beats on the GUI thread every HEARTBEAT_MS; a daemon thread
  checks that the last beat is not older than STALL_S, else logs the GUI thread's
  stack (`sys._current_frames()`) and the other threads' current lines, once per
  stall, and how long it lasted when the loop answers again.
"""

from __future__ import annotations

import functools
import logging
import sys
import threading
import time
import traceback
from typing import Callable

from PySide6.QtCore import QObject, QTimer, QtMsgType, Signal, qInstallMessageHandler

log = logging.getLogger(__name__)

HEARTBEAT_MS = 500
STALL_S = 5.0
CHECK_S = 0.5
OTHER_THREAD_FRAMES = 4  # lines shown for each non-GUI thread in a stall report

_handler: Callable[[str], None] | None = None
_bridge: "_NoticeBridge | None" = None
_installed = False
_previous_qt_handler = None


class _NoticeBridge(QObject):
    notice = Signal(str)


def set_notice_handler(function: Callable[[str], None] | None) -> None:
    """The function that shows a notice (text) on the GUI thread; None removes it."""
    global _handler, _bridge
    _handler = function
    if _bridge is None:
        _bridge = _NoticeBridge()  # created on the GUI thread: queued delivery from workers
        _bridge.notice.connect(_deliver)


def _deliver(text: str) -> None:
    handler = _handler
    if handler is None:
        return
    try:
        handler(text)
    except Exception:  # noqa: BLE001 - the notice itself must never fail loudly
        log.debug("Notice not shown", exc_info=True)


def notify(text: str) -> None:
    """Shows `text` in the status bar, from any thread (non-blocking)."""
    if _bridge is not None:
        try:
            _bridge.notice.emit(text)
        except RuntimeError:  # the bridge was deleted at shutdown
            pass


def report(where: str, exc_info=None) -> None:
    """Logs an unexpected error (with traceback) and shows the generic notice."""
    try:
        log.error("Unexpected error in %s", where, exc_info=exc_info or sys.exc_info())
    except Exception:  # noqa: BLE001
        pass
    try:
        from ..i18n import t

        notify(t("error.unexpected"))
    except Exception:  # noqa: BLE001
        pass


def safe_slot(function: Callable, name: str | None = None) -> Callable:
    """`function` that never lets an exception out: it is reported and None is returned."""
    label = name or getattr(function, "__qualname__", None) or repr(function)

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:  # noqa: BLE001
            report(label)
            return None

    return wrapper


def _excepthook(kind, value, tb) -> None:
    if issubclass(kind, KeyboardInterrupt):
        return
    report("the interface (unhandled)", (kind, value, tb))


def _thread_excepthook(args) -> None:
    if args.exc_type is SystemExit:
        return
    name = getattr(args.thread, "name", "?")
    report(f"thread {name}", (args.exc_type, args.exc_value, args.exc_traceback))


_QT_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}


# Qt warnings known to be harmless on Windows, logged at DEBUG (they filled the log by the
# dozen and hid the real ones): the platform plugin's unsupported window hints, Qt's own
# font folder missing in the wheel (the system fonts are used), thread storage at exit.
BENIGN_QT_WARNINGS = (
    "This plugin does not support propagateSizeHints()",
    "This plugin does not support raise()",
    "QFontDatabase: Cannot find font directory",
    "QThreadStorage: entry",
    "QDxgiVSyncService not destroyed in time",
)


def qt_level(mode, message: str) -> int:
    level = _QT_LEVELS.get(mode, logging.WARNING)
    if level == logging.WARNING and str(message or "").startswith(BENIGN_QT_WARNINGS):
        return logging.DEBUG
    return level


def _qt_message(mode, context, message) -> None:
    try:
        where = ""
        if context is not None and getattr(context, "file", None):
            where = f" ({context.file}:{context.line})"
        logging.getLogger("qt").log(qt_level(mode, message), "%s%s", message, where)
    except Exception:  # noqa: BLE001 - never raise inside Qt
        pass


def install() -> None:
    """Installs the hooks (once per process)."""
    global _installed, _previous_qt_handler
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    if not _installed:
        _previous_qt_handler = qInstallMessageHandler(_qt_message)
        _installed = True


# --- watchdog -----------------------------------------------------------------------------


def thread_stacks(gui_ident: int | None = None) -> str:
    """The GUI thread's full stack and the current lines of the other threads."""
    frames = sys._current_frames()
    names = {t.ident: t.name for t in threading.enumerate()}
    gui_ident = gui_ident if gui_ident is not None else threading.main_thread().ident
    parts = []
    frame = frames.get(gui_ident)
    if frame is not None:
        parts.append(f"GUI thread ({names.get(gui_ident, gui_ident)}) stack:")
        parts.append("".join(traceback.format_stack(frame)).rstrip())
    for ident, other in frames.items():
        if ident == gui_ident or ident == threading.get_ident():
            continue
        summary = traceback.extract_stack(other)[-OTHER_THREAD_FRAMES:]
        where = " <- ".join(f"{s.name} ({s.filename.rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1]}:{s.lineno})"
                            for s in reversed(summary))
        parts.append(f"thread {names.get(ident, ident)}: {where}")
    return "\n".join(parts)


class Watchdog:
    """Notices a GUI event loop that has not processed a heartbeat for `stall_s` seconds
    and writes the GUI thread's stack to the log (see the module docstring)."""

    def __init__(self, stall_s: float = STALL_S, heartbeat_ms: int = HEARTBEAT_MS, check_s: float = CHECK_S,
                 gui_ident: int | None = None, use_timer: bool = True):
        self.stall_s = stall_s
        self.check_s = check_s
        self.gui_ident = gui_ident if gui_ident is not None else threading.get_ident()
        self._last = time.monotonic()
        self._reported_at: float | None = None
        self.reports = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._timer: QTimer | None = None
        if use_timer:
            self._timer = QTimer()
            self._timer.setInterval(heartbeat_ms)
            self._timer.timeout.connect(self.beat)

    def beat(self) -> None:
        now = time.monotonic()
        if self._reported_at is not None:
            log.warning("GUI thread responsive again after %.1f s", now - self._last)
            self._reported_at = None
        self._last = now

    def start(self) -> "Watchdog":
        self.beat()
        if self._timer is not None:
            self._timer.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, daemon=True, name="gui-watchdog")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._timer is not None:
            self._timer.stop()
        if self._thread is not None:
            self._thread.join(2.0)

    def _watch(self) -> None:
        while not self._stop.wait(self.check_s):
            try:
                stalled = time.monotonic() - self._last
                if stalled >= self.stall_s and self._reported_at is None:
                    self._reported_at = time.monotonic()
                    self.reports += 1
                    log.warning("GUI thread not responding for %.1f s (event loop blocked)\n%s",
                                stalled, thread_stacks(self.gui_ident))
            except Exception:  # noqa: BLE001 - the watchdog must never die
                pass
