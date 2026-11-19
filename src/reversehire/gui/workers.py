"""A small worker runner for dialogs and panels used without the main window.

`QtRunner(parent).run(work, on_success, on_error, name=..., timeout=...)` has the same
shape as `MainWindow._in_background`: `work` runs in a daemon thread, its result (or
exception) comes back on the GUI thread through a queued signal, and after `timeout`
seconds the caller stops waiting (on_error gets a WorkerTimeout, a late result is
dropped). The main window's own runner is preferred when there is one (it also
cancels and logs centrally); this one exists so a dialog never blocks either way.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from .common import WorkerTimeout

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 120.0


class QtRunner(QObject):
    _finished = Signal(int, object, object)  # job id, result, error

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._jobs: dict[int, dict] = {}
        self._counter = 0
        self._finished.connect(self._deliver)

    def run(self, work: Callable[[], object], on_success: Callable[[object], None],
            on_error: Callable[[BaseException], None] | None = None, *, name: str | None = None,
            timeout: float | None = None, cancel: threading.Event | None = None) -> int:
        self._counter += 1
        job = self._counter
        label = name or getattr(work, "__qualname__", "job")
        limit = DEFAULT_TIMEOUT_S if timeout is None else timeout
        self._jobs[job] = {"ok": on_success, "error": on_error or (lambda _e: None), "name": label,
                           "started": time.monotonic(), "cancel": cancel}
        if limit:
            QTimer.singleShot(int(limit * 1000), self, lambda j=job, s=limit: self._expire(j, s))

        def target() -> None:
            result, error = None, None
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - delivered to on_error
                log.warning("Worker failed: %s", label, exc_info=True)
                error = exc
            try:
                self._finished.emit(job, result, error)
            except RuntimeError:  # the runner was deleted meanwhile
                pass

        log.info("Worker started: %s", label)
        threading.Thread(target=target, daemon=True, name="gui-worker").start()
        return job

    def busy(self) -> bool:
        return bool(self._jobs)

    def _expire(self, job: int, seconds: float) -> None:
        info = self._jobs.pop(job, None)
        if info is None:
            return
        log.warning("Worker timed out: %s (no answer after %.0f s)", info["name"], seconds)
        if info["cancel"] is not None:
            info["cancel"].set()
        info["error"](WorkerTimeout(info["name"], seconds))

    def _deliver(self, job: int, result, error) -> None:
        info = self._jobs.pop(job, None)
        if info is None:
            return  # timed out: the late result is dropped
        log.info("Worker finished: %s (%.1f s)", info["name"], time.monotonic() - info["started"])
        try:
            if error is not None:
                info["error"](error)
            else:
                info["ok"](result)
        except Exception:  # noqa: BLE001
            from . import safety

            safety.report(f"result handler of {info['name']}")


def runner_or_local(run: Callable | None, parent: QObject) -> Callable:
    """`run` when given (the main window's), else a QtRunner owned by `parent`."""
    if run is not None:
        return run
    runner = QtRunner(parent)
    return runner.run
