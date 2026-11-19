"""Right-click menus on table rows, built from the QActions the buttons and menus already
use (gui/menus.py rule: one action per command), plus the Delete key.

    context_menu.install(table, lambda: [open_action, rename_action, None, delete_action],
                         delete_action=delete_action)

`entries()` is called each time the menu opens (None = separator; disabled or hidden actions
are left out, so the menu shows only what applies to the rows under the cursor). A right
click on a row that is not selected selects it first (keeping a multiple selection when
the click is inside it). The Delete key triggers `delete_action` when the table has the
focus. The last menu shown is kept on the table (`last_menu(table)`) for the tests.
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import QAbstractItemView, QMenu

log = logging.getLogger(__name__)

_MENU_ATTR = "_context_menu_last"


def build_menu(parent, entries: list) -> QMenu:
    """The menu of the applicable actions (None = separator, no doubled or edge separators)."""
    menu = QMenu(parent)
    pending_separator = False
    for entry in entries:
        if entry is None:
            pending_separator = bool(menu.actions())
            continue
        if not isinstance(entry, QAction) or not entry.isVisible() or not entry.isEnabled():
            continue
        if pending_separator:
            menu.addSeparator()
            pending_separator = False
        menu.addAction(entry)
    return menu


def _select_under(table: QAbstractItemView, pos: QPoint) -> bool:
    index = table.indexAt(pos)
    if not index.isValid():
        return False
    selection = table.selectionModel()
    if selection is not None and not selection.isRowSelected(index.row(), index.parent()):
        table.setCurrentIndex(index)
        table.selectRow(index.row())
    return True


def install(table: QAbstractItemView, entries: Callable[[], list], *, delete_action: QAction | None = None,
            menu_runner: Callable[[QMenu, QPoint], None] | None = None) -> None:
    """Right-click menu on `table` (see the module docstring). `menu_runner(menu, global_pos)`
    shows the menu (default: popup, which never blocks; tests pass their own)."""
    table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def show(pos: QPoint) -> None:
        try:
            if not _select_under(table, pos):
                return
            menu = build_menu(table, entries())
            setattr(table, _MENU_ATTR, menu)
            if not menu.actions():
                return
            global_pos = table.viewport().mapToGlobal(pos)
            if menu_runner is not None:
                menu_runner(menu, global_pos)
            else:
                menu.popup(global_pos)
            log.info("Context menu of %s opened (%d entries)", table.objectName() or "a table",
                     len([a for a in menu.actions() if not a.isSeparator()]))
        except Exception:  # noqa: BLE001 - a menu must never break the table
            log.warning("Context menu not shown", exc_info=True)

    table.customContextMenuRequested.connect(show)
    if delete_action is not None:
        shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), table)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(lambda: delete_action.trigger() if delete_action.isEnabled() else None)
        table.setProperty("deleteShortcut", True)
        table._delete_shortcut = shortcut  # kept alive with the table


def action_for(button) -> QAction | None:
    """The QAction that runs `button`: its menu action (gui/menus.py) when it has one, else
    one action kept on the button (for windows whose buttons have no menu bar). Text and
    enabled state are refreshed from the button at every call (menu-open time)."""
    from .menus import menu_action_of

    if button is None:
        return None
    action = menu_action_of(button)
    if action is None:
        action = getattr(button, "_context_action", None)
        if action is None:
            action = QAction(button)
            action.triggered.connect(lambda _checked=False, b=button: b.click() if b.isEnabled() else None)
            button._context_action = action
        action.setText(button.text())
    action.setEnabled(button.isEnabled() and not button.isHidden())
    return action


def install_buttons(table: QAbstractItemView, groups: Callable[[], list], *, delete_button=None) -> None:
    """`install` with the row buttons themselves: `groups()` returns buttons (None = separator);
    the Delete key clicks `delete_button`."""
    install(table, lambda: [None if b is None else action_for(b) for b in groups()])
    if delete_button is not None:
        shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), table)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(lambda: delete_button.click() if delete_button.isEnabled() else None)
        table.setProperty("deleteShortcut", True)
        table._delete_shortcut = shortcut


def open_at_row(table: QAbstractItemView, row: int, column: int = 0) -> QMenu | None:
    """Opens the context menu of `row` as a right click would (for tests and keyboard users)."""
    index = table.model().index(row, column)
    rect = table.visualRect(index)
    table.customContextMenuRequested.emit(rect.center())
    return last_menu(table)


def last_menu(table) -> QMenu | None:
    return getattr(table, _MENU_ATTR, None)


def action_texts(menu: QMenu | None) -> list[str]:
    return [a.text() for a in (menu.actions() if menu is not None else []) if not a.isSeparator()]
