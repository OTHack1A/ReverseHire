"""Background execution and error handling for the tracker's widgets.

A *runner* is any callable ``run(work, on_success, on_error=None)``: the main window's
``_in_background`` fits, so does ``ThreadRunner`` (default) or ``immediate_runner`` (tests).
The callbacks always run on the GUI thread. Nothing here raises into the GUI: an error in
a worker or in a callback is logged (``reversehire.applications``) and shown as a notice.
"""

from __future__ import annotations

import functools
import itertools
import logging
import threading
from typing import Callable

from PySide6.QtCore import QObject, Signal

log = logging.getLogger("reversehire.applications")


def immediate_runner(work: Callable, on_success: Callable, on_error: Callable | None = None, **_options) -> int:
    """Runs `work` right now on the calling thread (tests, or where threads are unwanted)."""
    try:
        result = work()
    except Exception as error:  # noqa: BLE001
        log.warning("Job failed", exc_info=True)
        if on_error is not None:
            on_error(error)
        return 0
    on_success(result)
    return 0


class ThreadRunner(QObject):
    """Runs work in daemon threads and delivers the outcome on the GUI thread (queued signal)."""

    _done = Signal(int, object, object)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._ids = itertools.count(1)
        self._jobs: dict[int, tuple[Callable, Callable | None]] = {}
        self._lock = threading.Lock()
        self._done.connect(self._deliver)

    def __call__(self, work: Callable, on_success: Callable, on_error: Callable | None = None, **_options) -> int:
        job = next(self._ids)
        with self._lock:
            self._jobs[job] = (on_success, on_error)

        def body():
            try:
                self._done.emit(job, work(), None)
            except Exception as error:  # noqa: BLE001
                log.warning("Worker failed", exc_info=True)
                try:
                    self._done.emit(job, None, error)
                except RuntimeError:  # the runner was deleted at shutdown
                    pass

        threading.Thread(target=body, daemon=True, name="applications-job").start()
        return job

    def _deliver(self, job: int, result, error) -> None:
        with self._lock:
            on_success, on_error = self._jobs.pop(job, (None, None))
        try:
            if error is not None:
                if on_error is not None:
                    on_error(error)
            elif on_success is not None:
                on_success(result)
        except Exception:  # noqa: BLE001 - a callback must never break the event loop
            log.warning("Callback of a worker failed", exc_info=True)


def guarded(method: Callable) -> Callable:
    """Decorator for widget slots: an exception is logged and shown as a notice, never raised."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as error:  # noqa: BLE001
            log.warning("Error in %s", method.__qualname__, exc_info=True)
            report = getattr(self, "report_error", None)
            if report is not None:
                try:
                    report(error)
                except Exception:  # noqa: BLE001
                    log.debug("Notice not shown", exc_info=True)
            return None

    return wrapper
