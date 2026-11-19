"""User actions in the log, from one place: an application-wide event filter.

Every button press (push buttons, tool buttons, check boxes with their new state),
menu item, choice in a combo box, tab change and dialog opened/closed is written to
the log as "INFO gui.activity [MainThread]: <what> "<label>" in <panel>". Only the
labels of the interface are logged, never what the user typed (CV text, notes,
addresses) and never secrets.

Widgets are hooked once, when Qt polishes them (before they are first shown), so
dialogs and panels created later are covered too.
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QLineEdit, QMenu, QMenuBar,
    QRadioButton, QTabBar, QTabWidget, QWidget,
)

log = logging.getLogger(__name__)

_HOOKED = "_rhActivityHooked"
_PACKAGE = "reversehire."
DEDUPE_S = 0.05  # one menu action reported by a submenu and its parent menu: logged once


def label_of(widget) -> str:
    """The visible label of a widget (no '&' accelerators), or its accessible name."""
    text = ""
    for getter in ("text", "accessibleName", "toolTip", "objectName"):
        try:
            text = getattr(widget, getter)() or ""
        except Exception:  # noqa: BLE001
            text = ""
        if text.strip():
            break
    return " ".join(text.replace("&&", "\0").replace("&", "").replace("\0", "&").split())[:80]


def where(widget) -> str:
    """The closest panel or dialog of the app that contains `widget` (class name)."""
    current = widget
    while current is not None:
        if type(current).__module__.startswith(_PACKAGE + "gui") and type(current).__module__ != __name__:
            return type(current).__name__
        current = current.parentWidget() if isinstance(current, QWidget) else None
    window = widget.window() if isinstance(widget, QWidget) else None
    return type(window).__name__ if window is not None else "?"


class ActivityLog(QObject):
    """The event filter; install with `install(app)`."""

    _TYPES = (QEvent.Type.Polish, QEvent.Type.Show, QEvent.Type.Hide)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._last_action: tuple[int, float] = (0, 0.0)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt name)
        try:
            kind = event.type()
            if kind not in self._TYPES:
                return False
            if kind == QEvent.Type.Polish:
                self._hook(obj)
            elif isinstance(obj, QDialog) and obj.isWindow() and not event.spontaneous():
                verb = "opened" if kind == QEvent.Type.Show else "closed"
                log.info("Dialog %s: %s (%s)", verb, type(obj).__name__, label_of_window(obj))
        except Exception:  # noqa: BLE001 - logging must never break the interface
            pass
        return False

    def _hook(self, widget) -> None:
        if widget.property(_HOOKED):
            return
        if isinstance(widget, QAbstractButton):
            parent = widget.parentWidget()
            if isinstance(parent, (QLineEdit, QAbstractItemView, QTabBar)):  # clear/corner/scroll buttons
                return
            widget.setProperty(_HOOKED, True)
            widget.clicked.connect(lambda checked=False, w=widget: self._button(w, checked))
        elif isinstance(widget, QComboBox):
            widget.setProperty(_HOOKED, True)
            widget.activated.connect(lambda index, w=widget: log.info(
                "Choice in %s: \"%s\"", where(w), w.itemText(index)[:80]))
        elif isinstance(widget, QTabWidget):
            widget.setProperty(_HOOKED, True)
            widget.currentChanged.connect(lambda index, w=widget: log.info(
                "Tab shown in %s: \"%s\"", where(w.parentWidget() or w), w.tabText(index)))
        elif isinstance(widget, QMenuBar):
            widget.setProperty(_HOOKED, True)
            widget.triggered.connect(lambda action: self._action(action, "Menu"))
        elif isinstance(widget, QMenu) and not isinstance(widget.parent(), (QMenu, QMenuBar)):
            widget.setProperty(_HOOKED, True)
            widget.triggered.connect(lambda action: self._action(action, "Menu item"))

    def _button(self, widget, checked: bool) -> None:
        try:
            state = ""
            if widget.isCheckable() and isinstance(widget, (QCheckBox, QRadioButton)):
                state = " -> on" if checked else " -> off"
            log.info("Button \"%s\" in %s%s", label_of(widget), where(widget), state)
        except Exception:  # noqa: BLE001
            pass

    def _action(self, action, kind: str) -> None:
        try:
            now = time.monotonic()
            if self._last_action[0] == id(action) and now - self._last_action[1] < DEDUPE_S:
                return
            self._last_action = (id(action), now)
            log.info("%s \"%s\"", kind, label_of(action))
        except Exception:  # noqa: BLE001
            pass


def label_of_window(window) -> str:
    try:
        return " ".join((window.windowTitle() or "").split())[:80]
    except Exception:  # noqa: BLE001
        return ""


def install(app: QApplication | None = None) -> ActivityLog | None:
    """Installs the filter on the application (once)."""
    app = app or QApplication.instance()
    if app is None:
        return None
    current = getattr(app, "_reversehire_activity", None)
    if current is not None:
        return current
    activity = ActivityLog(app)
    app.installEventFilter(activity)
    app._reversehire_activity = activity
    # Widgets polished before the filter existed (the main window applies its theme
    # first) never get a Polish event again: hook them now.
    for widget in app.allWidgets():
        try:
            activity._hook(widget)
        except Exception:  # noqa: BLE001 - logging must never break the interface
            pass
    return activity
