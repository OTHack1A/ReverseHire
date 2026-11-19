"""Results table: sorting, filters, hiring companies highlighted, remote work in its
own colour, permanent contracts and salaries visible, favourite stars, CSV export.

The functions at the top are pure (no Qt) and tested directly.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import display_text
from ..i18n import t
from ..models import Assessment, Company, HiringStatus, Posting
from .common import PERMANENT_CONTRACTS, enum_label, format_number
from .theme import Palette, active_palette
from . import table_filter, table_sort
from .widgets import star_icon
from .affinity_box import AFFINITY_COLUMNS, affinity_cells, apply_cell, column_header, scores_tooltip
from .affinity_box import sort_value as affinity_sort_value

COLUMNS = ("star", "company", "status") + AFFINITY_COLUMNS + (
    "match", "salary", "contract", "remote", "fit", "desirability", "relevance", "distance", "postings", "confidence",
    "revenue_trend")
TEXT_COLUMNS = ("company", "status", "salary", "contract", "remote", "verdict", "fits_as", "company_type")
# The company-level scores of the search: shown muted, secondary to the affinity (their
# header tooltip says how they differ).
SECONDARY_SCORES = ("match", "fit", "desirability", "relevance", "confidence")
EXPORT_COLUMNS = tuple(c for c in COLUMNS if c != "star")
COMPANY_MIN_CHARS = 20   # the company column never gets narrower than this
SALARY_MAX_CHARS = 22    # longer published salaries are elided (full text in the tooltip)
# Status order: hiring companies first.
STATUS_RANK = {
    HiringStatus.HIRING: 0,
    HiringStatus.MAYBE: 1,
    HiringStatus.NO_SIGNAL: 2,
    HiringStatus.NOT_ASSESSED: 3,
    HiringStatus.UNREACHABLE: 4,
}
CONTRACT_RANK = {"permanent": 0, "permanent_possible": 1}
REMOTE_RANK = {"remote": 0, "hybrid": 1, "onsite": 2}
# Rough conversion of salaries to a yearly amount, only to sort the column.
PERIOD_FACTOR = {"year": 1, "month": 12, "week": 52, "day": 220, "hour": 1720}

STAR_ON, STAR_OFF = "★", "☆"  # values (CSV, tests); the table draws icons (the font has no star)
HIRING_MARK = "◆"  # in JetBrains Mono (it has no ★)
BLOCKED_MARK = "⊘"

Row = tuple[Company, Assessment | None]


def status_of(evaluation: Assessment | None) -> HiringStatus:
    return evaluation.status if evaluation is not None else HiringStatus.NOT_ASSESSED


def status_text(status: HiringStatus) -> str:
    text = t(f"hiring_status.{status.value}")
    return f"{HIRING_MARK} {text}" if status == HiringStatus.HIRING else text


# --- what a company offers, from its postings (best fit first) -----------------------


def _postings(evaluation: Assessment | None) -> list[Posting]:
    return list(evaluation.postings) if evaluation is not None else []


def best_salary_posting(evaluation: Assessment | None) -> Posting | None:
    """The best posting's salary, else the first posting that publishes one."""
    postings = _postings(evaluation)
    return next((p for p in postings if getattr(p, "salary", None)), None)


def company_salary(evaluation: Assessment | None) -> str:
    posting = best_salary_posting(evaluation)
    return (posting.salary or "") if posting is not None else ""


def company_contract(evaluation: Assessment | None) -> str | None:
    """"permanent" or "permanent_possible" if any posting offers it, else the best posting's."""
    contracts = [getattr(p, "contract", None) for p in _postings(evaluation)]
    for value in PERMANENT_CONTRACTS:
        if value in contracts:
            return value
    return next((c for c in contracts if c), None)


def company_remote(evaluation: Assessment | None) -> str | None:
    """"remote" or "hybrid" if any posting offers it, else the best posting's mode."""
    modes = [getattr(p, "remote", None) for p in _postings(evaluation)]
    for value in ("remote", "hybrid"):
        if value in modes:
            return value
    return next((m for m in modes if m), None)


def is_remote(evaluation: Assessment | None) -> bool:
    return company_remote(evaluation) in ("remote", "hybrid")


def is_permanent(evaluation: Assessment | None) -> bool:
    return company_contract(evaluation) in PERMANENT_CONTRACTS


def salary_yearly(posting: Posting | None) -> float | None:
    if posting is None:
        return None
    amount = getattr(posting, "salary_max", None) or getattr(posting, "salary_min", None)
    if amount is None:
        return None
    return float(amount) * PERIOD_FACTOR.get(getattr(posting, "salary_period", None) or "year", 1)


def salary_sort_value(posting: Posting | None) -> float | None:
    """Yearly amount for sorting: a range sorts by its LOWER bound."""
    if posting is None:
        return None
    amount = getattr(posting, "salary_min", None) or getattr(posting, "salary_max", None)
    if amount is None:
        return None
    return float(amount) * PERIOD_FACTOR.get(getattr(posting, "salary_period", None) or "year", 1)


def score_of(evaluation: Assessment | None, name: str) -> float | None:
    """A 0..100 score of the assessment (None when missing or not provided yet)."""
    if evaluation is None:
        return None
    value = getattr(evaluation, name, None)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def is_blocked(evaluation: Assessment | None) -> bool:
    return bool(getattr(evaluation, "blocked", False)) if evaluation is not None else False


# --- sorting, filters, values ------------------------------------------------------------


def _sort_value(row: Row, column: str, favourites: Callable[[Company], bool] | None = None,
                affinity: Callable[[Row], object] | None = None):
    company, evaluation = row
    if column in AFFINITY_COLUMNS:
        return affinity_sort_value(affinity(row) if affinity is not None else None, column)
    if column == "star":
        return 0 if favourites is not None and favourites(company) else 1
    if column == "company":
        return table_sort.text_key(company.name)
    if column == "status":
        return STATUS_RANK.get(status_of(evaluation), 9)
    if column == "distance":
        return company.distance_km
    if column == "revenue_trend":
        return company_revenue_trend(company).get("cagr")
    if evaluation is None:
        return None
    if column in ("match", "desirability", "relevance", "fit"):
        return score_of(evaluation, column)
    if column == "salary":
        return salary_sort_value(best_salary_posting(evaluation))
    if column == "contract":
        value = company_contract(evaluation)
        return None if value is None else CONTRACT_RANK.get(value, 5)
    if column == "remote":
        value = company_remote(evaluation)
        return None if value is None else REMOTE_RANK.get(value, 5)
    return {
        "confidence": evaluation.confidence,
        "postings": len(evaluation.postings),
    }[column]


def company_revenue_trend(company: Company) -> dict:
    """The revenue trend the search found on the user's card of this company
    (companies.annotate_revenue_trends: fields["revenue_trend"]), else {}."""
    trend = (getattr(company, "fields", None) or {}).get("revenue_trend")
    return trend if isinstance(trend, dict) and trend.get("cagr") is not None else {}


def revenue_trend_text(company: Company) -> str:
    trend = company_revenue_trend(company)
    if not trend:
        return ""
    from ..financials.trend import RevenueTrend, trend_cell
    from .company_card import fin_tr

    return trend_cell(RevenueTrend(state="", cagr=float(trend["cagr"]), first_year=trend.get("first_year"),
                                   last_year=trend.get("last_year")), fin_tr)


def row_id(row: Row) -> tuple[int, int]:
    """Identity of a result row: the filters rebuild the row tuples, not their objects."""
    return id(row[0]), id(row[1])


def has_match(rows: list[Row]) -> bool:
    return any(score_of(e, "match") is not None for _c, e in rows)


def sort_rows(rows: list[Row], column: str | None, descending: bool, hiring_first: bool = True,
              favourites: Callable[[Company], bool] | None = None,
              affinity: Callable[[Row], object] | None = None) -> list[Row]:
    """Sorts the rows; missing values always go last.

    Default order: the affinity ("postings first, then spontaneous": the affinity
    ``sort_key``) when ``affinity(row)`` gives results, rows without one after them;
    otherwise the "match" score when the assessments have it (blocked companies after
    the others), otherwise best fit first, then relevance.
    """
    result = list(rows)
    results = {id(r): affinity(r) for r in result} if affinity is not None else {}
    if column is None and any(v is not None for v in results.values()):
        from .affinity_service import sort_key as affinity_key

        result.sort(key=lambda r: affinity_key(results.get(id(r))))
    elif column is None:
        if has_match(result):
            def default(r: Row):
                match = score_of(r[1], "match")
                return (1 if is_blocked(r[1]) else 0, -(match if match is not None else -1.0))
        else:
            def default(r: Row):
                evaluation = r[1]
                if evaluation is None:
                    return (1.0, 1.0)
                fit = evaluation.fit if evaluation.fit is not None else -1.0
                relevance = evaluation.relevance if evaluation.relevance is not None else -1.0
                return (-fit, -relevance)

        result.sort(key=default)
    else:
        def value(r: Row):
            return _sort_value(r, column, favourites, lambda row: results.get(id(row)))

        present = [r for r in result if value(r) is not None]
        missing = [r for r in result if value(r) is None]
        present.sort(key=value, reverse=descending)
        result = present + missing
    if hiring_first:
        # stable sort: hiring companies on top, the rest in the chosen order
        result.sort(key=lambda r: 0 if status_of(r[1]) == HiringStatus.HIRING else 1)
    return result


def filter_rows(rows: list[Row], only_hiring: bool = False, only_with_site: bool = False,
                text: str = "", *, only_remote: bool = False, only_permanent: bool = False,
                only_salary: bool = False) -> list[Row]:
    needle = (text or "").strip().casefold()
    result = []
    for company, evaluation in rows:
        if only_hiring and status_of(evaluation) != HiringStatus.HIRING:
            continue
        if only_with_site and not company.site:
            continue
        if only_remote and not is_remote(evaluation):
            continue
        if only_permanent and not is_permanent(evaluation):
            continue
        if only_salary and not company_salary(evaluation):
            continue
        if needle and needle not in company.name.casefold():
            continue
        result.append((company, evaluation))
    return result


def _score_text(evaluation: Assessment | None, name: str) -> str:
    value = score_of(evaluation, name)
    return format_number(value) if value is not None else ""


def row_values(company: Company, evaluation: Assessment | None, favourite: bool = False,
               affinity=None) -> tuple[str, ...]:
    """Cell texts, in COLUMNS order (`affinity`: the row's AffinityResult, if any)."""
    match = _score_text(evaluation, "match")
    if is_blocked(evaluation):
        match = f"{BLOCKED_MARK} {match}".strip()
    values = {
        "star": STAR_ON if favourite else STAR_OFF,
        "company": company.name,
        "status": status_text(status_of(evaluation)),
        "match": match,
        "salary": display_text.display(company_salary(evaluation)),
        "contract": enum_label("contract", company_contract(evaluation)),
        "remote": enum_label("remote", company_remote(evaluation)),
        "fit": _score_text(evaluation, "fit"),
        "desirability": _score_text(evaluation, "desirability"),
        "relevance": format_number(evaluation.relevance) if evaluation and evaluation.relevance is not None else "",
        "distance": format_number(company.distance_km, 2) if company.distance_km is not None else t("table.manual"),
        "confidence": format_number(evaluation.confidence * 100) + "%" if evaluation else "",
        "postings": str(len(evaluation.postings)) if evaluation else "",
        "revenue_trend": revenue_trend_text(company),
    }
    if affinity is not None:
        values.update({c: cell.text for c, cell in affinity_cells(affinity).items()})
    return tuple(values.get(c, "") for c in COLUMNS)


def _csv_english(text: str | None) -> str:
    """The English translation of a site text when one is cached, else the text as
    published (no on-screen marker: the original column is next to it)."""
    shown = display_text.show(text or "", english=True)
    return shown.text if shown.translated else (text or "")


def export_csv(path: str | Path, rows: list[Row], favourites: Callable[[Company], bool] | None = None,
               affinity: Callable[[Row], object] | None = None) -> int:
    """Writes the rows as CSV (UTF-8 with BOM, ';' separator) and returns how many.

    With the English interface the texts that come from the websites (posting
    titles, salary) are written in English, as shown on screen, and a column pair
    keeps them "as published" in the original language next to them; the other
    interfaces write them as published only."""
    from ..companies import workplace

    if affinity is None:  # the Results table passes its is_favourite: its affinity comes along
        affinity = getattr(getattr(favourites, "__self__", None), "affinity_of", None)
    english = display_text.interface_is_english()
    header = [column_header(c)[0] if c in AFFINITY_COLUMNS else t(f"table.{c}") for c in EXPORT_COLUMNS] + [
        t("csv.site"), t("csv.address"), t("csv.workplace"), t("csv.careers_page"), t("csv.posting_titles"),
    ]
    if english:
        header += [t("csv.posting_titles_original"), t("csv.salary_original")]
    header += [t("csv.origin"), t("csv.travel"), t("csv.spontaneous"), t("csv.blocked"), t("csv.favourite"),
               t("csv.why_selected")]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(header)
        for company, evaluation in rows:
            favourite = bool(favourites and favourites(company))
            result = affinity((company, evaluation)) if affinity is not None else None
            values = dict(zip(COLUMNS, row_values(company, evaluation, favourite, result)))
            published_salary = company_salary(evaluation)
            values["salary"] = _csv_english(published_salary) if english else published_salary
            if is_blocked(evaluation):
                values["match"] = _score_text(evaluation, "match")
            titles = [a.title for a in evaluation.postings] if evaluation else []
            ads = " | ".join(_csv_english(x) if english else x for x in titles)
            travel = next((p.travel for p in _postings(evaluation) if getattr(p, "travel", None)), None)
            row = [values[c] for c in EXPORT_COLUMNS] + [
                company.site or "", company.address or "", workplace(company=company, assessment=evaluation),
                (evaluation.careers_url if evaluation else "") or "",
                ads,
            ]
            if english:
                row += [" | ".join(titles), published_salary or ""]
            row += [
                t(f"origin.{company.origin}"),
                enum_label("travel", travel),
                _score_text(evaluation, "spontaneous"),
                t("common.yes") if is_blocked(evaluation) else "",
                t("common.yes") if favourite else "",
                str((getattr(company, "fields", None) or {}).get("why_selected", "") or ""),
            ]
            writer.writerow(row)
    return len(rows)


def fit_first_column(table: QTableWidget, column: int, min_chars: int) -> None:
    """Gives `column` all the free width (at least `min_chars`); the other columns keep
    their content width and the table scrolls sideways when they do not fit."""
    header = table.horizontalHeader()
    metrics = header.fontMetrics()
    minimum = max(header.sectionSizeHint(column), metrics.averageCharWidth() * min_chars + 16)
    others = sum(header.sectionSize(i) for i in range(header.count()) if i != column and not header.isSectionHidden(i))
    available = table.viewport().width() - others
    header.resizeSection(column, max(minimum, available))


class _ResizeWatcher(QObject):
    def __init__(self, callback: Callable[[], None], parent: QObject):
        super().__init__(parent)
        self._callback = callback

    def eventFilter(self, obj, event):  # noqa: N802 (Qt name)
        if event.type() == QEvent.Type.Resize:
            self._callback()
        return False


def watch_resize(table: QTableWidget, callback: Callable[[], None]) -> None:
    table.viewport().installEventFilter(_ResizeWatcher(callback, table))


class ResultsTable(QWidget):
    def __init__(self, on_select: Callable[[Row | None], None], parent: QWidget | None = None,
                 on_star: Callable[[Company, Assessment | None, bool], None] | None = None):
        super().__init__(parent)
        self._on_select = on_select
        self._on_star = on_star
        self.rows: list[Row] = []
        self._visible: list[Row] = []
        self.sort_column: str | None = None
        self.descending = True
        self._palette: Palette = active_palette()
        self._updating = False
        self._favourites: set[str] = set()
        # the window's AffinityService (gui/affinity_service.py), set by the affinity feature
        self.affinity_service = None
        self._affinity: dict[tuple, object] = {}  # row_id(row) -> AffinityResult of the last redraw

        self.only_hiring = QCheckBox()
        self.only_site = QCheckBox()
        self.hiring_first = QCheckBox()
        self.hiring_first.setChecked(True)
        self.only_remote = QCheckBox()
        self.only_permanent = QCheckBox()
        self.only_salary = QCheckBox()
        self.search_label = QLabel()
        # the shared search box (gui/search_box.py): exact words at once, meaning on the
        # window's worker (runner / embedder_provider / store set by the main window)
        from .search_box import SearchBox

        self.search_box = SearchBox(self._search_documents, name="search of the results")
        self.search_field = self.search_box.field
        self.count_label = QLabel()
        self.count_label.setProperty("role", "muted")
        self.remote_legend = QLabel()
        self.remote_legend.setProperty("role", "remote")
        for box in self.filter_boxes():
            box.toggled.connect(self.refresh)
        self.search_box.resultsChanged.connect(self._search_changed)

        filters = QHBoxLayout()
        filters.setContentsMargins(0, 0, 0, 0)
        for box in (self.only_hiring, self.only_site, self.hiring_first):
            filters.addWidget(box)
        filters.addStretch(1)
        filters2 = QHBoxLayout()
        filters2.setContentsMargins(0, 0, 0, 0)
        for box in (self.only_remote, self.only_permanent, self.only_salary):
            filters2.addWidget(box)
        filters2.addStretch(1)
        filters2.addWidget(self.remote_legend)
        search = QHBoxLayout()
        search.setContentsMargins(0, 0, 0, 0)
        search.addWidget(self.search_label)
        search.addWidget(self.search_box, 1)
        search.addWidget(self.count_label)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setObjectName("resultsTable")
        self.filters = table_filter.attach(self.table, kinds=table_filter.named_kinds(COLUMNS, affinity="number"),
                                           sorting="delegate")
        self.sorter = table_sort.sorter_of(self.table)
        remembered = table_sort.remembered(self.sorter.key)   # the order chosen earlier in the session
        if remembered is not None and 0 <= remembered[0] < len(COLUMNS):
            self.sort_column, self.descending = COLUMNS[remembered[0]], not remembered[1]
        self.filters.changed.connect(lambda *_: self._update_count())
        self.search_box.bind_table(self.table, self._search_key_of_row)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        header = self.table.horizontalHeader()
        header.setSectionsClickable(True)
        header.setHighlightSections(False)
        header.setStretchLastSection(False)
        header.sectionClicked.connect(lambda i: self.sort_by(COLUMNS[i]))
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COLUMNS.index("company"), QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COLUMNS.index("salary"), QHeaderView.ResizeMode.Interactive)
        for i, column in enumerate(COLUMNS):
            align = Qt.AlignmentFlag.AlignLeft if column in TEXT_COLUMNS else Qt.AlignmentFlag.AlignRight
            if column == "star":
                align = Qt.AlignmentFlag.AlignHCenter
            item = QTableWidgetItem()
            item.setTextAlignment(align | Qt.AlignmentFlag.AlignVCenter)
            self.table.setHorizontalHeaderItem(i, item)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.cellClicked.connect(self._cell_clicked)
        watch_resize(self.table, self._fit_columns)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(filters)
        layout.addLayout(filters2)
        layout.addLayout(search)
        # Country -> Region -> Province (gui/geo_filter.py, predicate "geo" of the table filter)
        from .geo_filter import GeoFilter

        self.geo_filter = GeoFilter(self.table, self._row_location, self, name="resultsGeoFilter")
        layout.addWidget(self.geo_filter)
        layout.addWidget(self.table, 1)

    def filter_boxes(self) -> tuple[QCheckBox, ...]:
        return (self.only_hiring, self.only_site, self.hiring_first, self.only_remote,
                self.only_permanent, self.only_salary)

    def apply_palette(self, palette: Palette) -> None:
        """Colours of the highlighted rows, from the current theme."""
        self._palette = palette
        self.table.horizontalHeaderItem(COLUMNS.index("star")).setIcon(star_icon(palette.warning, True))
        self.refresh()

    def star_value(self, index: int) -> str:
        """STAR_ON / STAR_OFF of a visible row (the cell shows an icon)."""
        return self.table.item(index, COLUMNS.index("star")).data(Qt.ItemDataRole.UserRole)

    # --- data

    def clear(self) -> None:
        self.rows = []
        self.refresh()

    def set_rows(self, rows: list[Row]) -> None:
        self.rows = list(rows)
        self.refresh()
        self.search_box.rerun()

    def add(self, company: Company, evaluation: Assessment | None) -> None:
        self.rows.append((company, evaluation))
        self.refresh()
        self.search_box.rerun()

    def add_rows(self, rows: list[Row]) -> None:
        """Several results with one redraw (the GUI drains search events in batches:
        redrawing for each one would freeze big searches)."""
        if rows:
            self.rows.extend(rows)
            self.refresh()
            self.search_box.rerun()

    def visible_rows(self) -> list[Row]:
        """The rows shown, in table order: those hidden by the column filters are left out."""
        return [row for i, row in enumerate(self._visible) if not self.table.isRowHidden(i)]

    def row_index(self, row: Row) -> int:
        """Table row of a shown result (-1 when not in the table)."""
        for i, candidate in enumerate(self._visible):
            if candidate is row:
                return i
        return -1

    def set_favourites(self, keys: set[str]) -> None:
        """Keys (companies.card_key) of the starred companies."""
        self._favourites = set(keys)
        self.refresh()

    def is_favourite(self, company: Company) -> bool:
        if not self._favourites:
            return False
        from ..companies import card_key

        return card_key(company) in self._favourites

    def sort_by(self, column: str) -> None:
        """Header click: a new column ascending, the same column reversed (the arrow
        is the header's sort indicator; the order is remembered for the session)."""
        if self.sort_column == column:
            self.descending = not self.descending
        else:
            self.sort_column = column
            self.descending = False
        self.sorter.record(COLUMNS.index(column), not self.descending)
        self.retranslate()

    def selected(self) -> Row | None:
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            return None
        i = indexes[0].row()
        return self._visible[i] if 0 <= i < len(self._visible) else None

    def select(self, index: int) -> None:
        if 0 <= index < len(self._visible):
            self.table.selectRow(index)
            self.table.scrollToItem(self.table.item(index, COLUMNS.index("company")))

    def toggle_star(self, index: int) -> bool | None:
        """Flips the star of a visible row; returns the new state (None without a handler)."""
        if not 0 <= index < len(self._visible) or self._on_star is None:
            return None
        company, evaluation = self._visible[index]
        on = not self.is_favourite(company)
        self._on_star(company, evaluation, on)
        return on

    def affinity_of(self, row: Row):
        """The AffinityResult of a row (cached, never computed here), None without one."""
        service = self.affinity_service
        if service is None:
            return None
        found = self._affinity.get(row_id(row))
        return found if found is not None else service.result_for(row)

    def _load_affinity(self) -> None:
        """The cached results of every row; the missing ones are requested (one batch)."""
        service = self.affinity_service
        if service is None or not self.rows:
            self._affinity = {}
            return
        try:
            results = service.results_for(self.rows)
        except Exception:  # noqa: BLE001 - the affinity must never break the table
            results = [None] * len(self.rows)
        self._affinity = {row_id(r): v for r, v in zip(self.rows, results)}

    def _cell_clicked(self, row: int, column: int) -> None:
        if COLUMNS[column] == "star":
            self.toggle_star(row)

    def _row_colours(self, status: HiringStatus):
        p = self._palette
        if status == HiringStatus.HIRING:
            return p.hiring_bg, p.hiring_fg, True
        if status == HiringStatus.MAYBE:
            return p.maybe_bg, p.maybe_fg, False
        if status == HiringStatus.UNREACHABLE:
            return p.surface2, p.muted, False
        return None, None, False

    def row_style(self, index: int) -> tuple[str | None, str | None, bool]:
        """(background, foreground, bold) of a visible row (used by the tests)."""
        return self._row_colours(status_of(self._visible[index][1]))

    def cell_style(self, index: int, column: str) -> tuple[str | None, str | None, bool]:
        """(background, foreground, bold) of one cell (remote and contract cells differ)."""
        item = self.table.item(index, COLUMNS.index(column))
        background = item.background().color().name().upper() if item.background().style() != Qt.BrushStyle.NoBrush else None
        foreground = item.foreground().color().name().upper() if item.foreground().style() != Qt.BrushStyle.NoBrush else None
        return background, foreground, item.font().bold()

    def _cell_colours(self, column: str, evaluation: Assessment | None, row_colours):
        background, foreground, bold = row_colours
        p = self._palette
        if column in ("company", "remote") and is_remote(evaluation):
            return p.remote_bg, p.remote_fg, True
        if column == "contract" and is_permanent(evaluation):
            return background, p.accent if background is None else foreground, True
        if column == "star":
            return background, p.warning, False
        if column == "match" and is_blocked(evaluation):
            return background, p.error, True
        return background, foreground, bold

    def _tooltip(self, column: str, company: Company, evaluation: Assessment | None) -> str:
        if column == "company":
            return company.name
        if column == "star":
            return t("table.star_tooltip")
        if column == "salary":
            posting = best_salary_posting(evaluation)
            if posting is None:
                return ""
            salary, title = display_text.show(posting.salary), display_text.show(posting.title)
            text = f"{salary.text} — {title.text}"
            if salary.original or title.original:
                text += "\n" + t("display.original", text=f"{posting.salary} — {posting.title}")
            return text
        if column == "match" and is_blocked(evaluation):
            reason = getattr(evaluation, "blocked_reason", None) or ""
            return t("table.blocked") + (f": {reason}" if reason else "")
        if column == "remote" and is_remote(evaluation):
            return t("table.remote_tooltip")
        return ""

    def refresh(self) -> None:
        """Redraws the table keeping the selection when possible."""
        previous = self.selected()
        filtered = filter_rows(self.rows, self.only_hiring.isChecked(), self.only_site.isChecked(),
                               "", only_remote=self.only_remote.isChecked(),
                               only_permanent=self.only_permanent.isChecked(),
                               only_salary=self.only_salary.isChecked())
        self._load_affinity()
        self._visible = sort_rows(filtered, self.sort_column, self.descending,
                                  self.hiring_first.isChecked(), self.is_favourite,
                                  (lambda row: self._affinity.get(row_id(row))) if self._affinity else None)
        service = self.affinity_service
        self._updating = True
        try:
            self.table.clearSelection()
            self.table.setRowCount(len(self._visible))
            to_select = None
            for r, (company, evaluation) in enumerate(self._visible):
                colours = self._row_colours(status_of(evaluation))
                values = row_values(company, evaluation, self.is_favourite(company))
                shown = self._visible[r]
                result = self._affinity.get(row_id(shown))
                pending = result is None and service is not None and service.is_pending(shown)
                cells = affinity_cells(result, self._palette, pending=pending) if service is not None else {}
                for c, value in enumerate(values):
                    column = COLUMNS[c]
                    item = QTableWidgetItem(value)
                    if column in cells:
                        apply_cell(item, cells[column], self.table.font())
                        self.table.setItem(r, c, item)
                        continue
                    align = Qt.AlignmentFlag.AlignLeft if column in TEXT_COLUMNS else Qt.AlignmentFlag.AlignRight
                    if column == "star":
                        align = Qt.AlignmentFlag.AlignHCenter
                        item.setText("")
                        item.setData(Qt.ItemDataRole.UserRole, value)
                        item.setIcon(star_icon(self._palette.warning, value == STAR_ON))
                    item.setTextAlignment(align | Qt.AlignmentFlag.AlignVCenter)
                    tooltip = self._tooltip(column, company, evaluation)
                    if tooltip:
                        item.setToolTip(tooltip)
                    background, foreground, bold = self._cell_colours(column, evaluation, colours)
                    if column in SECONDARY_SCORES and foreground is None and service is not None:
                        foreground = self._palette.muted  # secondary to the affinity
                    if background:
                        item.setBackground(QBrush(QColor(background)))
                    if foreground:
                        item.setForeground(QBrush(QColor(foreground)))
                    if bold:
                        font = QFont(self.table.font())
                        font.setBold(True)
                        item.setFont(font)
                    self.table.setItem(r, c, item)
                if previous is not None and previous[0] is company:
                    to_select = r
            if to_select is not None:
                self.table.selectRow(to_select)
        finally:
            self._updating = False
        self.filters.apply()   # the column filters stay on the refilled table
        self._update_count()
        self._fit_columns()

    def _fit_columns(self) -> None:
        """Salary: content width up to SALARY_MAX_CHARS; company: all the free width."""
        header = self.table.horizontalHeader()
        metrics = self.table.fontMetrics()
        salary = COLUMNS.index("salary")
        widest = max([metrics.horizontalAdvance(self.table.item(r, salary).text())
                      for r in range(self.table.rowCount()) if self.table.item(r, salary)] or [0])
        width = min(widest + 24, metrics.averageCharWidth() * SALARY_MAX_CHARS + 24)
        header.resizeSection(salary, max(header.sectionSizeHint(salary), width))
        fit_first_column(self.table, COLUMNS.index("company"), COMPANY_MIN_CHARS)
        self._fit_width()

    def _fit_width(self) -> None:
        """Minimum width: star, company, status and match stay readable; the rest scrolls."""
        header = self.table.horizontalHeader()
        essential = sum(header.sectionSize(COLUMNS.index(c)) for c in ("star", "status", "match"))
        company = max(header.sectionSizeHint(COLUMNS.index("company")),
                      header.fontMetrics().averageCharWidth() * COMPANY_MIN_CHARS + 16)
        frame = self.table.frameWidth() * 2 + self.table.verticalScrollBar().sizeHint().width()
        self.table.setMinimumWidth(essential + company + frame)

    # --- the search box

    @staticmethod
    def search_key(company) -> str:
        return f"result:{id(company)}"

    def _search_documents(self) -> list:
        from ..scoring.semantic_index import company_document

        return [company_document(c, e, key=self.search_key(c)) for c, e in self.rows]

    def _search_key_of_row(self, row: int) -> str | None:
        return self.search_key(self._visible[row][0]) if 0 <= row < len(self._visible) else None

    def _search_changed(self) -> None:
        """Hits arrived: their sentence in the company's tooltip, then the counts."""
        from .search_box import hit_text

        column = COLUMNS.index("company")
        for r, (company, _e) in enumerate(self._visible):
            item = self.table.item(r, column)
            if item is not None:
                hit = self.search_box.hit(self.search_key(company)) if self.search_box.active else None
                extra = "\n" + hit_text(hit) if hit is not None and hit.matches else ""
                item.setToolTip(company.name + extra)
        self.filters.apply()
        self._update_count()

    def _update_count(self) -> None:
        hiring = sum(1 for _c, e in self.rows if status_of(e) == HiringStatus.HIRING)
        shown = sum(1 for i in range(len(self._visible)) if not self.table.isRowHidden(i))
        self.count_label.setText(t("table.count", visible=shown, total=len(self.rows), hiring=hiring))

    def _selection_changed(self) -> None:
        if not self._updating:
            self._on_select(self.selected())

    # --- texts

    def header_text(self, column: str) -> str:
        return self.table.horizontalHeaderItem(COLUMNS.index(column)).text()

    def _row_location(self, row: int):
        """The normalised workplace of a drawn row (for the place filters)."""
        from ..geo.places import location_of_company

        if not 0 <= row < len(self._visible):
            return None
        company, evaluation = self._visible[row]
        return location_of_company(company, assessment=evaluation)

    def retranslate(self) -> None:
        self.search_box.retranslate()
        self.geo_filter.retranslate()
        self.only_hiring.setText(t("table.hiring_only"))
        self.only_site.setText(t("table.site_only"))
        self.hiring_first.setText(t("table.hiring_first"))
        self.only_remote.setText(t("table.remote_only"))
        self.only_permanent.setText(t("table.permanent_only"))
        self.only_salary.setText(t("table.salary_only"))
        self.remote_legend.setText(t("table.remote_legend"))
        self.search_label.setText(t("table.search"))
        for i, column in enumerate(COLUMNS):
            if column in AFFINITY_COLUMNS:
                text, tooltip = column_header(column)
            else:
                text = "" if column == "star" else t(f"table.{column}")
                tooltip = scores_tooltip() if column in SECONDARY_SCORES else ""
            item = self.table.horizontalHeaderItem(i)
            item.setText(text.strip())
            item.setToolTip(tooltip)
            if column == "star":
                item.setIcon(star_icon(self._palette.warning, True))
                item.setToolTip(t("table.star_tooltip"))
        self.refresh()
