"""Country -> Region -> Province: three linked multi-select filters for a list.

`GeoFilter(view, location_of_row)` sits above a table (My companies, Favourites,
Results, Job search, Previous searches). Each level is a QAction with a menu of
checkable values with their counts ("Emilia-Romagna (12)"), "All" by default; the
button shows the action (so the menu bar entry and the button are the same action,
see gui/menus.py). The levels are linked: the regions offered are those of the
countries chosen, the provinces those of the regions chosen; a narrower choice drops
the provinces / regions that no longer belong.

Rows are filtered through the table's per-column filter controller
(gui/table_filter.py, predicate "geo"), so the place filters combine (AND) with the
column filters and the search box. The places come from geo/places.py (normalised,
cached): computing the counts happens only when a menu opens.
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QToolButton, QWidget

from ..geo import italy
from ..geo.places import EMPTY, Location
from ..i18n import current_language, t
from . import table_filter
from .menus import bind_action

log = logging.getLogger(__name__)

LEVELS = ("country", "region", "province")
UNKNOWN = ""   # the value of rows whose place is not known


def value_of(location: Location | None, level: str) -> str:
    location = location or EMPTY
    if level == "country":
        return location.country_code or ("IT" if location.province_code or location.region_key else UNKNOWN)
    if level == "region":
        return location.region_key
    return location.province_code


def value_label(level: str, value: str) -> str:
    if not value:
        return t("geo.unknown")
    if level == "country":
        return italy.country_name(value, current_language())
    if level == "region":
        return italy.region_name(value) or value
    found = italy.province(value)
    return f"{found.name} ({found.code})" if found else value


class GeoFilter(QWidget):
    """The three linked place filters of one table (see the module docstring)."""

    changed = Signal()

    def __init__(self, view, location_of_row: Callable[[int], Location | None], parent: QWidget | None = None,
                 *, name: str = ""):
        super().__init__(parent)
        self.setObjectName(name or "geoFilter")
        self.view = view
        self.location_of_row = location_of_row
        self.selected: dict[str, set[str] | None] = {level: None for level in LEVELS}
        self.label = QLabel(self)
        self.label.setProperty("role", "muted")
        self.menus: dict[str, QMenu] = {}
        self.level_actions: dict[str, QAction] = {}
        self.buttons: dict[str, QToolButton] = {}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)
        for level in LEVELS:
            menu = QMenu(self)
            menu.setObjectName(f"geoMenu_{level}")
            menu.aboutToShow.connect(lambda lv=level: self.fill_menu(lv))
            action = QAction(self)
            action.setMenu(menu)
            button = QToolButton(self)
            button.setObjectName(f"geoButton_{level}")
            button.setDefaultAction(action)
            button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            bind_action(button, action)
            self.menus[level], self.level_actions[level], self.buttons[level] = menu, action, button
            layout.addWidget(button)
        layout.addStretch(1)
        self.retranslate()

    # --- data
    def actions_for_menu(self) -> list[QAction]:
        """The three actions, for a submenu of the menu bar (same actions as the buttons)."""
        return [self.level_actions[level] for level in LEVELS]

    def _rows(self) -> int:
        model = self.view.model() if self.view is not None else None
        return model.rowCount() if model is not None else 0

    def _location(self, row: int) -> Location:
        try:
            return self.location_of_row(row) or EMPTY
        except Exception:  # noqa: BLE001 - a row that cannot be placed is "unknown"
            log.debug("Row place not available", exc_info=True)
            return EMPTY

    def _passes(self, location: Location, upto: int = len(LEVELS)) -> bool:
        for level in LEVELS[:upto]:
            chosen = self.selected[level]
            if chosen is not None and value_of(location, level) not in chosen:
                return False
        return True

    def counts(self, level: str) -> list[tuple[str, int]]:
        """(value, rows) of a level among the rows that pass the levels above it; most
        rows first, unknown last. Regions and provinces only for Italian rows."""
        upto = LEVELS.index(level)
        counts: dict[str, int] = {}
        for row in range(self._rows()):
            location = self._location(row)
            if not self._passes(location, upto):
                continue
            value = value_of(location, level)
            if level != "country" and value_of(location, "country") != "IT":
                continue
            counts[value] = counts.get(value, 0) + 1
        return sorted(counts.items(), key=lambda item: (item[0] == UNKNOWN, -item[1], value_label(level, item[0])))

    # --- choices
    def set_selection(self, level: str, values) -> None:
        """Values of one level (None = all); the levels below drop what no longer fits."""
        self.selected[level] = None if values is None else {str(v) for v in values}
        if level == "country":
            chosen = self.selected["country"]
            if chosen is not None and "IT" not in chosen:
                self.selected["region"] = self.selected["province"] = None
        if level in ("country", "region") and self.selected["province"] is not None:
            regions = self.selected["region"]
            if regions is not None:
                self.selected["province"] = {p for p in self.selected["province"]
                                             if (italy.province(p) and italy.province(p).region in regions)} or None
        log.info("Place filter: %s %s", level, "all" if values is None else f"{len(self.selected[level] or ())} chosen")
        self._apply()

    def toggle(self, level: str, value: str, on: bool) -> None:
        current = self.selected[level]
        if current is None:
            current = {v for v, _n in self.counts(level)}
        current = set(current)
        (current.add if on else current.discard)(value)
        every = {v for v, _n in self.counts(level)}
        self.set_selection(level, None if current >= every else current)

    def clear(self) -> None:
        for level in LEVELS:
            self.selected[level] = None
        self._apply()

    def active(self) -> bool:
        return any(value is not None for value in self.selected.values())

    def row_passes(self, row: int) -> bool:
        return self._passes(self._location(row))

    def _apply(self) -> None:
        controller = table_filter.controller_of(self.view)
        if controller is None and self.view is not None:
            controller = table_filter.attach(self.view)
        if controller is not None:
            controller.set_predicate("geo", self.row_passes if self.active() else None)
        else:  # no controller: hide the rows directly
            for row in range(self._rows()):
                self.view.setRowHidden(row, not self.row_passes(row))
        self._update_texts()
        self.changed.emit()

    # --- menus and texts
    def fill_menu(self, level: str) -> None:
        menu = self.menus[level]
        menu.clear()
        everything = menu.addAction(t("geo.all"))
        everything.setCheckable(True)
        everything.setChecked(self.selected[level] is None)
        everything.triggered.connect(lambda _c=False, lv=level: self.set_selection(lv, None))
        menu.addSeparator()
        values = self.counts(level)
        if not values:
            menu.addAction(t("geo.none")).setEnabled(False)
        for value, count in values:
            action = menu.addAction(f"{value_label(level, value)} ({count})")
            action.setCheckable(True)
            action.setChecked(self.selected[level] is None or value in self.selected[level])
            action.setData(value)
            action.toggled.connect(lambda on, lv=level, v=value: self.toggle(lv, v, on))

    def _summary(self, level: str) -> str:
        chosen = self.selected[level]
        if chosen is None:
            value = t("geo.all")
        elif len(chosen) == 1:
            value = value_label(level, next(iter(chosen)))
        else:
            value = t("geo.n_chosen", n=len(chosen))
        return t(f"geo.{level}", value=value)

    def _update_texts(self) -> None:
        italian = self.selected["country"] is None or "IT" in self.selected["country"]
        for level in LEVELS:
            action = self.level_actions[level]
            action.setText(self._summary(level))
            action.setToolTip(t(f"geo.{level}_tip"))
            action.setEnabled(level == "country" or italian)

    def retranslate(self) -> None:
        self.label.setText(t("geo.label"))
        self._update_texts()
