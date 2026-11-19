"""Hover and keyboard-focus feedback for the whole application, implemented once.

Two parts:
- the stylesheet (theme.build_stylesheet) gives buttons, tabs, the language chips, the
  theme toggle, boxes and table rows a clear hover highlight in both themes, and the
  keyboard focus the same accent ring;
- `install(app)` adds an application-wide event filter that makes clickable controls
  (push and tool buttons) grow slightly while the mouse is over them or while they
  have the keyboard focus: a short eased animation (DURATION_MS, to HOVER_SCALE) of a
  graphics effect that paints the button scaled around its centre. The widget's
  geometry and size hint never change, so the surrounding layout does not move.

Table rows only get the highlight (no size change). With reduced motion (the
operating system's "show animations" switched off on Windows, or the environment
variable REVERSEHIRE_REDUCED_MOTION=1) nothing grows; the highlight stays.
"""

from __future__ import annotations

import logging
import os
import sys

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPointF, QPropertyAnimation, QRectF, Qt, Property
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractItemView, QApplication, QCheckBox, QGraphicsEffect, QLineEdit, QPushButton,
    QRadioButton, QScrollBar, QTabBar, QToolButton, QWidget,
)

log = logging.getLogger(__name__)

HOVER_SCALE = 1.05      # 5 % larger while hovered / focused with the keyboard
DURATION_MS = 120
REDUCED_MOTION_ENV = "REVERSEHIRE_REDUCED_MOTION"
_SPI_GETCLIENTAREAANIMATION = 0x1042
_KEYBOARD_REASONS = (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                     Qt.FocusReason.ShortcutFocusReason)


def reduced_motion(env: dict | None = None, platform: str | None = None) -> bool:
    """True when animations should be avoided: REVERSEHIRE_REDUCED_MOTION=1, or on
    Windows the "Show animations in Windows" setting switched off. Other systems: only
    the environment variable (their setting cannot be read without running a program)."""
    env = os.environ if env is None else env
    value = (env.get(REDUCED_MOTION_ENV) or "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    if (platform or sys.platform) != "win32":
        return False
    try:
        import ctypes

        enabled = ctypes.c_int(1)
        if ctypes.windll.user32.SystemParametersInfoW(_SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0):
            return not bool(enabled.value)
    except Exception:  # noqa: BLE001 - unknown: animate
        pass
    return False


class ScaleEffect(QGraphicsEffect):
    """Paints its widget scaled by `scale` around the centre; the widget's geometry is
    untouched (the effect only draws a little outside it)."""

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._scale = 1.0
        self.animation: QPropertyAnimation | None = None

    def _get_scale(self) -> float:
        return self._scale

    def _set_scale(self, value: float) -> None:
        self._scale = max(1.0, float(value))
        self.updateBoundingRect()
        self.update()

    scale = Property(float, _get_scale, _set_scale)

    def boundingRectFor(self, rect: QRectF) -> QRectF:  # noqa: N802 (Qt name)
        grow_x = rect.width() * (self._scale - 1.0) / 2 + 1
        grow_y = rect.height() * (self._scale - 1.0) / 2 + 1
        return rect.adjusted(-grow_x, -grow_y, grow_x, grow_y)

    def draw(self, painter: QPainter) -> None:
        if self._scale <= 1.0001:
            self.drawSource(painter)
            return
        pixmap = self.sourcePixmap(Qt.CoordinateSystem.LogicalCoordinates, None,
                                   QGraphicsEffect.PixmapPadMode.NoPad)
        if isinstance(pixmap, tuple):  # some bindings also return the offset
            pixmap = pixmap[0]
        offset = self.sourceBoundingRect(Qt.CoordinateSystem.LogicalCoordinates).topLeft()
        if pixmap.isNull():
            self.drawSource(painter)
            return
        ratio = pixmap.devicePixelRatio() or 1.0
        size = pixmap.size() / ratio
        centre = QPointF(offset.x() + size.width() / 2, offset.y() + size.height() / 2)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.translate(centre)
        painter.scale(self._scale, self._scale)
        painter.translate(-centre)
        painter.drawPixmap(offset, pixmap)
        painter.restore()


def is_growing_control(widget) -> bool:
    """Push and tool buttons of the app (not Qt's own parts of other widgets, not
    check boxes and radio buttons, which are options)."""
    if not isinstance(widget, (QPushButton, QToolButton)) or isinstance(widget, (QCheckBox, QRadioButton)):
        return False
    parent = widget.parentWidget()
    if isinstance(parent, (QLineEdit, QAbstractItemView, QTabBar, QScrollBar)):
        return False
    if type(widget).__name__ in ("QLineEditIconButton", "QTableCornerButton"):
        return False
    return True


class HoverFeedback(QObject):
    """The application-wide event filter (see the module docstring)."""

    def __init__(self, parent: QObject | None = None, *, animate: bool | None = None,
                 duration_ms: int = DURATION_MS, scale: float = HOVER_SCALE):
        super().__init__(parent)
        self.animate = (not reduced_motion()) if animate is None else animate
        self.duration_ms = duration_ms
        self.target_scale = scale

    # --- events
    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt name)
        try:
            kind = event.type()
            if kind == QEvent.Type.Polish and isinstance(obj, QAbstractItemView):
                # whole-row hover highlight (stylesheet ::item:hover) needs hover events
                obj.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
                obj.viewport().setMouseTracking(True)
            elif kind in (QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.FocusIn, QEvent.Type.FocusOut,
                          QEvent.Type.Hide, QEvent.Type.EnabledChange) and isinstance(obj, QAbstractButton):
                self._button_event(obj, kind, event)
        except Exception:  # noqa: BLE001 - visual feedback must never break the interface
            log.debug("Hover feedback failed", exc_info=True)
        return False

    def _button_event(self, button: QAbstractButton, kind, event) -> None:
        if not self.animate or not is_growing_control(button):
            return
        if kind == QEvent.Type.Enter:
            if button.isEnabled():
                self.grow(button)
        elif kind == QEvent.Type.FocusIn:
            if button.isEnabled() and event.reason() in _KEYBOARD_REASONS:
                self.grow(button)
        elif kind == QEvent.Type.Leave:
            if not (button.hasFocus() and button.property("_rhKeyboardFocus")):
                self.shrink(button)
        elif kind == QEvent.Type.FocusOut:
            button.setProperty("_rhKeyboardFocus", False)
            if not button.underMouse():
                self.shrink(button)
        elif kind in (QEvent.Type.Hide, QEvent.Type.EnabledChange):
            if kind == QEvent.Type.Hide or not button.isEnabled():
                self.reset(button)
        if kind == QEvent.Type.FocusIn and event.reason() in _KEYBOARD_REASONS:
            button.setProperty("_rhKeyboardFocus", True)

    # --- animation
    @staticmethod
    def effect_of(widget: QWidget) -> ScaleEffect | None:
        effect = widget.graphicsEffect()
        return effect if isinstance(effect, ScaleEffect) else None

    def _effect(self, widget: QWidget) -> ScaleEffect | None:
        effect = widget.graphicsEffect()
        if effect is None:
            effect = ScaleEffect(widget)
            widget.setGraphicsEffect(effect)
        return effect if isinstance(effect, ScaleEffect) else None  # another effect: leave it alone

    def _animate(self, widget: QWidget, effect: ScaleEffect, target: float) -> None:
        if effect.animation is None:
            effect.animation = QPropertyAnimation(effect, b"scale", effect)
            effect.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            effect.animation.finished.connect(lambda w=widget, e=effect: self._finished(w, e))
        animation = effect.animation
        animation.stop()
        animation.setDuration(self.duration_ms)
        animation.setStartValue(effect.scale)
        animation.setEndValue(target)
        if self.duration_ms <= 0:
            effect.scale = target
            self._finished(widget, effect)
        else:
            animation.start()

    def _finished(self, widget: QWidget, effect: ScaleEffect) -> None:
        # back at the normal size: drop the effect (no cost while the mouse is elsewhere)
        try:
            if effect.scale <= 1.0001 and widget.graphicsEffect() is effect:
                if effect.animation is not None:
                    effect.animation.stop()
                widget.setGraphicsEffect(None)
        except RuntimeError:  # the widget was deleted
            pass

    def grow(self, widget: QWidget) -> None:
        effect = self._effect(widget)
        if effect is not None:
            self._animate(widget, effect, self.target_scale)

    def shrink(self, widget: QWidget) -> None:
        effect = self.effect_of(widget)
        if effect is not None:
            self._animate(widget, effect, 1.0)

    def reset(self, widget: QWidget) -> None:
        effect = self.effect_of(widget)
        if effect is not None:
            if effect.animation is not None:
                effect.animation.stop()
            effect.scale = 1.0
            self._finished(widget, effect)


def install(app: QApplication | None = None, **options) -> HoverFeedback | None:
    """Installs the filter on the application (once); returns it."""
    app = app or QApplication.instance()
    if app is None:
        return None
    current = getattr(app, "_reversehire_hover", None)
    if current is not None:
        return current
    feedback = HoverFeedback(app, **options)
    app.installEventFilter(feedback)
    app._reversehire_hover = feedback
    log.info("Hover feedback installed (animation %s)", "on" if feedback.animate else "off: reduced motion")
    return feedback
