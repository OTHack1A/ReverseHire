"""The main window's part for "Who could be interested in my profile" (fast screening, assistant suggestions, deep analysis) and the idle analysis (a mixin of MainWindow, kept apart to keep
main_window.py readable).

Hooks called by MainWindow._ext (each optional; a failure is logged, never raised):
_interest_init() before the body is built, _interest_build() after the tabs exist,
_interest_menus(mirror), _interest_retranslate(), _interest_theme(palette), _interest_cv_read(analysis),
_interest_settings_applied(), _interest_started() at the end of the constructor,
_interest_shutdown(), _interest_dialogs() -> list of open dialogs to close at exit.
Everything slow runs through the window's `_in_background` (deadline, cancel, logging);
progress goes to the shared bottom bar (`status_progress`), errors to the log and to
`show_notice`.

Public API for the other parts of the window
--------------------------------------------
``window.interest_action``            the QAction of the big tab-1 button and of the Search menu.
``window.open_interest_flow()``       shows (or raises) the ONE guided window (gui/interest_flow.py).
``window.interest_enqueue(rows)``     queues flow rows (or (key, name, priority, payload, host)
                                      tuples) for the idle analysis; returns how many were queued.
``window.idle_scheduler``             the ``background.IdleScheduler`` (None until started).
``window.idle_status_text()``         the status-bar line ("Idle analysis: 12 done, 240 queued —
                                      paused: you are using the PC").
``window.idle_pause_action``          the checkable pause switch (status bar button + Tools menu).
``window.set_idle_paused(on)``        the same switch from code.
``window.interest_card_builder``      None, or ``builder(card_ids, cancel, progress)`` replacing the
                                      card pipeline (tests).

The idle job builds the card of one company (created from the queued company when it was
not saved yet) with the card pipeline, counts its requests against the daily budget and
asks the affinity service for its result. The scheduler state is kept in
``<data folder>/idle_analysis.json``. User input anywhere in the app (key, click, wheel,
mouse move; throttled) pauses a running job at once; so does foreground work (a company
search, a job search, a card build, a suggestion request, the flow's own analysis),
checked every half second.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QToolButton

from ..i18n import t

log = logging.getLogger(__name__)

STATE_FILE = "idle_analysis.json"
FOREGROUND_POLL_MS = 500
SAVE_DELAY_MS = 2000
ACTIVITY_THROTTLE_S = 0.5
CARDS_REFRESH_MS = 3000
PROBES_FACTORY = None   # tests: a callable returning fake probes; None = background.SystemProbes


def idle_config(settings):
    """The scheduler configuration of the settings (minutes, budget, mains power)."""
    from ..background import IdleConfig

    try:
        minutes = max(1, int(getattr(settings, "idle_minutes", 5) or 5))
    except (TypeError, ValueError):
        minutes = 5
    try:
        budget = max(1, int(getattr(settings, "idle_daily_budget", 600) or 600))
    except (TypeError, ValueError):
        budget = 600
    return IdleConfig(idle_threshold_s=minutes * 60.0, app_inactive_fallback_s=max(600.0, minutes * 60.0),
                      require_ac=bool(getattr(settings, "idle_only_on_mains", True)),
                      daily_request_budget=budget)


def status_line(status, enabled: bool = True) -> str:
    """The status-bar line of a ``background.SchedulerStatus``."""
    from ..background import S_PAUSED, S_RUNNING, S_STOPPED

    if not enabled or status is None:
        return t("interest.idle.off")
    why = t("interest.idle.why." + str(status.reason or "").rsplit(".", 1)[-1])
    if status.state == S_RUNNING:
        state = t("interest.idle.state.running", name=(status.running or [""])[0])
    elif status.state == S_STOPPED:
        state = t("interest.idle.state.off")
    elif status.state == S_PAUSED:
        state = t("interest.idle.state.paused", why=why)
    else:
        state = t("interest.idle.state.waiting", why=why)
    return t("interest.idle.line", done=status.done, queued=status.queued, state=state)


class _ActivityFilter(QObject):
    """Application-wide: real user input (throttled) and the app's active state."""

    INPUT = (QEvent.Type.KeyPress, QEvent.Type.MouseButtonPress, QEvent.Type.Wheel, QEvent.Type.MouseMove)

    def __init__(self, on_input, parent=None):
        super().__init__(parent)
        self._on_input = on_input
        self._last = 0.0
        self.inactive_since: float | None = None

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt name)
        kind = event.type()
        if kind in self.INPUT:
            now = time.monotonic()
            if now - self._last >= ACTIVITY_THROTTLE_S:
                self._last = now
                try:
                    self._on_input()
                except Exception:  # noqa: BLE001
                    log.debug("User activity not reported", exc_info=True)
        elif kind == QEvent.Type.ApplicationStateChange:
            app = QApplication.instance()
            active = app is not None and app.applicationState() == Qt.ApplicationState.ApplicationActive
            self.inactive_since = None if active else time.monotonic()
        return False

    def app_inactive_seconds(self) -> float | None:
        since = self.inactive_since
        return 0.0 if since is None else time.monotonic() - since


class InterestMixin:
    """Mixed into MainWindow (see the module docstring)."""

    # --- hooks ----------------------------------------------------------------------------------

    def _interest_init(self) -> None:
        self.interest_window = None
        self.idle_scheduler = None
        self.interest_card_builder = None
        self._idle_status = None
        self._idle_posted: dict | None = None
        self._idle_state: dict | None = None
        self._idle_dirty = False
        self._idle_lock = threading.Lock()
        self._idle_filter = None
        self._idle_busy = False
        self._idle_timers: list[QTimer] = []
        self.interest_action = QAction(self, triggered=lambda _c=False: self.open_interest_flow())
        self.idle_pause_action = QAction(self, checkable=True)
        self.idle_pause_action.setChecked(bool(getattr(self._settings, "idle_paused", False)))
        self.idle_pause_action.toggled.connect(self.set_idle_paused)
        self.idle_label = None
        self.idle_button = None

    def _interest_build(self) -> None:
        from .menus import bind_action

        profile = self.profile_panel
        profile.on_interest = lambda: self.interest_action.trigger()
        bind_action(profile.interest_button, self.interest_action)

    def _interest_status_widgets(self) -> None:
        """The idle line and its pause button in the bottom bar (built after the tabs)."""
        from .menus import bind_action
        from .widgets import ElidedLabel

        self.idle_label = ElidedLabel()
        self.idle_label.setObjectName("idleStatus")
        self.idle_label.setProperty("role", "muted")
        self.idle_button = QToolButton()
        self.idle_button.setObjectName("idlePauseButton")
        # short "Pause" / "Resume" here; the shared action (Tools menu) carries the long text
        self.idle_button.setCheckable(True)
        self.idle_button.setChecked(self.idle_pause_action.isChecked())
        self.idle_button.clicked.connect(lambda _c=False: self.idle_pause_action.trigger())
        bind_action(self.idle_button, self.idle_pause_action)
        self.idle_button.setAutoRaise(True)
        layout = self.status_label.parentWidget().layout()
        index = layout.indexOf(self.osm_label)
        layout.insertWidget(index if index >= 0 else layout.count(), self.idle_label)
        layout.insertWidget((index + 1) if index >= 0 else layout.count(), self.idle_button)

    def _interest_menus(self, mirror) -> None:
        self.search_menu.addSeparator()
        self.search_menu.addAction(self.interest_action)
        self.tools_menu.addSeparator()
        self.tools_menu.addAction(self.idle_pause_action)

    def _interest_retranslate(self) -> None:
        self.interest_action.setText(t("interest.action"))
        self._idle_pause_texts()
        self._idle_show(self._idle_status)
        window = self._interest_open_window()
        if window is not None:
            window.retranslate()

    def _interest_theme(self, palette) -> None:
        window = self._interest_open_window()
        if window is not None:
            window.apply_palette(palette)

    def _interest_cv_read(self, analysis) -> None:
        return None  # the tab-1 button follows the profile panel's own state

    def _interest_settings_applied(self) -> None:
        settings = self._settings
        if self.idle_pause_action.isChecked() != bool(getattr(settings, "idle_paused", False)):
            self.idle_pause_action.setChecked(bool(getattr(settings, "idle_paused", False)))
        scheduler = self.idle_scheduler
        if scheduler is None:
            return
        scheduler.config = idle_config(settings)
        if getattr(settings, "idle_enabled", True):
            scheduler.start()
        else:
            scheduler.stop(timeout=2.0)
        self._idle_show(scheduler.status())

    def _interest_started(self) -> None:
        from .. import background

        self._interest_status_widgets()
        self._idle_pause_texts()
        state = self._idle_load()
        probes = PROBES_FACTORY() if PROBES_FACTORY is not None else None
        scheduler = background.IdleScheduler.from_state(
            state, self._idle_run_job, config=idle_config(self._settings), system_probes=probes,
            on_status=self._idle_status_from_thread, on_state=self._idle_state_changed,
            on_job_done=self._idle_job_done, app_inactive_seconds=lambda: self._idle_app_inactive())
        scheduler.set_paused(bool(getattr(self._settings, "idle_paused", False)))
        self.idle_scheduler = scheduler
        app = QApplication.instance()
        if app is not None:
            self._idle_filter = _ActivityFilter(self._idle_user_input, self)
            app.installEventFilter(self._idle_filter)
        for interval, slot in ((FOREGROUND_POLL_MS, self._interest_foreground_check),
                               (SAVE_DELAY_MS, self._idle_save)):
            timer = QTimer(self)
            timer.setInterval(interval)
            timer.timeout.connect(slot)
            timer.start()
            self._idle_timers.append(timer)
        self._cards_timer = QTimer(self)
        self._cards_timer.setSingleShot(True)
        self._cards_timer.setInterval(CARDS_REFRESH_MS)
        self._cards_timer.timeout.connect(self._interest_refresh_cards)
        if getattr(self._settings, "idle_enabled", True):
            scheduler.start()
        self._idle_show(scheduler.status())

    def _interest_shutdown(self) -> None:
        for timer in self._idle_timers:
            timer.stop()
        if getattr(self, "_cards_timer", None) is not None:
            self._cards_timer.stop()
        app = QApplication.instance()
        if app is not None and self._idle_filter is not None:
            app.removeEventFilter(self._idle_filter)
        window = self._interest_open_window()
        if window is not None:
            window.close()
        if self.idle_scheduler is not None:
            self.idle_scheduler.stop(timeout=3.0)
            with self._idle_lock:
                self._idle_state = self.idle_scheduler.to_state()
                self._idle_dirty = True
            self._idle_save()

    def _interest_dialogs(self) -> list:
        window = self._interest_open_window()
        return [window] if window is not None else []

    # --- the guided window ------------------------------------------------------------------------

    def _interest_open_window(self):
        window = getattr(self, "interest_window", None)
        if window is None:
            return None
        try:
            if window._closed:
                return None
            window.isVisible()
        except RuntimeError:
            self.interest_window = None
            return None
        return window

    def open_interest_flow(self):
        """The guided window (non-modal, one at a time); the fast screening starts at once."""
        window = self._interest_open_window()
        if window is not None:
            window.raise_()
            window.activateWindow()
            return window
        from .interest_flow import InterestFlowWindow

        log.info("Interest flow opened")
        window = InterestFlowWindow(self)
        from . import theme

        window.apply_palette(theme.PALETTES[self._effective_theme])
        self.interest_window = window
        window.show()
        if self.profile_panel.has_profile:
            window.run_fast()
        else:
            window.fast_note.setText(t("interest.suggest.no_cv"))
        return window

    def _interest_cards_changed(self) -> None:
        """Cards were added or built: My companies is reloaded shortly (coalesced)."""
        timer = getattr(self, "_cards_timer", None)
        if timer is not None and not timer.isActive():
            timer.start()

    def _interest_refresh_cards(self) -> None:
        if self._closed:
            return
        try:
            self.companies_panel.reload()
            self._refresh_favourites()
        except Exception:  # noqa: BLE001
            log.debug("My companies not reloaded", exc_info=True)

    # --- foreground work and user activity ----------------------------------------------------------

    def interest_foreground_busy(self) -> bool:
        """True while the app does foreground network work (the idle jobs pause)."""
        busy = bool(self.search_running)
        jobs = getattr(self, "jobs_panel", None)
        busy = busy or bool(getattr(jobs, "running", False))
        try:
            busy = busy or bool(self.companies_panel.card_view.busy)
        except Exception:  # noqa: BLE001
            pass
        window = self._interest_open_window()
        if window is not None:
            busy = busy or window.running
        from .suggestions_view import running_views

        return busy or bool(running_views(self))

    def _interest_foreground_check(self) -> None:
        scheduler = self.idle_scheduler
        if scheduler is None or self._closed:
            return
        busy = self.interest_foreground_busy()
        if busy != self._idle_busy:
            self._idle_busy = busy
            scheduler.set_foreground_busy(busy)

    def _idle_user_input(self) -> None:
        scheduler = self.idle_scheduler
        if scheduler is not None:
            scheduler.notify_user_active()

    def _idle_app_inactive(self) -> float | None:
        flt = self._idle_filter
        return flt.app_inactive_seconds() if flt is not None else None

    # --- status ---------------------------------------------------------------------------------------

    def _idle_status_from_thread(self, status) -> None:
        """on_status of the scheduler (its thread): posted to the GUI when it changed."""
        data = status.to_dict()
        if data == self._idle_posted:
            return
        self._idle_posted = data
        self._queue.put(("ok", self._idle_show, status))

    def _idle_show(self, status) -> None:
        if status is not None:
            self._idle_status = status
        if self.idle_label is None or self._closed:
            return
        enabled = bool(getattr(self._settings, "idle_enabled", True))
        text = status_line(self._idle_status, enabled)
        self.idle_label.setText(text)
        tip = t("background.title")
        if self._idle_status is not None:
            tip = "\n".join([tip, self._idle_status.message, self._idle_status.counts_text])
        self.idle_label.setToolTip(tip)

    def idle_status_text(self) -> str:
        return self.idle_label.text() if self.idle_label is not None else ""

    def _idle_pause_texts(self) -> None:
        paused = self.idle_pause_action.isChecked()
        self.idle_pause_action.setText(t("interest.idle.pause"))
        self.idle_pause_action.setToolTip(t("interest.idle.paused_tip") if paused else t("interest.idle.pause_tip"))
        if self.idle_button is not None:
            self.idle_button.setChecked(paused)
            self.idle_button.setText(t("background.resume") if paused else t("background.pause"))
            self.idle_button.setToolTip(self.idle_pause_action.toolTip())

    def set_idle_paused(self, paused: bool) -> None:
        """The pause switch (status bar button and Tools menu): persistent."""
        paused = bool(paused)
        if self.idle_pause_action.isChecked() != paused:
            self.idle_pause_action.setChecked(paused)   # comes back here through `toggled`
            return
        if bool(getattr(self._settings, "idle_paused", False)) != paused:
            self._settings.idle_paused = paused
            self._save_settings()
            log.info("Idle analysis %s by the user", "paused" if paused else "resumed")
        if self.idle_scheduler is not None:
            self.idle_scheduler.set_paused(paused)
            self._idle_show(self.idle_scheduler.status())
        self._idle_pause_texts()

    # --- queue ----------------------------------------------------------------------------------------

    def interest_enqueue(self, rows) -> int:
        """Queues companies for the idle analysis (see the module docstring)."""
        scheduler = self.idle_scheduler
        if scheduler is None:
            return 0
        from .interest_flow import FlowRow, _host

        count = 0
        with scheduler.batch():   # one state save for the whole list (not one per company)
            count = self._interest_enqueue_rows(scheduler, rows, FlowRow, _host)
        if count:
            log.info("Idle analysis: %d companies queued", count)
            self._idle_show(scheduler.status())
        return count

    @staticmethod
    def _interest_enqueue_rows(scheduler, rows, FlowRow, _host) -> int:  # noqa: N803 (classes passed in)
        count = 0
        for row in rows or []:
            if isinstance(row, FlowRow):
                site = getattr(row.item, "site", None) or getattr(row.item, "website", None)
                if row.card_id is not None:
                    payload = {"card_id": int(row.card_id)}
                else:
                    to_dict = getattr(row.item, "to_dict", None)
                    payload = {"company": to_dict()} if callable(to_dict) else {"name": row.name}
                key, name, priority, host = row.key, row.name, row.score, _host(site)
            else:
                key, name, priority, payload, host = row
            if scheduler.enqueue(key, name, priority, payload, host):
                count += 1
        return count

    def _idle_run_job(self, job, ctx):
        """One idle job (scheduler thread): build the card, then ask for its affinity."""
        from .. import companies
        from ..background import JobResult
        from ..models import Company

        store = self._archive
        settings = self._settings
        payload = job.payload or {}
        card = companies.get(store, int(payload["card_id"])) if payload.get("card_id") else None
        if card is None and isinstance(payload.get("company"), dict):
            company = Company.from_dict(payload["company"])
            card = companies.find(store, company) or companies.save_from_result(store, company, None)
        if card is None:
            return JobResult(ok=False, error="company not found", retry=False)
        if ctx.cancel.is_set():
            return JobResult(ok=False, interrupted=True)
        builder = self.interest_card_builder
        requests = 0
        if builder is not None:
            builder([card.id], ctx.cancel, None)
        else:
            from ..web.fetch import Fetcher

            fetcher = Fetcher(settings, store)
            try:
                companies.build_card(store, card.id, fetcher, settings, cancel=ctx.cancel)
            finally:
                counter = getattr(fetcher, "requests_per_domain", None) or {}
                try:
                    requests = int(sum(counter.values()))
                except (TypeError, ValueError, AttributeError):
                    requests = 0
                fetcher.close()
        if requests:
            ctx.charge(job.host, requests)
        if ctx.cancel.is_set():
            return JobResult(ok=False, interrupted=True)
        card_id = card.id
        self._queue.put(("ok", self._idle_card_built, card_id))
        return JobResult(ok=True)

    def _idle_card_built(self, card_id: int) -> None:
        from .. import companies

        if self._closed:
            return
        card = companies.get(self._archive, card_id)
        if card is not None:
            self.affinity_service.request_cards([card])
        self._interest_cards_changed()

    def _idle_job_done(self, job, result) -> None:
        self._queue.put(("ok", lambda _v: self._interest_job_finished(), None))

    def _interest_job_finished(self) -> None:
        window = self._interest_open_window()
        if window is not None:
            window._mark_states()
            window._redraw()

    # --- persistence ------------------------------------------------------------------------------------

    def _idle_path(self) -> Path:
        from .. import config

        return Path(config.data_folder()) / STATE_FILE

    def _idle_load(self) -> dict | None:
        try:
            return json.loads(self._idle_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            log.warning("Idle analysis state not readable: starting with an empty queue")
            return None

    def _idle_state_changed(self, state: dict) -> None:
        with self._idle_lock:
            self._idle_state = state
            self._idle_dirty = True

    def _idle_save(self) -> None:
        with self._idle_lock:
            if not self._idle_dirty or self._idle_state is None:
                return
            state, self._idle_dirty = self._idle_state, False
        path = self._idle_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            log.warning("Idle analysis state not saved", exc_info=True)
