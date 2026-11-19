"""Sorting by clicking a column header, for every table of the app, implemented once.

First click on a header: ascending; a second click on the same header: descending;
the header shows the arrow (Qt's sort indicator). The order of a table is kept while
it is refilled (e.g. Results during a running search) and remembered for the session
per table (keyed by the table's objectName).

Sort keys (`cell_key`):
- a cell may carry its exact key in SORT_ROLE (e.g. the percentage behind "64 %");
- otherwise a numeric UserRole, otherwise the text: numbers by value ("9 %" < "10 %"
  < "100 %", "2 km" < "10 km", a range "30.000–40.000 €" by its lower bound), dates
  chronologically (2026-11-18, 18/11/2026), text alphabetically, case- and accent-
  insensitive ("Élite" between "Delta" and "Fabbro");
- empty cells always last, in both directions.

Two ways to sort a table:
- physical (default, `enable(view)`): the items of a QTableWidget are reordered, with
  all their data. Every row keeps the index it had when the panel filled the table
  (ROW_ID_ROLE): panels that keep a Python list parallel to the rows translate with
  `source_row(view, row)` / `view_row(view, index)`;
- delegated (`enable(view, delegate=True)`): the panel sorts its own rows (Results, My
  companies, Favourites) with the same key functions (`value_key`), and reports the
  state with `TableSorter.record(column, ascending)` (arrow + session memory).
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import unicodedata
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Qt
from PySide6.QtWidgets import QTableView, QTableWidget

log = logging.getLogger(__name__)

SORT_ROLE = int(Qt.ItemDataRole.UserRole) + 40     # optional exact sort key of a cell
ROW_ID_ROLE = int(Qt.ItemDataRole.UserRole) + 41   # index of the row when the panel filled it
SORTER_PROPERTY = "_rhTableSorter"
RESORT_DELAY_MS = 30

_SESSION: dict[str, tuple[int, bool]] = {}   # table key -> (column, ascending)


# --- values ------------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"[-+−]?\d[\d.,'   ]*")
_DATE_RES = (
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})"), (1, 2, 3)),
    (re.compile(r"^(\d{1,2})[/.](\d{1,2})[/.](\d{4})"), (3, 2, 1)),
)


def fold(text) -> str:
    """Lowercase, no accents, collapsed spaces: "Forlì" == "forli", "Élite" ~ "elite"."""
    decomposed = unicodedata.normalize("NFKD", str(text or "").casefold())
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).split())


def parse_number(text) -> float | None:
    """The first number of a cell ("85 %", "12,5 km", "1.234", "-3"), else None."""
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text)
    match = _NUMBER_RE.search(str(text or ""))
    if match is None:
        return None
    raw = match.group().strip().replace("−", "-")
    for space in (" ", " ", " ", "'"):
        raw = raw.replace(space, "")
    if "," in raw and "." in raw:   # the last separator is the decimal one
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:   # "12,5" decimal; "1,234,567" thousands
        raw = raw.replace(",", "") if raw.count(",") > 1 else raw.replace(",", ".")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    elif "." in raw:
        head, _, tail = raw.rpartition(".")
        if len(tail) == 3 and len(head.lstrip("-+")) <= 3 and head.lstrip("-+") not in ("0", ""):
            raw = raw.replace(".", "")   # "1.234" (thousands)
    raw = raw.rstrip(".,")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_date(text) -> _dt.date | None:
    if isinstance(text, _dt.datetime):
        return text.date()
    if isinstance(text, _dt.date):
        return text
    value = str(text or "").strip()
    for pattern, (y, m, d) in _DATE_RES:
        match = pattern.match(value)
        if match:
            try:
                return _dt.date(int(match.group(y)), int(match.group(m)), int(match.group(d)))
            except ValueError:
                return None
    return None


def is_number_text(text) -> bool:
    """A cell that is a number with at most a short unit ("85 %", "12 km", "+6.3 %/y",
    "30.000–40.000 €/anno")."""
    value = str(text or "").strip()
    if not value or parse_number(value) is None:
        return False
    letters = sum(1 for c in value if c.isalpha())
    return letters <= 8 and value[:1] in "0123456789+-−.,<>~€$£"


BLANK_TEXTS = frozenset({"", "-", "–", "—", "?", "n/a", "n.d."})


def is_blank(value) -> bool:
    """No value: empty, or a dash standing for "nothing" (always sorted last)."""
    return value is None or (isinstance(value, str) and value.strip().casefold() in BLANK_TEXTS)


def text_key(text) -> tuple:
    """Alphabetical key: accents and case ignored, the original as tie-break."""
    value = str(text or "")
    return (fold(value), value.casefold(), value)


def value_key(value, kind: str | None = None) -> tuple:
    """Key of one value (blanks are handled by the callers: always last).
    Numbers < dates < texts when a column mixes them."""
    if isinstance(value, bool):
        return (0, float(value))
    if isinstance(value, (int, float)):
        return (0, float(value))
    if isinstance(value, (_dt.date, _dt.datetime)):
        return (1, parse_date(value).toordinal())
    if isinstance(value, (tuple, list)):
        return (3, tuple(value))
    text = str(value)
    if kind != "text":
        date = parse_date(text)
        if date is not None and (kind == "date" or not is_number_text(text) or "-" in text[:5] or "/" in text[:3]):
            return (1, date.toordinal())
        if kind == "number" or is_number_text(text):
            number = parse_number(text)
            if number is not None:
                return (0, number)
    return (2,) + text_key(text)


def sorted_by(items: list, key: Callable[[object], object], ascending: bool = True,
              kind: str | None = None) -> list:
    """`items` ordered by key(item), blanks always last, stable (ties keep their order)."""
    present = [(value_key(key(item), kind), position, item) for position, item in enumerate(items)
               if not is_blank(key(item))]
    blanks = [item for item in items if is_blank(key(item))]
    # Python's sort is stable also with reverse=True: ties keep their order
    present.sort(key=lambda entry: entry[0], reverse=not ascending)
    return [entry[2] for entry in present] + blanks


# --- the sorter --------------------------------------------------------------------------

def table_key(view) -> str:
    name = view.objectName()
    if name:
        return name
    parts, widget = [], view
    while widget is not None and len(parts) < 6:
        parts.append(type(widget).__name__)
        widget = widget.parentWidget()
    return "/".join(reversed(parts))


class TableSorter(QObject):
    """Header-click sorting of one view (see the module docstring)."""

    def __init__(self, view: QTableView, key: str, *, delegate: bool = False,
                 kinds: dict[int, str] | None = None, on_sorted: Callable[[], None] | None = None):
        super().__init__(view)
        self.view = view
        self.key = key
        self.delegate = delegate
        self.kinds = dict(kinds or {})
        self.on_sorted = on_sorted
        remembered = _SESSION.get(key)
        self.column: int | None = remembered[0] if remembered else None
        self.ascending: bool = remembered[1] if remembered else True
        self._sorting = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(RESORT_DELAY_MS)
        self._timer.timeout.connect(self.resort)
        self._model = None
        header = view.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        if not delegate:
            header.sectionClicked.connect(self.clicked)
            self._hook_model()
        else:  # the panel sorts its own rows; the click is still in the log like every other one
            header.sectionClicked.connect(lambda column: log.info(
                "Table %s: header of column %d clicked (sorted by the panel)", self.key, column))
        self.show_indicator()

    # --- state
    def clicked(self, column: int) -> None:
        """Header click: a new column ascending, the same column reversed."""
        if self.kinds.get(column) == "none" and not self.delegate:
            return
        ascending = not self.ascending if column == self.column else True
        self.record(column, ascending)
        self.resort()
        log.info("Table %s sorted by column %d (%s)", self.key, column, "ascending" if ascending else "descending")

    def record(self, column: int | None, ascending: bool = True) -> None:
        """The order in use (delegated tables call it after sorting their rows)."""
        self.column, self.ascending = column, bool(ascending)
        if column is None:
            _SESSION.pop(self.key, None)
        else:
            _SESSION[self.key] = (column, self.ascending)
        self.show_indicator()

    def show_indicator(self) -> None:
        try:
            header = self.view.horizontalHeader()
            header.setSortIndicatorShown(True)
            if self.column is None:
                header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
            else:
                header.setSortIndicator(self.column, Qt.SortOrder.AscendingOrder if self.ascending
                                        else Qt.SortOrder.DescendingOrder)
        except RuntimeError:
            pass

    # --- model
    def _hook_model(self) -> None:
        model = self.view.model()
        if model is None or model is self._model:
            return
        self._model = model
        for signal in (model.rowsInserted, model.modelReset, model.dataChanged):
            signal.connect(self.schedule)

    def schedule(self, *_args) -> None:
        if self._sorting or self.column is None or self.delegate:
            return
        try:
            self._timer.start()
        except RuntimeError:
            pass

    # --- sorting
    def cell_value(self, row: int, column: int):
        index = self.view.model().index(row, column)
        special = index.data(SORT_ROLE)
        if special is not None:
            return special
        user = index.data(Qt.ItemDataRole.UserRole)
        if isinstance(user, (int, float)) and not isinstance(user, bool):
            return user
        text = index.data(Qt.ItemDataRole.DisplayRole)
        if is_blank(text) and isinstance(user, str):
            return user   # e.g. the star column: no text, its value in UserRole
        return text

    def resort(self) -> None:
        """Applies the current order now (physical tables); delegated tables are sorted
        by their panels."""
        try:
            self._timer.stop()
        except RuntimeError:
            return
        if self.delegate or self.column is None or not isinstance(self.view, QTableWidget):
            return
        view = self.view
        if self._sorting or self.column >= view.columnCount():
            return
        self._sorting = True
        try:
            rows, columns = view.rowCount(), view.columnCount()
            stamp_rows(view)
            order = sorted_by(list(range(rows)), lambda r: self.cell_value(r, self.column), self.ascending,
                              self.kinds.get(self.column))
            if order == list(range(rows)):
                return
            selected = {source_row(view, i.row()) for i in view.selectionModel().selectedRows()} \
                if view.selectionModel() else set()
            current = view.currentRow()
            current_id = source_row(view, current) if current >= 0 else None
            blocked = view.blockSignals(True)
            try:
                taken = [[view.takeItem(r, c) for c in range(columns)] for r in range(rows)]
                headers = [view.takeVerticalHeaderItem(r) for r in range(rows)]
                for new_row, old_row in enumerate(order):
                    for c in range(columns):
                        if taken[old_row][c] is not None:
                            view.setItem(new_row, c, taken[old_row][c])
                    if headers[old_row] is not None:
                        view.setVerticalHeaderItem(new_row, headers[old_row])
                view.clearSelection()
                for new_row in range(rows):
                    if source_row(view, new_row) in selected:
                        view.selectRow(new_row)
            finally:
                view.blockSignals(blocked)
            if current_id is not None:
                position = view_row(view, current_id)
                if position is not None and not selected:
                    view.setCurrentCell(position, max(0, view.currentColumn()))
            from . import table_filter

            controller = table_filter.controller_of(view)
            if controller is not None:
                controller.apply()
            view.viewport().update()
            if self.on_sorted is not None:
                self.on_sorted()
        except RuntimeError:  # the view was deleted
            pass
        finally:
            self._sorting = False


def stamp_rows(view) -> None:
    """Rows without ROW_ID_ROLE get their current index (= the panel's list index: a
    panel fills its table in its own order)."""
    if not isinstance(view, QTableWidget):
        return
    for row in range(view.rowCount()):
        item = _first_item(view, row)
        if item is not None and item.data(ROW_ID_ROLE) is None:
            item.setData(ROW_ID_ROLE, row)


def _first_item(view, row: int):
    for column in range(view.columnCount()):
        item = view.item(row, column)
        if item is not None:
            return item
    return None


def source_row(view, row: int) -> int:
    """The index the panel gave this row when filling the table (the row itself when the
    table was never reordered)."""
    if not isinstance(view, QTableWidget) or row is None or row < 0:
        return row
    item = _first_item(view, row)
    value = item.data(ROW_ID_ROLE) if item is not None else None
    return int(value) if isinstance(value, int) else row


def view_row(view, index: int) -> int | None:
    """The row now showing the panel's row `index` (inverse of source_row)."""
    if not isinstance(view, QTableWidget):
        return index
    for row in range(view.rowCount()):
        if source_row(view, row) == index:
            return row
    return None


def sorter_of(view) -> TableSorter | None:
    value = view.property(SORTER_PROPERTY) if view is not None else None
    return value if isinstance(value, TableSorter) else None


def enable(view: QTableView, *, delegate: bool = False, kinds: dict[int, str] | None = None,
           key: str | None = None, on_sorted: Callable[[], None] | None = None) -> TableSorter | None:
    """Header-click sorting for `view` (once); returns the sorter."""
    if not isinstance(view, QTableView):
        return None
    current = sorter_of(view)
    if current is not None:
        if kinds:
            current.kinds.update(kinds)
        if on_sorted is not None:
            current.on_sorted = on_sorted
        return current
    sorter = TableSorter(view, key or table_key(view), delegate=delegate, kinds=kinds, on_sorted=on_sorted)
    view.setProperty(SORTER_PROPERTY, sorter)
    return sorter


def resort(view) -> None:
    """Re-applies the table's order now (panels call it after refilling)."""
    sorter = sorter_of(view)
    if sorter is not None:
        sorter.resort()


def remembered(key: str) -> tuple[int, bool] | None:
    return _SESSION.get(key)


def reset_session() -> None:
    _SESSION.clear()
