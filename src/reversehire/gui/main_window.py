"""Main window: CV -> profile (+ recruiter view) -> place -> results, plus the saved
companies ("My companies", favourites) and the previous searches.

Long work (reading the CV, geocoding, estimate, search, card building, model loading,
translation, clearing the cache) runs in worker threads; they talk to the GUI ONLY
through a `queue.Queue`, drained on the GUI thread by a QTimer (and by `process_queue`
in the tests). Search events (`Event`) come from the search thread the same way.
Every worker has a deadline (`_in_background(..., timeout=)`): when it passes, the
window stops waiting (the error callback gets a WorkerTimeout, the late result is
dropped, the job's cancel event is set) and the outcome of every worker is logged.

Every button of the tabs has a menu entry (gui/menus.py): the File, CV, Search,
Results, Companies, Tools and "?" menus share one QAction with each button.

Always visible at the bottom: the assistant strip (gui/chat_panel.py). "Write e-mail…"
(Results, My companies, Favourites, the company card) opens the mail dialog; File >
Export opens the preview of the company card or of the whole application.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import webbrowser
from dataclasses import replace
from typing import Callable

from PySide6.QtCore import QEvent, QLibraryInfo, Qt, QTimer, QTranslator
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMainWindow, QMenu, QProgressBar, QPushButton, QSplitter, QTabWidget,
    QToolButton, QVBoxLayout, QWidget,
)

from .. import companies, config, display_text, i18n, applog
from ..pipeline import (
    PROGRESS, ERROR, END, INFO, RESULT, DISCOVERY, COMPANY_ERROR, Event, SearchExecutor, estimate_from_count,
    open_store, search_parameters,
)
from ..geo.geocoding import Place
from ..models import CVProfile
from ..geo.sectors import sectors_for_profile
from ..geo.geocoding import CACHE_NAMESPACE as GEOCODING_CACHE
from ..geo.overpass import CACHE_NAMESPACE as OVERPASS_CACHE
from ..cv.esco import CACHE_NAMESPACE as ESCO_CACHE
from ..scoring.embedding import CACHE_NAMESPACE as EMBEDDING_CACHE
from ..web.fetch import CACHE_NAMESPACE as PAGES_CACHE
from ..i18n import t
from . import activity, hover, mail_support, safety, table_filter, theme
from .about_dialog import AboutDialog
from .chat_panel import ChatPanel
from .mail_dialog import MailDialog, draft_for, recipient_for
from .mcp_dialog import McpDialog
from .common import WorkerTimeout, cv_file_filter, error_message, format_km, is_cv_file, readable_duration
from .companies_panel import CompaniesPanel, FavouritesDialog
from .cv_analysis import CVAnalysis, analyse_cv, complete
from .cv_panel import CVPanel, version_title
from .detail_panel import DetailPanel
from .diagnostics_dialog import DiagnosticsDialog, ModelProblemDialog
from .history_panel import HistoryPanel
from .menus import MenuMirrors, bind_action
from .profile_panel import ProfilePanel
from .progress_bar import BarState, StatusProgress
from .recruiter_panel import RecruiterPanel
from ..progress import CVProgress, SearchProgress, remaining_minutes
from .results_table import ResultsTable, export_csv
from .search_panel import PARALLEL_DEFAULT, PARALLEL_MAX, PARALLEL_MIN, SearchPanel
from .settings_dialog import SettingsDialog
from .start_point_dialog import StartPointDialog
from .widgets import (Dialogs, ElidedLabel, LanguageChips, StatusDot, separator, star_icon, theme_icon,
                      wrapped_label)
from .window_features import FeaturesMixin
from .window_affinity import AffinityMixin
from .window_applications import ApplicationsMixin
from .window_interest import InterestMixin
from .window_registers import RegistersMixin

log = logging.getLogger(__name__)

QUEUE_INTERVAL_MS = 100
CLOSE_WAIT_S = 30.0
# Network caches only: imported company lists and discovery choices are user data.
CACHE_SPACES = (PAGES_CACHE, OVERPASS_CACHE, GEOCODING_CACHE, ESCO_CACHE, EMBEDDING_CACHE,
                "discovery_http", "overture")
# 1366 px: the width the ten tab labels are designed to fit (narrower windows get scroll arrows).
DEFAULT_SIZE = (1366, 820)
(TAB_PROFILE, TAB_RECRUITER, TAB_ANALYSIS, TAB_SEARCH, TAB_RESULTS, TAB_JOBS, TAB_COMPANIES, TAB_APPLICATIONS,
 TAB_HISTORY, TAB_MY_CV) = range(10)
# Feature mixins (gui/window_<name>.py): each may define _<name>_<hook> methods, called by _ext.
EXTENSIONS = ("affinity", "registers", "applications", "interest")
HELP_MENU_TITLE = "?"  # the same in every language
GENERIC_EVENT_KEY = "status.working"  # shown for pipeline events whose key has no text
# Long first searches: the confirmation offers to limit the search to this many
# companies (the most relevant first) when the estimate is long and no limit is set.
SUGGESTED_LIMIT = 100
# The current profile (CV analysed + the user's edits), restored at start-up.
PROFILE_NAMESPACE = "profile_current"
PROFILE_KEY = "current"
PROFILE_FORMAT = 1
NO_COMPANIES_KEYS = ("pipeline.no_companies_map", "pipeline.no_companies_no_site")
ESTIMATED_EVENT_KEY = "pipeline.estimated"   # estimated number of companies (progress bar)
MAP_INDEX_EVENT_KEY = "pipeline.map_index"   # local map data being prepared (percent)
REDUCED_EVENT_KEY = "pipeline.reduced_mode"
CHOICE_LIMIT, CHOICE_ALL, CHOICE_CANCEL = 0, 1, 2
# Worker deadlines (seconds): after them the window stops waiting (see the docstring).
DEFAULT_JOB_TIMEOUT_S = 600.0
CV_JOB_TIMEOUT_S = 900.0        # OCR of a long scanned CV
GEOCODE_TIMEOUT_S = 90.0
ESTIMATE_TIMEOUT_S = 900.0      # Overpass can be slow when busy
# The results table is redrawn at most once per ROWS_FLUSH_S during a search once it
# has ROWS_THROTTLE_FROM rows (each redraw costs time proportional to the rows).
ROWS_FLUSH_S = 1.0
ROWS_THROTTLE_FROM = 200


def elapsed_text(seconds: float) -> str:
    """"m:ss" (or "h:mm:ss") for the running search."""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def percent_text(fraction: float) -> str:
    """"42 %" on the progress bar (the same in every language)."""
    return f"{round(max(0.0, min(1.0, fraction)) * 100)} %"


HEADER_LOGO_MAX_HEIGHT = 40   # logical pixels: the header must not eat a small screen


def header_pixmap(pixmap):
    """The header logo, scaled down (smoothly) to HEADER_LOGO_MAX_HEIGHT."""
    if pixmap is None or pixmap.isNull():
        return pixmap
    ratio = pixmap.devicePixelRatio() or 1.0
    if pixmap.height() / ratio <= HEADER_LOGO_MAX_HEIGHT:
        return pixmap
    scaled = pixmap.scaledToHeight(int(HEADER_LOGO_MAX_HEIGHT * ratio), Qt.TransformationMode.SmoothTransformation)
    scaled.setDevicePixelRatio(ratio)
    return scaled


def initial_size(minimum, available) -> tuple[int, int]:
    """DEFAULT_SIZE, at least the minimum size, but never larger than the screen's
    available area (a window larger than the screen hides its right and bottom parts)."""
    width = max(DEFAULT_SIZE[0], minimum.width())
    height = max(DEFAULT_SIZE[1], minimum.height())
    if available is not None and available.width() > 0 and available.height() > 0:
        width = min(width, available.width())
        height = min(height, available.height() - 32)   # the title bar
    return max(width, 200), max(height, 200)


def counts_text(done: int, total: int, estimated: bool) -> str:
    """"10/15" on the progress bar; "3/~15" while the total is an estimate."""
    return f"{done}/~{total}" if estimated else f"{done}/{total}"


class _CornerWidget(QWidget):
    """Menu-bar corner holder: follows its children's size (the menu bar itself does
    not re-layout a corner widget whose content grows, e.g. after the style is applied)."""

    def event(self, event):
        result = super().event(event)
        if event.type() == QEvent.Type.LayoutRequest:
            hint = self.sizeHint()
            if self.size() != hint:
                self.resize(hint)
                bar = self.parentWidget()
                if bar is not None and hasattr(bar, "setCornerWidget"):
                    bar.setCornerWidget(self, Qt.Corner.TopRightCorner)
        return result


class _DialogsProxy:
    """Forwards to the window's current `dialogs` (tests replace it after construction)."""

    def __init__(self, window):
        self._window = window

    def __getattr__(self, name):
        return getattr(self._window.dialogs, name)


def _extension(path: str) -> str:
    """".pdf" of a CV path (the log never shows the file name: it may contain a name)."""
    from pathlib import Path

    return Path(path).suffix.lower() or "no extension"


def _file_size(path: str) -> str:
    from pathlib import Path

    try:
        return f"{Path(path).stat().st_size // 1024} KB"
    except OSError:
        return "size unknown"


def parallel_setting(settings: config.Settings) -> int:
    value = getattr(settings, "parallel_companies", PARALLEL_DEFAULT)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return PARALLEL_DEFAULT
    return max(PARALLEL_MIN, min(PARALLEL_MAX, value))


class MainWindow(AffinityMixin, RegistersMixin, ApplicationsMixin, InterestMixin, FeaturesMixin, QMainWindow):
    def __init__(
        self,
        settings: config.Settings | None = None,
        archive=None,
        *,
        executor_factory: Callable | None = None,
        geocoder=None,
        cv_reader: Callable | None = None,
        opener: Callable = webbrowser.open,
        embedder_factory: Callable | None = None,
        cv_library=None,
    ):
        super().__init__()
        self._app = theme.ensure_application()
        from .. import store as store_module

        store_module.mark_gui_thread()  # its database waits are kept short
        self._settings = settings if settings is not None else config.load()
        applog.register_secret(self._settings.firecrawl_key)
        applog.register_secret(getattr(self._settings, "paid_registry_key", ""))
        self._owns_archive = archive is None
        self._archive = archive if archive is not None else open_store()
        self._executor_factory = executor_factory or (lambda cfg, store: SearchExecutor(cfg, store))
        self._geocoder = geocoder
        self._cv_reader = cv_reader
        self._opener = opener
        self._embedder_factory = embedder_factory
        self._cv_library = cv_library  # cv_library.CVLibrary, created on first use
        self._embedder = None
        self._embedder_lock = threading.Lock()
        self._cv_generation = 0
        self.dialogs = Dialogs(self)
        self._mirrors = MenuMirrors(self)

        self._queue: queue.Queue = queue.Queue()
        self._jobs: list[threading.Thread] = []
        self._job_info: dict[int, dict] = {}  # running workers: name, start, deadline, cancel
        self._job_counter = 0
        self._last_rows_flush = 0.0
        self._search_thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._current_search: int | None = None
        self._status_key, self._status_params = "status.ready", {}
        self._notice_text: str | None = None  # a safety-net notice shown instead of the status
        self._close_requested = False
        self._closed = False
        self._close_deadline = 0.0
        self._qt_translator: QTranslator | None = None
        self.settings_dialog: SettingsDialog | None = None
        self.start_dialog: StartPointDialog | None = None
        self.about_dialog: AboutDialog | None = None
        self.mcp_dialog: McpDialog | None = None
        self.favourites_dialog: FavouritesDialog | None = None
        self.diagnostics_dialog: DiagnosticsDialog | None = None
        self.model_dialog: ModelProblemDialog | None = None
        self.mail_dialog: MailDialog | None = None
        self.export_dialog = None  # export.ExportPreviewDialog
        self._cli_checker: Callable | None = None   # tests replace the assistant detection
        self._mail_system = None                    # tests replace the mail side effects
        self._model_problem: dict | None = None   # {"cause", "details"} of the current search
        self._empty_key: tuple[str, dict] | None = None  # why the results are empty
        self._search_started = 0.0
        self._estimate_token = 0  # bumped by Cancel during the estimate
        self._pending_rows: list = []  # results drained but not drawn yet
        self._diagnostics_runner: Callable | None = None  # tests replace the checks
        self._translating: set[int] = set()  # rows being translated for the display (id of the assessment)
        display_text.set_store(self._archive)

        self._effective_theme = theme.apply_theme(self._app, self._settings.theme)
        self._init_features()
        self._ext("init")
        self._header_pixmaps = {name: header_pixmap(theme.load_pixmap(f"header_{name}.png"))
                                for name in theme.PALETTES}
        self._build_menu()
        self._build_body()
        from .ocr_reader import AdvancedReader

        self.ocr_reader = AdvancedReader(self)
        self._populate_menus()
        self._apply_theme()
        self.retranslate()
        activity.install(self._app)  # every user action in the log
        table_filter.install(self._app)  # safety net: header-click sorting on every table
        hover.install(self._app)     # hover / keyboard-focus highlight and growth, app-wide
        safety.set_notice_handler(self.show_notice)
        self.profile_panel.on_profile_edited = self._profile_edited
        # a CV file dropped anywhere on the window (not only on tab 1) is analysed at once
        from .profile_boxes import FileDropFilter

        self.window_drop = FileDropFilter(self.load_cv, self)
        self.installEventFilter(self.window_drop)
        self.setAcceptDrops(True)
        self.restore_profile()
        self.history.load(self._archive)
        self._refresh_favourites()
        self._my_cv_changed()

        self._timer = QTimer(self)
        self._timer.setInterval(QUEUE_INTERVAL_MS)
        self._timer.timeout.connect(self.process_queue)
        self._timer.start()
        if display_text.interface_is_english():
            # the language detector loads its data once: not on the GUI thread
            self._in_background(display_text.warm_up, lambda _r: None, lambda _e: None,
                                name="language detector warm-up")
        self.resize(*initial_size(self.minimumSizeHint(), self._available_screen()))
        self._ext("started")
        self._map_warm_tries = 0
        from . import lazy_map

        if lazy_map.web_maps_enabled():
            QTimer.singleShot(lazy_map.WARM_UP_DELAY_MS, self._warm_up_map)

    def _warm_up_map(self) -> None:
        """Qt WebEngine's first start blocks the GUI thread for seconds (it must run there):
        done once at a quiet moment after start-up, not on the user's first map click."""
        from . import lazy_map

        if self._closed or lazy_map.web_engine_started():
            return
        activity = getattr(self, "_idle_filter", None)
        last = getattr(activity, "_last", 0.0) or 0.0
        busy = self.search_running or time.monotonic() - last < lazy_map.WARM_UP_QUIET_S
        if busy and self._map_warm_tries < lazy_map.WARM_UP_TRIES:
            self._map_warm_tries += 1
            QTimer.singleShot(int(lazy_map.WARM_UP_QUIET_S * 2000), self._warm_up_map)
            return
        if not busy:
            lazy_map.warm_up(self._settings)

    def _ext(self, hook: str, *args) -> list:
        """Calls _<extension>_<hook>(*args) of every feature mixin that defines it; a failing
        hook is logged and reported in the status bar, never raised (the window must stay usable)."""
        results = []
        for name in EXTENSIONS:
            method = getattr(self, f"_{name}_{hook}", None)
            if method is None:
                continue
            try:
                results.append(method(*args))
            except Exception:  # noqa: BLE001
                log.exception("Extension hook %s of %s failed", hook, name)
                safety.report(f"{name} {hook}")
        return results

    def _available_screen(self):
        """The available geometry (no task bar) of the screen the window opens on."""
        screen = self.screen() or QApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else None

    # ------------------------------------------------------------------ building

    def _build_menu(self) -> None:
        bar = self.menuBar()
        self.file_menu = bar.addMenu("")
        self.open_cv_action = QAction(self, triggered=lambda: self.choose_cv())
        self.open_cv_action.setShortcut(QKeySequence.StandardKey.Open)
        self.export_action = QAction(self, triggered=lambda: self.export())
        self.favourites_action = QAction(self, triggered=lambda: self.open_favourites())
        self.favourites_action.setShortcut(QKeySequence("Ctrl+Shift+F"))
        self.settings_action = QAction(self, triggered=lambda: self.open_settings())
        self.exit_action = QAction(self, triggered=self.close)
        self.exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        self.export_card_action = QAction(self, triggered=lambda: self.export_card())
        self.export_application_action = QAction(self, triggered=lambda: self.export_application())
        self.file_menu.addAction(self.open_cv_action)
        self.file_menu.addAction(self.export_action)
        self.export_menu = self.file_menu.addMenu("")
        self.export_menu.addAction(self.export_card_action)
        self.export_menu.addAction(self.export_application_action)
        self.export_menu.aboutToShow.connect(self._update_export_actions)
        self.file_menu.aboutToShow.connect(self._update_export_actions)
        self.file_menu.addAction(self.favourites_action)
        self.file_menu.addSeparator()
        self.file_menu.addAction(self.settings_action)
        self.file_menu.addSeparator()
        self.file_menu.addAction(self.exit_action)

        # One menu per area; filled with the tabs' buttons by _populate_menus.
        self.cv_menu = bar.addMenu("")
        self.search_menu = bar.addMenu("")
        self.results_menu = bar.addMenu("")
        self.jobs_menu = bar.addMenu("")  # Job search (filled by _feature_menus)
        self.companies_menu = bar.addMenu("")
        self.tools_menu = bar.addMenu("")
        # Dark <-> light: one toggle (menu bar corner and Tools); "system" stays in Settings.
        self.theme_toggle_action = QAction(self, triggered=lambda: self.toggle_theme())
        self.theme_toggle_action.setShortcut(QKeySequence("Ctrl+Shift+L"))
        self.mcp_action = QAction(self, triggered=lambda: self.open_mcp_dialog())
        self.tools_menu.addAction(self.theme_toggle_action)
        self.tools_menu.addAction(self.mcp_action)
        # "Clear filters" of the table in use (every table's chip runs the same action)
        self.assistant_menu = self.tools_menu.addMenu("")  # the assistant strip's buttons

        self.help_menu = bar.addMenu(HELP_MENU_TITLE)
        self.about_action = QAction(self, triggered=lambda: self.open_about())
        self.diagnostics_action = QAction(self, triggered=lambda: self.open_diagnostics())
        self.open_log_action = QAction(self, triggered=lambda: self.open_log_folder())
        self.help_menu.addAction(self.diagnostics_action)
        self.help_menu.addAction(self.open_log_action)
        self.help_menu.addSeparator()
        self.help_menu.addAction(self.about_action)

        # Always visible in the menu bar corner: the favourites button and the MCP
        # switch state (red dot = off, green dot = on, plus text).
        self.corner = _CornerWidget(bar)
        corner_layout = QHBoxLayout(self.corner)
        corner_layout.setContentsMargins(0, 0, 0, 0)
        corner_layout.setSpacing(0)
        self.favourites_button = QPushButton(self.corner)
        self.favourites_button.setObjectName("favouritesButton")
        self.favourites_button.setFlat(True)
        self.favourites_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.favourites_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.favourites_button.clicked.connect(lambda: self.open_favourites())
        self.mcp_indicator = StatusDot(self.corner)
        self.mcp_indicator.clicked.connect(lambda: self.open_mcp_dialog())
        self.mcp_indicator.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # no focus ring at start-up
        # Interface language: three small chips instead of a "Language" menu.
        codes = i18n.available_languages()
        self.language_chips = LanguageChips(codes, {c: i18n.language_name(c) for c in codes},
                                            self.change_language, self.corner)
        # Sun / moon next to the language chips: switches dark <-> light (same action as Tools).
        self.theme_button = QToolButton(self.corner)
        self.theme_button.setObjectName("themeButton")
        self.theme_button.setDefaultAction(self.theme_toggle_action)
        self.theme_button.setAutoRaise(True)
        self.theme_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.theme_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        corner_layout.addWidget(self.language_chips)
        corner_layout.addWidget(self.theme_button)
        corner_layout.addWidget(self.favourites_button)
        corner_layout.addWidget(self.mcp_indicator)
        bar.setCornerWidget(self.corner, Qt.Corner.TopRightCorner)

    def _populate_menus(self) -> None:
        """Puts every button of the tabs in the menu bar (one shared QAction each)."""
        mirror = self._mirrors.mirror
        profile, search, cards = self.profile_panel, self.search_panel, self.companies_panel
        card, cv, history, detail = self.companies_panel.card_view, self.cv_panel, self.history, self.detail
        bind_action(profile.cv_button, self.open_cv_action)
        bind_action(self.favourites_button, self.favourites_action)
        bind_action(self.mcp_indicator, self.mcp_action)
        bind_action(self.theme_button, self.theme_toggle_action)
        # "Open CV…" (above) starts the analysis at once; "Read again" analyses the same file again
        self.read_again_action = mirror(profile.reread_button)
        self.file_menu.insertAction(self.export_action, self.read_again_action)

        for button in (profile.text_button, profile.home_box.use_button, profile.offer_add_button):
            self.cv_menu.addAction(mirror(button))
        self.cv_menu.addAction(profile.undo_action)  # Ctrl+Z on tab 1; the undo button runs it
        bind_action(profile.undo_button, profile.undo_action)
        self.cv_menu.addSeparator()
        self.my_cv_menu = self.cv_menu.addMenu("")
        for button in (cv.add_button, cv.replace_button, cv.remove_button, cv.primary_button, cv.use_button,
                       cv.open_button, cv.folder_button, cv.save_photo_button, cv.compare_button):
            self.my_cv_menu.addAction(mirror(button))
        self.my_cv_menu.addSeparator()
        self.my_cv_menu.addAction(mirror(cv.viewer.zoom_out_button, lambda: t("mycv.zoom_out")))
        self.my_cv_menu.addAction(mirror(cv.viewer.zoom_in_button, lambda: t("mycv.zoom_in")))
        self.my_cv_menu.addAction(mirror(cv.viewer.fit_button))

        for button in (search.check_button, search.start_point_button, search.start_button, self.cancel_button):
            self.search_menu.addAction(mirror(button))
        self.search_menu.addSeparator()
        for button in (search.map_button, search.import_button, search.register_button):
            self.search_menu.addAction(mirror(button))

        for button in (detail.site_button, detail.careers_button, detail.linkedin_button, detail.mail_button,
                       detail.ad_button):
            self.results_menu.addAction(mirror(button))
        self.results_menu.addSeparator()
        self.results_menu.addAction(mirror(self.why_button, lambda: t("menu.why_reduced")))
        self.results_menu.addAction(self.export_action)
        self.results_menu.addSeparator()
        for button in (history.open_button, history.resume_button, history.rename_button, history.delete_button,
                       history.delete_selected_button, history.refresh_button):
            self.results_menu.addAction(mirror(button))

        for button in (cards.add_button, cards.search_button):
            self.companies_menu.addAction(mirror(button))
        self.companies_menu.addSeparator()
        for button in (cards.save_button, cards.site_button, cards.linkedin_button, cards.similar_button,
                       cards.delete_button):
            self.companies_menu.addAction(mirror(button))
        self.companies_menu.addSeparator()
        for button in (card.build_button, card.mail_button, cards.batch_selected_button,
                       cards.batch_favourites_button, card.cancel_button, card.paid_button):
            self.companies_menu.addAction(mirror(button))
        # the card's "More" menu: the same actions, shown here too
        card_actions = (card.registro_open_action, card.registro_enter_action, card.register_action,
                        card.fin_sites_action, card.fin_paste_action, card.fin_import_action, card.fin_add_action)
        for action in card_actions:
            self.companies_menu.addAction(action)
        self.companies_menu.addMenu(card.refresh_menu)
        self.companies_menu.addAction(card.copy_action)
        self.companies_menu.addAction(card.export_action)
        for action in (*card_actions, card.copy_action, card.export_action, *card.refresh_actions.values()):
            action.triggered.connect(lambda _checked=False: self.tabs.setCurrentIndex(TAB_COMPANIES))

        chat = self.chat_panel
        self.assistant_menu.addAction(mirror(chat.toggle_button, lambda: chat.toggle_button.toolTip()))
        for button in (chat.send_button, chat.stop_button, chat.new_button):
            self.assistant_menu.addAction(mirror(button))
        self.assistant_menu.addSeparator()
        settings_action = mirror(chat.settings_button)
        self.assistant_menu.addAction(settings_action)
        bind_action(chat.hint_button, settings_action)  # the hint's button opens the same page
        detail.on_mail = lambda: self.open_mail_dialog(self.card_for_results())
        card.on_mail = lambda: self.open_mail_dialog(card.card)
        # the company's links (website, social networks): submenus holding the buttons' own actions
        self.results_menu.addSeparator()
        self.results_menu.addMenu(detail.social_bar.menu)
        self.companies_menu.addSeparator()
        self.companies_menu.addMenu(card.social_bar.menu)
        detail.saved_card = lambda company: companies.find(self._archive, company)
        # Country -> Region -> Province of each list: the buttons show these same actions
        self.geo_menus = {}
        for menu, owner, key in ((self.companies_menu, cards, "companies"), (self.results_menu, self.results_table,
                                 "results"), (self.results_menu, history, "history"),
                                (self.jobs_menu, self.jobs_panel, "jobs")):
            submenu = menu.addMenu("")
            submenu.addActions(owner.geo_filter.actions_for_menu())
            self.geo_menus[key] = submenu

        for menu in (self.file_menu, self.cv_menu, self.my_cv_menu, self.search_menu, self.results_menu,
                     self.companies_menu, self.tools_menu, self.help_menu, self.assistant_menu, self.export_menu):
            menu.aboutToShow.connect(self._mirrors.refresh)
        self._feature_menus(mirror)
        self.ocr_reader.add_menu(mirror)
        self._ext("menus", mirror)
        self._install_context_menus()

    def _install_context_menus(self) -> None:
        """Right-click menus of Results and My companies: the actions of their buttons
        (the same QActions as the menu bar); Delete removes a saved company (confirmed)."""
        from . import context_menu

        detail, cards, card = self.detail, self.companies_panel, self.companies_panel.card_view
        context_menu.install_buttons(self.results_table.table, lambda: [
            detail.site_button, detail.careers_button, detail.linkedin_button, None, detail.mail_button,
            detail.ad_button, detail.simulate_button])
        context_menu.install_buttons(cards.table, lambda: [
            card.build_button, card.mail_button, None, cards.site_button, cards.linkedin_button,
            cards.similar_button, None, cards.save_button, cards.delete_button], delete_button=cards.delete_button)

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(10, 6, 10, 6)
        self.header_logo = QLabel()
        self.header_logo.setProperty("role", "title")
        self.subtitle_label = QLabel()
        self.subtitle_label.setProperty("role", "muted")
        layout.addWidget(self.header_logo)
        layout.addSpacing(14)
        layout.addWidget(self.subtitle_label)
        layout.addStretch(1)
        return header

    def _build_bottom_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("bottomBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 4, 10, 4)
        self.status_label = ElidedLabel()
        self.progress = QProgressBar()
        self.progress.setMinimumWidth(self.fontMetrics().averageCharWidth() * 26)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("%v / %m")
        self.progress.setTextVisible(False)
        # One bar for every producer (CV analysis, search): see gui/progress_bar.py.
        self.status_progress = StatusProgress(self.progress)
        self._search_progress: SearchProgress | None = None
        self._cv_progress: CVProgress | None = None
        self.cancel_button = QPushButton()
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_search)
        self.osm_label = QLabel()
        self.osm_label.setProperty("role", "muted")
        self.elapsed_label = QLabel()  # time since the search started
        self.elapsed_label.setProperty("role", "muted")
        layout.addWidget(self.status_label, 1)
        layout.addWidget(self.elapsed_label)
        layout.addWidget(self.progress)
        layout.addWidget(self.cancel_button)
        layout.addWidget(self.osm_label)
        return bar

    def _build_body(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())
        outer.addWidget(separator())

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(False)
        # Ten tabs must stay readable at 1366 px: short labels (the full name in the tooltip)
        # and, on a narrower window or a larger font, scroll arrows instead of cut labels.
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideNone)
        self.profile_panel = ProfilePanel(lambda: self.choose_cv(), self._profile_changed,
                                          on_file_dropped=self.load_cv, on_use_home=self.use_home,
                                          on_read_again=lambda: self.read_cv_again(),
                                          on_add_version=lambda path, lang: self.add_cv_version(path, lang))
        self.recruiter_panel = RecruiterPanel()
        self.search_panel = SearchPanel(
            self.check_place, self.start, self.open_start_point_dialog, self._parallel_changed,
            initial_radius=self._settings.radius_km, initial_parallel=parallel_setting(self._settings),
            store=self._archive, settings=self._settings,
            on_settings_changed=lambda _settings: self._save_settings(),
        )
        results = QWidget()
        results_layout = QVBoxLayout(results)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.results_table = ResultsTable(self._row_selected, on_star=self.toggle_favourite)
        self.detail = DetailPanel(opener=self._opener)
        self.splitter.addWidget(self._build_results_view())
        self.splitter.addWidget(self.detail)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.search_info_label = wrapped_label(role="muted")
        # Phase of the running search (what it is doing, elapsed time), above the table.
        self.phase_label = wrapped_label(role="muted")
        self.phase_label.hide()
        # Reduced-accuracy notice (no scoring model), with "Why?".
        self.reduced_notice = QWidget()
        notice_layout = QHBoxLayout(self.reduced_notice)
        notice_layout.setContentsMargins(0, 0, 0, 0)
        self.reduced_label = wrapped_label(role="warning")
        self.why_button = QPushButton()
        self.why_button.setFlat(True)
        self.why_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.why_button.clicked.connect(lambda: self.show_model_problem())
        notice_layout.addWidget(self.reduced_label, 1)
        notice_layout.addWidget(self.why_button)
        self.reduced_notice.hide()
        # Why there are no results (no companies found, all failed), instead of an empty table.
        self.empty_label = wrapped_label(role="warning")
        self.empty_label.hide()
        results_layout.addWidget(self.phase_label)
        results_layout.addWidget(self.reduced_notice)
        results_layout.addWidget(self.empty_label)
        results_layout.addWidget(self.splitter, 1)
        results_layout.addWidget(self.search_info_label)
        self.results_tab = results
        self.history = HistoryPanel(self.open_search, self.resume_search, self.delete_search)
        self.history.set_refresh(lambda: self.history.load(self._archive))
        self.history.on_delete_many = self.delete_searches
        self.companies_panel = CompaniesPanel(
            self._archive, self._in_background, self._get_embedder, _DialogsProxy(self),
            opener=self._opener, on_changed=self._refresh_favourites,
        )
        self.cv_panel = CVPanel(self.cv_library, self._in_background, _DialogsProxy(self),
                                on_use=self.load_cv, opener=self._opener, store=self._archive,
                                settings=self._settings, on_changed=self._my_cv_changed,
                                folder_opener=lambda folder: self.open_folder(folder))
        self._build_search_map()
        for widget in (self.profile_panel, self.recruiter_panel, self._build_analysis_panel(), self.search_panel,
                       results, self._build_jobs_panel(), self.companies_panel, self._build_applications_panel(),
                       self.history, self.cv_panel):
            self.tabs.addTab(widget, "")
        self._ext("build")

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(8, 8, 8, 4)
        body_layout.addWidget(self.tabs, 1)
        self.warning_label = wrapped_label(role="warning")
        body_layout.addWidget(self.warning_label)
        # One notice when texts in another language are shown untranslated (English interface).
        # One line (full text in the tooltip): a wrapped notice made the window's minimum
        # height grow past a small laptop screen whenever it appeared.
        self.translation_notice = ElidedLabel()
        theme.set_role(self.translation_notice, "muted")
        self.translation_notice.hide()
        body_layout.addWidget(self.translation_notice)
        outer.addWidget(body, 1)
        outer.addWidget(separator())
        # The assistant strip: always visible, collapsed to one row by default.
        self.chat_panel = ChatPanel(lambda: self._settings, self._in_background, save_settings=self._save_settings,
                                    on_open_settings=lambda: self.open_settings("assistant"),
                                    mcp_enabled=lambda: self.mcp_enabled,
                                    cli_checker=lambda kind, override: self._check_cli(kind, override))
        outer.addWidget(self.chat_panel)
        outer.addWidget(separator())
        outer.addWidget(self._build_bottom_bar())
        self.setCentralWidget(central)
        self._update_requirements()

    # ------------------------------------------------------------------ language

    def change_language(self, code: str) -> None:
        """Changes the language of this session without restarting and redraws the texts.

        It is saved as the session language (the MCP server and the assistant follow
        it) but the next start begins in the start-up language (English by default,
        Settings -> General): see app.start_language."""
        i18n.set_language(code)
        display_text.clear_missing()
        self._settings.language = code
        self._save_settings()
        self.retranslate()

    def _install_qt_translator(self) -> None:
        """Qt's own texts (context menus, file dialog) in the current language."""
        if self._qt_translator is not None:
            self._app.removeTranslator(self._qt_translator)
            self._qt_translator = None
        code = i18n.current_language()
        if code == "en":
            return
        translator = QTranslator(self)
        folder = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        if translator.load(f"qtbase_{code}", folder):
            self._app.installTranslator(translator)
            self._qt_translator = translator

    # ------------------------------------------------------------------ theme

    def change_theme(self, name: str) -> None:
        """Changes theme without restarting, saves it and repaints everything (open dialogs too)."""
        if name not in theme.THEMES:
            name = config.DEFAULT_THEME
        log.info("Theme changed to %s", name)
        self._settings.theme = name
        self._save_settings()
        self._apply_theme()

    def toggle_theme(self) -> None:
        """The sun/moon button: dark <-> light (from "system", the opposite of what is shown)."""
        self.change_theme("light" if self._effective_theme == "dark" else "dark")

    def _update_theme_toggle(self) -> None:
        palette = theme.PALETTES[self._effective_theme]
        to_light = self._effective_theme == "dark"
        text = t("theme.switch_to_light") if to_light else t("theme.switch_to_dark")
        self.theme_toggle_action.setText(text)
        self.theme_toggle_action.setToolTip(text)
        self.theme_toggle_action.setIcon(theme_icon(palette.muted, sun=to_light))
        self.theme_button.setAccessibleName(text)

    def _apply_theme(self) -> None:
        self._effective_theme = theme.apply_theme(self._app, self._settings.theme)
        palette = theme.PALETTES[self._effective_theme]
        self.results_table.apply_palette(palette)
        self.detail.apply_palette(palette)
        self.recruiter_panel.apply_palette(palette)
        self.companies_panel.apply_palette(palette)
        self.cv_panel.apply_palette(palette)
        self.chat_panel.apply_palette(palette)
        self._feature_theme(palette)
        self._ext("theme", palette)
        if self.settings_dialog is not None:
            self.settings_dialog.assistant_page.apply_palette(palette)
        if self.favourites_dialog is not None:
            self.favourites_dialog.apply_palette(palette)
        self.mcp_indicator.set_colours(*theme.status_colours(palette))
        self.language_chips.apply_palette(palette)
        self.favourites_button.setIcon(star_icon(palette.warning, True))
        self._update_theme_toggle()
        if self.mcp_dialog is not None:
            self.mcp_dialog.apply_palette(palette)
        if self.about_dialog is not None:
            self.about_dialog.set_theme(self._effective_theme)
        pixmap = self._header_pixmaps.get(self._effective_theme)
        if pixmap is not None:
            self.header_logo.setPixmap(pixmap)
            self.header_logo.setToolTip(t("app.title"))
        else:
            self.header_logo.setPixmap(QPixmap())
            self.header_logo.setText(t("app.title"))

    @property
    def effective_theme(self) -> str:
        return self._effective_theme

    def header_has_image(self) -> bool:
        pixmap = self.header_logo.pixmap()
        return pixmap is not None and not pixmap.isNull()

    # ------------------------------------------------------------------ texts

    def retranslate(self) -> None:
        self._install_qt_translator()
        self.setWindowTitle(t("app.title"))
        self.language_chips.set_current(i18n.current_language())
        self.language_chips.setAccessibleName(t("menu.language"))
        self.file_menu.setTitle(t("menu.file"))
        self.cv_menu.setTitle(t("menu.cv"))
        self.my_cv_menu.setTitle(t("tab.my_cv"))
        self.search_menu.setTitle(t("menu.search"))
        self.results_menu.setTitle(t("menu.results"))
        self.companies_menu.setTitle(t("menu.companies"))
        self.tools_menu.setTitle(t("menu.tools"))
        self.mcp_action.setText(t("menu.mcp"))
        self.assistant_menu.setTitle(t("menu.assistant"))
        self.export_menu.setTitle(t("export.menu"))
        self.export_card_action.setText(t("export.menu_card"))
        self.export_application_action.setText(t("export.menu_application"))
        self.help_menu.setTitle(HELP_MENU_TITLE)
        self.about_action.setText(t("menu.about"))
        self.diagnostics_action.setText(t("menu.diagnostics"))
        self.open_log_action.setText(t("menu.open_log_folder"))
        self.why_button.setText(t("notice.why"))
        self._update_notices()
        self._update_mcp_indicator()
        self._update_theme_toggle()
        self.open_cv_action.setText(t("menu.open_cv"))
        self.export_action.setText(t("menu.export_csv"))
        self.favourites_action.setText(t("menu.favourites"))
        self.favourites_button.setText(t("favourites.button"))
        self.favourites_button.setToolTip(t("favourites.tooltip"))
        self.settings_action.setText(t("menu.settings"))
        self.exit_action.setText(t("menu.exit"))
        self.subtitle_label.setText(t("app.subtitle"))
        if not self.header_has_image():
            self.header_logo.setText(t("app.title"))
        self.tabs.setTabText(TAB_PROFILE, t("tab.profile"))
        self.tabs.setTabText(TAB_RECRUITER, t("tab.recruiter"))
        self.tabs.setTabText(TAB_ANALYSIS, t("tab.analysis"))
        self.tabs.setTabText(TAB_SEARCH, t("tab.place"))
        self.tabs.setTabText(TAB_RESULTS, t("tab.results"))
        self.tabs.setTabText(TAB_JOBS, t("tab.jobs"))
        self.tabs.setTabText(TAB_COMPANIES, t("tab.companies"))
        self.tabs.setTabText(TAB_APPLICATIONS, t("tab.applications"))
        self.tabs.setTabText(TAB_HISTORY, t("tab.history"))
        self.tabs.setTabText(TAB_MY_CV, t("tab.my_cv"))
        for index in range(self.tabs.count()):
            self.tabs.setTabToolTip(index, self.tabs.tabText(index))
        self.osm_label.setText(t("attribution.osm"))
        self.warning_label.setText(t("notice.incomplete_listing"))
        self.cancel_button.setText(t("common.cancel"))
        self.status_label.setText(self._notice_text or t(self._status_key, **self._status_params))
        self._update_search_label()
        self.profile_panel.retranslate()
        self.search_panel.retranslate()
        self._update_requirements()
        self.results_table.retranslate()
        self.detail.retranslate()
        self.history.retranslate()
        self.recruiter_panel.retranslate()
        self.companies_panel.retranslate()
        self.cv_panel.retranslate()
        self.chat_panel.retranslate()
        self._feature_retranslate()
        self._ext("retranslate")
        for key, submenu in getattr(self, "geo_menus", {}).items():
            submenu.setTitle(t(f"geo.menu.{key}"))
        if getattr(self, "ocr_reader", None) is not None:
            self.ocr_reader.retranslate()
        self._my_cv_changed()
        self.update_translation_notice()
        if self.start_dialog is not None:
            self.start_dialog.retranslate()
        for dialog in (self.about_dialog, self.mcp_dialog, self.favourites_dialog):
            if dialog is not None:
                dialog.retranslate()
        self._mirrors.refresh()

    def _set_status(self, key: str, **params) -> None:
        if t(key) == key:  # e.g. a pipeline event key this version has no text for
            log.debug("No text for status key %s", key)
            key, params = GENERIC_EVENT_KEY, {}
        self._status_key, self._status_params = key, params
        self._notice_text = None
        self.status_label.setText(t(key, **params))

    def status_text(self) -> str:
        return self.status_label.text()

    def show_notice(self, text: str) -> None:
        """A non-blocking notice in the status bar (errors caught by gui.safety)."""
        if self._closed:
            return
        self._notice_text = text
        self.status_label.setText(text)
        self.status_label.setToolTip(text)

    def _update_search_label(self) -> None:
        if self._current_search is None:
            self.search_info_label.setText(t("results.no_search"))
            return
        r = self._archive.read_search(self._current_search)
        if r is None:
            self.search_info_label.setText(t("results.no_search"))
            return
        text = t(
            "results.search", id=r["id"], place=r["place"] or "",
            radius=f"{r['radius_km']:g}", status=t(f"search.status.{r['status']}"),
        )
        if search_parameters(self._archive, r["id"]).get("reduced"):
            text += " " + t("results.reduced")
        self.search_info_label.setText(text)

    def _update_notices(self) -> None:
        """Reduced-accuracy notice and empty-results message, in the current language."""
        if self._model_problem is not None:
            reason = t(f"model_cause.{self._model_problem.get('cause', 'unknown')}.short")
            self.reduced_label.setText(t("notice.reduced_accuracy", reason=reason))
            self.reduced_notice.show()
        else:
            self.reduced_notice.hide()
        if self._empty_key is not None:
            key, params = self._empty_key
            self.empty_label.setText(f"{t('results.no_companies_title')}. {t(key, **params)}")
            self.empty_label.show()
        else:
            self.empty_label.hide()

    def _update_phase(self) -> None:
        """The phase label of the Results tab and the elapsed time in the status bar."""
        if not self.search_running and not self._search_started:
            return
        elapsed = elapsed_text(time.monotonic() - self._search_started) if self._search_started else ""
        remaining = self.remaining_text()
        self.elapsed_label.setText(f"{elapsed} · {remaining}" if elapsed and remaining else elapsed)
        if self.search_running:
            self.phase_label.setText(t("status.elapsed", text=self.status_text(), elapsed=elapsed))
            self.phase_label.show()

    @property
    def reduced_accuracy(self) -> bool:
        """True when the shown search ran without the scoring model."""
        return self._model_problem is not None

    def empty_text(self) -> str:
        return self.empty_label.text() if self._empty_key is not None else ""

    # ------------------------------------------------------------------ queue and threads

    def _in_background(self, function: Callable, on_success: Callable, on_error: Callable | None = None, *,
                       name: str | None = None, timeout: float | None = None,
                       cancel: threading.Event | None = None) -> int:
        """Runs `function` in a thread; the result comes back to the GUI through the queue.

        After `timeout` seconds (DEFAULT_JOB_TIMEOUT_S; 0 = none) the window stops
        waiting: `cancel` is set, `on_error` gets a WorkerTimeout and a late result is
        dropped. Start, outcome and duration are logged. Returns the job id."""
        self._job_counter += 1
        job_id = self._job_counter
        label = name or getattr(function, "__qualname__", "") or "job"
        limit = DEFAULT_JOB_TIMEOUT_S if timeout is None else timeout
        started = time.monotonic()
        self._job_info[job_id] = {"name": label, "started": started, "cancel": cancel,
                                  "deadline": started + limit if limit else None, "limit": limit,
                                  "on_error": on_error or self._show_error, "expired": False}

        def job():
            try:
                result = function()
            except Exception as error:
                log.warning("Worker failed: %s (%.1f s)", label, time.monotonic() - started, exc_info=True)
                self._queue.put(("job", job_id, on_error or self._show_error, error))
            else:
                self._queue.put(("job", job_id, on_success, result))

        thread = threading.Thread(target=job, daemon=True, name="gui-job")
        self._jobs = [th for th in self._jobs if th.is_alive()] + [thread]
        log.info("Worker started: %s", label)
        thread.start()
        return job_id

    def _job_done(self, job_id: int) -> bool:
        """True when the job's result is still awaited (logs the outcome)."""
        info = self._job_info.pop(job_id, None)
        if info is None:
            return True
        elapsed = time.monotonic() - info["started"]
        if info["expired"]:
            log.info("Late result of %s dropped (%.1f s)", info["name"], elapsed)
            return False
        log.info("Worker finished: %s (%.1f s)", info["name"], elapsed)
        return True

    def _check_deadlines(self) -> None:
        now = time.monotonic()
        for job_id, info in list(self._job_info.items()):
            if info["expired"] or info["deadline"] is None or now < info["deadline"]:
                continue
            info["expired"] = True
            log.warning("Worker timed out: %s (no answer after %.0f s): the window stops waiting",
                        info["name"], info["limit"])
            if info["cancel"] is not None:
                info["cancel"].set()
            try:
                info["on_error"](WorkerTimeout(info["name"], info["limit"]))
            except Exception:  # noqa: BLE001
                safety.report(f"timeout handler of {info['name']}")

    def running_jobs(self) -> list[str]:
        """Names of the workers whose result is still awaited."""
        return [info["name"] for info in self._job_info.values() if not info["expired"]]

    def process_queue(self) -> int:
        """Handles every waiting message (also called by the tests)."""
        handled = 0
        while not self._closed:
            try:
                kind, *data = self._queue.get_nowait()
            except queue.Empty:
                break
            handled += 1
            try:
                if kind != "event" or data[0].kind not in (RESULT, PROGRESS, INFO, DISCOVERY):
                    self._flush_rows()  # keep the order of rows and other messages
                if kind in ("ok", "error"):
                    callback, value = data
                    callback(value)
                elif kind == "job":
                    job_id, callback, value = data
                    if self._job_done(job_id):
                        callback(value)
                elif kind == "event":
                    self._handle_event(data[0])
                elif kind == "search_finished":
                    self._search_finished(data[0])
            except Exception:
                log.exception("Error while handling a queue message")
                self._set_status("error.generic")
        if not self._closed:
            if self._rows_due():
                self._flush_rows()
            self._check_deadlines()
            self._update_phase()
        if self._close_requested and not self._closed:
            self._check_close()
        return handled

    def _rows_due(self) -> bool:
        """Pending results are drawn now, or later when a big table was redrawn recently."""
        if not self._pending_rows:
            return False
        if len(self.results_table.rows) < ROWS_THROTTLE_FROM or not self.search_running:
            return True
        return time.monotonic() - self._last_rows_flush >= ROWS_FLUSH_S

    def _flush_rows(self) -> None:
        """Adds the results collected so far to the table with one redraw."""
        rows, self._pending_rows = self._pending_rows, []
        if rows:
            self.results_table.add_rows(rows)
            self._last_rows_flush = time.monotonic()
            self._results_changed()

    def wait_jobs(self, timeout: float = 10.0) -> None:
        """Waits for the worker threads and drains the queue (useful in tests)."""
        deadline = time.monotonic() + timeout
        while True:
            alive = [th for th in self._jobs if th.is_alive()]
            if self._search_thread is not None and self._search_thread.is_alive():
                alive.append(self._search_thread)
            for th in alive:
                th.join(max(0.0, deadline - time.monotonic()))
            self.process_queue()
            still = [th for th in self._jobs if th.is_alive()]
            if self._search_thread is not None and self._search_thread.is_alive():
                still.append(self._search_thread)
            if not still or time.monotonic() >= deadline:
                self.process_queue()
                return

    def _show_error(self, error: BaseException) -> None:
        self._set_status("status.ready")
        self.dialogs.error(error_message(error))

    # ------------------------------------------------------------------ steps 1-2: CV

    def choose_cv(self, path: str | None = None) -> None:
        if path is None:
            path = self.dialogs.open_file(t("profile.choose_cv"), cv_file_filter())
        if not path:
            return
        self.load_cv(path)

    def load_cv(self, path: str, advanced: str | None = None) -> None:
        """Reads and analyses the CV in a thread (profile, recruiter view, AI-style
        signals); one active CV at a time: a newer reading wins over an older one.
        `advanced`: "force" reads the scan again with the advanced OCR reader
        (default: as the settings say, see cv_analysis.advanced_mode)."""
        if not is_cv_file(path) and self._cv_reader is None:
            self.dialogs.error(t("error.cv_format"))
            return
        previous = getattr(self, "_cv_cancel", None)
        if previous is not None and not previous.is_set() and self._cv_progress is not None:
            previous.set()  # the older reading stops; its late result is ignored (generation)
            log.info("CV analysis cancelled: another file was chosen")
            self._finish_cv_progress()
        self._cv_generation += 1
        generation = self._cv_generation
        log.info("CV file chosen (%s, %s)", _extension(path), _file_size(path))
        self.profile_panel.show_reading(path)
        self._set_status("status.reading_cv")
        self.tabs.setCurrentIndex(TAB_PROFILE)
        reader = self._cv_reader or self._default_reader
        store, settings = self._archive, self._settings
        self._cv_progress = CVProgress()
        step = self._cv_step_callback(generation)
        self._cv_step(generation, "read", 0, 0)
        cancel = threading.Event()
        self._cv_cancel = cancel

        def work():
            if reader == self._default_reader:
                result = reader(path, step, advanced=advanced, cancel=cancel)
            else:
                result = reader(path)
            return complete(result, store, settings, step=step)

        self._in_background(work, lambda result: self._cv_read(result, generation),
                            lambda error: self._cv_not_read(error, generation), name="CV analysis",
                            timeout=CV_JOB_TIMEOUT_S, cancel=cancel)

    def _cv_step_callback(self, generation: int) -> Callable[[str, int, int], None]:
        """The progress callback handed to the CV worker: its calls reach the GUI
        thread through the queue (never touching a widget from the worker)."""
        def step(key: str, done: int = 0, total: int = 0) -> None:
            self._queue.put(("ok", lambda value: self._cv_step(*value), (generation, key, done, total)))

        return step

    def _default_reader(self, path: str, step=None, advanced: str | None = None, cancel=None) -> CVAnalysis:
        def progress(done: int, total: int) -> None:
            self._queue.put(("ok", lambda value: self.profile_panel.show_ocr_progress(*value), (done, total)))

        return analyse_cv(path, self._archive, self._settings, progress=progress,
                          embedder=self._analysis_embedder(), step=step, advanced=advanced, cancel=cancel)

    def _cv_read(self, result, generation: int | None = None) -> None:
        if generation is not None and generation != self._cv_generation:
            return  # an older CV finished after a newer one was chosen
        analysis = result if isinstance(result, CVAnalysis) else CVAnalysis(profile=result)
        profile = analysis.profile
        self._finish_cv_progress(success=True)
        self.profile_panel.set_profile(profile, sectors_for_profile(profile), analysis.reading)
        self.profile_panel.set_photo(getattr(getattr(analysis, "photo", None), "png", None))
        self.recruiter_panel.set_results(analysis.review, analysis.signals, getattr(analysis, "estimate", None))
        try:
            self._feature_cv_read(analysis)
            self._ext("cv_read", analysis)
        except Exception:  # noqa: BLE001 - the new views must never stop the CV reading
            safety.report("recruiter analysis and match after the CV reading")
        estimate = getattr(analysis, "estimate", None)
        if estimate is not None:
            log.info("AI-writing estimate: %d%% (band %d-%d%%, backend %s, %d signals)", estimate.percent,
                     *estimate.band_percent, estimate.backend, len(estimate.signals))
        self._offer_my_cv(profile)
        self._set_status("status.cv_read")
        self.update_translation_notice()
        try:  # last: its notice ("read again with the advanced reader") stays visible
            self.ocr_reader.cv_read(analysis.reading)
        except Exception:  # noqa: BLE001 - the advanced reader's offer never stops the CV reading
            safety.report("advanced OCR reader after the CV reading")

    # ------------------------------------------------------------------ "My CV" library

    def cv_library(self):
        """The library of CV versions (in the data folder), created on first use."""
        if self._cv_library is None:
            from ..cv_library import CVLibrary

            self._cv_library = CVLibrary()
        return self._cv_library

    def _my_cv_changed(self) -> None:
        """The empty tab 1 points at the primary version of the library (My CV tab, "Use")."""
        try:
            primary = self.cv_library().primary()
        except Exception:
            log.warning("CV library not readable", exc_info=True)
            primary = None
        self.profile_panel.set_my_cv(version_title(primary) if primary is not None else None)

    def read_cv_again(self) -> bool:
        """"Read again": the current CV file is read and analysed again (on demand; the
        profile restored at start-up is not re-read by itself)."""
        from pathlib import Path

        profile = self.profile_panel.current_profile()
        path = getattr(profile, "cv_path", "") or ""
        if not path:
            return False
        if self._cv_reader is None and not Path(path).is_file():
            log.info("CV not read again: the file is no longer there")
            self.dialogs.warning(t("profile.read_again_missing", path=path))
            return False
        log.info("CV read again on request")
        self.load_cv(path)
        return True

    def add_cv_version(self, path: str, language: str):
        """Adds a CV chosen in tab 1 to "My CV" (the offer of the info bar)."""
        version = self.cv_panel.add_version(path, language)
        if version is not None:
            self._set_status("mycv.added", language=t(f"cv_language.{language}"))
        return version

    def _offer_my_cv(self, profile) -> None:
        """A real CV file not in the library: offer to add it (non-blocking info bar)."""
        from pathlib import Path

        path = getattr(profile, "cv_path", "") or ""
        try:
            if not path or not Path(path).is_file() or self.cv_library().find_file(path) is not None:
                self.profile_panel.hide_offer()
                return
        except Exception:
            log.warning("CV library not readable", exc_info=True)
            return
        self.profile_panel.offer_add(path, getattr(profile, "language", "en") or "en")

    def _cv_not_read(self, error: BaseException, generation: int | None = None) -> None:
        if generation is not None and generation != self._cv_generation:
            return
        self._finish_cv_progress()
        self.profile_panel.reading_done()
        self.profile_panel.retranslate()
        self._show_error(error)

    # ------------------------------------------------------------------ CV progress

    def _cv_step(self, generation: int, key: str, done: int, total: int) -> None:
        """A step of the CV analysis (from the worker, through the queue)."""
        if generation != self._cv_generation or self._cv_progress is None:
            return  # an older CV, or already finished
        progress = self._cv_progress
        progress.update(key, done, total)
        step = t(f"progress.cv.{progress.step}")
        page = progress.ocr_page
        if page is not None:
            key, params = "progress.cv.status_page", {"step": step, "page": page, "pages": progress.pages_total}
            text = t("progress.cv.bar_page", page=page, pages=progress.pages_total)
        else:
            key, params = "progress.cv.status", {"step": step, "index": progress.index, "count": progress.count}
            text = percent_text(progress.fraction)
        self._set_status(key, **params)
        self.status_progress.show("cv", BarState(progress.fraction, text, t(key, **params)))

    def _finish_cv_progress(self, success: bool = False) -> None:
        """Releases the bar. A CV read to the end leaves "100 %" on it (when nothing else
        runs), so that the last value seen is never the 90-something of the last step."""
        if self._cv_progress is not None:
            self._cv_progress = None
            final = self._search_final_state()
            if final is None and success:
                final = BarState(1.0, percent_text(1.0), t("status.cv_read"))
            self.status_progress.finish("cv", final)

    def cv_progress_state(self) -> BarState | None:
        """What the bar shows for the CV analysis (None when no CV is being read)."""
        return self.status_progress.state("cv")

    def use_home(self, home: str) -> None:
        """"Use as starting point": the home town goes into the location field of tab 2."""
        if not home:
            return
        self.search_panel.address_field.setText(home)
        self._set_status("status.home_used", place=home)

    def _update_requirements(self) -> None:
        """What tab 2 says about the profile: nothing, or the field that is really missing."""
        missing = self.profile_panel.missing_for_search()
        if missing:
            self.search_panel.set_requirements(t("place.profile_missing", field=t(f"launch.field.{missing[0]}")))
        else:
            self.search_panel.set_requirements("")

    def _profile_changed(self) -> None:
        """Every edit of the profile (cheap): the note of tab 2."""
        self._update_requirements()

    def _profile_edited(self, profile) -> None:
        """The edits paused (profile_panel.AUTOSAVE_MS): the other tabs follow the profile
        (ATECO proposals, match with the CV) and it is saved on a worker."""
        try:  # ATECO categories of interest proposed from the profile
            self.search_panel.set_profile(profile)
        except Exception:
            log.warning("ATECO categories not proposed", exc_info=True)
        try:  # only a real change: setting it again empties the match cache
            current = getattr(self.match_service, "_profile", None)
            if current is None or current.to_dict() != profile.to_dict():
                self.match_service.set_profile(profile)
        except Exception:  # noqa: BLE001 - the match follows at the next request
            log.warning("Match service not updated", exc_info=True)
        self.save_profile(profile)

    def save_profile(self, profile) -> int:
        """Stores `profile` (and the inferred sectors) as the current profile, on a worker."""
        archive = self._archive
        value = {"version": PROFILE_FORMAT, "profile": profile.to_dict(),
                 "inferred": self.profile_panel.inferred_sectors(), "saved": time.time()}

        def done(_result) -> None:
            if not self._closed:
                self.profile_panel.show_saved()

        def failed(_error) -> None:
            log.warning("Profile not saved")
            if not self._closed:
                self.profile_panel.show_not_saved()

        return self._in_background(lambda: archive.cache_write(PROFILE_NAMESPACE, PROFILE_KEY, value),
                                   done, failed, name="profile autosave")

    def restore_profile(self) -> bool:
        """The profile saved by the previous session (CV analysed and edited), at start-up."""
        try:
            value = self._archive.cache_read(PROFILE_NAMESPACE, PROFILE_KEY)
            if not isinstance(value, dict) or not isinstance(value.get("profile"), dict):
                return False
            profile = CVProfile.from_dict(value["profile"])
        except Exception:  # noqa: BLE001 - a damaged entry: start without a profile
            log.warning("Saved profile not readable", exc_info=True)
            return False
        self.profile_panel.set_profile(profile, list(value.get("inferred") or []), restored=True)
        try:
            self.search_panel.set_profile(profile)
            self._feature_cv_read(CVAnalysis(profile=profile))
            self._ext("cv_read", CVAnalysis(profile=profile))
        except Exception:  # noqa: BLE001 - the other views follow at the next edit
            safety.report("views after restoring the profile")
        log.info("Profile of the previous session restored (%d roles, %d skills)",
                 len(profile.roles), len(profile.skills))
        return True

    # ------------------------------------------------------------------ step 3: place

    def _create_geocoder(self):
        if self._geocoder is None:
            from ..geo.geocoding import Geocoder

            self._geocoder = Geocoder(self._settings, self._archive)
        return self._geocoder

    def check_place(self, text: str, then: Callable | None = None) -> None:
        if not text:
            self.search_panel.show_place(text, None)
            return
        self.search_panel.show_checking()
        geocoder = self._create_geocoder()

        def done(place):
            self.search_panel.show_place(text, place)
            if then is not None and place is not None:
                then(place)

        def failed(error):
            self.search_panel.show_place(text, None)
            self._show_error(error)

        self._in_background(lambda: geocoder.geocode(text), done, failed, name="geocoding",
                            timeout=GEOCODE_TIMEOUT_S)

    def open_start_point_dialog(self) -> StartPointDialog:
        """Opens the starting-point dialog (window-modal, not blocking)."""
        place = self.search_panel.valid_place()
        initial = (place.lat, place.lon) if place is not None else None
        dialog = StartPointDialog(self, opener=self._opener, initial=initial)
        dialog.accepted.connect(lambda: self._start_point_chosen(dialog))
        dialog.finished.connect(lambda _r: setattr(self, "start_dialog", None))
        self.start_dialog = dialog
        dialog.open()
        return dialog

    def _start_point_chosen(self, dialog: StartPointDialog) -> None:
        point = dialog.result_point
        if point is None:
            return
        name = t("start.name", lat=f"{point.lat:.5f}", lon=f"{point.lon:.5f}")
        self.search_panel.set_start_point(Place(lat=point.lat, lon=point.lon, name=name))

    def _parallel_changed(self, value: int) -> None:
        self._settings.parallel_companies = int(value)
        self._save_settings()

    # ------------------------------------------------------------------ step 4: start

    @property
    def search_running(self) -> bool:
        return self._search_thread is not None and self._search_thread.is_alive()

    def _profile_ready(self) -> bool:
        """The search needs an analysed CV with at least one role (no confirmation step);
        otherwise the missing field is named, with a button that jumps to it in tab 1.
        No profile value is ever invented."""
        panel = self.profile_panel
        missing = panel.missing_for_search()
        if not missing:
            if panel.save_pending:
                panel.save_now()  # the search uses (and stores) the latest edits
            return True
        field = missing[0]
        name = t(f"launch.field.{field}")
        log.info("Search not started: the profile has no %s", field)
        text = t("launch.profile_missing", field=name)
        if self._ask_choice(text, t("launch.title"), [t("launch.go_to_field", field=name), t("common.cancel")]) == 0:
            self.tabs.setCurrentIndex(TAB_PROFILE)
            panel.focus_field(field)
        return False

    def _ask_choice(self, text: str, title: str, labels: list[str]) -> int:
        """Index of the button chosen (the last one = cancel); fake dialogs answer through `ask`."""
        choose = getattr(self.dialogs, "choose", None)
        if choose is None and isinstance(self.dialogs, Dialogs):
            choose = self._choose_box
        if choose is not None:
            return choose(text, title, labels)
        return 0 if self.dialogs.ask(text, title) else len(labels) - 1

    def start(self) -> None:
        """"Start search". In radius mode the address is required; in province and
        region mode it is optional (without one, or when it is not found, the search
        starts from the province's / region's chief town). Every refusal is shown next
        to the button and written to the log."""
        panel = self.search_panel
        panel.show_start_problem("")
        if self.search_running:
            log.info("Start not taken: a search is already running")
            return
        if not self._profile_ready():
            return
        area = panel.area()
        radius = panel.radius()
        if radius is None:
            if area == "radius":
                self._refuse_start("invalid radius", t("launch.invalid_radius"), focus=panel.radius_field)
                return
            radius = float(getattr(self._settings, "radius_km", 0) or 15.0)
        text = panel.place_text()
        if area == "province" and not panel.discovery_choices().get("province"):
            # without a province the search would neither cover a province nor say which
            # radius it used: ask for it (the user may also choose "region" or "radius")
            self._refuse_start("province mode without a province", t("launch.province_required"),
                               focus=panel.province_combo)
            return
        self._settings.parallel_companies = panel.parallel()
        place = panel.valid_place()
        log.info("Start clicked: area %s, address %s, place %s", area, "given" if text else "empty",
                 "verified" if place is not None else "to check")
        if place is not None:
            self._estimate_and_confirm(place, radius)
            return
        if not text:
            # radius mode without an address: the radius around the chosen province's (or
            # region's) chief town, said next to the button; refused only when none is known
            self._start_from_area_centre(radius, area)
            return
        self._geocode_then_start(text, radius, area)

    def _refuse_start(self, reason: str, text: str, focus=None) -> None:
        log.info("Search not started: %s", reason)
        self.search_panel.show_start_problem(text)
        self.dialogs.warning(text)
        if focus is self.search_panel.address_field:
            self.search_panel.focus_address()
        elif focus is not None:
            focus.setFocus()

    def _area_centre_place(self, area: str) -> Place | None:
        from ..geo.regions import area_centre

        choices = self.search_panel.discovery_choices()
        if area == "radius":  # the province chosen in the panel (kept while disabled), else the region
            area = "province" if choices.get("province") else "region"
        centre = area_centre(area, choices.get("province", ""), choices.get("region", ""))
        if centre is None:
            return None
        return Place(lat=centre[0], lon=centre[1], name=centre[2])

    def _start_from_area_centre(self, radius: float, area: str, ignored: str = "") -> None:
        place = self._area_centre_place(area)
        if place is None:
            self._refuse_start(f"no centre known for the {area}", t("launch.place_required"),
                               focus=self.search_panel.address_field)
            return
        if area == "radius":
            note = t("launch.centre_radius", place=place.name, radius=format_km(radius))
        else:
            note = t(f"launch.centre_{area}", place=place.name)
        if ignored:
            note = t("launch.place_ignored", place=ignored) + " " + note
        log.info("Search start point: %s (%s)%s", "chief town, no address given" if area == "radius"
                 else f"centre of the {area}", place.name, ", address not found" if ignored else "")
        self.search_panel.show_start_problem(note)
        self._estimate_and_confirm(place, radius)

    def _geocode_then_start(self, text: str, radius: float, area: str) -> None:
        panel = self.search_panel
        panel.show_checking()
        geocoder = self._create_geocoder()

        def done(place) -> None:
            panel.show_place(text, place)
            if place is not None:
                self._estimate_and_confirm(place, radius)
            elif area in ("province", "region"):
                self._start_from_area_centre(radius, area, ignored=text)
            else:
                self._refuse_start("address not found", t("launch.place_not_found", place=text),
                                   focus=panel.address_field)

        def failed(error) -> None:
            panel.show_place(text, None)
            if area in ("province", "region"):
                log.info("Geocoding failed (%s): the area centre is used", type(error).__name__)
                self._start_from_area_centre(radius, area, ignored=text)
            else:
                self._refuse_start(f"geocoding failed ({type(error).__name__})",
                                   t("launch.place_not_found", place=text), focus=panel.address_field)

        self._in_background(lambda: geocoder.geocode(text), done, failed, name="geocoding",
                            timeout=GEOCODE_TIMEOUT_S)

    def _estimate_and_confirm(self, place, radius: float) -> None:
        profile = self.profile_panel.current_profile()
        sectors = self.profile_panel.chosen_sectors()
        urls = self.search_panel.manual_urls()
        executor = self._executor_factory(self._settings, self._archive)
        self._set_status("status.estimating")
        self.search_panel.enable_start(False)
        # Overpass can take minutes when busy: Cancel stops waiting for the estimate.
        self._estimate_token += 1
        token = self._estimate_token
        self.cancel_button.setEnabled(True)

        def current() -> bool:
            return token == self._estimate_token and not self.search_running

        def estimated(estimate):
            if not current():
                return  # cancelled meanwhile
            self.cancel_button.setEnabled(False)
            self.search_panel.enable_start(True)
            self._set_status("status.ready")
            choice = self._confirm_estimate(estimate, sectors, len(urls))
            if choice == CHOICE_LIMIT:
                limited = replace(self._settings, max_companies=SUGGESTED_LIMIT)
                self._start_search(self._executor_factory(limited, self._archive),
                                   profile, place, radius, urls, sectors)
            elif choice == CHOICE_ALL:
                self._start_search(executor, profile, place, radius, urls, sectors)

        def failed(error):
            if not current():
                return
            self.cancel_button.setEnabled(False)
            self.search_panel.enable_start(True)
            from ..pipeline import SearchError

            if isinstance(error, SearchError) and error.key == "error.overpass" and urls:
                # The map service is down, but the websites added by hand can be read.
                self._set_status("error.overpass")
                if self.dialogs.ask(f"{t('error.overpass')}\n\n{t('confirm.overpass_down_manual', n=len(urls))}",
                                    t("confirm.title")):
                    self._start_search(executor, profile, place, radius, urls, sectors)
                return
            self._show_error(error)

        self._in_background(lambda: executor.estimate(place, radius, sectors, len(urls)), estimated, failed,
                            name="search estimate", timeout=ESTIMATE_TIMEOUT_S)

    def _confirm_estimate(self, estimate, sectors: list[str], n_urls: int = 0) -> int:
        """CHOICE_LIMIT, CHOICE_ALL or CHOICE_CANCEL. A long search without a limit
        is offered a SUGGESTED_LIMIT (the most relevant companies first)."""
        parts = [t("confirm.estimate", companies=estimate.companies, with_site=estimate.with_site,
                   duration=readable_duration(estimate.seconds))]
        if not sectors:
            parts.append(t("confirm.all_sectors"))
        if self._settings.max_companies:
            parts.append(t("confirm.limit", n=self._settings.max_companies))
        if estimate.is_long:
            parts.append(t("confirm.long_search"))
        offer_limit = (estimate.is_long and not self._settings.max_companies
                       and estimate.companies > SUGGESTED_LIMIT)
        if offer_limit:
            limited = estimate_from_count(
                replace(self._settings, max_companies=SUGGESTED_LIMIT),
                max(0, estimate.companies - n_urls), n_urls)
            parts.append(t("confirm.limit_suggestion", n=SUGGESTED_LIMIT,
                           limited=readable_duration(limited.seconds)))
        parts.append(t("confirm.question"))
        text = "\n\n".join(parts)
        if offer_limit:
            choose = getattr(self.dialogs, "choose", None)
            if choose is None and isinstance(self.dialogs, Dialogs):
                choose = self._choose_box
            if choose is not None:
                labels = [t("confirm.button_limit", n=SUGGESTED_LIMIT), t("confirm.button_all"),
                          t("common.cancel")]
                return choose(text, t("confirm.title"), labels)
        answer = self.dialogs.ask(text, t("confirm.title"), warning=estimate.is_long)
        return CHOICE_ALL if answer else CHOICE_CANCEL

    def _choose_box(self, text: str, title: str, labels: list[str]) -> int:
        """Message box with one button per label; the index of the one clicked
        (the last one, Cancel, on Escape)."""
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(text)
        box.setTextFormat(Qt.TextFormat.PlainText)
        roles = (QMessageBox.ButtonRole.AcceptRole, QMessageBox.ButtonRole.ActionRole,
                 QMessageBox.ButtonRole.RejectRole)
        buttons = [box.addButton(label, roles[min(i, 2)]) for i, label in enumerate(labels)]
        box.setDefaultButton(buttons[0])
        box.setEscapeButton(buttons[-1])
        box.exec()
        clicked = box.clickedButton()
        return buttons.index(clicked) if clicked in buttons else len(labels) - 1

    def _start_search(self, executor, profile, place, radius, urls, sectors,
                      search_id: int | None = None) -> None:
        self._cancel = threading.Event()
        self._current_search = search_id
        if search_id is None:
            self.results_table.clear()
        self._model_problem = None
        self._empty_key = None
        self._update_notices()
        self._search_started = time.monotonic()
        self.detail.show_row(None)
        self.tabs.setCurrentIndex(TAB_RESULTS)
        self._set_searching(True)
        if search_id is None:
            log.info("Search started: radius %s km, %d sectors, %d websites added by hand, parallel %s",
                     radius, len(sectors or []), len(urls or []), self._settings.parallel_companies)
        else:
            log.info("Search %s resumed", search_id)
        # Busy until discovery has the candidate list, then "analysed / estimated".
        self._search_progress = SearchProgress()
        self._search_final = None
        self.status_progress.show("search", BarState(None, "", t("pipeline.started")))
        self._set_status("pipeline.started")
        cancel = self._cancel
        on_progress = lambda event: self._queue.put(("event", event))  # noqa: E731

        def job():
            try:
                if search_id is None:
                    sid = executor.run(profile, place, radius, urls, sectors, on_progress, cancel)
                else:
                    sid = executor.resume(search_id, on_progress, cancel)
                self._queue.put(("search_finished", sid))
            except Exception as error:
                log.warning("Search stopped by an error", exc_info=True)
                self._queue.put(("search_finished", error))

        self._search_thread = threading.Thread(target=job, daemon=True, name="search")
        self._search_thread.start()

    def _set_searching(self, active: bool) -> None:
        self.cancel_button.setEnabled(active)
        self.search_panel.enable_start(not active)
        self.history.set_search_running(active)

    def _handle_event(self, event: Event) -> None:
        if event.search_id is not None and self._current_search != event.search_id:
            self._current_search = event.search_id
            self._update_search_label()
        self._search_progress_event(event)
        if event.kind == RESULT and event.company is not None:
            self._pending_rows.append((event.company, event.assessment))
            if self._empty_key is not None:
                self._empty_key = None
                self._update_notices()
        if event.key:
            self._set_status(event.key, **event.parameters)
        if event.kind == INFO and event.key == "pipeline.source_status":
            self.search_panel.show_source_status(event)
        if event.kind == INFO and event.key == REDUCED_EVENT_KEY:
            self._model_problem = {"cause": event.parameters.get("cause", "unknown"),
                                   "details": event.parameters.get("details", "")}
            self._update_notices()
        if event.kind == INFO and event.key in NO_COMPANIES_KEYS:
            self._empty_key = (event.key, dict(event.parameters))
            self._update_notices()
        if event.kind == ERROR:
            self.dialogs.error(event.message or t(event.key))
        if event.kind == END:
            processed = int(event.parameters.get("processed", event.current) or 0)
            errors = int(event.parameters.get("errors", 0) or 0)
            if errors and errors >= processed and not self.results_table.rows:
                self._empty_key = ("results.all_failed", {"n": errors})
                self._update_notices()
            self._update_search_label()

    # ------------------------------------------------------------------ search progress

    def _search_progress_event(self, event: Event) -> None:
        """Estimated total as soon as discovery has its list, then analysed / total.
        The bar never goes back; a revised total is logged and named in the tooltip."""
        progress = self._search_progress
        if progress is None:
            return
        revision = None
        if event.kind == INFO and event.key == ESTIMATED_EVENT_KEY:
            if progress.total is None or progress.estimated:
                revision = progress.set_total(int(event.parameters.get("estimated", 0) or 0), estimated=True)
        elif event.kind == INFO and event.key == MAP_INDEX_EVENT_KEY and progress.total is None:
            percent = int(event.parameters.get("percent", 0) or 0)
            self.status_progress.show("search", BarState(
                percent / 100, percent_text(percent / 100), t("pipeline.map_index", percent=percent)))
            return
        elif event.kind in (DISCOVERY, PROGRESS, RESULT, COMPANY_ERROR, END) and (
                event.total or event.kind == DISCOVERY):
            if event.kind == DISCOVERY or progress.total is None or progress.estimated \
                    or event.total != progress.total:
                revision = progress.set_total(event.total, estimated=False)
            progress.update(event.current)
        else:
            return
        if revision is not None:
            log.info("Search progress: number of companies revised from %d to %d%s", revision.old,
                     revision.new, " (estimate)" if revision.estimated else "")
        self.status_progress.show("search", self._search_bar_state())

    def _search_bar_state(self) -> BarState:
        progress = self._search_progress
        if progress is None or progress.total is None:
            return BarState(None, "", t("pipeline.started"))
        done, total = progress.done, progress.shown_total
        tips = [t("progress.search.tooltip_estimated" if progress.estimated else "progress.search.tooltip",
                  done=done, total=total)]
        for revision in progress.revisions[-3:]:
            tips.append(t("progress.search.revised", old=revision.old, new=revision.new))
        return BarState(progress.fraction, counts_text(done, total, progress.estimated), "\n".join(tips))

    def _search_final_state(self) -> BarState | None:
        """What the bar keeps after the search (e.g. "15/15"), when nothing else runs."""
        if self.search_running and self._search_progress is not None:
            return self._search_bar_state()
        return getattr(self, "_search_final", None)

    def search_progress_text(self) -> str:
        """The counts on the bar for the search ("10/15", "3/~15"), "" before discovery."""
        state = self.status_progress.state("search") or self._search_final_state()
        return state.text if state is not None else ""

    def remaining_text(self) -> str:
        """"about N min left" from the measured pace; "" while there is not enough data."""
        progress = self._search_progress
        if progress is None or not self.search_running:
            return ""
        minutes = remaining_minutes(progress.remaining_seconds())
        if minutes is None:
            return ""
        return t("progress.search.eta_soon") if minutes == 0 else t("progress.search.eta", minutes=minutes)

    def _search_finished(self, outcome) -> None:
        self._flush_rows()
        if self._search_progress is not None and self._search_progress.total is not None:
            self._search_final = self._search_bar_state()
        else:
            self._search_final = None
        self.status_progress.finish("search", self._search_final)
        if self._search_started:
            self.elapsed_label.setText(elapsed_text(time.monotonic() - self._search_started))
        self._search_started = 0.0
        self.phase_label.hide()
        self._set_searching(False)
        if isinstance(outcome, BaseException):
            log.warning("Search ended with an error: %s", type(outcome).__name__)
        else:
            log.info("Search %s finished: %d results shown%s", outcome, len(self.results_table.rows),
                     " (cancelled)" if self._cancel.is_set() else "")
        self.history.load(self._archive)
        self._update_search_label()
        if isinstance(outcome, BaseException):
            from ..pipeline import SearchError

            if isinstance(outcome, SearchError) and outcome.key in ("error.overpass", "error.model",
                                                                    "error.discovery_failed"):
                self._set_status(outcome.key)  # already shown by the ERROR event
            else:
                self._show_error(outcome)

    def cancel_search(self) -> None:
        log.info("Cancel requested (%s)", "search" if self.search_running else "estimate")
        if self.search_running:
            self._cancel.set()
            self.cancel_button.setEnabled(False)
            self._set_status("status.cancelling")
        else:  # waiting for the estimate: forget it
            self._estimate_token += 1
            self.cancel_button.setEnabled(False)
            self.search_panel.enable_start(True)
            self._set_status("status.estimate_cancelled")

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel

    # ------------------------------------------------------------------ history

    def open_search(self, search_id: int) -> None:
        """Reopens saved results without crawling again."""
        if self.search_running and search_id != self._current_search:
            self.dialogs.info(t("history.wait_for_finish"))
            return
        self._current_search = search_id
        self.results_table.set_rows(self._archive.results_of(search_id))
        self.detail.show_row(None)
        self._results_changed()
        parameters = search_parameters(self._archive, search_id)
        self._model_problem = ({"cause": parameters.get("model_cause", "unknown"), "details": ""}
                               if parameters.get("reduced") else None)
        self._empty_key = None
        self._update_notices()
        self._update_search_label()
        self.tabs.setCurrentIndex(TAB_RESULTS)

    def resume_search(self, search_id: int) -> None:
        if self.search_running:
            return
        search = self._archive.read_search(search_id)
        if search is None or search.get("profile") is None:
            self.dialogs.error(t("error.search_not_found"))
            return
        self.results_table.set_rows(self._archive.results_of(search_id))
        executor = self._executor_factory(self._settings, self._archive)
        self._start_search(executor, None, None, None, None, None, search_id=search_id)

    def delete_search(self, search_id: int) -> None:
        if self.search_running:
            return
        if not self.dialogs.ask(t("history.confirm_delete"), t("history.delete"), warning=True):
            return
        self._archive.delete_search(search_id)
        if self._current_search == search_id:
            self._current_search = None
            self.results_table.clear()
            self.detail.show_row(None)
            self._update_search_label()
        self.history.load(self._archive)

    def delete_searches(self, search_ids: list[int]) -> int:
        """Deletes several previous searches after ONE confirmation; returns how many."""
        ids = list(dict.fromkeys(search_ids))
        if self.search_running or not ids:
            return 0
        if len(ids) == 1:
            self.delete_search(ids[0])
            return 0 if self._archive.read_search(ids[0]) is not None else 1
        if not self.dialogs.ask(t("history.confirm_delete_many", n=len(ids)), t("history.delete_selected"),
                                warning=True):
            return 0
        for search_id in ids:
            self._archive.delete_search(search_id)
        if self._current_search in ids:
            self._current_search = None
            self.results_table.clear()
            self.detail.show_row(None)
            self._update_search_label()
        log.info("Previous searches deleted: %d", len(ids))
        self.history.load(self._archive)
        return len(ids)

    # ------------------------------------------------------------------ results

    def _row_selected(self, row) -> None:
        self.detail.show_row(row)
        if row is not None and self.results_map.created:
            rows = self.results_table.visible_rows()
            self.results_map.select(f"r{self.results_table.row_index(row)}" if row in rows else None)
        self._translate_row(row)
        self.update_translation_notice()

    def _translate_row(self, row) -> None:
        """Texts of a row not translated yet (e.g. a search saved before, or with the
        interface in another language): translated in a worker, then shown again."""
        if row is None or row[1] is None or not display_text.interface_is_english():
            return
        evaluation = row[1]
        texts = display_text.assessment_texts(evaluation)
        todo = display_text.pending(texts)
        if not todo or id(evaluation) in self._translating:
            return
        if not any(display_text.model_installed(lang) for lang in display_text.TRANSLATABLE):
            return
        self._translating.add(id(evaluation))
        store = self._archive

        def done(_made) -> None:
            self._translating.discard(id(evaluation))
            if self.results_table.selected() is row or self.detail._row is row:
                self.detail.show_row(row)
            self.results_table.refresh()
            self.update_translation_notice()

        def failed(_error) -> None:
            self._translating.discard(id(evaluation))

        self._in_background(lambda: display_text.translate_now(todo, store), done, failed,
                            name="translation of a result")

    def update_translation_notice(self) -> None:
        """"Some texts are in their original language": shown once, with how to enable
        the local translation (English interface only)."""
        missing = display_text.missing_languages() if display_text.interface_is_english() else set()
        missing = {lang for lang in missing if not display_text.model_installed(lang)}
        if missing:
            names = ", ".join(t(f"cv_language.{lang}") for lang in sorted(missing))
            self.translation_notice.setText(t("display.notice", languages=names,
                                              command="python -m reversehire.cv.translate download it uk"))
        self.translation_notice.setVisible(bool(missing))

    # ------------------------------------------------------------------ favourites and cards

    def toggle_favourite(self, company, assessment, on: bool) -> None:
        """Star / un-star a result: the card is created on the first star."""
        try:
            companies.set_favourite(self._archive, company, on, assessment=assessment,
                                    search_id=self._current_search)
        except Exception:
            log.warning("Favourite not saved", exc_info=True)
            self.dialogs.error(t("error.generic"))
            return
        self._set_status("favourites.added" if on else "favourites.removed", name=company.name)
        self._refresh_favourites()

    def _refresh_favourites(self) -> None:
        try:
            keys = companies.favourite_keys(self._archive)
        except Exception:
            log.warning("Favourites cannot be read", exc_info=True)
            keys = set()
        self.results_table.set_favourites(keys)
        if self.favourites_dialog is not None:
            self.favourites_dialog.reload()
        self.companies_panel.reload()

    def open_favourites(self) -> FavouritesDialog:
        """Non-modal window with the starred companies (one at a time)."""
        if self.favourites_dialog is not None:
            self.favourites_dialog.reload()
            self.favourites_dialog.raise_()
            self.favourites_dialog.activateWindow()
            return self.favourites_dialog
        dialog = FavouritesDialog(self, self._archive, opener=self._opener, on_changed=self._refresh_favourites,
                                  on_open_card=self.open_card, on_mail=lambda card: self.open_mail_dialog(card))
        self.wire_search_box(dialog.search_box)
        from . import context_menu

        context_menu.install_buttons(dialog.table, lambda d=dialog: [
            d.card_button, d.mail_button, None, d.site_button, d.careers_button, d.linkedin_button, d.posting_button,
            None, d.unstar_button])
        dialog.finished.connect(lambda _r: setattr(self, "favourites_dialog", None))
        self.favourites_dialog = dialog
        dialog.show()
        return dialog

    def open_card(self, card_id: int) -> None:
        self.tabs.setCurrentIndex(TAB_COMPANIES)
        self.companies_panel.select_card(card_id)

    def _analysis_embedder(self):
        """The embedder for the CV analysis, only if the model is already on disk.

        Reading a CV must never start the model download: without the model the
        semantic checks are simply skipped.
        """
        try:
            from ..scoring.embedding import PREFERRED_MODEL, find_local_model, model_search_folders

            if find_local_model(PREFERRED_MODEL, model_search_folders()) is None:
                return None
            embedder = self._get_embedder()
            # The CV checks calibrate on the real model: never on the lexical fallback.
            return getattr(embedder, "primary", embedder)
        except Exception:
            return None

    def _get_embedder(self):
        """The local embedder, created on first use (called from worker threads).

        By default the real model with the built-in lexical fallback
        (`ResilientEmbedder`), so "My companies" keeps searching without the model
        and goes back to the model as soon as it loads."""
        with self._embedder_lock:
            if self._embedder is None:
                if self._embedder_factory is not None:
                    self._embedder = self._embedder_factory(self._archive)
                else:
                    from ..scoring.embedding import ResilientEmbedder, create_embedder

                    self._embedder = ResilientEmbedder(create_embedder(self._archive))
            return self._embedder

    # ------------------------------------------------------------------ diagnostics

    def show_model_problem(self) -> ModelProblemDialog | None:
        """"Why?" of the reduced-accuracy notice: cause, advice, details, Copy."""
        if self._model_problem is None:
            return None
        details = self._model_problem.get("details") or ""
        if not details:  # a saved search: the details of this session, if any
            from ..scoring.embedding import failure_details

            error = getattr(self._embedder, "last_error", None)
            details = failure_details(error) if error is not None else ""
        dialog = ModelProblemDialog(self, self._model_problem.get("cause", "unknown"), details,
                                    on_diagnostics=self.open_diagnostics)
        dialog.finished.connect(lambda _r: setattr(self, "model_dialog", None))
        self.model_dialog = dialog
        dialog.open()
        return dialog

    def open_diagnostics(self) -> DiagnosticsDialog:
        """"? > Diagnostics…": the checks run in a worker thread."""
        if self.diagnostics_dialog is not None:
            self.diagnostics_dialog.raise_()
            return self.diagnostics_dialog
        settings = self._settings

        def runner():
            from ..diagnostics import run_checks

            return run_checks(settings)

        dialog = DiagnosticsDialog(self, self._in_background, self._diagnostics_runner or runner,
                                   on_open_log=self.open_log_folder)
        dialog.finished.connect(lambda _r: setattr(self, "diagnostics_dialog", None))
        self.diagnostics_dialog = dialog
        dialog.open()
        dialog.start()
        return dialog

    def open_log_folder(self) -> bool:
        """Opens the folder with reversehire.log in File Explorer."""
        from pathlib import Path

        return self.open_folder(Path(applog.log_file_path()).parent.resolve())

    def open_folder(self, folder) -> bool:
        """Shows a folder in the system file manager (`applog.open_folder`: Explorer,
        Finder, xdg-open); an opener injected by the tests receives its file URI."""
        from pathlib import Path

        folder = Path(folder)
        if self._opener is not webbrowser.open:
            try:
                self._opener(folder.as_uri())
            except Exception:
                log.warning("Folder cannot be opened", exc_info=True)
                return False
            return True
        return applog.open_folder(folder)

    def export(self, path: str | None = None) -> int | None:
        """Exports the visible rows (current filters and order) as CSV."""
        rows = self.results_table.visible_rows()
        if not rows:
            self.dialogs.info(t("export.empty"))
            return None
        if path is None:
            path = self.dialogs.save_file(t("menu.export_csv"), t("file.csv_name"),
                                          f"{t('file.csv')} (*.csv)")
        if not path:
            return None
        try:
            n = export_csv(path, rows, self.results_table.is_favourite)
        except OSError as error:
            log.warning("CSV export failed: %s", error)
            self.dialogs.error(t("export.error"))
            return None
        log.info("Results exported to CSV: %d rows", n)
        self._set_status("export.done", n=n)
        return n

    # ------------------------------------------------------------------ selected company

    def card_for_results(self):
        """The company of the Results detail as a card: its saved card when there is one,
        otherwise an unsaved card with what the result knows (nothing is stored)."""
        row = self.detail.row
        if row is None:
            return None
        company, evaluation = row
        try:
            saved = companies.find(self._archive, company)
        except Exception:  # noqa: BLE001 - a busy store must not block the action
            log.warning("Saved card not read", exc_info=True)
            saved = None
        if saved is not None:
            return saved
        hiring = {}
        if evaluation is not None:
            hiring = {"careers_url": evaluation.careers_url or "",
                      "postings": [{"title": p.title, "url": p.url, "location": p.location or ""}
                                   for p in (evaluation.postings or [])]}
        return companies.CompanyCard(id=0, key=companies.card_key(company), name=company.name, site=company.site,
                                     address=company.address or "", website=company.site or "", hiring=hiring)

    def current_card(self):
        """The company selected where the user is: the favourites window when it is the
        active one, else the current tab (Results or My companies), else the other."""
        dialog = self.favourites_dialog
        if dialog is not None and dialog.isActiveWindow() and dialog.selected() is not None:
            return dialog.selected()
        results = self.card_for_results
        cards = lambda: self.companies_panel.card_view.card  # noqa: E731
        order = (cards, results) if self.tabs.currentIndex() == TAB_COMPANIES else (results, cards)
        for source in order:
            card = source()
            if card is not None:
                return card
        if dialog is not None and dialog.selected() is not None:
            return dialog.selected()
        return None

    def _update_export_actions(self) -> None:
        has = self.current_card() is not None
        self.export_card_action.setEnabled(has)
        self.export_application_action.setEnabled(has)

    # ------------------------------------------------------------------ mail

    def _primary_cv_pdf(self) -> str:
        try:
            return mail_support.primary_cv_pdf(self.cv_library)
        except Exception:  # noqa: BLE001
            return ""

    def mail_settings(self):
        """The user's mail settings; the primary CV as attachment when none is set."""
        settings = mail_support.mail_settings_of(self._settings)
        if not settings.attachment_path:
            settings.attachment_path = self._primary_cv_pdf()
        return settings

    def _default_role(self) -> str:
        """The role written in e-mails and letters by default: the profile's first role."""
        try:
            profile = self.profile_panel.current_profile()
            roles = list(getattr(profile, "roles", None) or []) if profile is not None else []
            return str(getattr(roles[0], "value", "") or "") if roles else ""
        except Exception:  # noqa: BLE001
            return ""

    def open_mail_dialog(self, card=None, role: str | None = None) -> MailDialog | None:
        """"Write e-mail…": the draft to `card` (default: the selected company). `role`: the
        posting's title when the e-mail answers a posting, else the profile's first role
        (editable in the dialog)."""
        card = card if card is not None else self.current_card()
        if card is None:
            self.show_notice(t("mail.dialog.no_company"))
            return None
        if self.mail_dialog is not None:
            self.mail_dialog.close()
        dialog = MailDialog(self, card, self.mail_settings(), self._in_background, system=self._mail_system,
                            on_open_settings=lambda: self.open_settings("mail"),
                            on_done=lambda outcome, name=card.name: self._mail_done(outcome, name),
                            role=role if role is not None else self._default_role())
        dialog.finished.connect(lambda _r, d=dialog: self._dialog_closed("mail_dialog", d))
        self.mail_dialog = dialog
        dialog.show()
        self._ext("mail_dialog_opened", card, dialog)
        return dialog

    def _mail_done(self, outcome, name: str) -> None:
        if outcome.ok:
            self._set_status("mail.done", company=name)
        else:
            self.show_notice(mail_support.outcome_text(outcome).splitlines()[0])

    def _dialog_closed(self, attribute: str, dialog) -> None:
        if getattr(self, attribute, None) is dialog:
            setattr(self, attribute, None)

    # ------------------------------------------------------------------ export

    def _chart_of(self, card):
        """(png, caption) of the card's financial chart, None without figures."""
        view = self.companies_panel.card_view
        try:
            png = view.chart_png(card)
            return (png, view.chart_caption(card)) if png else None
        except Exception:  # noqa: BLE001 - the chart must never stop the export
            log.warning("Financial chart not drawn for the export", exc_info=True)
            return None

    def export_card(self, card=None):
        """File > Export > company card: the preview first (print, PDF, Excel, Word)."""
        from ..export import build_company_card_document
        from .company_card import tr as card_tr

        card = card if card is not None else self.current_card()
        if card is None:
            self.show_notice(t("export.no_company"))
            return None
        document = build_company_card_document(card, tr=card_tr, chart=self._chart_of(card))
        return self._export_preview(document)

    def export_application(self, card=None):
        """File > Export > application: the letter (the e-mail text for this company), the
        CV (the reference PDF of Settings > Mail) and the card as a private appendix."""
        from ..export import build_application_document
        from .company_card import tr as card_tr

        card = card if card is not None else self.current_card()
        if card is None:
            self.show_notice(t("export.no_company"))
            return None
        settings = self.mail_settings()
        draft = draft_for(card, settings, recipient_for(card))
        sender = tuple(x for x in (settings.my_name, settings.my_email, settings.my_phone) if x)
        contact = card.contact_line() if hasattr(card, "contact_line") else ""
        document = build_application_document(card, draft.subject, draft.body, settings.attachment_path or None,
                                              signature_name="", contact=contact, sender_lines=sender,
                                              tr=card_tr, chart=self._chart_of(card))
        dialog = self._export_preview(document)
        self._ext("application_export_opened", card, document, dialog,
                  {"subject": draft.subject, "body": draft.body, "cv_path": settings.attachment_path or ""})
        return dialog

    def _export_preview(self, document):
        from ..export import ExportPreviewDialog
        from .company_card import tr as card_tr

        if self.export_dialog is not None:
            self.export_dialog.close()
        dialog = ExportPreviewDialog(document, self, tr=card_tr)
        dialog.finished.connect(lambda _r, d=dialog: self._dialog_closed("export_dialog", d))
        self.export_dialog = dialog
        dialog.show()
        log.info("Export preview opened: %s", document.kind)
        return dialog

    # ------------------------------------------------------------------ settings

    def open_settings(self, page: str | None = None) -> SettingsDialog:
        """The settings dialog (one at a time), on `page` ("mail", "assistant", ...)."""
        if self.settings_dialog is not None:
            if page:
                self.settings_dialog.show_page(page)
            self.settings_dialog.raise_()
            self.settings_dialog.activateWindow()
            return self.settings_dialog
        dialog = SettingsDialog(self, self._settings, self.apply_settings, self.clear_cache, self.dialogs,
                                run_in_background=self._in_background, primary_cv=self._primary_cv_pdf,
                                mcp_enabled=lambda: self.mcp_enabled, on_mcp_toggle=self.set_mcp_enabled,
                                on_mcp_details=lambda: self.open_mcp_dialog(),
                                cli_checker=lambda kind, override: self._check_cli(kind, override),
                                mail_system=self._mail_system, on_assistant_checked=self._assistant_checked,
                                page=page, ocr_reader=self.ocr_reader)
        dialog.finished.connect(lambda _r: setattr(self, "settings_dialog", None))
        self.settings_dialog = dialog
        dialog.open()
        return dialog

    def _check_cli(self, kind: str, override: str | None):
        """Detection of an assistant CLI (worker thread); tests set `_cli_checker`."""
        if self._cli_checker is not None:
            return self._cli_checker(kind, override)
        from . import chat_panel

        return chat_panel.default_cli_checker(kind, override)

    def _assistant_checked(self, infos: dict) -> None:
        """"Check again" in the settings: the strip uses the same result."""
        self.chat_panel.set_detection(infos.get(self.chat_panel.kind()), infos.get("mcp"))

    def apply_settings(self, new: config.Settings) -> None:
        language_changed = new.language != self._settings.language
        theme_changed = new.theme != self._settings.theme
        changed = []
        for name, value in vars(new).items():
            if getattr(self._settings, name, None) == value:
                continue
            secret = any(word in name for word in ("key", "token", "password", "secret"))
            shown = applog.mask(value) if secret else (value if isinstance(value, (bool, int, float)) or (
                isinstance(value, str) and len(value) <= 40) else "…")
            changed.append(f"{name}={shown}")
        log.info("Settings changed: %s", ", ".join(changed) or "nothing")
        for name, value in vars(new).items():
            setattr(self._settings, name, value)
        applog.register_secret(self._settings.firecrawl_key)
        applog.register_secret(getattr(self._settings, "paid_registry_key", ""))
        self._save_settings()
        self._feature_settings_applied()
        self._ext("settings_applied")
        if language_changed:
            self.change_language(new.language)
        if theme_changed:
            self._apply_theme()
        self.chat_panel.settings_changed()
        self._set_status("settings.saved")

    # ------------------------------------------------------------------ about and MCP

    def open_about(self) -> AboutDialog:
        # The PC information is collected on a worker (a sleeping disk may take seconds).
        dialog = AboutDialog(self, self.mcp_enabled, self._effective_theme,
                             collector=getattr(self, "about_collector", None),
                             run_in_background=self._in_background)
        dialog.finished.connect(lambda _r: setattr(self, "about_dialog", None))
        self.about_dialog = dialog
        dialog.open()
        return dialog

    def open_mcp_dialog(self) -> McpDialog:
        dialog = McpDialog(self, self.mcp_enabled, self.set_mcp_enabled)
        dialog.finished.connect(lambda _r: setattr(self, "mcp_dialog", None))
        self.mcp_dialog = dialog
        dialog.open()
        return dialog

    @property
    def mcp_enabled(self) -> bool:
        return bool(getattr(self._settings, "mcp_enabled", False))

    def set_mcp_enabled(self, on: bool) -> None:
        """Saves the MCP switch at once; the server reads it before every tool call."""
        self._settings.mcp_enabled = bool(on)
        self._save_settings()
        self._update_mcp_indicator()
        if self.mcp_dialog is not None:
            self.mcp_dialog.set_enabled(self.mcp_enabled)
        if self.about_dialog is not None:
            self.about_dialog.set_mcp_enabled(self.mcp_enabled)
        if self.settings_dialog is not None:
            self.settings_dialog.assistant_page.set_mcp_enabled(self.mcp_enabled)
        self.chat_panel.mcp_changed()

    def _update_mcp_indicator(self) -> None:
        on = self.mcp_enabled
        self.mcp_indicator.set_state(on, t("mcp.on") if on else t("mcp.off"),
                                     t("mcp.tooltip_on") if on else t("mcp.tooltip_off"))
        # The menu bar does not re-layout its corner widget when the text changes.
        self.mcp_indicator.adjustSize()
        self.favourites_button.adjustSize()
        self.corner.adjustSize()
        self.menuBar().setCornerWidget(self.corner, Qt.Corner.TopRightCorner)
        self.corner.show()

    def clear_cache(self) -> None:
        """Deletes the network caches in a worker (a large database takes a while)."""
        archive = self._archive
        from .lazy_map import clear_map_cache

        map_cleared = clear_map_cache()

        def work():
            import shutil

            from ..jobsearch import default_cache

            for space in CACHE_SPACES:
                archive.cache_clear(space)
            # the file caches of the job search (sitemaps, postings, robots) and of the map tiles
            folder = config.data_folder() / "cache"
            shutil.rmtree(folder / "jobsearch", ignore_errors=True)
            default_cache().clear()
            if not map_cleared:
                shutil.rmtree(folder / "map" / "http", ignore_errors=True)
            log.info("Caches cleared (store, job search files, map tiles)")

        self._set_status("status.working")
        self._in_background(work, lambda _r: self._set_status("settings.cache_cleared"), name="clear cache")

    def _save_settings(self) -> None:
        try:
            config.save(self._settings)
        except OSError:
            log.warning("Settings cannot be saved", exc_info=True)

    @property
    def settings(self) -> config.Settings:
        return self._settings

    # ------------------------------------------------------------------ closing

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt name)
        """During a search: cancel it and wait (at most CLOSE_WAIT_S) before closing."""
        if self.search_running and not self._closed:
            if not self._close_requested:
                self._close_requested = True
                self._cancel.set()
                self.cancel_button.setEnabled(False)
                self._set_status("status.closing")
                self._close_deadline = time.monotonic() + CLOSE_WAIT_S
            event.ignore()
            return
        self.shutdown()
        event.accept()

    def request_close(self) -> None:
        self.close()

    def _check_close(self) -> None:
        if self.search_running and time.monotonic() < self._close_deadline:
            return
        if self.search_running:
            log.warning("The search did not stop in time: forced close")
        self.shutdown()
        self.close()

    @property
    def is_closed(self) -> bool:
        return self._closed

    def shutdown(self) -> None:
        """Stops the timer and the search, closes the archive if this window opened it."""
        if self._closed:
            return
        self._closed = True
        self._timer.stop()
        safety.set_notice_handler(None)
        for info in self._job_info.values():  # workers stop at their next check; nobody waits
            if info["cancel"] is not None:
                info["cancel"].set()
        self.companies_panel.card_view.cancel()
        self.chat_panel.shutdown()
        if self.search_running:
            self._cancel.set()
            self._search_thread.join(2.0)
        self.analysis_panel.cancel()
        self.jobs_panel.cancel()
        self._ext("shutdown")
        for dialog in (self.settings_dialog, self.start_dialog, self.about_dialog, self.mcp_dialog,
                       self.favourites_dialog, self.profile_panel.text_viewer, self.companies_panel.add_dialog,
                       self.diagnostics_dialog, self.model_dialog, self.mail_dialog, self.export_dialog,
                       self.history.note_dialog,
                       *self._feature_dialogs(), *[d for ds in self._ext("dialogs") for d in (ds or [])]):
            if dialog is not None:
                dialog.close()
        if display_text._store is self._archive:
            display_text.set_store(None)
        if self._owns_archive and not self.search_running:
            try:
                self._archive.close()
            except Exception:
                pass
