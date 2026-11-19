"""Fast screening first; deep analysis when the PC is idle.

    from reversehire import background

    ranked = background.screen_companies(candidates, cv_profile, affinity_preferences)
    scheduler = background.IdleScheduler.from_state(stored_state, run_job,
                                                    config=background.IdleConfig(...),
                                                    on_status=..., on_state=store_it,
                                                    app_inactive_seconds=window_inactive_s)
    scheduler.enqueue_screened(ranked, payload_of=lambda r: {"card_id": ...}, host_of=...)
    scheduler.start()                    # daemon thread; tick() twice a second
    scheduler.set_foreground_busy(True)  # a search started: running jobs pause at once
    scheduler.set_paused(True)           # the user's switch
    scheduler.stop()                     # at exit (running jobs go back to the queue)

`screen_companies` only orders the queue; it never hides a company.
"""

from .probes import SystemProbes
from .scheduler import (DONE, FAILED, QUEUED, S_IDLE, S_PAUSED, S_RUNNING, S_STOPPED, IdleConfig, IdleScheduler,
                        Job, JobContext, JobResult, SchedulerStatus)
from .screen import ScreenResult, screen_companies
from .texts import FALLBACK as TEXTS_EN
from .texts import text

__all__ = [
    "DONE", "FAILED", "IdleConfig", "IdleScheduler", "Job", "JobContext", "JobResult", "QUEUED", "S_IDLE",
    "S_PAUSED", "S_RUNNING", "S_STOPPED", "SchedulerStatus", "ScreenResult", "SystemProbes", "TEXTS_EN",
    "screen_companies", "text",
]
