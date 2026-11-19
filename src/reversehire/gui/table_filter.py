"""Row tests and header-click sorting for every table of the app, implemented once.

`attach(view)` gives a QTableView / QTableWidget:
- header-click sorting (gui/table_sort.py): first click ascending, second descending,
  numbers by value, dates chronologically, text alphabetically (case and accents
  ignored), blanks last; the order survives refills and is remembered per table for
  the session. Panels that sort their own rows pass `sorting="delegate"`;
- a `TableFilter` controller that HIDES the rows that do not pass the row tests other
  features plug in with `set_predicate(name, fn)`: the shared search box ("search")
  and the Country -> Region -> Province filters ("geo"). All tests combine (AND).
  A test receives the row's index in the panel's own list (`table_sort.source_row`),
  so it stays right after the rows were reordered by a header click.

Rows are hidden, never removed: the panels keep addressing their rows by index.
A cell can carry its exact sort key in FILTER_ROLE (= table_sort.SORT_ROLE), e.g. the
percentage behind "64 %".
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QTableView

from . import table_sort
from .table_sort import SORT_ROLE, fold, parse_date, parse_number  # noqa: F401 (re-exported)

log = logging.getLogger(__name__)

FILTER_ROLE = SORT_ROLE                            # the exact (numeric) value of a cell
NO_FILTER_PROPERTY = "rhNoFilter"                  # a view with this property set stays as it is
CONTROLLER_PROPERTY = "_rhTableFilter"
KINDS = ("text", "number", "category", "date", "none")
APPLY_DELAY_MS = 30

_CONTROLLERS: list["TableFilter"] = []


class TableFilter(QObject):
    """Hides the rows of one view that do not pass the row tests (predicates)."""

    changed = Signal(int, int)          # visible rows, all rows

    def __init__(self, view: QTableView, key: str, kinds: dict[int, str] | None = None):
        super().__init__(view)
        self.view = view
        self.key = key
        self.kinds = {int(c): k for c, k in (kinds or {}).items() if k in KINDS}
        self.predicates: dict[str, Callable[[int], bool]] = {}
        self.auto = False   # attached by the safety net, not by its panel
        self._visible = 0
        self._total = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(APPLY_DELAY_MS)
        self._timer.timeout.connect(self.apply)
        self._model = None
        self._hook_model()
        _CONTROLLERS.append(self)
        view.destroyed.connect(lambda *_: _CONTROLLERS.remove(self) if self in _CONTROLLERS else None)

    def model(self):
        return self.view.model()

    def _hook_model(self) -> None:
        model = self.model()
        if model is None or model is self._model:
            return
        self._model = model
        for signal in (model.rowsInserted, model.rowsRemoved, model.modelReset, model.layoutChanged):
            signal.connect(self.schedule)

    def schedule(self, *_args) -> None:
        """Re-apply soon (the rows of a refill are coalesced into one pass)."""
        if not self.predicates:
            return
        try:
            self._timer.start()
        except RuntimeError:
            pass

    def row_count(self) -> int:
        model = self.model()
        return model.rowCount() if model is not None else 0

    def set_predicate(self, name: str, test: Callable[[int], bool] | None) -> None:
        """An extra row test (panel's row index -> keep?); None removes it."""
        if test is None:
            self.predicates.pop(name, None)
        else:
            self.predicates[name] = test
        self.apply()

    def active(self) -> bool:
        return bool(self.predicates)

    def row_passes(self, row: int) -> bool:
        index = table_sort.source_row(self.view, row)
        for test in list(self.predicates.values()):
            try:
                if not test(index):
                    return False
            except Exception:  # noqa: BLE001 - a broken test must not hide the table
                log.debug("Row predicate failed", exc_info=True)
        return True

    def apply(self) -> None:
        """Hides / shows the rows now."""
        try:
            self._timer.stop()
            self._hook_model()
            total = self.row_count()
            visible = 0
            any_test = bool(self.predicates)
            for row in range(total):
                keep = self.row_passes(row) if any_test else True
                if self.view.isRowHidden(row) == keep:
                    self.view.setRowHidden(row, not keep)
                visible += keep
            self._visible, self._total = visible, total
            self.changed.emit(visible, total)
        except RuntimeError:  # the view was deleted
            pass

    def counts(self) -> tuple[int, int]:
        return self._visible, self._total

    def visible_rows(self) -> list[int]:
        return [r for r in range(self.row_count()) if not self.view.isRowHidden(r)]


# --- attaching ---------------------------------------------------------------------------

# Column names that hold a few known values (status, contract…) and icon-only columns.
CATEGORY_COLUMNS = frozenset({"status", "card_status", "hiring", "contract", "remote", "source", "system", "band",
                              "kind", "origin"})
ICON_COLUMNS = frozenset({"star"})


def named_kinds(columns, **extra: str) -> dict[int, str]:
    """Kinds of a table's columns from their names (sort hints: "number", "date",
    "text"; "category"/"none" sort as detected); `extra` gives more, e.g.
    named_kinds(COLUMNS, score="number")."""
    kinds = {}
    for index, name in enumerate(columns):
        if name in extra:
            kinds[index] = extra[name]
        elif name in ICON_COLUMNS:
            kinds[index] = "none"
        elif name in CATEGORY_COLUMNS:
            kinds[index] = "category"
    return kinds


def filter_key(view) -> str:
    return table_sort.table_key(view)


def controller_of(view) -> TableFilter | None:
    value = view.property(CONTROLLER_PROPERTY) if view is not None else None
    return value if isinstance(value, TableFilter) else None


def attach(view: QTableView, *, kinds: dict[int, str] | None = None, key: str | None = None,
           sorting: str = "physical") -> TableFilter | None:
    """Gives `view` the row-test controller and header-click sorting (once); returns the
    controller. `sorting`: "physical" (the generic sort reorders the items), "delegate"
    (the panel sorts its rows itself, see table_sort) or "" (no sorting)."""
    if not isinstance(view, QTableView):
        return None
    current = controller_of(view)
    sort_kinds = {c: k for c, k in (kinds or {}).items() if k in ("number", "date", "text", "none")}
    if current is not None:
        if kinds:
            current.kinds.update(kinds)
        if sorting:
            table_sort.enable(view, delegate=sorting == "delegate", kinds=sort_kinds, key=key)
        return current
    controller = TableFilter(view, key or filter_key(view), kinds)
    view.setProperty(CONTROLLER_PROPERTY, controller)
    if sorting:
        table_sort.enable(view, delegate=sorting == "delegate", kinds=sort_kinds, key=key)
    controller.apply()
    return controller


def all_controllers() -> list[TableFilter]:
    return list(_CONTROLLERS)


def reset_session() -> None:
    """Forget the remembered table orders (tests)."""
    table_sort.reset_session()


class _AutoAttach(QObject):
    """Safety net: a table that a panel did not attach gets sorting when Qt polishes it."""

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        try:
            if event.type() == QEvent.Type.Polish and isinstance(obj, QTableView) \
                    and not obj.property(NO_FILTER_PROPERTY) and controller_of(obj) is None:
                _auto_attach(obj)
        except Exception:  # noqa: BLE001 - must never break the interface
            log.debug("Table sorting not attached", exc_info=True)
        return False


def _auto_attach(view: QTableView) -> TableFilter | None:
    controller = attach(view)
    if controller is not None:
        controller.auto = True
        log.info("Table sorting attached late to %s", controller.key)
    return controller


def install(app: QApplication | None = None) -> QObject | None:
    app = app or QApplication.instance()
    if app is None:
        return None
    current = getattr(app, "_reversehire_table_filter", None)
    if current is not None:
        return current
    watcher = _AutoAttach(app)
    app.installEventFilter(watcher)
    app._reversehire_table_filter = watcher
    for widget in app.allWidgets():
        if isinstance(widget, QTableView) and not widget.property(NO_FILTER_PROPERTY) and controller_of(widget) is None:
            try:
                _auto_attach(widget)
            except Exception:  # noqa: BLE001
                log.debug("Table sorting not attached", exc_info=True)
    return watcher
