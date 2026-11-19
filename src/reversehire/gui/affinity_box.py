"""How the affinity is shown: the four list columns (any QTableWidget) and the box at the
top of the company card.

Table helper (used by Results, My companies, Favourites and the Job search companies list):

    AFFINITY_COLUMNS = ("affinity", "verdict", "fits_as", "company_type")
    column_header(column) -> (header text, header tooltip)
    affinity_cells(result, palette, pending=False) -> {column: Cell}
    apply_cell(item, cell, font=None)   # text, tooltip, colours and the NUMERIC sort key
                                        # (table_sort.SORT_ROLE and UserRole)
    sort_value(result, column)          # the same key for panels that sort their own rows

"Affinity" sorts by the percentage, "Verdict" by APPLY < SPONTANEOUS < WATCH < NOT_A_FIT
< UNKNOWN, the two others alphabetically. A missing result gives empty cells (or "…"
while it is computed), always sorted last.

``AffinityBox``: the card box (verdict badge, affinity with confidence, "fits you as",
company type, details with the component scores and their sources, strengths, gaps,
talking points, opportunity signals, what to collect, Agree / Disagree).
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from ..affinity import text as atext
from ..affinity import weights as W
from ..i18n import t
from .table_sort import SORT_ROLE
from .theme import Palette, active_palette
from .widgets import ElidedLabel

log = logging.getLogger(__name__)

AFFINITY_COLUMNS = ("affinity", "verdict", "fits_as", "company_type")
PENDING = "…"
VERDICT_RANK = {W.APPLY: 0, W.SPONTANEOUS: 1, W.WATCH: 2, W.NOT_A_FIT: 3, W.UNKNOWN: 4}
MAX_LINES = 6  # evidence lines per component in the details
DETAILS_LINES = 14  # height of the details, in text lines


@dataclass
class Cell:
    text: str = ""
    tooltip: str = ""
    sort: object = None
    foreground: str | None = None
    background: str | None = None
    bold: bool = False
    right: bool = False  # numbers are right-aligned


# --- texts ---------------------------------------------------------------------------------


def verdict_text(verdict: str) -> str:
    return atext(f"affinity.verdict.{verdict}")


def verdict_help(verdict: str) -> str:
    return atext(f"affinity.verdict_help.{verdict}")


def type_text(key: str | None) -> str:
    return atext(f"affinity.type.{key}") if key else ""


def percent_text(value: float | None) -> str:
    return "" if value is None else f"{round(value)} %"


def direction_name(ident: str, name: str) -> str:
    """A direction's name in the interface language (the shipped ones are translated)."""
    key = f"affinity.ui.direction.{ident}"
    text = t(key)
    return name if text == key else text


def fits_as_text(result) -> str:
    if result is None or result.affinity is None:
        return ""
    if result.direction == "general" or not result.direction_name:
        return atext("affinity.direction.general")
    return direction_name(result.direction, result.direction_name)


def headline_text(result) -> str:
    """"Fits you as: <direction>, 78 % (medium confidence)" in the interface language."""
    if result is None:
        return ""
    if result.affinity is None or result.direction == "general":
        return result.headline_note().render()
    return atext("affinity.headline", direction=fits_as_text(result), score=round(result.affinity),
                 confidence=f"affinity.confidence.{result.confidence}")


def verdict_colours(verdict: str, palette: Palette) -> tuple[str, str]:
    """(background, text) of a verdict, readable in both themes."""
    return {
        W.APPLY: (palette.hiring_bg, palette.hiring_fg),
        W.SPONTANEOUS: (palette.remote_bg, palette.remote_fg),
        W.WATCH: (palette.maybe_bg, palette.maybe_fg),
        W.NOT_A_FIT: (palette.surface2, palette.error),
    }.get(verdict, (palette.surface2, palette.muted))


def column_header(column: str) -> tuple[str, str]:
    """(text, tooltip) of an affinity column header."""
    return t(f"affinity.ui.column.{column}"), t(f"affinity.ui.column.{column}_tooltip")


def scores_tooltip() -> str:
    """Header tooltip of the older company-level scores (secondary to the affinity)."""
    return t("affinity.ui.scores_tooltip")


def other_directions(result) -> list[str]:
    lines = []
    if result is None:
        return lines
    for scope in result.scopes:
        if scope.id in (result.direction, "general") or not scope.applicable or scope.affinity is None:
            continue
        lines.append(atext("affinity.direction.other", direction=direction_name(scope.id, scope.name),
                           score=round(scope.affinity)))
    return lines


def types_lines(result) -> list[str]:
    lines = []
    for ctype in (result.company_types if result is not None else []) or []:
        conf = atext(f"affinity.confidence.{ctype.get('confidence', 'low')}")
        line = atext("affinity.type.line", type=type_text(ctype.get("key")), confidence=conf)
        evidence = (ctype.get("evidence") or [])[:1]
        if evidence:
            source = source_name(evidence[0].get("source", ""))
            line += f" — \"{evidence[0].get('text', '')[:120]}\"" + (f" ({source})" if source else "")
        lines.append(line)
    return lines


def affinity_tooltip(result) -> str:
    lines = [headline_text(result)]
    lines += [n.render() for n in result.reasons]
    lines.append(t("affinity.ui.estimate_note"))
    return "\n".join(lines)


def sort_value(result, column: str):
    """Sort key of an affinity column (None = no value: always last)."""
    if result is None:
        return None
    if column == "affinity":
        return result.affinity
    if column == "verdict":
        return VERDICT_RANK.get(result.verdict, 9)
    if column == "fits_as":
        return fits_as_text(result) or None
    if column == "company_type":
        return type_text(result.primary_type) or None
    return None


def affinity_cells(result, palette: Palette | None = None, *, pending: bool = False) -> dict[str, Cell]:
    """The four cells of a row (see the module docstring)."""
    palette = palette or active_palette()
    if result is None:
        text = PENDING if pending else ""
        tip = t("affinity.ui.computing") if pending else ""
        return {c: Cell(text, tip, None, palette.muted, right=c == "affinity") for c in AFFINITY_COLUMNS}
    background, foreground = verdict_colours(result.verdict, palette)
    cells = {
        "affinity": Cell(percent_text(result.affinity) or "–", affinity_tooltip(result), result.affinity,
                         None, None, result.verdict in (W.APPLY, W.SPONTANEOUS), True),
        "verdict": Cell(verdict_text(result.verdict), verdict_help(result.verdict),
                        VERDICT_RANK.get(result.verdict, 9), foreground, background, result.verdict == W.APPLY),
        "fits_as": Cell(fits_as_text(result), "\n".join(other_directions(result)), fits_as_text(result) or None),
        "company_type": Cell(type_text(result.primary_type), "\n".join(types_lines(result)),
                             type_text(result.primary_type) or None),
    }
    if result.type_excluded:
        cells["company_type"].foreground = palette.error
    return cells


def apply_cell(item: QTableWidgetItem, cell: Cell, font: QFont | None = None) -> QTableWidgetItem:
    """Puts a Cell on a table item (the numeric/rank sort key in SORT_ROLE and UserRole)."""
    item.setText(cell.text)
    if cell.tooltip:
        item.setToolTip(cell.tooltip)
    if isinstance(cell.sort, (int, float)) and not isinstance(cell.sort, bool):
        item.setData(SORT_ROLE, float(cell.sort))
        item.setData(Qt.ItemDataRole.UserRole, float(cell.sort))
    elif cell.sort is not None:
        item.setData(SORT_ROLE, str(cell.sort))
    if cell.background:
        item.setBackground(QBrush(QColor(cell.background)))
    if cell.foreground:
        item.setForeground(QBrush(QColor(cell.foreground)))
    if cell.bold:
        bold = QFont(font) if font is not None else QFont(item.font())
        bold.setBold(True)
        item.setFont(bold)
    align = Qt.AlignmentFlag.AlignRight if cell.right else Qt.AlignmentFlag.AlignLeft
    item.setTextAlignment(align | Qt.AlignmentFlag.AlignVCenter)
    return item


# --- sources ---------------------------------------------------------------------------------


def source_name(source: str) -> str:
    """A named source: the site's domain, "company card: <field>", "OpenStreetMap"..."""
    source = str(source or "")
    if not source:
        return ""
    if source.startswith(("http://", "https://")):
        return urlparse(source).netloc or source
    if source == "osm":
        return "OpenStreetMap"
    if source.startswith("card:"):
        return t("affinity.ui.source.card", field=source.split(":", 1)[1])
    if source.startswith("company:"):
        return t("affinity.ui.source.result", field=source.split(":", 1)[1])
    if source in ("site", "cv"):
        return t(f"affinity.ui.source.{source}")
    return source


def source_html(source: str, palette: Palette) -> str:
    source = str(source or "")
    if not source:
        return ""
    name = html.escape(source_name(source))
    if source.startswith(("http://", "https://")):
        return f' <a href="{html.escape(source, quote=True)}" style="color:{palette.accent}">[{name}]</a>'
    return f' <span style="color:{palette.muted}">[{name}]</span>'


def point_html(point, palette: Palette) -> str:
    text = html.escape(point.note.render())
    link = point.link
    return f"<li>{text}{source_html(link.company_source or '', palette)}</li>"


# --- the card box ------------------------------------------------------------------------------


class AffinityBox(QFrame):
    """The affinity of the shown company card (see the module docstring).

    ``service``: the AffinityService; ``opener(url)``: opens a source link;
    ``on_insert(card, text)``: opens the mail draft for the card and inserts the talking
    points."""

    def __init__(self, service, *, opener: Callable[[str], object] | None = None,
                 on_insert: Callable[[object, str], object] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("affinityBox")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._service = service
        self._opener = opener or (lambda _url: None)
        self._on_insert = on_insert
        self._palette: Palette = active_palette()
        self.card = None
        self.result = None

        self.verdict_label = QLabel()
        self.verdict_label.setObjectName("affinityVerdict")
        # the score and its confidence always visible, even when the headline is cut short
        self.score_label = QLabel()
        self.score_label.setObjectName("affinityScore")
        self.headline_label = ElidedLabel()
        self.headline_label.setProperty("role", "title")
        self.type_label = ElidedLabel()
        self.type_label.setProperty("role", "muted")
        self.reason_label = ElidedLabel()
        self.reason_label.setProperty("role", "muted")
        self.judgement_label = ElidedLabel()
        self.judgement_label.setProperty("role", "muted")
        self.details_button = QPushButton()
        self.details_button.setObjectName("affinityDetails")
        self.details_button.setCheckable(True)
        self.details_button.toggled.connect(self._details_toggled)
        self.agree_button = QPushButton()
        self.agree_button.setObjectName("affinityAgree")
        self.agree_button.clicked.connect(lambda: self.judge(True))
        self.disagree_button = QPushButton()
        self.disagree_button.setObjectName("affinityDisagree")
        self.disagree_button.clicked.connect(lambda: self.judge(False))
        self.insert_button = QPushButton()
        self.insert_button.setObjectName("affinityInsert")
        self.insert_button.clicked.connect(lambda: self.insert_talking_points())
        self.details = QTextBrowser()
        self.details.setObjectName("affinityDetailsText")
        self.details.setOpenLinks(False)
        self.details.setOpenExternalLinks(False)
        self.details.anchorClicked.connect(lambda url: self._open(url.toString()))
        self.details.setMaximumHeight(self.fontMetrics().lineSpacing() * DETAILS_LINES)
        self.details.hide()

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.verdict_label)
        top.addWidget(self.score_label)
        top.addWidget(self.headline_label, 3)
        top.addWidget(self.type_label, 2)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        for button in (self.details_button, self.agree_button, self.disagree_button, self.insert_button):
            buttons.addWidget(button)
        buttons.addWidget(self.judgement_label, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)
        layout.addLayout(top)
        layout.addWidget(self.reason_label)
        layout.addLayout(buttons)
        layout.addWidget(self.details)
        self.retranslate()

    # --- API
    def buttons(self) -> list[QPushButton]:
        return [self.details_button, self.agree_button, self.disagree_button, self.insert_button]

    def set_card(self, card) -> None:
        """Card listener: shows the card's affinity (requesting it when missing)."""
        self.card = card
        self.refresh(request=True)

    def refresh(self, request: bool = False) -> None:
        service, card = self._service, self.card
        self.setVisible(card is not None)
        if card is None:
            self.result = None
            return
        result = service.result_for_card(card) if service is not None else None
        if result is None and request and service is not None and service.has_cv():
            service.request_cards([card])
            result = service.result_for_card(card)  # a cached or synchronous answer
        self.result = result
        self._render()

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.setStyleSheet(f"QFrame#affinityBox {{ border: 1px solid {palette.border}; border-radius: 4px; }}")
        self._render()

    def details_shown(self) -> bool:
        return self.details_button.isChecked()

    def talking_points_text(self) -> str:
        if self.result is None:
            return ""
        return "\n".join(f"- {p.note.render()}" for p in self.result.talking_points)

    def judge(self, agree: bool) -> bool:
        """Agree / Disagree with the verdict shown (saved at once)."""
        if self.card is None or self.result is None or self._service is None:
            return False
        self._service.record_feedback(self.card, self.result, agree)
        self._render()
        return True

    def insert_talking_points(self) -> bool:
        text = self.talking_points_text()
        if not text or self.card is None or self._on_insert is None:
            return False
        log.info("Affinity talking points inserted into the e-mail draft (%d)", len(self.result.talking_points))
        self._on_insert(self.card, text)
        return True

    # --- drawing
    def _open(self, url: str) -> None:
        if url.startswith(("http://", "https://")):
            self._opener(url)

    def _details_toggled(self, shown: bool) -> None:
        self.details.setVisible(shown and self.card is not None)
        self.details_button.setText(t("affinity.ui.hide_details") if shown else t("affinity.ui.details"))
        if shown:
            self._render_details()

    def _render(self) -> None:
        palette, result, card = self._palette, self.result, self.card
        service = self._service
        has_cv = service is not None and service.has_cv()
        if result is None:
            self.verdict_label.hide()
            self.verdict_label.setText("")
            self.score_label.hide()
            if not has_cv:
                self.headline_label.setText(t("affinity.ui.no_cv"))
            elif service.is_failed(card):
                self.headline_label.setText(t("affinity.ui.failed"))
            else:
                self.headline_label.setText(t("affinity.ui.computing"))
            self.type_label.setText("")
            self.reason_label.setText("")
            self.judgement_label.setText("")
        else:
            background, foreground = verdict_colours(result.verdict, palette)
            self.verdict_label.setText(verdict_text(result.verdict))
            self.verdict_label.setToolTip(verdict_help(result.verdict))
            self.verdict_label.setStyleSheet(
                f"QLabel#affinityVerdict {{ background: {background}; color: {foreground}; font-weight: bold;"
                f" padding: 1px 6px; border-radius: 3px; }}")
            self.verdict_label.show()
            if result.affinity is not None:
                confidence = atext(f"affinity.confidence.{result.confidence}")
                self.score_label.setText(f"<b>{round(result.affinity)} %</b> · {html.escape(confidence)}")
                self.score_label.setToolTip(headline_text(result))
                self.score_label.show()
            else:
                self.score_label.hide()
            self.headline_label.setText(headline_text(result))
            self.headline_label.setToolTip(headline_text(result))
            self.type_label.setText(type_text(result.primary_type))
            self.type_label.setToolTip("\n".join(types_lines(result)))
            reasons = [n.render() for n in result.reasons]
            self.reason_label.setText(" ".join(reasons))
            self.reason_label.setToolTip("\n".join(reasons + [t("affinity.ui.estimate_note")]))
            feedback = service.feedback_for(card) if service is not None else None
            if feedback is None:
                self.judgement_label.setText("")
            else:
                self.judgement_label.setText(t("affinity.ui.judged_agree") if feedback.get("agrees")
                                             else t("affinity.ui.judged_disagree"))
        has = result is not None
        self.agree_button.setEnabled(has)
        self.disagree_button.setEnabled(has)
        self.insert_button.setEnabled(has and bool(result.talking_points) and self._on_insert is not None)
        self.details_button.setEnabled(has)
        self.details.setVisible(has and self.details_button.isChecked())
        if self.details.isVisibleTo(self):
            self._render_details()

    def details_html(self) -> str:
        result, p = self.result, self._palette
        if result is None:
            return ""
        parts = [f"<p><b>{html.escape(headline_text(result))}</b> — "
                 f"<span style='color:{p.muted}'>{html.escape(t('affinity.ui.estimate_note'))}</span></p>"]

        def block(title: str, points, render=point_html) -> None:
            items = "".join(render(x, p) for x in points)
            if items:
                parts.append(f"<p><b>{html.escape(title)}</b></p><ul>{items}</ul>")

        reasons = "".join(f"<li>{html.escape(n.render())}</li>" for n in result.reasons)
        if reasons:
            parts.append(f"<ul>{reasons}</ul>")
        others = other_directions(result)
        if others:
            parts.append("<p>" + "<br>".join(html.escape(x) for x in others) + "</p>")
        rows = []
        for key in W.COMPONENTS:
            comp = result.components.get(key)
            if comp is None:
                continue
            score = percent_text(comp.score) if comp.score is not None else t("affinity.ui.no_data")
            lines = "".join(point_html(e, p) for e in comp.evidence[:MAX_LINES])
            rows.append(f"<li><b>{html.escape(atext(f'affinity.component.{key}'))}</b>: {html.escape(score)}"
                        + (f"<ul>{lines}</ul>" if lines else "") + "</li>")
        if rows:
            parts.append(f"<p><b>{html.escape(t('affinity.ui.components'))}</b></p><ul>{''.join(rows)}</ul>")
        block(t("affinity.ui.strengths"), result.strengths)
        block(t("affinity.ui.gaps"), result.gaps)
        block(t("affinity.ui.talking_points"), result.talking_points)
        opportunity = result.opportunity
        if opportunity.signals:
            title = f"{t('affinity.ui.opportunity')} ({atext(f'affinity.opp.level.{opportunity.level}')})"
            block(title, opportunity.signals)
        block(t("affinity.ui.extras"), result.extras)
        hints = "".join(f"<li>{html.escape(n.render())}</li>" for n in result.hints)
        if hints:
            parts.append(f"<p><b>{html.escape(t('affinity.ui.hints'))}</b></p><ul>{hints}</ul>")
        types = types_lines(result)
        if types:
            parts.append("<p>" + "<br>".join(html.escape(x) for x in types) + "</p>")
        return "".join(parts)

    def _render_details(self) -> None:
        position = self.details.verticalScrollBar().value()
        self.details.setHtml(self.details_html())
        self.details.verticalScrollBar().setValue(position)

    def retranslate(self) -> None:
        self.details_button.setText(t("affinity.ui.hide_details") if self.details_button.isChecked()
                                    else t("affinity.ui.details"))
        self.details_button.setToolTip(t("affinity.ui.details_tooltip"))
        self.agree_button.setText(t("affinity.ui.agree"))
        self.agree_button.setToolTip(t("affinity.ui.agree_tooltip"))
        self.disagree_button.setText(t("affinity.ui.disagree"))
        self.disagree_button.setToolTip(t("affinity.ui.disagree_tooltip"))
        self.insert_button.setText(t("affinity.ui.insert"))
        self.insert_button.setToolTip(t("affinity.ui.insert_tooltip"))
        self._render()
