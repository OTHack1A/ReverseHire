"""`IdleScheduler`: deep analysis of queued companies only while the computer is idle.

Conditions to run (all must hold; checked twice a second while a job runs):
- user-input idle time >= `idle_threshold_s` (unknown idle time: the app window has
  been inactive for `app_inactive_fallback_s`, reported by the GUI; both unknown: no);
- on AC power when detectable (`require_ac`; unknown = allowed);
- CPU load <= `cpu_max` (unknown = allowed);
- network available (`require_network`; unknown = allowed);
- no foreground search in the app (`set_foreground_busy`), not paused by the user;
- today's request budget not spent, per-host daily limit not reached.

When a condition fails while a job runs, the job's cancel event is set at once (user
activity reported by the app, a foreground search, the pause switch) or within one
poll (`poll_s`, default 0.5 s): the job callable must stop at its next network wait
(the polite gateway and the Fetcher honour cancel events). An interrupted job goes back
to the queue without penalty. A job that fails is retried with exponential back-off
(`backoff_base_s` * 2^(attempts-1), capped), and marked failed after `max_attempts`.

The job work (read the site, build the card, compute affinity) is injected as one
callable ``run_job(job, ctx) -> JobResult | bool | None`` (exceptions are caught).
`ctx.charge(host, n)` counts requests as they are made (budget and per-host limits),
`ctx.cancel` is the event to honour.

State: `to_state()` is a JSON-serialisable dict (queue with priorities, attempts,
next retry time, counters of the day) that the integrator stores and passes back to
`from_state()` / `load_state()` after a restart; `on_state` is called after every change.
Status: `status()` and the `on_status` callback (state idle/running/paused/stopped, the
reason key + text, done/queued/failed counts, next company, today's requests). All
probes and the clock are injectable; `tick()` performs one scheduling step (tests call
it directly, `start()` runs it on a daemon thread). At most `max_parallel` jobs (default
1) run at a time.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable

from .texts import text

log = logging.getLogger(__name__)

STATE_VERSION = 1
# Job.state
QUEUED, DONE, FAILED = "queued", "done", "failed"
# SchedulerStatus.state
S_IDLE, S_RUNNING, S_PAUSED, S_STOPPED = "idle", "running", "paused", "stopped"


@dataclass
class IdleConfig:
    idle_threshold_s: float = 300.0
    app_inactive_fallback_s: float = 600.0
    require_ac: bool = True
    cpu_max: float = 0.5
    require_network: bool = True
    daily_request_budget: int = 600
    per_host_daily: int = 60
    estimated_requests_per_job: int = 25   # a job starts only when this much budget is left
    max_attempts: int = 5
    backoff_base_s: float = 300.0
    backoff_max_s: float = 6 * 3600.0
    poll_s: float = 0.5
    max_parallel: int = 1

    @classmethod
    def from_dict(cls, d: dict | None) -> "IdleConfig":
        d = dict(d or {})
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Job:
    key: str
    name: str = ""
    priority: float = 0.0
    payload: dict = field(default_factory=dict)   # JSON values only (card id, site...)
    host: str = ""
    state: str = QUEUED
    attempts: int = 0
    next_try_at: float = 0.0
    last_error: str = ""
    added_at: float = 0.0
    done_at: float | None = None
    requests: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class JobResult:
    ok: bool = True
    requests: int = 0             # requests made (when not counted with ctx.charge)
    error: str = ""
    retry: bool = True            # False: a permanent failure (no retry)
    interrupted: bool = False     # stopped because ctx.cancel was set


class JobContext:
    """What a running job sees: the cancel event and the request accounting."""

    def __init__(self, scheduler: "IdleScheduler", job: Job):
        self._scheduler = scheduler
        self.job = job
        self.cancel = threading.Event()
        self.charged = 0

    def charge(self, host: str = "", n: int = 1) -> bool:
        """Counts `n` requests to `host`; False when the daily budget or the host's limit
        is reached (the job should stop and return)."""
        self.charged += n
        return self._scheduler._charge(host, n)

    def requests_left(self) -> int:
        return self._scheduler._budget_left()

    def host_requests_left(self, host: str) -> int:
        return self._scheduler._host_left(host)


@dataclass
class SchedulerStatus:
    state: str
    reason: str                   # reason key "background.reason.*"
    message: str                  # readable reason
    done: int = 0
    queued: int = 0
    failed: int = 0
    running: list[str] = field(default_factory=list)
    next_name: str = ""
    requests_today: int = 0
    budget: int = 0
    paused_by_user: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def counts_text(self) -> str:
        return text("background.counts", done=self.done, queued=self.queued, failed=self.failed,
                    requests=self.requests_today, budget=self.budget)


class IdleScheduler:
    def __init__(self, run_job: Callable, *, config: IdleConfig | None = None,
                 idle_seconds: Callable[[], float | None] | None = None,
                 on_ac_power: Callable[[], bool | None] | None = None,
                 cpu_load: Callable[[], float | None] | None = None,
                 network_available: Callable[[], bool | None] | None = None,
                 app_inactive_seconds: Callable[[], float | None] | None = None,
                 clock: Callable[[], float] = time.time,
                 on_status: Callable[[SchedulerStatus], None] | None = None,
                 on_state: Callable[[dict], None] | None = None,
                 on_job_done: Callable[[Job, JobResult], None] | None = None,
                 system_probes=None):
        """`run_job(job, ctx)` does the work. Probes default to the real system probes
        (`probes.SystemProbes`); pass fakes in tests. `clock` returns epoch seconds."""
        self.run_job = run_job
        self.config = config or IdleConfig()
        if system_probes is None and None in (idle_seconds, on_ac_power, cpu_load, network_available):
            from .probes import SystemProbes

            system_probes = SystemProbes()
        self._idle = idle_seconds or system_probes.idle_seconds
        self._ac = on_ac_power or system_probes.on_ac_power
        self._cpu = cpu_load or system_probes.cpu_load
        self._net = network_available or system_probes.network_available
        self._app_inactive = app_inactive_seconds
        self._clock = clock
        self.on_status = on_status
        self.on_state = on_state
        self.on_job_done = on_job_done
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._running: dict[str, tuple[threading.Thread | None, JobContext]] = {}
        self._day = self._today()
        self._requests_today = 0
        self._host_requests: dict[str, int] = {}
        self._done_total = 0
        self._paused_by_user = False
        self._foreground_busy = False
        self._user_active_until = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_status: SchedulerStatus | None = None
        self._stopped = True

    # --- persistence ----------------------------------------------------------------------
    def to_state(self) -> dict:
        with self._lock:
            return {"version": STATE_VERSION, "jobs": [j.to_dict() for j in self._jobs.values()],
                    "day": self._day, "requests_today": self._requests_today,
                    "host_requests": dict(self._host_requests), "done_total": self._done_total,
                    "paused_by_user": self._paused_by_user}

    def load_state(self, state: dict | None) -> None:
        """Restores a stored state (jobs that were running become queued again)."""
        state = state if isinstance(state, dict) else {}
        with self._lock:
            self._jobs = {}
            for raw in state.get("jobs") or []:
                try:
                    job = Job.from_dict(raw)
                except (TypeError, ValueError):
                    continue
                if job.state not in (QUEUED, DONE, FAILED):
                    job.state = QUEUED
                self._jobs[job.key] = job
            if state.get("day") == self._today():
                self._requests_today = int(state.get("requests_today") or 0)
                self._host_requests = {str(k): int(v) for k, v in (state.get("host_requests") or {}).items()}
            self._done_total = int(state.get("done_total") or 0)
            self._paused_by_user = bool(state.get("paused_by_user", False))
        self._changed()

    @classmethod
    def from_state(cls, state: dict | None, run_job: Callable, **kwargs) -> "IdleScheduler":
        scheduler = cls(run_job, **kwargs)
        scheduler.load_state(state)
        return scheduler

    # --- queue --------------------------------------------------------------------------------
    def enqueue(self, key: str, name: str = "", priority: float = 0.0, payload: dict | None = None,
                host: str = "", *, requeue_done: bool = False) -> bool:
        """Adds a job (or updates the priority of a queued one). True when queued."""
        with self._lock:
            job = self._jobs.get(key)
            if job is not None:
                if job.state == QUEUED or (requeue_done and job.state in (DONE, FAILED)):
                    job.priority, job.name = float(priority), name or job.name
                    job.payload = dict(payload or job.payload)
                    job.host = host or job.host
                    if job.state != QUEUED:
                        job.state, job.attempts, job.next_try_at, job.last_error = QUEUED, 0, 0.0, ""
                    changed = True
                else:
                    changed = False
            else:
                self._jobs[key] = Job(key=key, name=name, priority=float(priority), payload=dict(payload or {}),
                                      host=host, added_at=self._clock())
                changed = True
        if changed:
            self._changed()
        return changed

    def enqueue_screened(self, results, payload_of: Callable | None = None, host_of: Callable | None = None) -> int:
        """Queues pre-screen results (`screen.ScreenResult`) with their score as priority."""
        count = 0
        with self.batch():
            for result in results:
                payload = payload_of(result) if payload_of is not None else {"key": result.key}
                host = host_of(result) if host_of is not None else ""
                if self.enqueue(result.key, result.name, result.score, payload, host):
                    count += 1
        return count

    def update_priorities(self, priorities: dict[str, float]) -> None:
        with self._lock:
            for key, value in priorities.items():
                if key in self._jobs:
                    self._jobs[key].priority = float(value)
        self._changed()

    def remove(self, key: str) -> None:
        with self._lock:
            self._jobs.pop(key, None)
        self._changed()

    def clear_finished(self) -> None:
        with self._lock:
            self._jobs = {k: j for k, j in self._jobs.items() if j.state == QUEUED}
        self._changed()

    def jobs(self) -> list[Job]:
        with self._lock:
            return [Job.from_dict(j.to_dict()) for j in self._jobs.values()]

    # --- signals from the app -----------------------------------------------------------------
    def set_paused(self, paused: bool) -> None:
        """The user's pause switch (persistent)."""
        with self._lock:
            self._paused_by_user = bool(paused)
        if paused:
            self._interrupt("paused by the user")
        self._changed()
        self._publish(self._evaluate()[1])

    def set_foreground_busy(self, busy: bool) -> None:
        """A foreground search started (True) or ended (False)."""
        with self._lock:
            self._foreground_busy = bool(busy)
        if busy:
            self._interrupt("foreground search")
        self._publish(self._evaluate()[1])

    def notify_user_active(self) -> None:
        """The app saw user input: running jobs pause at once."""
        with self._lock:
            self._user_active_until = self._clock() + 1.0
        self._interrupt("user active")

    # --- budget ---------------------------------------------------------------------------------
    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock()).date().isoformat()

    def _roll_day(self) -> None:
        today = self._today()
        if today != self._day:
            self._day, self._requests_today, self._host_requests = today, 0, {}

    def _budget_left(self) -> int:
        with self._lock:
            self._roll_day()
            return max(0, self.config.daily_request_budget - self._requests_today)

    def _host_left(self, host: str) -> int:
        with self._lock:
            return max(0, self.config.per_host_daily - self._host_requests.get(host, 0)) if host else 1 << 30

    def _charge(self, host: str, n: int) -> bool:
        with self._lock:
            self._roll_day()
            self._requests_today += n
            if host:
                self._host_requests[host] = self._host_requests.get(host, 0) + n
            ok = self._requests_today <= self.config.daily_request_budget and (
                not host or self._host_requests[host] <= self.config.per_host_daily)
        return ok

    # --- conditions -----------------------------------------------------------------------------
    def _conditions(self) -> tuple[bool, str, dict]:
        """(ok, reason key, params) of the machine/app conditions (budget not included)."""
        config = self.config
        with self._lock:
            if self._paused_by_user:
                return False, "background.reason.paused_by_user", {}
            if self._foreground_busy:
                return False, "background.reason.foreground", {}
            if self._clock() < self._user_active_until:
                return False, "background.reason.user_active", {}
        idle = _safe(self._idle)
        if idle is None:
            inactive = _safe(self._app_inactive) if self._app_inactive is not None else None
            minutes = max(1, round(config.app_inactive_fallback_s / 60))
            if inactive is None or inactive < config.app_inactive_fallback_s:
                return False, "background.reason.idle_unknown", {"minutes": minutes}
        elif idle < config.idle_threshold_s:
            key = "background.reason.user_active" if idle < 5 else "background.reason.not_idle"
            return False, key, {"minutes": max(1, round(config.idle_threshold_s / 60))}
        if config.require_ac and _safe(self._ac) is False:
            return False, "background.reason.on_battery", {}
        load = _safe(self._cpu)
        if load is not None and load > config.cpu_max:
            return False, "background.reason.cpu_busy", {"percent": int(round(load * 100))}
        if config.require_network and _safe(self._net) is False:
            return False, "background.reason.offline", {}
        return True, "background.reason.running", {}

    def _next_job(self, now: float) -> tuple[Job | None, str]:
        """(next runnable job, reason key when none)."""
        with self._lock:
            self._roll_day()
            queued = [j for j in self._jobs.values() if j.state == QUEUED and j.key not in self._running]
            if not queued:
                return None, "background.reason.queue_empty"
            if self._budget_left() < max(1, self.config.estimated_requests_per_job):
                return None, "background.reason.budget_spent"
            ready = [j for j in queued if j.next_try_at <= now]
            if not ready:
                return None, "background.reason.waiting_retry"
            allowed = [j for j in ready if not j.host or self._host_left(j.host) > 0]
            if not allowed:
                return None, "background.reason.host_limit"
            allowed.sort(key=lambda j: (-j.priority, j.added_at, j.key))
            return allowed[0], ""

    def _evaluate(self):
        ok, key, params = self._conditions()
        return ok, self._status(key, params, ok)

    # --- the loop -------------------------------------------------------------------------------
    def tick(self) -> SchedulerStatus:
        """One scheduling step: pause running jobs when a condition fails, else start the
        next job(s). Never raises."""
        try:
            return self._tick()
        except Exception:  # noqa: BLE001
            log.exception("Idle scheduler: unexpected error in a step")
            return self._status("background.reason.stopped", {}, False)

    def _tick(self) -> SchedulerStatus:
        self._reap()
        ok, key, params = self._conditions()
        if not ok:
            if self._running:
                self._interrupt(key)
            status = self._status(key, params, False)
            self._publish(status)
            return status
        now = self._clock()
        reason = "background.reason.running"
        with self._lock:
            slots = max(1, self.config.max_parallel) - len(self._running)
        for _ in range(max(0, slots)):  # a fast job may end before the loop checks again
            job, why = self._next_job(now)
            if job is None:
                reason = why if not self._running else reason
                break
            self._start_job(job)
        status = self._status(reason, {}, True)
        self._publish(status)
        return status

    def _start_job(self, job: Job) -> None:
        context = JobContext(self, job)
        with self._lock:
            self._running[job.key] = (None, context)
        thread = threading.Thread(target=self._work, args=(job, context), name=f"reversehire-idle-{job.key[:20]}",
                                  daemon=True)
        with self._lock:
            self._running[job.key] = (thread, context)
        log.info("Idle analysis started: %s", job.name or job.key)
        thread.start()

    def _work(self, job: Job, context: JobContext) -> None:
        result: JobResult
        try:
            raw = self.run_job(Job.from_dict(job.to_dict()), context)
            result = _as_result(raw)
        except Exception as exc:  # noqa: BLE001 - the job's errors become a retry
            log.warning("Idle analysis of %s failed: %s", job.name or job.key, type(exc).__name__, exc_info=True)
            result = JobResult(ok=False, error=f"{type(exc).__name__}: {exc}"[:300])
        if context.cancel.is_set() and not result.ok:
            result.interrupted = True
        self._finish(job.key, result, context)

    def _finish(self, key: str, result: JobResult, context: JobContext) -> None:
        now = self._clock()
        with self._lock:
            self._running.pop(key, None)
            job = self._jobs.get(key)
            extra = max(0, int(result.requests or 0) - context.charged)
            if extra:
                self._roll_day()
                self._requests_today += extra
                if job is not None and job.host:
                    self._host_requests[job.host] = self._host_requests.get(job.host, 0) + extra
            if job is None:
                return
            job.requests += max(context.charged, int(result.requests or 0))
            if result.interrupted or (context.cancel.is_set() and not result.ok):
                job.state = QUEUED  # paused: back in the queue, no penalty
                log.info("Idle analysis paused: %s", job.name or job.key)
            elif result.ok:
                job.state, job.done_at, job.last_error = DONE, now, ""
                self._done_total += 1
                log.info("Idle analysis done: %s", job.name or job.key)
            else:
                job.attempts += 1
                job.last_error = result.error[:300]
                if not result.retry or job.attempts >= self.config.max_attempts:
                    job.state = FAILED
                    log.info("Idle analysis failed for good: %s (%d attempts)", job.name or job.key, job.attempts)
                else:
                    delay = min(self.config.backoff_max_s, self.config.backoff_base_s * (2 ** (job.attempts - 1)))
                    job.next_try_at = now + delay
                    log.info("Idle analysis error for %s: retry in %.0f s", job.name or job.key, delay)
            snapshot = Job.from_dict(job.to_dict())
        self._changed()
        if self.on_job_done is not None:
            try:
                self.on_job_done(snapshot, result)
            except Exception:  # noqa: BLE001
                log.exception("Idle scheduler: on_job_done raised an exception")

    def _reap(self) -> None:
        with self._lock:
            dead = [k for k, (t, _c) in self._running.items() if t is not None and not t.is_alive()]
        for key in dead:  # a worker that ended without _finish (should not happen)
            with self._lock:
                self._running.pop(key, None)

    def _interrupt(self, why: str) -> None:
        with self._lock:
            contexts = [c for _t, c in self._running.values()]
        for context in contexts:
            if not context.cancel.is_set():
                log.info("Idle analysis pausing (%s): %s", why, context.job.name or context.job.key)
                context.cancel.set()

    # --- status ---------------------------------------------------------------------------------
    def _status(self, key: str, params: dict, ok: bool) -> SchedulerStatus:
        with self._lock:
            jobs = list(self._jobs.values())
            running = [self._jobs[k].name or k for k in self._running if k in self._jobs]
            queued = [j for j in jobs if j.state == QUEUED and j.key not in self._running]
            queued.sort(key=lambda j: (-j.priority, j.added_at, j.key))
            if running:
                state = S_RUNNING
            elif self._paused_by_user or not ok:
                state = S_PAUSED
            else:
                state = S_IDLE
            status = SchedulerStatus(
                state=state, reason=key, message=text(key, **params),
                done=sum(1 for j in jobs if j.state == DONE), queued=len(queued) + len(running),
                failed=sum(1 for j in jobs if j.state == FAILED), running=running,
                next_name=queued[0].name if queued else "", requests_today=self._requests_today,
                budget=self.config.daily_request_budget, paused_by_user=self._paused_by_user)
        return status

    def status(self) -> SchedulerStatus:
        ok, key, params = self._conditions()
        if ok:
            job, why = self._next_job(self._clock())
            if job is None and not self._running:
                key = why
        return self._status(key, params, ok)

    def _publish(self, status: SchedulerStatus) -> None:
        self._last_status = status
        if self.on_status is not None:
            try:
                self.on_status(status)
            except Exception:  # noqa: BLE001
                log.exception("Idle scheduler: on_status raised an exception")

    @contextlib.contextmanager
    def batch(self):
        """Many changes, one state save: `on_state` (a JSON copy of the whole queue) ran once
        per queued job, so queueing 1,800 companies froze the GUI thread for 5 seconds."""
        self._batch_depth = getattr(self, "_batch_depth", 0) + 1
        try:
            yield self
        finally:
            self._batch_depth -= 1
            if self._batch_depth == 0 and getattr(self, "_batch_dirty", False):
                self._batch_dirty = False
                self._changed()

    def _changed(self) -> None:
        if getattr(self, "_batch_depth", 0):
            self._batch_dirty = True
            return
        if self.on_state is not None:
            try:
                self.on_state(self.to_state())
            except Exception:  # noqa: BLE001
                log.exception("Idle scheduler: on_state raised an exception")

    # --- thread -------------------------------------------------------------------------------
    def start(self) -> None:
        """Runs `tick()` every `poll_s` seconds on a daemon thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._stopped = False
            self._thread = threading.Thread(target=self._loop, name="reversehire-idle-scheduler", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(max(0.05, float(self.config.poll_s)))

    def stop(self, timeout: float = 5.0) -> None:
        """Stops the loop and interrupts the running jobs (they go back to the queue)."""
        self._stop.set()
        self._stopped = True
        self._interrupt("stopped")
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self.wait_jobs(timeout)
        self._thread = None
        self._publish(self._status("background.reason.stopped", {}, False))

    def wait_jobs(self, timeout: float | None = None) -> bool:
        """Waits for the running jobs (tests, shutdown). True when none is left."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                threads = [t for t, _c in self._running.values() if t is not None]
            if not threads:
                return True
            for thread in threads:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                thread.join(remaining)
            if deadline is not None and time.monotonic() >= deadline:
                with self._lock:
                    return not self._running


def _safe(probe) -> object:
    if probe is None:
        return None
    try:
        return probe()
    except Exception:  # noqa: BLE001 - a probe never stops the scheduler
        return None


def _as_result(raw) -> JobResult:
    if isinstance(raw, JobResult):
        return raw
    if raw is None or raw is True:
        return JobResult(ok=True)
    if raw is False:
        return JobResult(ok=False, error="failed")
    if isinstance(raw, dict):
        return JobResult(**{k: raw[k] for k in JobResult.__dataclass_fields__ if k in raw})
    return JobResult(ok=True)
