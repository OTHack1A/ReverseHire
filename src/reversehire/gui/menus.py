"""Menu entries that mirror the buttons of the tabs.

`mirror_button(button, parent)` gives the button ONE QAction, stored on the button
(property MENU_ACTION) and put in the menu bar by the main window: triggering it
shows the button's tab and clicks the button, so the menu and the button run the same
handler; the action's enabled state follows the button (an event filter on its
enabled/shown changes) and its text follows the button's text (refreshed when the menu
opens and on language change), so selection- and tab-dependent states stay in sync.

`bind_action(button, action)` records that a button already runs an existing menu
action (e.g. "Open CV…").
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QAbstractButton, QStackedWidget, QTabWidget, QWidget

MENU_ACTION = "menuAction"


def menu_action_of(button: QAbstractButton) -> QAction | None:
    value = button.property(MENU_ACTION)
    return value if isinstance(value, QAction) else None


def show_tab_of(widget: QWidget) -> None:
    """Makes the tab (of every QTabWidget up the tree) that contains `widget` current."""
    child = widget
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QStackedWidget) and isinstance(parent.parentWidget(), QTabWidget):
            tabs = parent.parentWidget()
            index = tabs.indexOf(child)
            if index >= 0 and tabs.currentIndex() != index:
                tabs.setCurrentIndex(index)
        child, parent = parent, parent.parentWidget()


def explicitly_hidden(button: QWidget) -> bool:
    """Hidden on purpose (by its panel), not just because its tab or window is not shown:
    the button or a container inside its tab page was hidden explicitly."""
    widget = button
    while widget is not None:
        parent = widget.parentWidget()
        if isinstance(parent, QStackedWidget) or widget.isWindow():
            return False  # a tab page (or the window): hidden only because it is not current
        if widget.testAttribute(Qt.WidgetAttribute.WA_WState_ExplicitShowHide) and widget.isHidden():
            return True
        widget = parent
    return False


class _Mirror(QObject):
    """Keeps one action in step with its button."""

    def __init__(self, button: QAbstractButton, action: QAction, text: Callable[[], str] | None):
        super().__init__(action)
        self.button = button
        self.action = action
        self._text = text
        button.installEventFilter(self)
        self.sync()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt name)
        if event.type() in (QEvent.Type.EnabledChange, QEvent.Type.ShowToParent, QEvent.Type.HideToParent):
            self.sync()
        return False

    def sync(self) -> None:
        try:
            text = self._text() if self._text is not None else self.button.text()
            if text:
                self.action.setText(text)
            self.action.setEnabled(self.button.isEnabled() and not explicitly_hidden(self.button))
        except RuntimeError:  # the button was deleted
            self.action.setEnabled(False)


class MenuMirrors:
    """All the mirrored buttons of a window."""

    def __init__(self, parent: QObject):
        self._parent = parent
        self.mirrors: list[_Mirror] = []

    def mirror(self, button: QAbstractButton, text: Callable[[], str] | None = None) -> QAction:
        action = QAction(self._parent)

        def trigger() -> None:
            mirror.sync()
            if not action.isEnabled():
                return
            show_tab_of(button)
            button.click()

        action.triggered.connect(lambda _checked=False: trigger())
        mirror = _Mirror(button, action, text)
        button.setProperty(MENU_ACTION, action)
        self.mirrors.append(mirror)
        return action

    def refresh(self) -> None:
        for mirror in self.mirrors:
            mirror.sync()


def bind_action(button: QAbstractButton, action: QAction) -> QAction:
    """The button runs `action` (already in a menu): recorded for the menu check."""
    button.setProperty(MENU_ACTION, action)
    return action
