"""Job search, second list: "Companies for a spontaneous application".

The companies the app already knows (saved cards, the regional register, the local map
index; no network) whose stored text matches the searched profile by meaning
(jobsearch/spontaneous.py), each with the reason (the words found or the sentence close in
meaning, with its source), the affinity for the direction of the search (the window's
affinity service, gui/affinity_box.py columns, numeric sort) and the state of its data:
"Card built", "Not analysed yet" ("Analyse now" builds the card on a worker), "Not saved"
("Create card" saves it with where it came from, then builds it). Under the list, on
request, the assistant's suggestions for this search (gui/suggestions_view.py).

Everything slow runs on the window's worker (`run_in_background`); the progress goes to
the shared bottom bar (`on_progress(owner, BarState | None)`).
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QPushButton, QSplitter,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..i18n import t
from . import context_menu, table_filter, table_sort
from .affinity_box import AFFINITY_COLUMNS, affinity_cells, apply_cell, column_header
from .common import run_job
from .menus import menu_action_of
from .theme import Palette, active_palette
from .widgets import wrapped_label

log = logging.getLogger(__name__)

PROGRESS_OWNER = "jobs_companies"
COMPANIES_TIMEOUT_S = 600.0
COLUMNS = ("name", "origin", "place", "reason") + AFFINITY_COLUMNS + ("state",)


def origin_text(origin: str) -> str:
    return t(f"jobs.companies.origin.{origin}")


def reason_text(candidate) -> str:
    """Why the company matches (the words found, the sentence close in meaning, or the
    sector), with the source of the text."""
    from .search_box import source_label

    source = source_label(candidate.passage) if candidate.passage is not None else origin_text(candidate.origin)
    if candidate.sector and not candidate.terms and not candidate.snippet:
        return t("jobs.companies.reason.sector", sector=candidate.sector, source=origin_text(candidate.origin))
    if candidate.exact and candidate.terms:
        return t("jobs.companies.reason.words", terms=", ".join(candidate.terms[:4]), source=source)
    return t("jobs.companies.reason.meaning", relevance=f"{candidate.relevance:.0f}",
             snippet=candidate.snippet[:90], source=source)


def state_text(candidate) -> str:
    if getattr(candidate, "analysing", False):
        return t("jobs.companies.state.analysing")
    if candidate.card_id is None:
        return t("jobs.companies.state.not_saved")
    return t("jobs.companies.state.built") if candidate.built else t("jobs.companies.state.not_analysed")


class SpontaneousCompaniesView(QWidget):
    """See the module docstring. The window sets the providers and callbacks."""

    def __init__(self, run_in_background: Callable, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("jobsCompaniesView")
        self._run = run_in_background
        self._palette: Palette = active_palette()
        # set by the window (each optional)
        self.cards_provider: Callable[[], list] = lambda: []
        self.register_provider: Callable[[object], tuple[list, list]] = lambda query: ([], [])
        self.map_provider: Callable[[object], list] = lambda query: []
        self.embedder_provider: Callable[[], object] | None = None
        self.vector_store = None
        self.affinity_service_getter: Callable[[], object] = lambda: None
        self.preferences_provider: Callable[[], object] = lambda: None
        self.on_progress: Callable[[str, object], None] = lambda owner, state: None
        self.on_analyse: Callable[[object], object] = lambda candidate: None
        self.on_open_card: Callable[[int], None] = lambda card_id: None
        self.on_notice: Callable[[str], None] = lambda text: None
        self.query = None
        self.candidates: list = []
        self.results: list = []          # AffinityResult | None, aligned with candidates
        self.mode = ""
        self.direction: str | None = None
        self.running = False
        self._cancel: threading.Event | None = None
        self._generation = 0
        self._local_actions: dict[int, QAction] = {}

        self.intro_label = wrapped_label(role="muted")
        self.note_label = wrapped_label(role="muted")
        self.status_label = wrapped_label(role="muted")
        self.refresh_button = QPushButton()
        self.refresh_button.clicked.connect(lambda: self.refresh())
        self.analyse_button = QPushButton()
        self.analyse_button.setProperty("role", "accent")
        self.analyse_button.clicked.connect(lambda: self.analyse_selected())
        self.card_button = QPushButton()
        self.card_button.clicked.connect(lambda: self.create_card_selected())
        self.open_card_button = QPushButton()
        self.open_card_button.clicked.connect(lambda: self.open_card_selected())

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setObjectName("jobsCompaniesTable")
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
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.cellDoubleClicked.connect(lambda *_: self._double_click())
        table_filter.attach(self.table)
        context_menu.install(self.table, lambda: [self.action_of(self.analyse_button),
                                                  self.action_of(self.card_button),
                                                  self.action_of(self.open_card_button), None,
                                                  self.action_of(self.refresh_button)])

        self.suggestions_title = QLabel()
        self.suggestions_title.setProperty("role", "title")
        self.suggestions = None           # SuggestionsView, set by `set_suggestions`
        self._suggestions_holder = QWidget()
        self._suggestions_layout = QVBoxLayout(self._suggestions_holder)
        self._suggestions_layout.setContentsMargins(0, 0, 0, 0)
        self._suggestions_layout.addWidget(self.suggestions_title)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        for button in (self.analyse_button, self.card_button, self.open_card_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.refresh_button)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(self.intro_label)
        top_layout.addWidget(self.note_label)
        top_layout.addWidget(self.status_label)
        top_layout.addWidget(self.table, 1)
        top_layout.addLayout(buttons)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(top)
        self.splitter.addWidget(self._suggestions_holder)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.splitter, 1)
        self.retranslate()

    # --- the suggestions of the assistant

    def set_suggestions(self, view: QWidget | None) -> None:
        """Embeds the SuggestionsView of the window (gui/suggestions_view.py)."""
        if view is None:
            self._suggestions_holder.hide()
            return
        self.suggestions = view
        self._suggestions_layout.addWidget(view, 1)

    # --- actions shared by the buttons, the menus and the context menu

    def action_of(self, button: QPushButton) -> QAction:
        """The button's menu QAction (gui/menus.py), or a local one when the panel is used
        outside the window."""
        action = menu_action_of(button)
        if action is not None:
            if button.text():
                action.setText(button.text())
            return action
        local = self._local_actions.get(id(button))
        if local is None:
            local = QAction(self, triggered=lambda _c=False, b=button: b.click())
            self._local_actions[id(button)] = local
        local.setText(button.text())
        local.setEnabled(button.isEnabled())
        return local

    # --- computing

    def set_query(self, query, *, start: bool = True) -> bool:
        """A new search (or a changed one): the list is computed again."""
        self.query = query
        if not start:
            return False
        return self.refresh()

    def cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def refresh(self) -> bool:
        """Computes the list on a worker (False without a query)."""
        query = self.query
        if query is None:
            self.status_label.setText(t("jobs.companies.no_query"))
            return False
        self.cancel()
        cancel = threading.Event()
        self._cancel = cancel
        self._generation += 1
        generation = self._generation
        self.running = True
        self._update_buttons()
        self.status_label.setText(t("jobs.companies.searching"))
        from .progress_bar import BarState

        self.on_progress(PROGRESS_OWNER, BarState(None, "", t("jobs.companies.searching")))
        cards_provider, register_provider, map_provider = self.cards_provider, self.register_provider, \
            self.map_provider
        embedder_provider, store = self.embedder_provider, self.vector_store
        log.info("Spontaneous companies requested (%d role words, %d skills)", len(query.role_words),
                 len(query.skills))

        def work():
            from ..jobsearch.spontaneous import rank_companies

            cards = _safe(cards_provider, [])
            by_text, by_sector = _safe(lambda: register_provider(query), ([], []))
            if cancel.is_set():
                return [], "keyword"
            map_companies = _safe(lambda: map_provider(query), [])
            embedder = _safe(embedder_provider, None) if embedder_provider is not None else None
            return rank_companies(query, cards=cards, register=by_text, register_by_sector=by_sector,
                                  map_companies=map_companies, embedder=embedder, store=store, cancel=cancel)

        def done(outcome) -> None:
            if generation != self._generation:
                return
            candidates, mode = outcome
            self.running = False
            self._cancel = None
            self.on_progress(PROGRESS_OWNER, None)
            self.show_candidates(candidates, mode)
            self.request_affinity()

        def failed(error) -> None:
            if generation != self._generation:
                return
            self.running = False
            self._cancel = None
            self.on_progress(PROGRESS_OWNER, None)
            log.warning("Spontaneous companies failed: %s", type(error).__name__)
            self.status_label.setText(t("jobs.companies.failed"))
            self.on_notice(t("jobs.companies.failed"))
            self._update_buttons()

        run_job(self._run, work, done, failed, name="spontaneous companies", timeout=COMPANIES_TIMEOUT_S,
                cancel=cancel)
        return True

    def show_candidates(self, candidates: list, mode: str) -> None:
        self.candidates = list(candidates)
        self.results = [None] * len(self.candidates)
        self.mode = mode
        self.note_label.setText(t("jobs.companies.mode.semantic") if mode == "semantic"
                                else t("jobs.companies.mode.keyword"))
        counts = {}
        for candidate in self.candidates:
            counts[candidate.origin] = counts.get(candidate.origin, 0) + 1
        if self.candidates:
            self.status_label.setText(t("jobs.companies.found", count=len(self.candidates),
                                        cards=counts.get("card", 0), register=counts.get("register", 0),
                                        map=counts.get("map", 0)))
        else:
            self.status_label.setText(t("jobs.companies.none"))
        log.info("Spontaneous companies shown: %d (%s)", len(self.candidates), mode)
        self._fill()

    def request_affinity(self) -> bool:
        """The affinity of every row for the direction of the search (on a worker)."""
        service = _safe(self.affinity_service_getter, None)
        if service is None or not self.candidates:
            return False
        from ..jobsearch.spontaneous import direction_for_query

        self.direction = direction_for_query(self.query, _safe(self.preferences_provider, None))
        items = [c.item for c in self.candidates]
        generation = self._generation

        def done(results) -> None:
            if generation != self._generation:
                return
            self.results = list(results or [])[:len(self.candidates)]
            self.results += [None] * (len(self.candidates) - len(self.results))
            self._sort_by_affinity()
            self._fill()

        try:
            return bool(service.compute_for_candidates(items, done, direction=self.direction))
        except Exception:  # noqa: BLE001 - the list stays without the affinity
            log.warning("Affinity of the spontaneous companies not computed", exc_info=True)
            return False

    def _sort_by_affinity(self) -> None:
        from .affinity_service import sort_key

        order = sorted(range(len(self.candidates)),
                       key=lambda i: (sort_key(self.results[i]), not self.candidates[i].exact,
                                      -self.candidates[i].relevance))
        self.candidates = [self.candidates[i] for i in order]
        self.results = [self.results[i] for i in order]

    # --- the table

    def _fill(self) -> None:
        selected = self.selected_index()
        p = self._palette
        self.table.setRowCount(len(self.candidates))
        for row, candidate in enumerate(self.candidates):
            values = {"name": candidate.name, "origin": origin_text(candidate.origin),
                      "place": candidate.place or "–", "reason": reason_text(candidate),
                      "state": state_text(candidate)}
            for column, name in enumerate(COLUMNS):
                if name in AFFINITY_COLUMNS:
                    continue
                item = QTableWidgetItem(values[name])
                if name == "reason":
                    item.setToolTip(candidate.snippet or values[name])
                    item.setData(table_sort.SORT_ROLE, -float(candidate.relevance))
                elif name == "name":
                    item.setToolTip(candidate.name)
                self.table.setItem(row, column, item)
            result = self.results[row] if row < len(self.results) else None
            pending = result is None and self.running is False and _safe(self.affinity_service_getter, None) \
                is not None and getattr(_safe(self.affinity_service_getter, None), "has_cv", lambda: False)()
            cells = affinity_cells(result, p, pending=pending)
            for name in AFFINITY_COLUMNS:
                item = QTableWidgetItem()
                apply_cell(item, cells[name])
                self.table.setItem(row, COLUMNS.index(name), item)
        table_sort.stamp_rows(self.table)
        for column in range(len(COLUMNS)):
            self.table.resizeColumnToContents(column)
        width = self.fontMetrics().averageCharWidth() * 48
        reason = COLUMNS.index("reason")
        if self.table.horizontalHeader().sectionSize(reason) > width:
            self.table.horizontalHeader().resizeSection(reason, width)
        table_sort.resort(self.table)
        if selected is not None and selected < len(self.candidates):
            self.select(selected)
        self._update_buttons()

    def selected_index(self) -> int | None:
        model = self.table.selectionModel()
        rows = model.selectedRows() if model is not None else []
        if not rows:
            return None
        index = table_sort.source_row(self.table, rows[0].row())
        return index if 0 <= index < len(self.candidates) else None

    def selected(self):
        index = self.selected_index()
        return self.candidates[index] if index is not None else None

    def select(self, index: int) -> None:
        if 0 <= index < len(self.candidates):
            row = table_sort.view_row(self.table, index)
            self.table.selectRow(index if row is None else row)

    def row_of(self, name: str) -> int | None:
        return next((i for i, c in enumerate(self.candidates) if c.name == name), None)

    def _update_buttons(self) -> None:
        candidate = self.selected()
        self.analyse_button.setEnabled(candidate is not None and not candidate.built
                                       and not getattr(candidate, "analysing", False))
        self.card_button.setEnabled(candidate is not None and candidate.card_id is None)
        self.open_card_button.setEnabled(candidate is not None and candidate.card_id is not None)
        self.refresh_button.setEnabled(self.query is not None)

    def _double_click(self) -> None:
        candidate = self.selected()
        if candidate is None:
            return
        if candidate.card_id is not None:
            self.open_card_selected()
        else:
            self.create_card_selected()

    # --- row actions

    def analyse_selected(self):
        """"Analyse now": the card is created when needed, then built (on a worker)."""
        candidate = self.selected()
        if candidate is None or candidate.built:
            return None
        return self._analyse(candidate)

    def create_card_selected(self):
        """"Create card": saved with where it came from, then built."""
        candidate = self.selected()
        if candidate is None or candidate.card_id is not None:
            return None
        return self._analyse(candidate)

    def _analyse(self, candidate):
        try:
            card = self.on_analyse(candidate)
        except Exception:  # noqa: BLE001
            log.warning("Spontaneous company not analysed", exc_info=True)
            card = None
        if card is None:
            return None
        candidate.card_id = getattr(card, "id", None)
        candidate.analysing = True
        log.info("Spontaneous company: card %s (%s)", "created" if candidate.origin != "card" else "built",
                 candidate.origin)
        self._fill()
        return card

    def open_card_selected(self) -> bool:
        candidate = self.selected()
        if candidate is None or candidate.card_id is None:
            return False
        self.on_open_card(candidate.card_id)
        return True

    # --- appearance and texts

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        if self.suggestions is not None and hasattr(self.suggestions, "apply_palette"):
            self.suggestions.apply_palette(palette)
        self._fill()

    def retranslate(self) -> None:
        self.intro_label.setText(t("jobs.companies.intro"))
        self.refresh_button.setText(t("jobs.companies.refresh"))
        self.analyse_button.setText(t("jobs.companies.analyse"))
        self.card_button.setText(t("jobs.companies.create_card"))
        self.open_card_button.setText(t("jobs.companies.open_card"))
        self.suggestions_title.setText(t("jobs.companies.suggest_title"))
        for i, name in enumerate(COLUMNS):
            if name in AFFINITY_COLUMNS:
                text, tip = column_header(name)
                self.table.horizontalHeaderItem(i).setText(text)
                self.table.horizontalHeaderItem(i).setToolTip(tip)
            else:
                self.table.horizontalHeaderItem(i).setText(t(f"jobs.companies.column.{name}"))
        if self.mode:
            self.note_label.setText(t("jobs.companies.mode.semantic") if self.mode == "semantic"
                                    else t("jobs.companies.mode.keyword"))
        if self.suggestions is not None and hasattr(self.suggestions, "retranslate"):
            self.suggestions.retranslate()
        self._fill()


def _safe(function, default):
    try:
        return function() if function is not None else default
    except Exception:  # noqa: BLE001 - one missing source must not empty the list
        log.warning("A source of the spontaneous companies is not available", exc_info=True)
        return default
