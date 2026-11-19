"""A row of small labelled link buttons: the company's website, social networks, company
pages on job sites, Wikipedia/Wikidata and a map search (see web/social.py).

Used beside the company name in the card header and in the Results detail panel. Each
button runs ONE QAction (`QToolButton.setDefaultAction`), and the same actions are listed
in `menu` ("Links of this company"), which the main window puts in its menu bar: the menu
and the buttons always share their actions, rebuilt when the company changes. Searches
(LinkedIn company search when the page is unknown, the map) are drawn with a dashed
border and labelled "search"; the app only opens them in the browser.
"""

from __future__ import annotations

import logging
import webbrowser

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QSizePolicy, QToolButton, QWidget

from ..i18n import t
from ..web import social
from .common import open_url
from .menus import bind_action
from .theme import Palette, active_palette

log = logging.getLogger(__name__)

ICON_PX = 18


def tr(key: str, default: str = "", **params) -> str:
    text = t(key, **params)
    if text == key:
        try:
            return default.format(**params)
        except (KeyError, IndexError):
            return default
    return text


def network_name(network: str) -> str:
    return tr(f"social.network.{network}", social.NAMES.get(network, network))


def link_label(link: social.SocialLink) -> str:
    """"LinkedIn" for a page, "LinkedIn (search)" for a search."""
    name = network_name(link.network)
    return tr("social.search_label", "{name} (search)", name=name) if link.is_search else name


def link_tooltip(link: social.SocialLink) -> str:
    return f"{link_label(link)}\n{link.url}\n{social.provenance_text(link, tr)}"


def glyph_icon(text: str, colour: str, *, dashed: bool = False, size: int = ICON_PX) -> QIcon:
    """A monochrome round badge with a short glyph ("in", "f", "YT")."""
    ratio = 2
    pixmap = QPixmap(size * ratio, size * ratio)
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(colour), 1.4)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        rect = QRectF(1, 1, size - 2, size - 2)
        painter.drawEllipse(rect)
        font = QFont()
        font.setBold(True)
        font.setPixelSize(max(6, int(size * (0.55 if len(text) == 1 else 0.42))))
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
    finally:
        painter.end()
    return QIcon(pixmap)


class SocialLinksBar(QWidget):
    """The row of link buttons (see the module docstring)."""

    def __init__(self, parent: QWidget | None = None, *, opener=webbrowser.open, compact: bool = False):
        super().__init__(parent)
        self._opener = opener
        self._compact = compact  # icons only (text in the tooltip): narrow panels
        self._palette: Palette = active_palette()
        self.links: list[social.SocialLink] = []
        self.buttons: list[QToolButton] = []
        self.actions_: list[QAction] = []
        self.menu = QMenu(self)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)
        self.empty_label = QLabel()
        self.empty_label.setProperty("role", "muted")
        self._layout.addWidget(self.empty_label)
        self._layout.addStretch(1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.retranslate()

    # --- content

    def set_links(self, links: list[social.SocialLink]) -> None:
        links = list(links or [])
        if links == self.links and len(self.buttons) == len(links):
            return  # unchanged: keep the buttons (and the keyboard focus)
        self.links = links
        self._rebuild()

    def set_card(self, card) -> None:
        try:
            self.set_links(social.links_for_card(card) if card is not None else [])
        except Exception:  # noqa: BLE001 - links must never break the card
            log.warning("Company links not built", exc_info=True)
            self.set_links([])

    def set_company(self, company) -> None:
        try:
            self.set_links(social.links_for_company(company) if company is not None else [])
        except Exception:  # noqa: BLE001
            log.warning("Company links not built", exc_info=True)
            self.set_links([])

    def _rebuild(self) -> None:
        for button in self.buttons:
            self._layout.removeWidget(button)
            button.hide()
            button.deleteLater()
        for action in self.actions_:
            action.deleteLater()
        self.buttons, self.actions_ = [], []
        self.menu.clear()
        position = 0
        for link in self.links:
            action = QAction(self)
            action.triggered.connect(lambda _checked=False, item=link: self.open_link(item))
            button = QToolButton(self)
            button.setDefaultAction(action)
            button.setAutoRaise(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.setIconSize(QSize(ICON_PX, ICON_PX))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly if self._compact
                                      else Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setProperty("socialNetwork", link.network)
            button.setProperty("socialKind", link.kind)
            bind_action(button, action)
            self._layout.insertWidget(position, button)
            position += 1
            self.buttons.append(button)
            self.actions_.append(action)
            self.menu.addAction(action)
        self.empty_label.setVisible(not self.links)
        self.menu.setEnabled(bool(self.links))
        self._texts()
        self._style()
        if self.width() > 0 and self.isVisible():
            self._fit(self.width())
        self.updateGeometry()

    def open_link(self, link: social.SocialLink) -> bool:
        opened = open_url(link.url, self._opener)
        log.info("Company link opened: %s (%s)%s", link.network, link.kind, "" if opened else " - failed")
        return opened

    # --- texts and theme

    def _texts(self) -> None:
        for link, action, button in zip(self.links, self.actions_, self.buttons):
            label = link_label(link)
            action.setText(label.replace("&", "&&"))
            action.setToolTip(link_tooltip(link))
            button.setToolTip(link_tooltip(link))
            button.setAccessibleName(label)

    def _style(self) -> None:
        palette = self._palette
        for link, action, button in zip(self.links, self.actions_, self.buttons):
            colour = palette.muted if link.is_search else palette.strong
            action.setIcon(glyph_icon(social.GLYPHS.get(link.network, "?"), colour, dashed=link.is_search))
            border = "dashed" if link.is_search else "solid"
            style = "italic" if link.is_search else "normal"
            button.setStyleSheet(
                f"QToolButton {{ color: {colour}; border: 1px {border} {palette.border}; border-radius: 9px;"
                f" padding: 1px 6px 1px 3px; font-style: {style}; }}"
                f" QToolButton:hover, QToolButton:focus {{ border-color: {palette.accent}; }}")

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self._style()

    def retranslate(self) -> None:
        self.menu.setTitle(tr("social.menu", "Links of this company"))
        self.empty_label.setText(tr("social.none", "No links known"))
        self._texts()

    # --- width: labelled when there is room, icons only otherwise (never widens the window)

    def _icon_only_width(self) -> int:
        spacing = self._layout.spacing()
        return sum(ICON_PX + 18 + spacing for _ in self.buttons)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt name)
        hint = super().minimumSizeHint()
        return QSize(min(hint.width(), max(self._icon_only_width(), 40)), hint.height())

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(event)
        self._fit(event.size().width())

    def _fit(self, width: int) -> None:
        if self._compact or not self.buttons:
            return
        labelled = sum(b.sizeHint().width() for b in self.buttons if
                       b.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        if labelled == 0:  # currently icons only: what would the labels need?
            metrics = self.fontMetrics()
            labelled = sum(ICON_PX + 24 + metrics.horizontalAdvance(b.text()) for b in self.buttons)
        style = (Qt.ToolButtonStyle.ToolButtonTextBesideIcon if labelled + self._layout.spacing() * len(self.buttons)
                 <= width else Qt.ToolButtonStyle.ToolButtonIconOnly)
        for button in self.buttons:
            if button.toolButtonStyle() != style:
                button.setToolButtonStyle(style)

    # --- for the tests and the exports

    def labels(self) -> list[str]:
        return [link_label(link) for link in self.links]
