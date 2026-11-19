"""Small Qt building blocks: an eliding label with tooltip, themed message boxes
and file dialogs (replaceable in tests), separators, a status dot button."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import QFileDialog, QFrame, QLabel, QMessageBox, QPushButton, QSizePolicy, QWidget

from ..i18n import t
from .theme import APP_NAME


class ElidedLabel(QLabel):
    """One-line label that elides with "…" when narrow and shows the full text as tooltip.

    It never forces the window wider: its minimum width is small, and the full
    text is always reachable through the tooltip.
    """

    MIN_CHARS = 8

    def __init__(self, text: str = "", parent: QWidget | None = None, mode=Qt.TextElideMode.ElideRight):
        super().__init__(text, parent)
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self._update_tooltip()

    def setText(self, text: str) -> None:  # noqa: N802 (Qt name)
        super().setText(text)
        self._update_tooltip()
        self.update()

    def is_elided(self) -> bool:
        return self.fontMetrics().horizontalAdvance(self.text()) > self.contentsRect().width()

    def _update_tooltip(self) -> None:
        self.setToolTip(self.text() if self.text() and self.is_elided() else "")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_tooltip()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        metrics = self.fontMetrics()
        margins = self.contentsMargins()
        return QSize(metrics.averageCharWidth() * self.MIN_CHARS + margins.left() + margins.right(),
                     super().minimumSizeHint().height())

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), self._mode, rect.width())
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(rect, int(self.alignment() | Qt.AlignmentFlag.AlignVCenter), text)


def wrapped_label(text: str = "", role: str | None = None) -> QLabel:
    """Multi-line label for explanations: wraps instead of widening the window."""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        label.setProperty("role", role)
    return label


def separator() -> QFrame:
    line = QFrame()
    line.setProperty("role", "separator")
    line.setFrameShape(QFrame.Shape.NoFrame)
    line.setFixedHeight(1)
    return line


def dot_pixmap(colour: str, size: int = 12, ratio: float = 1.0) -> QPixmap:
    """A round dot of `colour` with a slightly darker rim (transparent background)."""
    pixels = max(1, round(size * ratio))
    pixmap = QPixmap(pixels, pixels)
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    fill = QColor(colour)
    painter.setPen(fill.darker(140))
    painter.setBrush(fill)
    painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.end()
    return pixmap


def star_pixmap(colour: str, filled: bool = True, size: int = 14, ratio: float = 1.0) -> QPixmap:
    """A five-pointed star (the bundled font has no star glyph): filled or outlined."""
    pixels = max(1, round(size * ratio))
    pixmap = QPixmap(pixels, pixels)
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    centre, outer, inner = size / 2, size / 2 - 0.8, size / 5
    points = []
    for i in range(10):
        radius = outer if i % 2 == 0 else inner
        angle = -math.pi / 2 + i * math.pi / 5
        points.append(QPointF(centre + radius * math.cos(angle), centre + 0.6 + radius * math.sin(angle)))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen_colour = QColor(colour)
    painter.setPen(pen_colour)
    painter.setBrush(pen_colour if filled else Qt.BrushStyle.NoBrush)
    painter.drawPolygon(QPolygonF(points))
    painter.end()
    return pixmap


def star_icon(colour: str, filled: bool = True, size: int = 14) -> QIcon:
    icon = QIcon()
    for ratio in (1.0, 2.0):
        icon.addPixmap(star_pixmap(colour, filled, size, ratio))
    return icon


def theme_pixmap(colour: str, sun: bool, size: int = 14, ratio: float = 1.0) -> QPixmap:
    """A sun (switch to light) or a crescent moon (switch to dark), drawn: the bundled
    font has no such glyphs."""
    pixels = max(1, round(size * ratio))
    pixmap = QPixmap(pixels, pixels)
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QColor(colour)
    centre = size / 2
    if sun:
        painter.setPen(pen)
        painter.setBrush(pen)
        radius = size / 5
        painter.drawEllipse(QPointF(centre, centre), radius, radius)
        for i in range(8):
            angle = i * math.pi / 4
            inner, outer = radius + 1.6, size / 2 - 0.6
            painter.drawLine(QPointF(centre + inner * math.cos(angle), centre + inner * math.sin(angle)),
                             QPointF(centre + outer * math.cos(angle), centre + outer * math.sin(angle)))
    else:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(pen)
        radius = size / 2 - 1.2
        painter.drawEllipse(QPointF(centre, centre), radius, radius)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.drawEllipse(QPointF(centre + radius * 0.55, centre - radius * 0.35), radius * 0.85, radius * 0.85)
    painter.end()
    return pixmap


def theme_icon(colour: str, sun: bool, size: int = 14) -> QIcon:
    icon = QIcon()
    for ratio in (1.0, 2.0):
        icon.addPixmap(theme_pixmap(colour, sun, size, ratio))
    return icon


class StatusDot(QPushButton):
    """Flat button with a coloured dot and a short text ("MCP on" / "MCP off").

    The colour is never the only signal: the text and the tooltip say the same
    thing, for colour-blind users.
    """

    DOT_SIZE = 12

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("statusDot")
        self.setFlat(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setIconSize(QSize(self.DOT_SIZE, self.DOT_SIZE))
        self._on = False
        self._colours = {False: "#FF6B6B", True: "#39D98A"}

    def set_colours(self, off: str, on: str) -> None:
        self._colours = {False: off, True: on}
        self._repaint_dot()

    def set_state(self, on: bool, text: str, tooltip: str = "") -> None:
        self._on = bool(on)
        self.setText(text)
        self.setToolTip(tooltip)
        self.setAccessibleName(text)
        self._repaint_dot()

    def _repaint_dot(self) -> None:
        ratio = self.devicePixelRatioF() if self.isVisible() else 1.0
        self.setIcon(QIcon(dot_pixmap(self.colour(), self.DOT_SIZE, max(1.0, ratio))))

    def is_on(self) -> bool:
        return self._on

    def colour(self) -> str:
        return self._colours[self._on]


class LanguageChips(QWidget):
    """Small flat toggle chips with the interface languages ("EN", "UK", "IT").

    Text codes drawn in the theme (no flag emoji: the bundled font lacks them and a
    flag is not a language); the active one is highlighted; each chip has the
    language name as tooltip and accessible name, and takes the keyboard focus with Tab
    (Space/Enter switches)."""

    def __init__(self, codes: list[str], names: dict[str, str], on_choose, parent: QWidget | None = None):
        super().__init__(parent)
        from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QToolButton

        self.setObjectName("languageChips")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 0, 4, 0)
        layout.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self.buttons: dict[str, QToolButton] = {}
        for code in codes:
            button = QToolButton(self)
            button.setObjectName("languageChip")
            button.setText(code.upper())
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            name = names.get(code, code)
            button.setToolTip(name)
            button.setAccessibleName(name)
            button.clicked.connect(lambda _checked=False, c=code: on_choose(c))
            self._group.addButton(button)
            layout.addWidget(button)
            self.buttons[code] = button

    def set_current(self, code: str) -> None:
        for c, button in self.buttons.items():
            button.setChecked(c == code)

    def current(self) -> str | None:
        return next((c for c, b in self.buttons.items() if b.isChecked()), None)

    def apply_palette(self, p) -> None:
        self.setStyleSheet(
            f"QWidget#languageChips {{ background: transparent; }}"
            f"QToolButton#languageChip {{ background: transparent; color: {p.muted};"
            f" border: 1px solid transparent; border-radius: 3px; padding: 1px 5px; margin: 1px 0;"
            f" font-size: 8pt; }}"
            f"QToolButton#languageChip:hover {{ color: {p.strong}; background-color: {p.selection};"
            f" border-color: {p.accent}; }}"
            f"QToolButton#languageChip:checked {{ color: {p.on_accent}; background-color: {p.accent};"
            f" border-color: {p.accent}; font-weight: bold; }}"
            f"QToolButton#languageChip:checked:hover {{ border-color: {p.strong}; }}"
            f"QToolButton#languageChip:focus {{ border-color: {p.accent}; }}")


class Dialogs:
    """Message boxes and file pickers in the application's theme and language.

    The main window keeps one instance in `dialogs`; tests replace it with a fake.
    """

    def __init__(self, parent: QWidget):
        self._parent = parent

    def _box(self, icon, text: str, title: str | None = None) -> QMessageBox:
        box = QMessageBox(self._parent)
        box.setIcon(icon)
        box.setWindowTitle(title or APP_NAME)
        box.setText(text)
        box.setTextFormat(Qt.TextFormat.PlainText)
        return box

    def _show(self, icon, text: str, title: str | None = None) -> None:
        box = self._box(icon, text, title)
        box.addButton(t("common.ok"), QMessageBox.ButtonRole.AcceptRole)
        box.exec()

    def error(self, text: str, title: str | None = None) -> None:
        self._show(QMessageBox.Icon.Critical, text, title)

    def warning(self, text: str, title: str | None = None) -> None:
        self._show(QMessageBox.Icon.Warning, text, title)

    def info(self, text: str, title: str | None = None) -> None:
        self._show(QMessageBox.Icon.Information, text, title)

    def ask(self, text: str, title: str | None = None, warning: bool = False) -> bool:
        box = self._box(QMessageBox.Icon.Warning if warning else QMessageBox.Icon.Question, text, title)
        yes = box.addButton(t("common.yes"), QMessageBox.ButtonRole.YesRole)
        no = box.addButton(t("common.no"), QMessageBox.ButtonRole.NoRole)
        box.setDefaultButton(no if warning else yes)
        box.setEscapeButton(no)
        box.exec()
        return box.clickedButton() is yes

    # Qt's own dialog instead of the native one, so it follows the app theme.
    _OPTIONS = QFileDialog.Option.DontUseNativeDialog

    def open_file(self, title: str, filters: str) -> str:
        path, _ = QFileDialog.getOpenFileName(self._parent, title, "", filters, options=self._OPTIONS)
        return path

    def save_file(self, title: str, default_name: str, filters: str) -> str:
        path, _ = QFileDialog.getSaveFileName(self._parent, title, default_name, filters,
                                              options=self._OPTIONS)
        return path
