"""Saved company cards: the tab "My companies" (filters, semantic search, card editor,
companies added by hand, similar companies) and the "Favourites" window (starred
cards with their best posting, salary, contract and work mode).

Both read and write through `companies.py`; the semantic search runs in a worker
thread through the `run_in_background(function, on_success, on_error)` callable
given by the main window (the first search loads the language model)."""

from __future__ import annotations

import logging
import webbrowser
from typing import Callable

from PySide6.QtCore import QTime, QTimer, Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from .. import companies
from ..i18n import t
from ..models import Assessment, HiringStatus
from .common import enum_label, format_number, is_openable_url, open_url, run_job
from .. import display_text
from ..display_text import display
from .company_card import CompanyCardView, tr as card_tr
from .results_table import (
    STAR_OFF, STAR_ON, company_contract, company_remote, company_salary, is_permanent, is_remote,
    status_text,
)
from .theme import Palette, active_palette, section
from . import table_filter, table_sort
from .widgets import ElidedLabel, star_icon, wrapped_label
from .affinity_box import AFFINITY_COLUMNS, affinity_cells, apply_cell, column_header, scores_tooltip
from .affinity_box import sort_value as affinity_sort_value

log = logging.getLogger(__name__)

SEARCH_LIMIT = 200
CARD_AUTOSAVE_MS = 500  # notes, contacts and tags are saved after this pause
ALL_STATUSES = ""  # combo value: no status filter


# --- pure helpers ---------------------------------------------------------------------------


def card_assessment(card) -> Assessment | None:
    try:
        return card.assessment_obj()
    except Exception:
        log.debug("Card assessment not readable", exc_info=True)
        return None


def card_values(card) -> dict[str, str]:
    """Texts shown for a card in the lists (from its last assessment snapshot)."""
    evaluation = card_assessment(card)
    try:
        hiring = HiringStatus(card.hiring_status)
    except ValueError:
        hiring = HiringStatus.NOT_ASSESSED
    # posting titles come from the sites: shown in English for an English interface
    best = display(evaluation.postings[0].title) if evaluation and evaluation.postings else ""
    return {
        "star": STAR_ON if card.favourite else STAR_OFF,
        "company": card.name,
        "workplace": companies.workplace(card),
        "card_status": enum_label("card_status", card.status),
        "hiring": status_text(hiring),
        "best_posting": best,
        "salary": display(company_salary(evaluation)),
        "contract": enum_label("contract", company_contract(evaluation)),
        "remote": enum_label("remote", company_remote(evaluation)),
        "revenue_trend": revenue_trend_cell(card),
    }


def revenue_trend_cell(card) -> str:
    """"+6,3 %/anno (2021–2024)" from the card's stored revenue (>= 3 years), else ""."""
    from ..financials.trend import trend_cell
    from .company_card import fin_tr

    try:
        return trend_cell(card.revenue_trend(), fin_tr)
    except Exception:  # noqa: BLE001 - a damaged series must not break the list
        return ""


def revenue_trend_key(card) -> float | None:
    """Sort key of "Revenue trend": the CAGR (None = no trend: always last)."""
    try:
        return card.revenue_trend().cagr
    except Exception:  # noqa: BLE001
        return None


# Sort keys of the card tables: numbers and ranks where the text would sort badly.
_HIRING_RANK = {"hiring": 0, "maybe": 1, "no_signal": 2, "not_assessed": 3, "unreachable": 4}


def card_sort_key(card, column: str, score: float | None = None, affinity=None):
    """Sort key of a card for a column of the "My companies" / favourites tables
    (None = no value: always last; `affinity`: the card's AffinityResult)."""
    if column in AFFINITY_COLUMNS:
        value = affinity_sort_value(affinity, column)
        if value is None:
            return None
        return (0, float(value)) if isinstance(value, (int, float)) else (1,) + table_sort.text_key(value)
    if column == "star":
        return 0 if card.favourite else 1
    if column == "score":
        return score
    if column == "hiring":
        return _HIRING_RANK.get(card.hiring_status, 9)
    if column == "card_status":
        return companies.CARD_STATUSES.index(card.status) if card.status in companies.CARD_STATUSES else 9
    if column == "revenue_trend":
        return revenue_trend_key(card)
    if column == "salary":
        from .results_table import best_salary_posting, salary_sort_value

        return salary_sort_value(best_salary_posting(card_assessment(card)))
    value = card_values(card).get(column, "")
    if not value:
        return None
    number = table_sort.parse_number(value) if table_sort.is_number_text(value) else None
    return (0, number) if number is not None else (1,) + table_sort.text_key(value)


def sort_cards(items: list, column: str | None, descending: bool, key=lambda item: (item, None),
               affinity=None) -> list:
    """`items` sorted by `column` (missing values last); `key(item)` gives (card, score).
    `affinity(card)`: the card's AffinityResult; without a column the order is then the
    affinity's ("postings first, then spontaneous"; cards without a result after, in their
    order)."""
    if not column:
        if affinity is None:
            return list(items)
        from .affinity_service import sort_key as affinity_key

        results = [affinity(key(i)[0]) for i in items]
        if not any(r is not None for r in results):
            return list(items)
        order = sorted(range(len(items)), key=lambda n: affinity_key(results[n]))
        return [items[n] for n in order]
    keyed = [(card_sort_key(key(i)[0], column, key(i)[1], affinity(key(i)[0]) if affinity else None), i)
             for i in items]
    present = [pair for pair in keyed if pair[0] is not None]
    missing = [pair[1] for pair in keyed if pair[0] is None]
    present.sort(key=lambda pair: pair[0], reverse=descending)
    return [pair[1] for pair in present] + missing


def remembered_sort(table, columns) -> tuple[str | None, bool]:
    """(column, descending) chosen earlier in the session for this table."""
    sorter = table_sort.sorter_of(table)
    remembered = table_sort.remembered(sorter.key) if sorter is not None else None
    if remembered is None or not 0 <= remembered[0] < len(columns):
        return None, False
    return columns[remembered[0]], not remembered[1]


def record_sort(table, columns, column: str | None, descending: bool) -> None:
    """The header arrow and the session memory of a card table."""
    sorter = table_sort.sorter_of(table)
    if sorter is not None:
        sorter.record(columns.index(column) if column in columns else None, not descending)


def sort_label(text: str, column: str, sort_column: str | None, descending: bool) -> str:
    if column == sort_column:
        text = f"{text} {'▼' if descending else '▲'}".strip()
    return text


def best_posting_url(card) -> str | None:
    evaluation = card_assessment(card)
    if evaluation and evaluation.postings:
        return evaluation.postings[0].url
    return None


def _fill_row(table: QTableWidget, row: int, columns: tuple[str, ...], values: dict[str, str],
              card, palette: Palette, cells: dict | None = None) -> None:
    """`cells`: the affinity cells (affinity_box.affinity_cells) when the window has the service."""
    evaluation = card_assessment(card)
    remote, permanent = is_remote(evaluation), is_permanent(evaluation)
    for c, column in enumerate(columns):
        value = values.get(column, "")
        item = QTableWidgetItem(value)
        if column in AFFINITY_COLUMNS:
            if cells and column in cells:
                apply_cell(item, cells[column], table.font())
            table.setItem(row, c, item)
            continue
        if column == "score" and cells is not None:
            item.setForeground(QBrush(QColor(palette.muted)))  # secondary to the affinity
        if value:
            item.setToolTip(value)
        bold = False
        if column == "star":
            item.setText("")
            item.setToolTip(t("table.star_tooltip"))
            item.setData(Qt.ItemDataRole.UserRole, value)
            item.setIcon(star_icon(palette.warning, value == STAR_ON))
        if column in ("company", "remote") and remote:
            item.setBackground(QBrush(QColor(palette.remote_bg)))
            item.setForeground(QBrush(QColor(palette.remote_fg)))
            bold = True
        if column == "contract" and permanent:
            item.setForeground(QBrush(QColor(palette.accent)))
            bold = True
        if column == "hiring" and card.hiring_status == HiringStatus.HIRING.value:
            item.setForeground(QBrush(QColor(palette.hiring_fg)))
            item.setBackground(QBrush(QColor(palette.hiring_bg)))
        if bold:
            font = QFont(table.font())
            font.setBold(True)
            item.setFont(font)
        table.setItem(row, c, item)


def _table(columns, name: str = "") -> QTableWidget:
    """A list of cards; `columns` = the column names (filters by column kind)."""
    names = tuple(columns) if not isinstance(columns, int) else ()
    table = QTableWidget(0, len(names) if names else int(columns))
    if name:
        table.setObjectName(name)
    table_filter.attach(table, kinds=table_filter.named_kinds(names, affinity="number"), sorting="delegate")
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setTextElideMode(Qt.TextElideMode.ElideRight)
    table.setWordWrap(False)
    table.verticalHeader().setVisible(False)
    table.setShowGrid(False)
    table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    for i in range(table.columnCount()):
        table.setHorizontalHeaderItem(i, QTableWidgetItem())
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    return table


# --- favourites window ---------------------------------------------------------------------


def header_text(column: str) -> tuple[str, str]:
    """(text, tooltip) of a column of the card tables."""
    if column in AFFINITY_COLUMNS:
        return column_header(column)
    if column == "star":
        return "", t("table.star_tooltip")
    return t(f"cards.column.{column}"), scores_tooltip() if column == "score" else ""


def affinity_results(service, cards: list) -> dict[int, object]:
    """card id -> AffinityResult (cached) of the cards; the missing ones are requested."""
    if service is None or not cards:
        return {}
    try:
        results = service.results_for(cards)
    except Exception:  # noqa: BLE001 - the affinity must never break the list
        log.warning("Affinity of the cards not read", exc_info=True)
        return {}
    return {card.id: result for card, result in zip(cards, results)}


def affinity_cells_of(service, card, results: dict, palette: Palette) -> dict | None:
    if service is None:
        return None
    result = results.get(card.id)
    return affinity_cells(result, palette, pending=result is None and service.is_pending(card))


def _card_location(cards, row: int):
    """The normalised workplace of the card drawn in `row` (for the place filters)."""
    from ..geo.places import location_of_company

    if not 0 <= row < len(cards):
        return None
    card = cards[row]
    return location_of_company(card=card, assessment=card_assessment(card))


class FavouritesDialog(QDialog):
    """Non-modal window with the starred companies."""

    COLUMNS = ("star", "company") + AFFINITY_COLUMNS + (
        "workplace", "card_status", "hiring", "best_posting", "salary", "contract", "remote", "revenue_trend")

    def __init__(self, parent: QWidget, store, *, opener=webbrowser.open,
                 on_changed: Callable[[], None] | None = None,
                 on_open_card: Callable[[int], None] | None = None,
                 on_mail: Callable[[object], None] | None = None):
        super().__init__(parent)
        self.setModal(False)
        self._on_mail = on_mail
        self._store = store
        self._opener = opener
        self._on_changed = on_changed or (lambda: None)
        self._on_open_card = on_open_card or (lambda _id: None)
        self._palette: Palette = active_palette()
        self.cards: list = []
        self.sort_column: str | None = None
        self.descending = False
        # the window's AffinityService (gui/affinity_service.py), when the window has one
        self.affinity_service = getattr(parent, "affinity_service", None)
        self._affinity: dict[int, object] = {}
        if self.affinity_service is not None:
            self.affinity_service.listeners.append(self._affinity_changed)
            self.finished.connect(lambda _r: self._drop_listener())

        self.intro_label = wrapped_label(role="muted")
        from .search_box import SearchBox

        # the shared search box; runner, embedder and store are set by the main window
        self.search_box = SearchBox(self._search_documents, store=store, name="search of the favourites")
        self.table = _table(self.COLUMNS, "favouritesTable")
        self.search_box.bind_table(self.table, self._search_key_of_row)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COLUMNS.index("best_posting"), QHeaderView.ResizeMode.Stretch)
        header.setSectionsClickable(True)
        header.sectionClicked.connect(lambda i: self.sort_by(self.COLUMNS[i]))
        self.sort_column, self.descending = remembered_sort(self.table, self.COLUMNS)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.cellDoubleClicked.connect(lambda *_: self.open_card())
        self.empty_label = wrapped_label(role="muted")

        self.site_button = QPushButton()
        self.careers_button = QPushButton()
        self.linkedin_button = QPushButton()
        self.posting_button = QPushButton()
        self.unstar_button = QPushButton()
        self.card_button = QPushButton()
        self.mail_button = QPushButton()
        self.mail_button.setVisible(on_mail is not None)
        self.close_button = QPushButton()
        self.mail_button.clicked.connect(lambda: self.write_mail())
        self.site_button.clicked.connect(lambda: self.open_site())
        self.careers_button.clicked.connect(lambda: self.open_careers())
        self.linkedin_button.clicked.connect(lambda: self.open_linkedin())
        self.posting_button.clicked.connect(lambda: self.open_posting())
        self.unstar_button.clicked.connect(lambda: self.remove_star())
        self.card_button.clicked.connect(lambda: self.open_card())
        self.close_button.clicked.connect(self.close)
        buttons = QHBoxLayout()
        for button in (self.site_button, self.careers_button, self.linkedin_button, self.posting_button,
                       self.unstar_button, self.card_button, self.mail_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.intro_label)
        layout.addWidget(self.search_box)
        from .geo_filter import GeoFilter

        self.geo_filter = GeoFilter(self.table, lambda row: _card_location(self.cards, row), self,
                                    name="favouritesGeoFilter")
        layout.addWidget(self.geo_filter)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.empty_label)
        layout.addLayout(buttons)
        self.retranslate()
        metrics = self.fontMetrics()
        self.resize(max(self.sizeHint().width(), metrics.averageCharWidth() * 150), metrics.height() * 26)

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.reload()

    def _drop_listener(self) -> None:
        service = self.affinity_service
        if service is not None and self._affinity_changed in service.listeners:
            service.listeners.remove(self._affinity_changed)

    def _affinity_changed(self) -> None:
        if self.isVisible():
            self.reload()

    def reload(self) -> None:
        selected = self.selected()
        cards = companies.list_cards(self._store, favourites_only=True)
        service = self.affinity_service
        self._affinity = affinity_results(service, cards)
        self.cards = sort_cards(cards, self.sort_column, self.descending,
                                affinity=(lambda card: self._affinity.get(card.id)) if service is not None else None)
        self.table.setRowCount(len(self.cards))
        for r, card in enumerate(self.cards):
            _fill_row(self.table, r, self.COLUMNS, card_values(card), card, self._palette,
                      affinity_cells_of(service, card, self._affinity, self._palette))
        if selected is not None:
            for r, card in enumerate(self.cards):
                if card.id == selected.id:
                    self.table.selectRow(r)
        self.empty_label.setText("" if self.cards else t("favourites.empty"))
        self.empty_label.setVisible(not self.cards)
        self._update_buttons()
        self.search_box.rerun()

    def _search_documents(self) -> list:
        from ..scoring.semantic_index import card_document

        return [card_document(card) for card in self.cards]

    def _search_key_of_row(self, row: int) -> str | None:
        return f"card:{self.cards[row].id}" if 0 <= row < len(self.cards) else None

    def sort_by(self, column: str) -> None:
        """Header click: sort by `column`; a second click reverses the order."""
        if self.sort_column == column:
            self.descending = not self.descending
        else:
            self.sort_column, self.descending = column, False
        record_sort(self.table, self.COLUMNS, self.sort_column, self.descending)
        self._header_texts()
        self.reload()

    def _header_texts(self) -> None:
        for i, column in enumerate(self.COLUMNS):
            item = self.table.horizontalHeaderItem(i)
            text, tooltip = header_text(column)
            item.setText(text)
            item.setToolTip(tooltip)
            if column == "star":
                item.setIcon(star_icon(self._palette.warning, True))

    def selected(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        i = rows[0].row()
        return self.cards[i] if 0 <= i < len(self.cards) else None

    def select(self, index: int) -> None:
        if 0 <= index < len(self.cards):
            self.table.selectRow(index)

    def _update_buttons(self) -> None:
        card = self.selected()
        self.site_button.setEnabled(bool(card and is_openable_url(card.site)))
        self.careers_button.setEnabled(bool(card and is_openable_url(card.careers_url)))
        self.linkedin_button.setEnabled(card is not None)
        self.posting_button.setEnabled(bool(card and is_openable_url(best_posting_url(card))))
        self.unstar_button.setEnabled(card is not None)
        self.card_button.setEnabled(card is not None)
        self.mail_button.setEnabled(card is not None)

    def write_mail(self) -> bool:
        card = self.selected()
        if card is None or self._on_mail is None:
            return False
        self._on_mail(card)
        return True

    def open_site(self) -> bool:
        card = self.selected()
        return bool(card) and open_url(card.site, self._opener)

    def open_careers(self) -> bool:
        card = self.selected()
        return bool(card) and open_url(card.careers_url, self._opener)

    def open_linkedin(self) -> bool:
        card = self.selected()
        return bool(card) and open_url(companies.linkedin_search_url(card.name), self._opener)

    def open_posting(self) -> bool:
        card = self.selected()
        return bool(card) and open_url(best_posting_url(card), self._opener)

    def remove_star(self) -> bool:
        card = self.selected()
        if card is None:
            return False
        companies.set_favourite(self._store, card.id, False)
        self.reload()
        self._on_changed()
        return True

    def open_card(self) -> bool:
        card = self.selected()
        if card is None:
            return False
        self._on_open_card(card.id)
        return True

    def retranslate(self) -> None:
        self.setWindowTitle(t("favourites.title"))
        self.geo_filter.retranslate()
        self.search_box.retranslate()
        self.intro_label.setText(t("favourites.intro"))
        self._header_texts()
        self.site_button.setText(t("detail.open_site"))
        self.careers_button.setText(t("detail.open_careers"))
        self.linkedin_button.setText(t("detail.open_linkedin"))
        self.linkedin_button.setToolTip(t("detail.open_linkedin_tooltip"))
        self.posting_button.setText(t("detail.open_posting"))
        self.unstar_button.setText(t("favourites.remove_star"))
        self.card_button.setText(t("favourites.open_card"))
        self.mail_button.setText(t("mail.write"))
        self.mail_button.setToolTip(t("mail.write_tooltip"))
        self.close_button.setText(t("common.close"))
        self.reload()


# --- add by hand -----------------------------------------------------------------------------


class AddCompanyDialog(QDialog):
    """Name and website of a company added by hand."""

    def __init__(self, parent: QWidget, on_add: Callable[[str, str], bool]):
        super().__init__(parent)
        self._on_add = on_add
        self.setWindowTitle(t("cards.add_title"))
        self.name_field = QLineEdit()
        self.site_field = QLineEdit()
        self.site_field.setPlaceholderText("https://www.example.com")
        self.error_label = wrapped_label(role="error")
        form = QFormLayout()
        form.addRow(t("cards.name"), self.name_field)
        form.addRow(t("cards.site"), self.site_field)
        self.add_button = QPushButton(t("cards.add"))
        self.add_button.setProperty("role", "accent")
        self.add_button.setDefault(True)
        self.add_button.clicked.connect(self.add)
        self.cancel_button = QPushButton(t("common.cancel"))
        self.cancel_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.error_label)
        layout.addLayout(buttons)
        self.error_label.hide()
        self.resize(max(self.sizeHint().width(), self.fontMetrics().averageCharWidth() * 70),
                    self.sizeHint().height())

    def add(self) -> bool:
        name = " ".join(self.name_field.text().split())
        site = self.site_field.text().strip()
        if not name:
            self.error_label.setText(t("cards.name_required"))
            self.error_label.show()
            return False
        if site and not is_openable_url(site):
            self.error_label.setText(t("cards.site_invalid"))
            self.error_label.show()
            return False
        if self._on_add(name, site):
            self.accept()
            return True
        return False


# --- tab "My companies" ----------------------------------------------------------------------


class CompaniesPanel(QWidget):
    COLUMNS = ("star", "company") + AFFINITY_COLUMNS + (
        "score", "workplace", "hiring", "salary", "remote", "card_status", "revenue_trend", "snippet")

    def __init__(self, store, run_in_background: Callable, embedder_provider: Callable[[], object],
                 dialogs, *, opener=webbrowser.open, on_changed: Callable[[], None] | None = None,
                 settings_provider: Callable[[], object] | None = None, fetcher_factory: Callable | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._store = store
        self._run = run_in_background
        self._embedder_provider = embedder_provider
        self._dialogs = dialogs
        self._opener = opener
        self._on_changed = on_changed or (lambda: None)
        self._palette: Palette = active_palette()
        self._model_ready = False
        self._busy = False
        self._mode = "list"  # "list", "search" or "similar"
        self._query = ""
        self._relevance_pending = False
        self.rows: list[tuple[object, float | None, str]] = []
        self.current = None
        self.sort_column: str | None = None
        self.descending = False
        self.add_dialog: AddCompanyDialog | None = None
        # the window's AffinityService (gui/affinity_service.py), set by the affinity feature
        self.affinity_service = None
        self._affinity: dict[int, object] = {}  # card id -> AffinityResult of the last redraw

        self.intro_label = wrapped_label(role="muted")
        self.status_label = QLabel()
        self.status_combo = QComboBox()
        self.status_combo.addItem("", ALL_STATUSES)
        for status in companies.CARD_STATUSES:
            self.status_combo.addItem("", status)
        self.favourites_only = QCheckBox()
        self.hiring_only = QCheckBox()
        self.add_button = QPushButton()
        self.status_combo.currentIndexChanged.connect(lambda _i: self._filters_changed())
        self.favourites_only.toggled.connect(lambda _c: self._filters_changed())
        self.hiring_only.toggled.connect(lambda _c: self._filters_changed())
        self.add_button.clicked.connect(lambda: self.open_add_dialog())
        filters = QHBoxLayout()
        filters.setContentsMargins(0, 0, 0, 0)
        filters.addWidget(self.status_label)
        filters.addWidget(self.status_combo)
        filters.addWidget(self.favourites_only)
        filters.addWidget(self.hiring_only)
        filters.addStretch(1)
        filters.addWidget(self.add_button)

        # the shared search box (gui/search_box.py): exact words at once while typing,
        # then meaning + exact words on the worker; the rows follow its relevance
        from .search_box import SearchBox

        self.search_box = SearchBox(self._search_documents, runner=run_in_background,
                                    embedder_provider=embedder_provider, store=store,
                                    name="search of my companies")
        self.search_field = self.search_box.field
        self.search_field.textChanged.connect(self._query_changed)
        self.search_box.resultsChanged.connect(self._search_results)
        self.search_box.started.connect(
            lambda: self._set_busy(True, "cards.searching" if self._model_ready else "cards.loading_model"))
        self.search_box.finished.connect(self._search_finished)
        self._hit_sources: dict[int, tuple[str, str]] = {}   # card id -> (source label, snippet html)
        self.search_button = QPushButton()
        self.search_button.setProperty("role", "accent")
        self.search_button.clicked.connect(lambda: self.search())
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setMaximumWidth(self.fontMetrics().averageCharWidth() * 16)
        self.progress.hide()
        search = QHBoxLayout()
        search.setContentsMargins(0, 0, 0, 0)
        search.addWidget(self.search_box, 1)
        search.addWidget(self.search_button)
        search.addWidget(self.progress)
        self.search_info = ElidedLabel()
        self.search_info.setProperty("role", "muted")

        self.table = _table(self.COLUMNS, "companiesTable")
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COLUMNS.index("company"), QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setSectionsClickable(True)
        header.sectionClicked.connect(lambda i: self.sort_by(self.COLUMNS[i]))
        self.sort_column, self.descending = remembered_sort(self.table, self.COLUMNS)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.cellClicked.connect(self._cell_clicked)
        # several cards can be selected for "Create full card" on all of them
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.batch_selected_button = QPushButton()
        self.batch_selected_button.clicked.connect(lambda: self.build_selected())
        self.batch_favourites_button = QPushButton()
        self.batch_favourites_button.clicked.connect(lambda: self.build_favourites())
        batch = QHBoxLayout()
        batch.setContentsMargins(0, 0, 0, 0)
        batch.addWidget(self.batch_selected_button)
        batch.addWidget(self.batch_favourites_button)
        batch.addStretch(1)

        # card editor
        self.editor = QGroupBox()
        self.name_label = QLabel()
        self.name_label.setProperty("role", "title")
        self.name_label.setWordWrap(True)
        self.site_label = ElidedLabel()
        self.site_label.setProperty("role", "muted")
        self.info_label = wrapped_label(role="muted")
        self.card_status_label = QLabel()
        self.card_status = QComboBox()
        for status in companies.CARD_STATUSES:
            self.card_status.addItem("", status)
        self.favourite_box = QCheckBox()
        self.tags_label = QLabel()
        self.tags_field = QLineEdit()
        self.notes_label = QLabel()
        self.notes_field = QPlainTextEdit()
        self.contacts_label = QLabel()
        self.contacts_field = QPlainTextEdit()
        self.contacts_field.setMaximumHeight(self.fontMetrics().height() * 4 + 12)
        self.save_button = QPushButton()
        self.save_button.setProperty("role", "accent")
        self.save_button.clicked.connect(lambda: self.save_card())
        # Notes, contacts and tags are saved as they are typed (after CARD_AUTOSAVE_MS, on a
        # worker); the status and the star at once. "Save" stays as "save now".
        self._filling = False
        self._card_autosave = QTimer(self)
        self._card_autosave.setSingleShot(True)
        self._card_autosave.setInterval(CARD_AUTOSAVE_MS)
        self._card_autosave.timeout.connect(self.autosave_card)
        for field_ in (self.notes_field, self.contacts_field):
            field_.textChanged.connect(self._card_edited)
        self.tags_field.textEdited.connect(lambda _text: self._card_edited())
        self.card_status.activated.connect(lambda _i: self._card_choice_changed())
        self.favourite_box.clicked.connect(lambda _checked=False: self._card_choice_changed())
        self.site_button = QPushButton()
        self.site_button.clicked.connect(lambda: self.current is not None and open_url(self.current.site, self._opener))
        self.linkedin_button = QPushButton()
        self.linkedin_button.clicked.connect(lambda: self.open_linkedin())
        self.similar_button = QPushButton()
        self.similar_button.clicked.connect(lambda: self.find_similar())
        self.delete_button = QPushButton()
        self.delete_button.clicked.connect(lambda: self.delete_card())
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.addWidget(self.card_status_label)
        status_row.addWidget(self.card_status, 1)
        status_row.addWidget(self.favourite_box)
        editor_buttons = QHBoxLayout()
        editor_buttons.setContentsMargins(0, 0, 0, 0)
        editor_buttons.addWidget(self.save_button)
        editor_buttons.addWidget(self.site_button)
        editor_buttons.addWidget(self.linkedin_button)
        editor_buttons.addWidget(self.similar_button)
        editor_buttons.addStretch(1)
        editor_buttons.addWidget(self.delete_button)
        editor_layout = QVBoxLayout(self.editor)
        editor_layout.addWidget(self.name_label)
        editor_layout.addWidget(self.site_label)
        editor_layout.addWidget(self.info_label)
        editor_layout.addLayout(status_row)
        editor_layout.addWidget(self.tags_label)
        editor_layout.addWidget(self.tags_field)
        editor_layout.addWidget(self.notes_label)
        editor_layout.addWidget(self.notes_field, 1)
        editor_layout.addWidget(self.contacts_label)
        editor_layout.addWidget(self.contacts_field)
        editor_layout.addLayout(editor_buttons)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 4, 0)
        left_layout.addLayout(filters)
        left_layout.addLayout(search)
        left_layout.addWidget(self.search_info)
        from .geo_filter import GeoFilter

        self.geo_filter = GeoFilter(self.table, lambda row: _card_location([r[0] for r in self.rows], row), self,
                                    name="companiesGeoFilter")
        left_layout.addWidget(self.geo_filter)
        left_layout.addWidget(self.table, 1)
        left_layout.addLayout(batch)

        # company card and the user's notes, side by side as tabs
        self._settings_provider = settings_provider or self._window_settings
        self.card_view = CompanyCardView(store, run_in_background, settings_provider=self._settings_provider,
                                         opener=opener, on_changed=self._card_built,
                                         fetcher_factory=fetcher_factory)
        self.side_tabs = QTabWidget()
        self.side_tabs.addTab(self.card_view, "")
        # the notes form scrolls: its fields must not set the window's minimum height
        self.editor_scroll = QScrollArea()
        self.editor_scroll.setWidgetResizable(True)
        self.editor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.editor_scroll.setWidget(self.editor)
        self.side_tabs.addTab(self.editor_scroll, "")
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(left)
        self.splitter.addWidget(self.side_tabs)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([560, 600])

        layout = QVBoxLayout(self)
        layout.addWidget(self.intro_label)
        layout.addWidget(self.splitter, 1)
        self.retranslate()

    # --- data

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.card_view.apply_palette(palette)
        self._redraw()

    def _window_settings(self):
        settings = getattr(self.window(), "settings", None)
        if settings is None or callable(settings):
            from .. import config

            return config.load()
        return settings

    def _card_built(self, card_id: int) -> None:
        """A card was built or got register data: refresh its row and the favourites."""
        self._refresh_card(card_id)
        self._on_changed()

    def filtered_cards(self) -> list:
        status = self.status_combo.currentData() or None
        return companies.list_cards(self._store, status=status, favourites_only=self.favourites_only.isChecked(),
                                    hiring_only=self.hiring_only.isChecked())

    def reload(self) -> None:
        """Re-reads the cards; a search in progress is kept (its scores refreshed later)."""
        if self._mode == "list" or not self._query:
            self._mode = "list"
            self.rows = [(card, None, "") for card in self.filtered_cards()]
            self._redraw()
            self._set_info(t("cards.count", count=len(self.rows)))
        else:
            # keep the order of the last search, with fresh card data
            fresh = {c.id: c for c in self.filtered_cards()}
            self.rows = [(fresh[c.id], s, snippet) for c, s, snippet in self.rows if c.id in fresh]
            self._redraw()

    def _filters_changed(self) -> None:
        if self._mode == "search" and self._query:
            self.search_box.rerun()
            self.search()
        else:
            self._mode = "list"
            self.reload()

    def _query_changed(self, text: str) -> None:
        if not text.strip() and self._mode != "list":
            self._mode = "list"
            self._query = ""
            self.reload()

    def _set_info(self, text: str) -> None:
        self.search_info.setText(text)

    def _set_busy(self, busy: bool, key: str | None = None) -> None:
        self._busy = busy
        self.progress.setVisible(busy)
        self.search_button.setEnabled(not busy)
        self.similar_button.setEnabled(not busy and self.current is not None)
        if key:
            self._set_info(t(key))

    def search(self, query: str | None = None) -> None:
        """Ranks the filtered cards by meaning and exact words (worker thread); empty = plain list."""
        if query is not None and query != self.search_field.text():
            self.search_field.setText(query)
        query = " ".join(self.search_field.text().split())
        if query != self._query:
            self._relevance_pending = True   # a new query: relevance order again
        self._query = query
        if not query:
            self._mode = "list"
            self.reload()
            return
        self._mode = "search"
        self.search_box.search()

    def _search_documents(self) -> list:
        from ..scoring.semantic_index import card_document

        return [card_document(card) for card in self.filtered_cards()]

    def _search_results(self) -> None:
        """New hits of the search box: the matching cards, most relevant first."""
        from .search_box import hit_html, source_label

        box = self.search_box
        if not box.active:
            return
        if self._mode == "similar":
            return
        self._mode = "search"
        if box.query != self._query or self._relevance_pending:
            self._clear_sort()   # a new query: most relevant first again
        self._relevance_pending = False
        self._query = box.query
        rows, sources = [], {}
        for card in self.filtered_cards():
            hit = box.hit(f"card:{card.id}")
            if hit is None or not hit.matches:
                continue
            rows.append((card, hit.relevance, hit.snippet()))
            sources[card.id] = (source_label(hit.passage), hit_html(hit))
        rows.sort(key=lambda row: -row[1])
        self.rows = rows[:SEARCH_LIMIT]
        self._hit_sources = sources
        self._redraw()
        status = box.status.text()
        self._set_info(t("cards.results", count=len(self.rows), query=self._query) + (f" · {status}" if status else ""))

    def _search_finished(self) -> None:
        self._model_ready = True
        self._set_busy(False)
        if self.search_box.active:
            self._search_results()

    def _search_done(self, results) -> None:
        """Results of "Find similar": [(card, score, snippet)]."""
        self._model_ready = True
        self._set_busy(False)
        self._hit_sources = {}
        self._clear_sort()   # most similar first
        self.rows = list(results)
        self._redraw()
        self._set_info(t("cards.results", count=len(self.rows), query=self._query))

    def _search_failed(self, error: BaseException) -> None:
        self._set_busy(False)
        log.warning("Card search failed: %s", error)
        self._set_info(t("cards.search_error"))

    def find_similar(self) -> None:
        card = self.current
        if card is None or self._busy:
            return
        cards = companies.list_cards(self._store)
        self._mode = "similar"
        self._query = card.name
        self._set_busy(True, "cards.searching" if self._model_ready else "cards.loading_model")

        def work():
            from ..scoring.company_search import similar_cards

            return similar_cards(cards, card, self._embedder_provider(), limit=20)

        def done(results):
            self._search_done(results)
            self._set_info(t("cards.similar_results", count=len(self.rows), name=card.name))

        run_job(self._run, work, done, self._search_failed, name="similar companies")

    # --- table

    def _clear_sort(self) -> None:
        if self.sort_column is not None:
            self.sort_column, self.descending = None, False
            record_sort(self.table, self.COLUMNS, None, False)
            self._header_texts()

    def sort_by(self, column: str) -> None:
        """Header click: sort the shown cards by `column`; a second click reverses the order."""
        if self.sort_column == column:
            self.descending = not self.descending
        else:
            self.sort_column, self.descending = column, False
        record_sort(self.table, self.COLUMNS, self.sort_column, self.descending)
        self._header_texts()
        self._redraw()

    def _header_texts(self) -> None:
        for i, column in enumerate(self.COLUMNS):
            item = self.table.horizontalHeaderItem(i)
            text, tooltip = header_text(column)
            item.setText(text)
            item.setToolTip(tooltip)
            if column == "star":
                item.setIcon(star_icon(self._palette.warning, True))

    def _affinity_order(self) -> bool:
        """The default order is the affinity's (plain list, no column chosen)."""
        return self.affinity_service is not None and self.sort_column is None and self._mode == "list"

    def _can_redraw(self) -> bool:
        """No note being typed: redrawing would refill the notes editor."""
        from PySide6.QtWidgets import QApplication

        focus = QApplication.focusWidget()
        return not self.card_save_pending and not (focus is not None and self.editor.isAncestorOf(focus))

    def refresh_affinity(self) -> None:
        """New affinity results: the cells are updated; the rows are re-ordered when the
        order depends on the affinity (and nothing is being typed in the notes)."""
        service = self.affinity_service
        if service is None:
            return
        cards = [row[0] for row in self.rows]
        self._affinity = affinity_results(service, cards)
        reorder = self._affinity_order() or self.sort_column in AFFINITY_COLUMNS
        if reorder and self._can_redraw():
            before = [row[0].id for row in self.rows]
            ordered = self._ordered(self.rows)
            if [row[0].id for row in ordered] != before:
                self._redraw()
                return
        for r, (card, _score, _snippet) in enumerate(self.rows):
            cells = affinity_cells_of(service, card, self._affinity, self._palette) or {}
            for column, cell in cells.items():
                item = self.table.item(r, self.COLUMNS.index(column))
                if item is not None:
                    apply_cell(item, cell, self.table.font())

    def _ordered(self, rows: list) -> list:
        affinity = (lambda card: self._affinity.get(card.id)) if self.affinity_service is not None else None
        if self.sort_column:
            return sort_cards(rows, self.sort_column, self.descending, key=lambda row: (row[0], row[1]),
                              affinity=affinity)
        if self._affinity_order():
            return sort_cards(rows, None, False, key=lambda row: (row[0], row[1]), affinity=affinity)
        return list(rows)

    def open_linkedin(self) -> bool:
        """LinkedIn's company search for the selected card (browser only)."""
        card = self.current
        return card is not None and open_url(companies.linkedin_search_url(card.name), self._opener)

    def _redraw(self) -> None:
        service = self.affinity_service
        self._affinity = affinity_results(service, [row[0] for row in self.rows])
        self.rows = self._ordered(self.rows)
        selected_id = self.current.id if self.current is not None else None
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.rows))
        to_select = None
        for r, (card, score, snippet) in enumerate(self.rows):
            values = card_values(card)
            values["score"] = (f"{score:.0f} %" if self._mode == "search" else format_number(score))                 if score is not None and self._mode != "list" else ""
            values["snippet"] = display(" ".join((snippet or "").split()))
            source, html_hit = self._hit_sources.get(card.id, ("", "")) if self._mode == "search" else ("", "")
            if source and values["snippet"]:
                values["snippet"] = f"{source}: {values['snippet']}"
            _fill_row(self.table, r, self.COLUMNS, values, card, self._palette,
                      affinity_cells_of(service, card, self._affinity, self._palette))
            if values["snippet"]:
                for c in range(len(self.COLUMNS)):
                    if self.COLUMNS[c] != "star":
                        self.table.item(r, c).setToolTip(html_hit or values["snippet"])
            if card.id == selected_id:
                to_select = r
        self.table.clearSelection()
        if to_select is not None:
            self.table.selectRow(to_select)
        self.table.blockSignals(False)
        header = self.table.horizontalHeader()
        column = self.COLUMNS.index("company")
        widest = max([self.table.fontMetrics().horizontalAdvance(c.name) for c, _s, _n in self.rows] or [0])
        limit = self.fontMetrics().averageCharWidth() * 28
        header.resizeSection(column, max(header.sectionSizeHint(column), min(widest + 24, limit)))
        if to_select is None and selected_id is not None:
            card = companies.get(self._store, selected_id)
            self.show_card(card)
        elif to_select is not None:
            self.show_card(self.rows[to_select][0])

    def selected_row(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        return min(r.row() for r in rows) if rows else None

    def selected_cards(self) -> list:
        rows = sorted({r.row() for r in self.table.selectionModel().selectedRows()})
        return [self.rows[i][0] for i in rows if 0 <= i < len(self.rows)]

    def build_selected(self) -> bool:
        """"Create full card" for every selected card, one after the other."""
        cards = self.selected_cards()
        if not cards:
            self._set_info(t("cards.batch_none"))
            return False
        return self.card_view.build_many([c.id for c in cards])

    def build_favourites(self) -> bool:
        """"Create full card" for all the favourite cards that have a website."""
        cards = [c for c in companies.list_cards(self._store, favourites_only=True) if c.site]
        if not cards:
            self._set_info(t("cards.batch_none"))
            return False
        return self.card_view.build_many([c.id for c in cards])

    def select(self, index: int) -> None:
        if 0 <= index < len(self.rows):
            self.table.selectRow(index)

    def select_card(self, card_id: int) -> bool:
        """Shows one card (e.g. from the favourites window), clearing filters if needed."""
        for i, (card, _s, _n) in enumerate(self.rows):
            if card.id == card_id:
                self.select(i)
                return True
        self.status_combo.setCurrentIndex(0)
        self.favourites_only.setChecked(False)
        self.hiring_only.setChecked(False)
        self.search_field.clear()
        self._mode = "list"
        self.reload()
        for i, (card, _s, _n) in enumerate(self.rows):
            if card.id == card_id:
                self.select(i)
                return True
        return False

    def _selection_changed(self) -> None:
        i = self.selected_row()
        self.show_card(self.rows[i][0] if i is not None and 0 <= i < len(self.rows) else None)

    def _cell_clicked(self, row: int, column: int) -> None:
        if self.COLUMNS[column] == "star" and 0 <= row < len(self.rows):
            card = self.rows[row][0]
            companies.set_favourite(self._store, card.id, not card.favourite)
            self._refresh_card(card.id)
            self._on_changed()

    def _refresh_card(self, card_id: int) -> None:
        fresh = companies.get(self._store, card_id)
        self.rows = [((fresh if c.id == card_id else c), s, n) for c, s, n in self.rows if fresh or c.id != card_id]
        if self.current is not None and self.current.id == card_id:
            self.current = fresh
        self._redraw()

    # --- editor

    def show_card(self, card) -> None:
        if self._card_autosave.isActive():  # the edits of the card shown so far are saved first
            self.autosave_card()
        self._filling = True
        try:
            self._show_card(card)
        finally:
            self._filling = False

    def _card_edited(self) -> None:
        if not self._filling and self.current is not None:
            self._card_autosave.start()

    def _card_choice_changed(self) -> None:
        if not self._filling and self.current is not None:
            self._card_autosave.stop()
            self.save_card()

    @property
    def card_save_pending(self) -> bool:
        return self._card_autosave.isActive()

    def autosave_card(self) -> int | None:
        """Saves the notes, contacts and tags of the card shown, on a worker (the editor
        is not redrawn: the user may still be typing)."""
        self._card_autosave.stop()
        card = self.current
        if card is None:
            return None
        values = {"notes": self.notes_field.toPlainText(), "contacts": self.contacts_field.toPlainText(),
                  "tags": self.tags_field.text()}
        store, card_id, name = self._store, card.id, card.name

        def done(fresh) -> None:
            self.rows = [((fresh if c.id == card_id else c), score, note) for c, score, note in self.rows]
            if self.current is not None and self.current.id == card_id:
                for key in ("notes", "contacts", "tags"):
                    setattr(self.current, key, getattr(fresh, key))
            self._set_info(t("cards.autosaved", name=name, time=QTime.currentTime().toString("HH:mm")))

        def failed(_error) -> None:
            log.warning("Card notes not saved (card %s)", card_id)
            self._set_info(t("cards.not_saved", name=name))

        return self._run(lambda: companies.update(store, card_id, **values), done, failed,
                         name="company card autosave")

    def _show_card(self, card) -> None:
        self.current = card
        self.card_view.show_card(card)
        has = card is not None
        for widget in (self.card_status, self.favourite_box, self.tags_field, self.notes_field,
                       self.contacts_field, self.save_button, self.delete_button):
            widget.setEnabled(has)
        self.similar_button.setEnabled(has and not self._busy)
        self.site_button.setEnabled(has and is_openable_url(card.site))
        self.linkedin_button.setEnabled(has)
        if not has:
            self.name_label.setText(t("cards.select"))
            self.site_label.setText("")
            self.info_label.setText("")
            for field in (self.tags_field,):
                field.clear()
            self.notes_field.clear()
            self.contacts_field.clear()
            return
        self.name_label.setText(card.name)
        self.site_label.setText(card.site or t("detail.no_site"))
        values = card_values(card)
        parts = [values["hiring"]]
        if values["best_posting"]:
            parts.append(t("cards.best_posting", title=values["best_posting"]))
        for key in ("salary", "contract", "remote"):
            if values[key]:
                parts.append(values[key])
        if card.sectors:
            # OSM category names and sectors: English with an English interface
            parts.append(", ".join(display(s) for s in card.sectors))
        if card.description:
            self.info_label.setToolTip("\n".join(display(line) for line in card.description.splitlines()))
        else:
            self.info_label.setToolTip("")
        self.info_label.setText(" · ".join(parts))
        index = self.card_status.findData(card.status)
        self.card_status.setCurrentIndex(max(0, index))
        self.favourite_box.setChecked(bool(card.favourite))
        self.tags_field.setText(", ".join(card.tags))
        self.notes_field.setPlainText(card.notes)
        self.contacts_field.setPlainText(card.contacts)

    def save_card(self) -> bool:
        card = self.current
        if card is None:
            return False
        self._card_autosave.stop()
        favourite_changed = bool(card.favourite) != self.favourite_box.isChecked()
        companies.update(self._store, card.id, status=self.card_status.currentData(),
                         favourite=self.favourite_box.isChecked(), notes=self.notes_field.toPlainText(),
                         contacts=self.contacts_field.toPlainText(), tags=self.tags_field.text())
        self._refresh_card(card.id)
        self._set_info(t("cards.saved", name=card.name))
        if favourite_changed:
            self._on_changed()
        return True

    def delete_card(self) -> bool:
        card = self.current
        if card is None:
            return False
        if not self._dialogs.ask(t("cards.confirm_delete", name=card.name), t("cards.delete"), warning=True):
            return False
        companies.delete(self._store, card.id)
        self.current = None
        self.rows = [r for r in self.rows if r[0].id != card.id]
        self._redraw()
        self.show_card(None)
        self._on_changed()
        return True

    def add_company(self, name: str, site: str = "") -> bool:
        try:
            card = companies.add_manual(self._store, name, site or None)
        except ValueError:
            self._dialogs.error(t("cards.name_required"))
            return False
        self.search_field.clear()
        self._mode = "list"
        self.reload()
        self.select_card(card.id)
        return True

    def open_add_dialog(self) -> AddCompanyDialog:
        dialog = AddCompanyDialog(self, self.add_company)
        dialog.finished.connect(lambda _r: setattr(self, "add_dialog", None))
        self.add_dialog = dialog
        dialog.open()
        return dialog

    # --- texts

    def retranslate(self) -> None:
        self.geo_filter.retranslate()
        self.intro_label.setText(t("cards.intro"))
        self.status_label.setText(t("cards.status_filter"))
        self.status_combo.setItemText(0, t("cards.all_statuses"))
        for i, status in enumerate(companies.CARD_STATUSES, start=1):
            self.status_combo.setItemText(i, enum_label("card_status", status))
        for i, status in enumerate(companies.CARD_STATUSES):
            self.card_status.setItemText(i, enum_label("card_status", status))
        self.favourites_only.setText(t("cards.favourites_only"))
        self.hiring_only.setText(t("cards.hiring_only"))
        self.add_button.setText(t("cards.add_manual"))
        self.search_box.retranslate()
        self.search_field.setPlaceholderText(t("cards.search_placeholder"))
        self.search_button.setText(t("cards.search"))
        self._header_texts()
        self.editor.setTitle(section(t("cards.editor")))
        self.side_tabs.setTabText(0, card_tr("card.tab"))
        self.side_tabs.setTabText(1, card_tr("card.tab_notes"))
        self.card_view.retranslate()
        self.card_status_label.setText(t("cards.card_status"))
        self.favourite_box.setText(t("cards.favourite"))
        self.tags_label.setText(t("cards.tags"))
        self.tags_field.setPlaceholderText(t("cards.tags_placeholder"))
        self.notes_label.setText(t("cards.notes"))
        self.contacts_label.setText(t("cards.contacts"))
        self.save_button.setText(t("common.save"))
        self.site_button.setText(t("detail.open_site"))
        self.linkedin_button.setText(t("detail.open_linkedin"))
        self.linkedin_button.setToolTip(t("detail.open_linkedin_tooltip"))
        self.similar_button.setText(t("cards.similar"))
        self.delete_button.setText(t("cards.delete"))
        self.batch_selected_button.setText(t("cards.batch_selected"))
        self.batch_selected_button.setToolTip(t("cards.batch_tooltip"))
        self.batch_favourites_button.setText(t("cards.batch_favourites"))
        self.batch_favourites_button.setToolTip(t("cards.batch_tooltip"))
        self.reload()
        self.show_card(self.current)
