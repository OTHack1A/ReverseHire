"""Tab "Job search": a search box used like a job search engine, the query as it was
understood (editable chips, and "Searched also for": the synonyms, translations and
corrected typos, each removable), then three lists: "Open postings" (with "Match with my
CV", per-source status lines, a map view and the actions on a result), "Companies for a
spontaneous application" (gui/jobsearch_companies.py) and "Previous searches" (open,
rename / note, delete; right-click menu and Delete key).

Sources: only those whose terms allow automated reading (jobsearch package). Portals
the app must not read (LinkedIn, Indeed) get "Open the same search in the browser"
buttons only. The search runs on a worker with progress ("done/estimated") and Cancel;
the postings are ranked by meaning too when the language model is there (hybrid with the
exact words; without it: exact words, typos and synonyms, said in a muted note). The
results are stored (postings and searches tables). Removing a "Searched also for" term
ranks the postings already found again, without asking the sources.
"""

from __future__ import annotations

import html
import logging
import threading
from typing import Callable
from urllib.parse import quote_plus

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QScrollArea,
    QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from ..i18n import t
from ..jobsearch.texts import state_line, text as js_text
from .common import enum_label, format_number, open_url, run_job
from .cv_match import PENDING, badge_percent, badge_text, tooltip_text
from .jobsearch_companies import SpontaneousCompaniesView
from .lazy_map import LazyMap
from .menus import menu_action_of
from .theme import Palette, active_palette
from . import context_menu, table_filter, table_sort
from .widgets import ElidedLabel, wrapped_label

log = logging.getLogger(__name__)

COLUMNS = ("match", "title", "company", "workplace", "distance", "contract", "salary", "date", "source")
SEARCH_TIMEOUT_S = 900.0
EMPLOYERS_TIMEOUT_S = 120.0
RERANK_TIMEOUT_S = 300.0
PARSE_DELAY_MS = 350
NOTE_SAVE_DELAY_MS = 400
MAX_ALSO_CHIPS = 14
HISTORY_COLUMNS = ("when", "title", "found", "status", "note")
TAB_POSTINGS, TAB_COMPANIES, TAB_HISTORY = 0, 1, 2
PASTED_SOURCE = "pasted"


def source_text(source_id: str) -> str:
    """The posting's source as the interface names it ("Randstad", "Pasted by you"), never
    the raw id ("agency:randstad")."""
    from ..jobsearch.texts import source_label

    return source_label(source_id or "", source_id or "–") if source_id else "–"
LOCAL_SEARCHES = 10  # saved company searches whose postings feed the "already found" source
MAX_GEOCODES = 25  # places of postings looked up online per search (Nominatim: 1 per second)
LINKEDIN_SEARCH = "https://www.linkedin.com/jobs/search/?keywords={q}&location={l}"
INDEED_SEARCH = "https://it.indeed.com/jobs?q={q}&l={l}&radius={r}"
# Chips of the understood query: (field, kind) where kind is "value" or "list".
CHIP_FIELDS = (("role", "value"), ("skills", "list"), ("place", "value"), ("radius_km", "value"),
               ("contract", "value"), ("hours", "value"), ("remote", "value"), ("seniority", "value"),
               ("phrases", "list"), ("excluded", "list"))


def limited_geocode(geocoder, limit: int = MAX_GEOCODES) -> Callable[[str], object]:
    """The `geocode` of search_jobs: the app's geocoder (cached, polite), asked at most
    `limit` times per search; a bare Italian town is looked up in Italy ("Viadana, Italia"),
    a foreign one in its country ("Vienna, Austria": geo.places.geocode_query), a "Town,
    Region, Country" text as it is. Never raises."""
    from ..geo.places import geocode_query

    asked = [0]

    def geocode(name: str):
        name = (name or "").strip()
        if not name or geocoder is None or asked[0] >= limit:
            return None
        asked[0] += 1
        try:
            return geocoder.geocode(geocode_query(name))
        except Exception:  # noqa: BLE001 - a place not found is an unknown distance
            log.info("Posting place not geocoded", exc_info=True)
            return None

    return geocode


def _e(value) -> str:
    return html.escape(str(value or ""))


def chip_value(field: str, value) -> str:
    if field == "radius_km":
        return f"{format_number(float(value))} km"
    if field == "contract":
        return enum_label("contract", value)
    if field == "remote":
        return enum_label("remote", value)
    if field == "hours":
        return t(f"jobs.hours.{value}") if t(f"jobs.hours.{value}") != f"jobs.hours.{value}" else str(value)
    if field == "phrases":
        return f"“{value}”"
    if field == "excluded":
        return f"-{value}"
    return str(value)


def corrected_words(text: str, corrections: dict[str, str]) -> str:
    """`text` with each mistyped word replaced by its correction (`corrections` maps the
    folded typo to the corrected word); the other words stay as typed."""
    from ..jobsearch._util import fold

    out = []
    for word in str(text or "").split():
        core = word.strip(".,;:!?()\"'")
        fixed = corrections.get(fold(core)) if core else None
        out.append(word.replace(core, fixed) if fixed else word)
    return " ".join(out)


def chips_html(query, p: Palette) -> str:
    """The understood query as chips; the value edits the chip, "×" removes it."""
    if query is None:
        return ""
    chips = []
    corrections = dict(getattr(query, "corrections", None) or {})
    for field, kind in CHIP_FIELDS:
        value = getattr(query, field, None)
        values = list(value or []) if kind == "list" else ([value] if value not in (None, "") else [])
        if field == "role" and corrections:
            # "Interpreted as" shows what is searched: the typos already corrected
            values = [corrected_words(v, corrections) for v in values]
        for index, item in enumerate(values):
            label = t(f"jobs.chip.{field}")
            chips.append(
                f"<span style='background-color:{p.surface2}; color:{p.muted}'>&nbsp;{_e(label)}: </span>"
                f"<a href='edit:{field}:{index}' style='color:{p.strong}; background-color:{p.surface2}'>"
                f"{_e(chip_value(field, item))}</a>"
                f"<a href='remove:{field}:{index}' style='color:{p.error}; background-color:{p.surface2}'>"
                f"&nbsp;×&nbsp;</a>")
    if not chips:
        return f"<span style='color:{p.muted}'>{_e(t('jobs.chips.empty'))}</span>"
    return f"<span style='color:{p.muted}'>{_e(t('jobs.chips.title'))}</span> " + " &nbsp;".join(chips)


def also_terms(query) -> list[tuple[str, str]]:
    try:
        return list(query.also_terms())[:MAX_ALSO_CHIPS] if query is not None else []
    except Exception:  # noqa: BLE001 - a query saved before the understanding fields
        return []


def also_html(query, p: Palette) -> str:
    """"Searched also for": the synonyms, translations and corrected typos; "×" removes
    one (link "also:<index>")."""
    terms = also_terms(query)
    if not terms:
        return ""
    chips = []
    for index, (term, kind) in enumerate(terms):
        label = t("jobs.also.typo", term=term) if kind == "typo" else term
        chips.append(f"<span style='color:{p.strong}; background-color:{p.surface2}'>&nbsp;{_e(label)}</span>"
                     f"<a href='also:{index}' style='color:{p.error}; background-color:{p.surface2}'>"
                     f"&nbsp;×&nbsp;</a>")
    return f"<span style='color:{p.muted}'>{_e(t('jobs.also.title'))}</span> " + " &nbsp;".join(chips)


def workplace_text(posting) -> str:
    """The posting's place in the format of every list ("Castel Maggiore (BO)",
    "Lugano, Svizzera"), plus remote / hybrid."""
    place = ""
    try:
        from ..geo.places import location_of_posting

        found = location_of_posting(posting)
        if found.known:
            place = found.display()
    except Exception:  # noqa: BLE001 - the text as published is still right
        log.debug("Posting place not normalised", exc_info=True)
    place = place or posting.town or (posting.location.split(",")[0].strip() if posting.location else "")
    if posting.remote in ("remote", "hybrid"):
        mode = enum_label("remote", posting.remote)
        return f"{place} · {mode}" if place else mode
    return place or "–"


def browser_search_urls(query) -> dict[str, str]:
    """The same search on the portals the app must not read (opened in the browser)."""
    role = (getattr(query, "role", "") or getattr(query, "raw", "") or "").strip()
    place = (getattr(query, "place", "") or "").strip()
    radius = int(getattr(query, "radius_km", 0) or 25)
    return {"linkedin": LINKEDIN_SEARCH.format(q=quote_plus(role), l=quote_plus(place)),
            "indeed": INDEED_SEARCH.format(q=quote_plus(role), l=quote_plus(place), r=radius)}


class _Bridge(QObject):
    progress = Signal(int, int, str)


class JobSearchPanel(QWidget):
    """See the module docstring. The main window sets the providers and callbacks."""

    def __init__(self, run_in_background: Callable, match_service, *, opener=None,
                 settings_provider: Callable[[], object] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self._run = run_in_background
        self._match = match_service
        self._opener = opener
        self._settings_provider = settings_provider or (lambda: None)
        self._palette: Palette = active_palette()
        self._dark = True
        self.store = None
        # main-window hooks
        self.search_runner: Callable | None = None  # (query, cancel, progress) -> SearchResult (tests)
        self.sources_factory: Callable[[], list] | None = None
        self.geocoder_factory: Callable[[], object] | None = None  # the app's Geocoder (towns of postings)
        self.on_progress: Callable[[int, int, str], None] = lambda done, total, text: None
        self.on_finished: Callable[[str], None] = lambda text: None
        self.on_create_card: Callable[[object], None] = lambda posting: None
        self.on_possible_employers: Callable[[object], None] = lambda posting: None
        self.on_simulate: Callable[[object], None] = lambda posting: None
        self.on_paste: Callable[[], None] = lambda: None
        self.embedder_provider: Callable[[], object] | None = None  # the language model (worker threads)
        self.ask: Callable[[str], bool] = lambda text: False         # confirmation (the window's dialogs)
        self.on_bar: Callable[[str, object], None] = lambda owner, state: None  # bottom bar (BarState | None)
        self.all_postings: list = []       # the postings of the search before "Searched also for" changes
        self.all_ids: list = []
        self._reranking = False
        self.query = None
        self.edited = False
        self.result = None
        self.postings: list = []
        self.posting_ids: list[int | None] = []
        self.favourites: set[int] = set()
        self.hypotheses: dict[str, list] = {}  # posting identity -> EmployerHypothesis list (map)
        self.current_search_id: int | None = None
        self._cancel: threading.Event | None = None
        self._running = False
        self._bridge = _Bridge()
        self._bridge.progress.connect(lambda done, total, text: self.on_progress(done, total, text))
        self._parse_timer = QTimer(self)
        self._parse_timer.setSingleShot(True)
        self._parse_timer.setInterval(PARSE_DELAY_MS)
        self._parse_timer.timeout.connect(self.parse_now)

        self.title_label = QLabel()
        self.title_label.setProperty("role", "title")
        self.query_field = QLineEdit()
        self.query_field.setClearButtonEnabled(True)
        self.query_field.textChanged.connect(lambda _t: self._parse_timer.start())
        self.query_field.returnPressed.connect(lambda: self.search())
        self.search_button = QPushButton()
        self.search_button.setProperty("role", "accent")
        self.search_button.clicked.connect(lambda: self.search())
        self.cancel_button = QPushButton()
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(lambda: self.cancel())
        self.help_label = wrapped_label(role="muted")
        self.chips = QLabel()
        self.chips.setWordWrap(True)
        self.chips.setTextFormat(Qt.TextFormat.RichText)
        self.chips.linkActivated.connect(self._chip_clicked)
        self.chip_editor = QWidget()
        editor_row = QHBoxLayout(self.chip_editor)
        editor_row.setContentsMargins(0, 0, 0, 0)
        self.chip_editor_label = QLabel()
        self.chip_editor_field = QLineEdit()
        self.chip_editor_field.returnPressed.connect(lambda: self.apply_chip_edit())
        editor_row.addWidget(self.chip_editor_label)
        editor_row.addWidget(self.chip_editor_field, 1)
        self.chip_editor.hide()
        self._editing: tuple[str, int] | None = None
        self.also_label = QLabel()
        self.also_label.setObjectName("jobsAlsoChips")
        self.also_label.setWordWrap(True)
        self.also_label.setTextFormat(Qt.TextFormat.RichText)
        self.also_label.linkActivated.connect(self._also_clicked)
        self.also_label.hide()
        self.ranking_note = wrapped_label(role="muted")
        self.ranking_note.hide()

        # previous searches: a small table (several can be selected and deleted)
        self.history_label = wrapped_label(role="muted")
        self.history_table = QTableWidget(0, len(HISTORY_COLUMNS))
        self.history_table.setObjectName("jobsHistoryTable")
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setWordWrap(False)
        self.history_table.horizontalHeader().setStretchLastSection(True)
        for i in range(len(HISTORY_COLUMNS)):
            self.history_table.setHorizontalHeaderItem(i, QTableWidgetItem())
        self.history_table.itemSelectionChanged.connect(self._history_selection_changed)
        self.history_table.cellDoubleClicked.connect(lambda *_: self.open_selected_search())
        table_filter.attach(self.history_table)
        self.history_ids: list[int] = []
        self.open_search_button = QPushButton()
        self.open_search_button.clicked.connect(lambda: self.open_selected_search())
        self.note_search_button = QPushButton()
        self.note_search_button.clicked.connect(lambda: self.edit_search_note())
        self.delete_search_button = QPushButton()
        self.delete_search_button.clicked.connect(lambda: self.delete_search())
        self.delete_selected_button = QPushButton()
        self.delete_selected_button.clicked.connect(lambda: self.delete_selected_searches())
        self._local_actions: dict[int, QAction] = {}
        self.note_editor = QWidget()
        note_row = QHBoxLayout(self.note_editor)
        note_row.setContentsMargins(0, 0, 0, 0)
        self.note_title_label = QLabel()
        self.note_title_field = QLineEdit()
        self.note_text_label = QLabel()
        self.note_text_field = QLineEdit()
        for widget in (self.note_title_label, self.note_title_field, self.note_text_label):
            note_row.addWidget(widget)
        note_row.addWidget(self.note_text_field, 2)
        self.note_editor.hide()
        self._note_search_id: int | None = None
        self._note_timer = QTimer(self)
        self._note_timer.setSingleShot(True)
        self._note_timer.setInterval(NOTE_SAVE_DELAY_MS)
        self._note_timer.timeout.connect(self.save_search_note)
        for field in (self.note_title_field, self.note_text_field):
            field.textEdited.connect(lambda _t: self._note_timer.start())
            field.editingFinished.connect(self.save_search_note)
        context_menu.install(
            self.history_table,
            lambda: [self.action_of(self.open_search_button), self.action_of(self.note_search_button), None,
                     self.action_of(self.delete_search_button), self.action_of(self.delete_selected_button)],
            delete_action=self._delete_key_action())

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setObjectName("jobsTable")
        table_filter.attach(self.table, kinds=table_filter.named_kinds(COLUMNS))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for i in range(len(COLUMNS)):
            self.table.setHorizontalHeaderItem(i, QTableWidgetItem())
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.cellDoubleClicked.connect(lambda *_: self.open_ad())
        context_menu.install(self.table, lambda: [
            self.action_of(self.open_button), self.action_of(self.card_button),
            self.action_of(self.employers_button), None, self.action_of(self.favourite_button),
            self.action_of(self.simulate_button)])
        # the shared search box over the postings found (title, description, skills,
        # publisher); runner, embedder and store are set by the main window
        from .search_box import SearchBox

        self.results_search = SearchBox(self._search_documents, name="search of the postings")
        self.results_search.bind_table(self.table, self._search_key_of_row)
        self.map = LazyMap(self._settings_provider, lambda: (self._palette, self._dark))
        self.map.pointActivated.connect(self._map_point)
        self.views = QStackedWidget()
        # the table with its place filters (gui/geo_filter.py: Country -> Region -> Province)
        from .geo_filter import GeoFilter
        from ..geo.places import location_of_posting

        self.geo_filter = GeoFilter(
            self.table, lambda row: location_of_posting(self.postings[row]) if 0 <= row < len(self.postings) else None,
            name="jobsGeoFilter")
        table_page = QWidget()
        table_layout = QVBoxLayout(table_page)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.addWidget(self.geo_filter)
        table_layout.addWidget(self.table, 1)
        self.views.addWidget(table_page)
        self.views.addWidget(self.map)
        self.details = QTextBrowser()
        self.details.setOpenLinks(False)
        self.details.anchorClicked.connect(lambda url: self._open(url.toString()))
        self.result_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.result_splitter.setChildrenCollapsible(False)
        self.result_splitter.addWidget(self.views)
        self.result_splitter.addWidget(self.details)
        self.result_splitter.setStretchFactor(0, 3)
        self.result_splitter.setStretchFactor(1, 1)
        self.result_splitter.setSizes([780, 360])

        self.summary_label = ElidedLabel()
        self.summary_label.setProperty("role", "muted")
        # one line per source; a small scrolling box beside the actions (there are many sources)
        self.sources_label = QTextBrowser()
        self.sources_label.setOpenLinks(False)
        self.sources_label.setMaximumHeight(self.fontMetrics().lineSpacing() * 6 + 8)
        self.attribution_label = QLabel()
        self.attribution_label.setProperty("role", "muted")

        self.open_button = QPushButton()
        self.open_button.clicked.connect(lambda: self.open_ad())
        self.card_button = QPushButton()
        self.card_button.clicked.connect(lambda: self.create_card())
        self.employers_button = QPushButton()
        self.employers_button.clicked.connect(lambda: self.possible_employers())
        self.favourite_button = QPushButton()
        self.favourite_button.clicked.connect(lambda: self.toggle_favourite())
        self.simulate_button = QPushButton()
        self.simulate_button.clicked.connect(lambda: self.simulate())
        self.map_button = QPushButton()
        self.map_button.clicked.connect(lambda: self.toggle_map())
        self.paste_button = QPushButton()
        self.paste_button.clicked.connect(lambda: self.on_paste())
        self.linkedin_button = QPushButton()
        self.linkedin_button.clicked.connect(lambda: self.open_portal("linkedin"))
        self.indeed_button = QPushButton()
        self.indeed_button.clicked.connect(lambda: self.open_portal("indeed"))

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.addWidget(self.query_field, 1)
        search_row.addWidget(self.search_button)
        search_row.addWidget(self.cancel_button)
        search_row.addWidget(self.paste_button)
        history_row = QHBoxLayout()
        history_row.setContentsMargins(0, 0, 0, 0)
        for button in (self.open_search_button, self.note_search_button, self.delete_search_button,
                       self.delete_selected_button):
            history_row.addWidget(button)
        history_row.addStretch(1)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        for button in (self.open_button, self.card_button, self.employers_button):
            actions.addWidget(button)
        actions.addStretch(1)
        actions2 = QHBoxLayout()
        actions2.setContentsMargins(0, 0, 0, 0)
        for button in (self.favourite_button, self.simulate_button, self.map_button):
            actions2.addWidget(button)
        actions2.addStretch(1)
        portals = QHBoxLayout()
        portals.setContentsMargins(0, 0, 0, 0)
        self.portals_label = QLabel()
        self.portals_label.setProperty("role", "muted")
        portals.addWidget(self.portals_label)
        portals.addWidget(self.linkedin_button)
        portals.addWidget(self.indeed_button)
        portals.addStretch(1)

        # The content scrolls when the window is small (as in tab 2): the many rows of this
        # tab must not push the window's minimum height beyond a small laptop screen.
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addWidget(self.title_label)
        layout.addLayout(search_row)
        layout.addWidget(self.help_label)
        layout.addWidget(self.chips)
        layout.addWidget(self.chip_editor)
        layout.addWidget(self.also_label)
        layout.addWidget(self.summary_label)
        # three lists: open postings, companies for a spontaneous application, previous searches
        postings_page = QWidget()
        postings_layout = QVBoxLayout(postings_page)
        postings_layout.setContentsMargins(0, 0, 0, 0)
        postings_layout.addWidget(self.ranking_note)
        postings_layout.addWidget(self.results_search)
        postings_layout.addWidget(self.result_splitter, 1)
        self.companies_view = SpontaneousCompaniesView(run_in_background)
        history_page = QWidget()
        history_layout = QVBoxLayout(history_page)
        history_layout.setContentsMargins(0, 0, 0, 0)
        history_layout.addWidget(self.history_label)
        history_layout.addWidget(self.history_table, 1)
        history_layout.addWidget(self.note_editor)
        history_layout.addLayout(history_row)
        # the postings pasted by hand (Tools > Paste…): shown, edited and deleted here
        self.pasted_button = QPushButton()
        self.pasted_button.clicked.connect(lambda: self.open_pasted())
        self.pasted_edit_button = QPushButton()
        self.pasted_edit_button.clicked.connect(lambda: self.edit_pasted_selected())
        self.pasted_delete_button = QPushButton()
        self.pasted_delete_button.clicked.connect(lambda: self.delete_pasted_selected())
        pasted_row = QHBoxLayout()
        pasted_row.addWidget(self.pasted_button)
        pasted_row.addWidget(self.pasted_edit_button)
        pasted_row.addWidget(self.pasted_delete_button)
        pasted_row.addStretch(1)
        history_layout.addLayout(pasted_row)
        self.showing_pasted = False
        self.on_edit_pasted: Callable[[dict], None] | None = None   # set by the window (paste window)
        self.result_tabs = QTabWidget()
        self.result_tabs.setObjectName("jobsResultTabs")
        self.result_tabs.addTab(postings_page, "")
        self.result_tabs.addTab(self.companies_view, "")
        self.result_tabs.addTab(history_page, "")
        self.result_tabs.currentChanged.connect(lambda _i: self._update_buttons())
        layout.addWidget(self.result_tabs, 1)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        buttons = QVBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addLayout(actions)
        buttons.addLayout(actions2)
        buttons.addLayout(portals)
        bottom.addLayout(buttons)
        sources = QVBoxLayout()
        sources.setContentsMargins(0, 0, 0, 0)
        sources.addWidget(self.sources_label, 1)
        sources.addWidget(self.attribution_label)
        bottom.addLayout(sources, 1)
        postings_layout.addLayout(bottom)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.scroll)
        self.retranslate()

    # --- appearance

    def apply_palette(self, palette: Palette, dark: bool = True) -> None:
        self._palette = palette
        self._dark = dark
        self.map.apply_theme(palette, dark)
        self.companies_view.apply_palette(palette)
        self._show_chips()
        self._fill_table()

    def action_of(self, button: QPushButton) -> QAction:
        """The button's menu QAction (gui/menus.py) for the right-click menus; a local one
        when the panel is used outside the window."""
        action = menu_action_of(button)
        if action is not None:
            if button.text():
                action.setText(button.text())  # a text that follows the selection ("Delete selected (2)")
            return action
        local = self._local_actions.get(id(button))
        if local is None:
            local = QAction(self, triggered=lambda _c=False, b=button: b.click())
            self._local_actions[id(button)] = local
        local.setText(button.text())
        local.setEnabled(button.isEnabled())
        return local

    def _delete_key_action(self) -> QAction:
        self.delete_key_action = QAction(self, triggered=lambda _c=False: self.delete_selected_searches())
        return self.delete_key_action

    # --- query

    def parse_now(self):
        """The query as understood now (local parsing, no network)."""
        from ..jobsearch import parse_query

        text = self.query_field.text().strip()
        if self.query is not None and self.edited and text == self.query.raw:
            return self.query
        self.query = parse_query(text) if text else None
        self.edited = False
        self._show_chips()
        return self.query

    def _show_chips(self) -> None:
        self.chips.setText(chips_html(self.query, self._palette))
        also = also_html(self.query, self._palette)
        self.also_label.setText(also)
        self.also_label.setVisible(bool(also))

    def _also_clicked(self, link: str) -> None:
        action, _, index = link.partition(":")
        if action == "also" and index.isdigit():
            self.remove_also(int(index))

    def remove_also(self, index: int) -> bool:
        """Removes one "Searched also for" term; the postings already found are ranked
        again without it (no source is asked again) and the companies list follows."""
        terms = also_terms(self.query)
        if self.query is None or not 0 <= index < len(terms):
            return False
        term = terms[index][0]
        if not self.query.remove_term(term):
            return False
        self.edited = True
        log.info("Job search: a 'searched also for' term removed")
        self._show_chips()
        if self.all_postings and not self._running:
            self.rerank()
        self.companies_view.set_query(self.query)
        return True

    def rerank(self) -> bool:
        """The postings of the search ranked again for the current query (on a worker)."""
        if self.query is None or self._running or self._reranking:
            return False
        from ..jobsearch.pipeline import rerank_postings

        query, postings, ids = self.query, list(self.all_postings), list(self.all_ids)
        match = self._match
        embedder_provider, store = self.embedder_provider, self.store
        self._reranking = True
        self.summary_label.setText(t("jobs.reranking"))
        from .progress_bar import BarState

        self.on_bar("jobs_rerank", BarState(None, "", t("jobs.reranking")))

        def work():
            embedder = None
            if embedder_provider is not None:
                try:
                    embedder = embedder_provider()
                except Exception:  # noqa: BLE001 - exact words only
                    log.info("Language model not available for the ranking", exc_info=True)
            ranked, stats = rerank_postings(query, postings, cv_profile=match.profile, scorer=match.scorer(),
                                            embedder=embedder, vector_store=store)
            position = {id(p): i for i, p in enumerate(postings)}
            return ranked, [ids[position[id(p)]] if id(p) in position and position[id(p)] < len(ids) else None
                            for p in ranked], stats

        def done(outcome) -> None:
            self._reranking = False
            self.on_bar("jobs_rerank", None)
            ranked, ranked_ids, stats = outcome
            if self.result is None:
                return
            from ..jobsearch import SearchResult

            result = SearchResult(query=query, postings=ranked, reports=list(self.result.reports),
                                  attributions=list(self.result.attributions),
                                  stats=dict(self.result.stats or {}, **{"ranking": stats.get("ranking", "")}),
                                  seconds=self.result.seconds)
            self.show_result(result, ranked_ids, keep_all=True)
            log.info("Job search ranked again: %d of %d postings", len(ranked), len(postings))

        def failed(error) -> None:
            self._reranking = False
            self.on_bar("jobs_rerank", None)
            log.warning("Job search not ranked again: %s", type(error).__name__)
            self.summary_label.setText(t("jobs.failed"))

        run_job(self._run, work, done, failed, name="job search ranking", timeout=RERANK_TIMEOUT_S)
        return True

    def _chip_clicked(self, link: str) -> None:
        action, _, rest = link.partition(":")
        field, _, index = rest.partition(":")
        if self.query is None or not hasattr(self.query, field):
            return
        position = int(index or 0)
        if action == "remove":
            self.remove_chip(field, position)
        elif action == "edit":
            self._editing = (field, position)
            value = getattr(self.query, field)
            current = value[position] if isinstance(value, list) else value
            self.chip_editor_label.setText(t("jobs.chip.edit", field=t(f"jobs.chip.{field}")))
            self.chip_editor_field.setText(str(current if current is not None else ""))
            self.chip_editor.show()
            self.chip_editor_field.setFocus()

    def remove_chip(self, field: str, position: int = 0) -> None:
        value = getattr(self.query, field, None)
        if isinstance(value, list):
            if 0 <= position < len(value):
                value.pop(position)
        else:
            setattr(self.query, field, None if field not in ("role",) else "")
            if field == "place":
                self.query.lat = self.query.lon = self.query.province = self.query.place_level = None
                self.query.region_key = self.query.country = None
                self.query.radius_km = None
            if field == "role":
                self.query.role_words, self.query.role_variants = [], []
        self.edited = True
        log.info("Job search: filter %s removed", field)
        self._show_chips()

    def apply_chip_edit(self) -> None:
        """The chip editor's value replaces the chip (place and radius are checked)."""
        from ..jobsearch import parse_query
        from ..jobsearch._util import fold, words

        if self._editing is None or self.query is None:
            return
        field, position = self._editing
        value = self.chip_editor_field.text().strip()
        self.chip_editor.hide()
        self._editing = None
        if not value:
            self.remove_chip(field, position)
            return
        query = self.query
        if field == "radius_km":
            try:
                query.radius_km = max(1.0, min(300.0, float(value.replace(",", ".").replace("km", "").strip())))
            except ValueError:
                return
        elif field == "place":
            probe = parse_query(value)
            query.place = probe.place or value
            query.lat, query.lon, query.province, query.place_level = probe.lat, probe.lon, probe.province, \
                probe.place_level
            query.region_key, query.country = probe.region_key, probe.country
            if query.radius_km is None and probe.radius_km is not None:
                query.radius_km = probe.radius_km
        elif field == "role":
            query.role = value
            query.role_words = words(value)
            query.role_variants = [fold(value)]
        elif isinstance(getattr(query, field), list):
            items = getattr(query, field)
            if 0 <= position < len(items):
                items[position] = fold(value) if field in ("phrases", "excluded") else value
        elif field in ("contract", "remote", "hours", "seniority"):
            probe = parse_query(value)
            setattr(query, field, getattr(probe, field) or None)
        self.edited = True
        log.info("Job search: filter %s edited", field)
        self._show_chips()

    # --- searching

    @property
    def running(self) -> bool:
        return self._running

    def search(self) -> bool:
        """Starts the search on a worker (False when the box is empty or one is running)."""
        if self._running:
            return False
        query = self.parse_now()
        if query is None:
            self.summary_label.setText(t("jobs.empty_query"))
            return False
        cancel = threading.Event()
        self._cancel = cancel
        self._running = True
        self._update_buttons()
        bridge = self._bridge
        runner = self.search_runner or self._default_runner
        store = self.store
        search_id = None
        if store is not None:
            try:
                search_id = store.create_job_search(query.raw, query.to_dict(), place=query.place or "",
                                                    lat=query.lat, lon=query.lon, radius_km=query.radius_km)
            except Exception:  # noqa: BLE001 - a busy database must not stop the search
                log.warning("Job search not stored", exc_info=True)
        self.current_search_id = search_id
        log.info("Job search started (%d role words, %d skills, place %s, radius %s, contract %s)",
                 len(query.role_words), len(query.skills), bool(query.place), query.radius_km, query.contract)
        self.summary_label.setText(t("jobs.searching"))
        bridge.progress.emit(0, 0, t("jobs.searching"))

        def progress(done: int, total: int, message: str) -> None:
            bridge.progress.emit(int(done), int(total), str(message or ""))

        def work():
            result = runner(query, cancel, progress)
            if store is not None and search_id is not None:
                status = "cancelled" if result.cancelled else "completed"
                try:
                    ids = store.finish_job_search(search_id, status, [p.to_dict() for p in result.postings],
                                                  reports=[r.to_dict() for r in result.reports], stats=result.stats)
                except Exception:  # noqa: BLE001
                    log.warning("Job search results not stored", exc_info=True)
                    ids = [None] * len(result.postings)
            else:
                ids = [None] * len(result.postings)
            return result, ids

        def done(outcome) -> None:
            result, ids = outcome
            self._running = False
            self._cancel = None
            self._update_buttons()
            self.show_result(result, ids)
            self.refresh_history()
            text = t("jobs.done", count=len(result.postings), seconds=f"{result.seconds:.0f}")
            if result.cancelled:
                text = t("jobs.cancelled", count=len(result.postings))
            log.info("Job search finished: %d postings, %d sources%s", len(result.postings), len(result.reports),
                     " (cancelled)" if result.cancelled else "")
            self.on_finished(text)

        def failed(error) -> None:
            self._running = False
            self._cancel = None
            self._update_buttons()
            log.warning("Job search failed: %s", type(error).__name__)
            if store is not None and search_id is not None:
                try:
                    store.finish_job_search(search_id, "failed", [])
                except Exception:  # noqa: BLE001
                    pass
            self.summary_label.setText(t("jobs.failed"))
            self.on_finished(t("jobs.failed"))

        run_job(self._run, work, done, failed, name="job search", timeout=SEARCH_TIMEOUT_S, cancel=cancel)
        return True

    def _default_runner(self, query, cancel, progress):
        from .. import config
        from ..jobsearch import JobSearchCache, build_sources, search_jobs

        settings = self._settings_provider()
        cards, local = self._local_data()
        sources = self.sources_factory() if self.sources_factory is not None else build_sources(
            {name: getattr(settings, name, "") for name in config.JOBSEARCH_KEY_FIELDS}, cards=cards,
            local_postings=local,
            max_pages=int(getattr(settings, "jobsearch_max_pages", config.JOBSEARCH_PAGES_DEFAULT)))
        try:
            cache = JobSearchCache(config.data_folder() / "cache" / "jobsearch")
        except Exception:  # noqa: BLE001
            cache = None
        overrides = dict(getattr(settings, "jobsearch_sources", {}) or {})
        enabled = {s.id for s in sources if overrides.get(s.id, s.enabled_by_default)}
        profile = self._match.profile
        geocoder = None
        if self.geocoder_factory is not None:
            try:
                geocoder = self.geocoder_factory()
            except Exception:  # noqa: BLE001
                log.warning("Geocoder not available for the job search", exc_info=True)
        embedder = None
        if self.embedder_provider is not None:
            try:
                embedder = self.embedder_provider()
            except Exception:  # noqa: BLE001 - the exact words, typos and synonyms still work
                log.info("Language model not available for the job search", exc_info=True)
        return search_jobs(query, sources, cv_profile=profile, scorer=self._match.scorer(), enabled=enabled,
                           cache=cache, progress=progress, cancel=cancel,
                           geocode=limited_geocode(geocoder) if geocoder is not None else None,
                           source_timeout_s=float(getattr(settings, "jobsearch_timeout_s",
                                                          config.JOBSEARCH_TIMEOUT_DEFAULT_S)),
                           embedder=embedder, vector_store=self.store)

    def _local_data(self) -> tuple[list, list]:
        """(cards of "My companies", postings already found by the company searches) for
        the two local sources; read on the worker."""
        from .. import companies

        if self.store is None:
            return [], []
        try:
            cards = companies.list_cards(self.store)
        except Exception:  # noqa: BLE001
            log.warning("Company cards not read for the job search", exc_info=True)
            cards = []
        local = []
        try:
            for search in self.store.list_searches()[:LOCAL_SEARCHES]:
                for company, evaluation in self.store.results_of(search["id"]):
                    for posting in (evaluation.postings if evaluation is not None else []) or []:
                        local.append({"company": company.name, "posting": posting, "lat": company.lat,
                                      "lon": company.lon})
        except Exception:  # noqa: BLE001
            log.warning("Saved postings not read for the job search", exc_info=True)
        local.extend(p for _row, p in self.pasted_postings())
        return cards, local

    # --- postings pasted by hand

    def pasted_postings(self) -> list:
        """[(stored row, jobsearch Posting)] of the postings the user pasted (source "pasted";
        one without its own address gets a "pasted:<id>" key so that it can be listed)."""
        from ..jobsearch import Posting

        if self.store is None:
            return []
        out = []
        try:
            for row in self.store.list_postings(origin="pasted"):
                posting = Posting.from_dict(row["data"])
                posting.source = PASTED_SOURCE
                if not posting.url:
                    posting.url = f"pasted:{row['id']}"
                out.append((row, posting))
        except Exception:  # noqa: BLE001
            log.warning("Pasted postings not read", exc_info=True)
        return out

    def open_pasted(self) -> bool:
        """Shows the pasted postings in the postings list (no network)."""
        if self._running:
            return False
        from ..jobsearch import SearchResult

        items = self.pasted_postings()
        result = SearchResult(query=self.query, postings=[p for _r, p in items], reports=[], stats={})
        self.current_search_id = None
        self.show_result(result, [r["id"] for r, _p in items])
        self.showing_pasted = True
        self.result_tabs.setCurrentIndex(TAB_POSTINGS)
        self._update_pasted_buttons()
        log.info("Pasted postings shown (%d)", len(items))
        return True

    def _selected_pasted_id(self) -> int | None:
        index = self.selected_index()
        if not self.showing_pasted or index is None or index >= len(self.posting_ids):
            return None
        return self.posting_ids[index]

    def delete_pasted_selected(self) -> bool:
        posting_id = self._selected_pasted_id()
        if posting_id is None or self.store is None:
            return False
        if not self.ask(t("jobs.pasted.confirm_delete")):
            return False
        self.store.delete_posting(posting_id)
        log.info("Pasted posting deleted")
        self.open_pasted()
        return True

    def edit_pasted_selected(self) -> bool:
        posting_id = self._selected_pasted_id()
        if posting_id is None or self.on_edit_pasted is None:
            return False
        row = next((r for r, _p in self.pasted_postings() if r["id"] == posting_id), None)
        if row is None:
            return False
        self.on_edit_pasted(row)
        return True

    def _update_pasted_buttons(self) -> None:
        count = len(self.store.list_postings(origin="pasted")) if self.store is not None else 0
        self.pasted_button.setText(t("jobs.pasted.show", n=count))
        self.pasted_button.setEnabled(count > 0)
        self.pasted_edit_button.setText(t("jobs.pasted.edit"))
        self.pasted_delete_button.setText(t("jobs.pasted.delete"))
        selected = self._selected_pasted_id() is not None
        self.pasted_edit_button.setEnabled(selected and self.on_edit_pasted is not None)
        self.pasted_delete_button.setEnabled(selected)

    def cancel(self) -> None:
        if self._cancel is not None:
            log.info("Job search: cancel requested")
            self._cancel.set()
            self.summary_label.setText(t("jobs.cancelling"))

    def _update_buttons(self) -> None:
        self.search_button.setEnabled(not self._running)
        self.cancel_button.setEnabled(self._running)
        posting = self.selected()
        has = posting is not None
        self.open_button.setEnabled(has and bool(posting.url))
        self.card_button.setEnabled(has and bool(posting.company or posting.agency))
        self.employers_button.setEnabled(has and posting.employer_hidden)
        index = self.selected_index()
        self.favourite_button.setEnabled(has and index is not None and self.posting_ids[index] is not None)
        self.simulate_button.setEnabled(has)
        query = self.query
        self.linkedin_button.setEnabled(query is not None)
        self.indeed_button.setEnabled(query is not None)
        chosen = self.selected_search_ids()
        self.open_search_button.setEnabled(len(chosen) == 1 and not self._running)
        self.note_search_button.setEnabled(len(chosen) == 1 and self.store is not None)
        self.delete_search_button.setEnabled(len(chosen) == 1 and not self._running)
        self.delete_selected_button.setEnabled(len(chosen) > 1 and not self._running)
        self.delete_selected_button.setText(t("jobs.history.delete_selected", n=len(chosen)) if len(chosen) > 1
                                            else t("jobs.history.delete_selected_none"))
        favourite = has and index is not None and self.posting_ids[index] in self.favourites
        self.favourite_button.setText(t("jobs.unfavourite") if favourite else t("jobs.favourite"))

    # --- results

    def show_result(self, result, ids: list | None = None, *, keep_all: bool = False) -> None:
        """Shows a result. `keep_all`: a new ranking of the same search (the postings found
        first stay the base of later rankings)."""
        self.result = result
        self.postings = list(result.postings)
        self.posting_ids = list(ids) if ids is not None else [None] * len(self.postings)
        self.showing_pasted = False   # open_pasted sets it again after this call
        if not keep_all:
            self.all_postings, self.all_ids = list(self.postings), list(self.posting_ids)
            query = getattr(result, "query", None)
            if query is not None and hasattr(query, "role_words"):
                self.companies_view.set_query(query, start=bool(query.role_words or query.skills))
        mode = (result.stats or {}).get("ranking")
        self.ranking_note.setText(t("jobs.ranking.semantic") if mode == "semantic" else t("jobs.ranking.keyword"))
        self.ranking_note.setVisible(bool(mode))
        self._load_favourites()
        stats = result.stats or {}
        summary = js_text("jobsearch.result.summary", count=len(self.postings),
                          merged=stats.get("merged_duplicates", 0), far=stats.get("dropped_far", 0))
        if not self.postings:
            summary = js_text("jobsearch.result.none")
        elif stats.get("unknown_distance"):
            summary += " " + js_text("jobsearch.result.unknown_distance", n=stats["unknown_distance"])
        self.summary_label.setText(summary)
        lines = [state_line(r) for r in result.reports]
        self.sources_label.setPlainText("\n".join(lines))
        self.attribution_label.setText(" · ".join(result.attributions))
        self.attribution_label.setVisible(bool(result.attributions))
        self._fill_table()
        if self.postings:
            self.table.selectRow(0)
        self._match.request(self.postings, self._fill_table)
        self._update_map()
        self._update_buttons()

    def _load_favourites(self) -> None:
        self.favourites = set()
        if self.store is None:
            return
        try:
            self.favourites = {row["id"] for row in self.store.list_postings(favourite=True)}
        except Exception:  # noqa: BLE001
            log.warning("Favourite postings not read", exc_info=True)

    def _fill_table(self) -> None:
        self.results_search.rerun()
        p = self._palette
        selected = self.selected_index()
        self.table.setRowCount(len(self.postings))
        for row, posting in enumerate(self.postings):
            result = self._match.cached(posting)
            not_computable = result is None and self._match.has_cv() and self._match.is_known(posting)
            if not_computable:
                match = "–"  # nothing in the ad to compare (no requirements): not "pending" forever
            elif result is None and self._match.has_cv():
                match = PENDING
            else:
                match = badge_text(result) if result is not None else (
                    format_number(posting.score) if posting.score is not None else "–")
            star = "★ " if self.posting_ids[row] in self.favourites else ""
            company = posting.company or (js_text("jobsearch.result.hidden_employer") if posting.employer_hidden
                                          else (posting.agency or "–"))
            distance = format_number(posting.distance_km) if posting.distance_km is not None else "–"
            values = [match, star + posting.title, company, workplace_text(posting), distance,
                      enum_label("contract", posting.contract) or "–", posting.salary or "–",
                      posting.date_posted or "–", source_text(posting.source)]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setToolTip(tooltip_text(result) if result is not None else
                                    t("match.not_computable") if not_computable else
                                    "\n".join(posting.why) or t("match.no_cv"))
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    if result is not None:  # 0-100 %: numeric for sorting and the column filter
                        from .table_filter import FILTER_ROLE

                        item.setData(FILTER_ROLE, badge_percent(result))
                        colour = p.accent if result.score >= 60 else (p.warning if result.score >= 35 else p.error)
                        item.setForeground(QBrush(QColor(colour)))
                elif column == 1:
                    item.setToolTip(posting.title)
                elif column == 2 and posting.employer_hidden:
                    item.setForeground(QBrush(QColor(p.muted)))
                    item.setToolTip(js_text("jobsearch.why.agency", agency=posting.agency or "-"))
                self.table.setItem(row, column, item)
        header = self.table.horizontalHeader()
        for column in range(len(COLUMNS)):
            self.table.resizeColumnToContents(column)
        if header.sectionSize(1) > self.fontMetrics().averageCharWidth() * 46:
            header.resizeSection(1, self.fontMetrics().averageCharWidth() * 46)
        if selected is not None and selected < len(self.postings):
            self.table.selectRow(selected)
        table_sort.resort(self.table)   # the order chosen with a header click stays
        self._show_details()

    def selected_index(self) -> int | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        index = table_sort.source_row(self.table, rows[0].row())
        return index if 0 <= index < len(self.postings) else None

    def selected(self):
        index = self.selected_index()
        return self.postings[index] if index is not None else None

    def select(self, index: int) -> None:
        if 0 <= index < len(self.postings):
            row = table_sort.view_row(self.table, index)
            self.table.selectRow(index if row is None else row)

    def _selection_changed(self) -> None:
        self._update_buttons()
        self._update_pasted_buttons()
        self._show_details()
        index = self.selected_index()
        self.map.select(f"p{index}" if index is not None else None)

    def _show_details(self) -> None:
        p = self._palette
        posting = self.selected()
        if posting is None:
            self.details.setHtml(f"<p style='color:{p.muted}'>{_e(t('jobs.select'))}</p>")
            return
        result = self._match.cached(posting)
        out = [f"<p><b>{_e(posting.title)}</b> · {_e(posting.publisher or '–')}</p>"]
        if result is not None:
            out.append(f"<p style='color:{p.accent}'>{_e(t('jobs.match_line', score=badge_text(result)))} "
                       f"<span style='color:{p.muted}'>{_e(tooltip_text(result).splitlines()[1])}</span></p>")
        why = self._why(posting, result)
        if why:
            out.append("<ul>" + "".join(f"<li>{_e(w)}</li>" for w in why) + "</ul>")
        if posting.url:
            out.append(f"<p><a href='{_e(posting.url)}' style='color:{p.accent}'>{_e(posting.url)}</a> "
                       f"<span style='color:{p.muted}'>({_e(t('jobs.source', source=source_text(posting.source)))})</span></p>")
        if posting.description:
            out.append(f"<p style='color:{p.muted}'>{_e(posting.description[:1200])}</p>")
        self.details.setHtml("".join(out))

    def _why(self, posting, match) -> list[str]:
        """The reasons of a result in the interface language (the stored ones were written
        in the language of the search): the CV part comes from the match above, the rest is
        rendered again from the posting."""
        query = getattr(self.result, "query", None)
        if query is None or not hasattr(query, "place"):
            return list(posting.why)
        from ..jobsearch.pipeline import context_why

        try:
            lines = context_why(query, posting)
        except Exception:  # noqa: BLE001 - the stored lines are better than nothing
            log.warning("Reasons of a posting not rendered", exc_info=True)
            return list(posting.why)
        return lines

    # --- map

    def toggle_map(self) -> None:
        showing_map = self.views.currentIndex() == 1
        if showing_map:
            self.views.setCurrentIndex(0)
        else:
            self.map.ensure_soon()
            self.views.setCurrentIndex(1)
            self._update_map()
        log.info("Job search: %s view", "table" if showing_map else "map")
        self.map_button.setText(t("jobs.show_table") if not showing_map else t("jobs.show_map"))

    def _update_map(self) -> None:
        from ..mapview import MapPoint
        from .cv_match import posting_identity

        points = []
        for index, posting in enumerate(self.postings):
            if posting.lat is None or posting.lon is None:
                continue
            result = self._match.cached(posting)
            label = posting.title + (f" ({badge_text(result)})" if result is not None else "")
            points.append(MapPoint(f"p{index}", posting.lat, posting.lon, label, "posting",
                                   tooltip=f"{posting.publisher or ''}\n{workplace_text(posting)}"))
            for n, hypothesis in enumerate(self.hypotheses.get(posting_identity(posting), [])):
                if hypothesis.lat is None or hypothesis.lon is None:
                    continue
                points.append(MapPoint(f"h{index}_{n}", hypothesis.lat, hypothesis.lon, hypothesis.name,
                                       "hypothesis", tooltip=f"{100 * hypothesis.probability:.0f}% · "
                                                             f"{hypothesis.unverified}"))
        query = self.query if self.result is None else self.result.query
        center = (query.lat, query.lon) if query is not None and query.lat is not None else None
        if center:
            points.append(MapPoint("home", center[0], center[1], query.place or "", "home"))
        self.map.set_data(points, center=center, radius_km=getattr(query, "radius_km", None) if center else None)

    def _map_point(self, point_id: str) -> None:
        if point_id.startswith("p") and point_id[1:].isdigit():
            self.select(int(point_id[1:]))

    def add_hypotheses(self, posting, hypotheses) -> None:
        from .cv_match import posting_identity

        self.hypotheses[posting_identity(posting)] = list(hypotheses)
        self._update_map()

    # --- actions

    def _open(self, url: str) -> bool:
        return open_url(url, self._opener) if self._opener is not None else open_url(url)

    def open_ad(self) -> bool:
        posting = self.selected()
        return posting is not None and self._open(posting.url)

    def create_card(self) -> None:
        posting = self.selected()
        if posting is not None:
            self.on_create_card(posting)

    def possible_employers(self) -> None:
        posting = self.selected()
        if posting is not None:
            self.on_possible_employers(posting)

    def simulate(self) -> None:
        posting = self.selected()
        if posting is not None:
            self.on_simulate(posting)

    def toggle_favourite(self) -> bool:
        index = self.selected_index()
        if index is None or self.store is None or self.posting_ids[index] is None:
            return False
        posting_id = self.posting_ids[index]
        on = posting_id not in self.favourites
        try:
            self.store.set_posting_favourite(posting_id, on)
        except Exception:  # noqa: BLE001
            log.warning("Favourite posting not saved", exc_info=True)
            return False
        (self.favourites.add if on else self.favourites.discard)(posting_id)
        log.info("Job search: posting %s favourites", "added to" if on else "removed from")
        self._fill_table()
        self._update_buttons()
        return True

    def open_portal(self, portal: str) -> bool:
        query = self.parse_now()
        if query is None:
            return False
        log.info("Job search: same search opened in the browser (%s)", portal)
        return self._open(browser_search_urls(query)[portal])

    # --- previous searches

    def refresh_history(self) -> None:
        """The previous job searches (newest first), with the user's titles and notes."""
        import time

        selected = set(self.selected_search_ids())
        searches, notes = [], {}
        if self.store is not None:
            try:
                searches = self.store.list_job_searches()[:200]
            except Exception:  # noqa: BLE001
                log.warning("Previous job searches not read", exc_info=True)
            try:
                notes = self.store.search_notes("jobs")
            except Exception:  # noqa: BLE001 - the searches are listed without their notes
                log.warning("Notes of the job searches not read", exc_info=True)
        table = self.history_table
        blocked = table.blockSignals(True)
        try:
            table.clearSelection()
            table.setRowCount(len(searches))
            self.history_ids = [int(s["id"]) for s in searches]
            for row, search in enumerate(searches):
                note = notes.get(int(search["id"]), {})
                query_text = search["query"] or "–"
                title = note.get("title") or query_text
                values = [time.strftime("%Y-%m-%d %H:%M", time.localtime(search["created"])), title,
                          str(search["found"]), t(f"jobs.history.status.{search['status']}"),
                          note.get("note", "")]
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if column == 1:
                        item.setToolTip(query_text if title == query_text else f"{title}\n{query_text}")
                    elif column == 2:
                        item.setData(table_sort.SORT_ROLE, int(search["found"] or 0))
                    elif column == 4:
                        item.setToolTip(note.get("note", ""))
                    table.setItem(row, column, item)
            table_sort.stamp_rows(table)
            table_sort.resort(table)
            for row in range(table.rowCount()):
                if self._history_id_at(row) in selected:
                    table.selectRow(row)
        finally:
            table.blockSignals(blocked)
        for column in range(len(HISTORY_COLUMNS) - 1):
            table.resizeColumnToContents(column)
        width = self.fontMetrics().averageCharWidth() * 50
        if table.horizontalHeader().sectionSize(1) > width:
            table.horizontalHeader().resizeSection(1, width)
        self.history_label.setText(t("jobs.history.count", n=len(searches)) if searches else t("jobs.history.empty"))
        if self._note_search_id is not None and self._note_search_id not in self.history_ids:
            self._close_note_editor()
        self._update_buttons()

    def _history_id_at(self, row: int) -> int | None:
        index = table_sort.source_row(self.history_table, row)
        return self.history_ids[index] if 0 <= index < len(self.history_ids) else None

    def selected_search_ids(self) -> list[int]:
        model = self.history_table.selectionModel() if hasattr(self, "history_table") else None
        rows = sorted({i.row() for i in model.selectedRows()}) if model is not None else []
        return [s for s in (self._history_id_at(r) for r in rows) if s is not None]

    def select_searches(self, search_ids) -> None:
        wanted = set(search_ids)
        self.history_table.clearSelection()
        mode = self.history_table.selectionMode()
        self.history_table.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        for row in range(self.history_table.rowCount()):
            if self._history_id_at(row) in wanted:
                self.history_table.selectRow(row)
        self.history_table.setSelectionMode(mode)

    def _history_selection_changed(self) -> None:
        chosen = self.selected_search_ids()
        if self._note_search_id is not None and chosen != [self._note_search_id]:
            self.save_search_note()
            self._close_note_editor()
        self._update_buttons()

    def open_selected_search(self) -> bool:
        chosen = self.selected_search_ids()
        if len(chosen) != 1:
            return False
        opened = self.open_search(chosen[0])
        if opened:
            self.result_tabs.setCurrentIndex(TAB_POSTINGS)
        return opened

    def edit_search_note(self) -> bool:
        """"Rename / note": the title and the note of the selected search, saved as typed."""
        chosen = self.selected_search_ids()
        if len(chosen) != 1 or self.store is None:
            return False
        try:
            note = self.store.search_note_get("jobs", chosen[0])
        except Exception:  # noqa: BLE001
            log.warning("Note of a job search not read", exc_info=True)
            note = {"title": "", "note": ""}
        self._note_search_id = chosen[0]
        for field, value in ((self.note_title_field, note.get("title", "")), (self.note_text_field, note.get("note", ""))):
            field.blockSignals(True)
            field.setText(value)
            field.blockSignals(False)
        self.note_editor.show()
        self.result_tabs.setCurrentIndex(TAB_HISTORY)
        self.note_title_field.setFocus()
        log.info("Job search: title / note editor opened")
        return True

    def save_search_note(self) -> bool:
        self._note_timer.stop()
        search_id = self._note_search_id
        if search_id is None or self.store is None:
            return False
        title, note = self.note_title_field.text().strip(), self.note_text_field.text().strip()
        try:
            current = self.store.search_note_get("jobs", search_id)
            if current.get("title", "") == title and current.get("note", "") == note:
                return False
            self.store.search_note_set("jobs", search_id, title, note)
        except Exception:  # noqa: BLE001
            log.warning("Note of a job search not saved", exc_info=True)
            return False
        log.info("Job search: title / note saved")
        self._refresh_history_texts(search_id, title, note)
        return True

    def _refresh_history_texts(self, search_id: int, title: str, note: str) -> None:
        for row in range(self.history_table.rowCount()):
            if self._history_id_at(row) != search_id:
                continue
            query_item = self.history_table.item(row, 1)
            if query_item is not None:
                original = query_item.toolTip().splitlines()[-1] if query_item.toolTip() else query_item.text()
                query_item.setText(title or original)
                query_item.setToolTip(original if not title else f"{title}\n{original}")
            note_item = self.history_table.item(row, 4)
            if note_item is not None:
                note_item.setText(note)
                note_item.setToolTip(note)

    def _close_note_editor(self) -> None:
        self._note_search_id = None
        self.note_editor.hide()

    def open_search(self, search_id) -> bool:
        """Shows a stored search again (no network)."""
        if search_id is None or self.store is None or self._running:
            return False
        from ..jobsearch import Posting, SearchQuery, SearchResult, SourceReport

        search = self.store.read_job_search(search_id)
        if search is None:
            return False
        rows = self.store.postings_of_job_search(search_id)
        from ..jobsearch.query import apply_understanding

        query = apply_understanding(SearchQuery.from_dict(search["understood"]))
        result = SearchResult(query=query, postings=[Posting.from_dict(r["data"]) for r in rows],
                              reports=[SourceReport(**{k: v for k, v in r.items()
                                                       if k in SourceReport.__dataclass_fields__})
                                       for r in search["reports"]], stats=search["stats"])
        self.query = query
        self.edited = True
        self.query_field.blockSignals(True)
        self.query_field.setText(search["query"])
        self.query_field.blockSignals(False)
        self._show_chips()
        self.current_search_id = search_id
        self.show_result(result, [r["id"] for r in rows])
        log.info("Previous job search opened (%d postings)", len(rows))
        return True

    def delete_search(self) -> bool:
        """"Delete": the selected previous search (after a confirmation)."""
        chosen = self.selected_search_ids()
        if len(chosen) != 1:
            return False
        return self._delete_searches(chosen)

    def delete_selected_searches(self) -> bool:
        """"Delete selected" and the Delete key: every selected search (one confirmation)."""
        chosen = self.selected_search_ids()
        if not chosen:
            return False
        return self._delete_searches(chosen)

    def _delete_searches(self, search_ids: list[int]) -> bool:
        if self.store is None or self._running or not search_ids:
            return False
        question = t("jobs.history.confirm_delete_one") if len(search_ids) == 1 else \
            t("jobs.history.confirm_delete_many", n=len(search_ids))
        if not self.ask(question):
            log.info("Deletion of previous job searches cancelled")
            return False
        deleted = 0
        for search_id in search_ids:
            try:
                self.store.delete_job_search(search_id)
                try:
                    self.store.search_note_set("jobs", search_id, "", "")
                except Exception:  # noqa: BLE001 - a note left behind is harmless
                    log.debug("Note of a deleted job search not removed", exc_info=True)
                deleted += 1
            except Exception:  # noqa: BLE001
                log.warning("Previous job search not deleted", exc_info=True)
            if self.current_search_id == search_id:
                self.current_search_id = None
        log.info("Previous job searches deleted: %d", deleted)
        self.refresh_history()
        return deleted > 0

    # --- texts

    def _search_documents(self) -> list:
        from ..scoring.semantic_index import posting_document

        return [posting_document(p, key=f"posting:{i}") for i, p in enumerate(self.postings)]

    def _search_key_of_row(self, row: int) -> str | None:
        return f"posting:{row}" if 0 <= row < len(self.postings) else None

    def retranslate(self) -> None:
        self._update_pasted_buttons()
        self.results_search.retranslate()
        self.geo_filter.retranslate()
        self.title_label.setText(t("jobs.title"))
        self.query_field.setPlaceholderText(js_text("jobsearch.search.placeholder"))
        self.search_button.setText(js_text("jobsearch.search.button"))
        self.cancel_button.setText(js_text("jobsearch.search.cancel"))
        self.help_label.setText(js_text("jobsearch.search.help"))
        self.open_search_button.setText(t("jobs.history.open"))
        self.note_search_button.setText(t("jobs.history.note"))
        self.delete_search_button.setText(t("jobs.history.delete"))
        self.note_title_label.setText(t("jobs.history.note_title"))
        self.note_text_label.setText(t("jobs.history.note_text"))
        self.note_title_field.setPlaceholderText(t("jobs.history.note_title_placeholder"))
        self.note_text_field.setPlaceholderText(t("jobs.history.note_text_placeholder"))
        for i, column in enumerate(HISTORY_COLUMNS):
            self.history_table.horizontalHeaderItem(i).setText(t(f"jobs.history.column.{column}"))
        self.result_tabs.setTabText(TAB_POSTINGS, t("jobs.tab.postings"))
        self.result_tabs.setTabText(TAB_COMPANIES, t("jobs.tab.companies"))
        self.result_tabs.setTabText(TAB_HISTORY, t("jobs.tab.history"))
        self.companies_view.retranslate()
        mode = (getattr(self.result, "stats", None) or {}).get("ranking") if self.result is not None else None
        if mode:
            self.ranking_note.setText(t("jobs.ranking.semantic") if mode == "semantic"
                                      else t("jobs.ranking.keyword"))
        self.paste_button.setText(t("jobs.paste"))
        self.open_button.setText(js_text("jobsearch.result.open"))
        self.card_button.setText(js_text("jobsearch.result.create_card"))
        self.employers_button.setText(js_text("jobsearch.result.possible_employers"))
        self.simulate_button.setText(t("jobs.simulate"))
        self.map_button.setText(t("jobs.show_map") if self.views.currentIndex() == 0 else t("jobs.show_table"))
        self.portals_label.setText(t("jobs.portals"))
        self.linkedin_button.setText(t("jobs.open_linkedin"))
        self.linkedin_button.setToolTip(t("jobs.portal_tooltip"))
        self.indeed_button.setText(t("jobs.open_indeed"))
        self.indeed_button.setToolTip(t("jobs.portal_tooltip"))
        for i, column in enumerate(COLUMNS):
            self.table.horizontalHeaderItem(i).setText(t(f"jobs.column.{column}"))
        self.table.horizontalHeaderItem(0).setToolTip(t("match.column_tooltip"))
        self.map.retranslate()
        self._show_chips()
        self.refresh_history()
        self._fill_table()
        self._update_buttons()
