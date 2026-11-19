"""Tab "Recruiter view": how a recruiter would screen the CV (scores per area,
findings with the CV lines that show them and advice, role coverage) and the
AI-style signals: the estimated probability that the text was generated with AI
(always with its confidence band and the fixed caption that it is an estimate
from writing signals, not proof), the signals behind it with a rewrite
suggestion per sentence, the level and the limits notice. Filled after every CV
reading."""

from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QProgressBar, QScrollArea, QSplitter, QTextBrowser, QVBoxLayout,
    QWidget,
)

from .. import display_text
from ..i18n import t
from .common import format_number
from .theme import Palette, active_palette, section, severity_colour
from .widgets import wrapped_label

AREAS = ("readability", "career", "substance", "coverage", "personal", "hidden")
# Glyphs present in the bundled JetBrains Mono (it has no ✔/✖).
SEVERITY_MARKS = {"problem": "✗", "warning": "▲", "info": "●", "good": "✓"}
METHODS = ("exact", "fuzzy", "tokens", "semantic")


def _number(value: float) -> str:
    if float(value).is_integer():
        return format_number(value)
    return format_number(value, 1 if abs(value) >= 1 else 2)


def _value(v):
    if isinstance(v, float):
        return _number(v)
    if isinstance(v, str):
        return display_text.display(v)  # CV words in English when the interface is English
    return v


def _text(key: str, params: dict | None) -> str:
    return t(key, **{k: _value(v) for k, v in (params or {}).items()})


def _e(text: str) -> str:
    return html.escape(str(text))


def evidence_html(lines, p: Palette, show_original: bool = False) -> str:
    """Quoted CV lines, in English with an English interface; with `show_original`
    the CV wording follows each translated line (the evidence stays verifiable)."""
    rows = []
    for line in lines:
        shown = display_text.show(line)
        rows.append(f"“{_e(shown.text)}”")
        if show_original and shown.original is not None:
            rows.append(f'<span style="color:{p.muted}"><i>{_e(t("display.original", text=shown.original))}</i></span>')
    return "<br>".join(rows)


def findings_html(review, p: Palette, show_original: bool = False) -> str:
    """The findings grouped by area, with severity mark and colour, evidence and advice."""
    if review is None:
        return f'<p style="color:{p.muted}">{_e(t("recruiter.empty"))}</p>'
    parts = []
    for area in AREAS:
        findings = [f for f in review.findings if f.area == area]
        coverage = list(getattr(review, "coverage", []) or []) if area == "coverage" else []
        if not findings and not coverage:
            continue
        score = review.area_scores.get(area)
        score_text = f"{score}/100" if score is not None else t("recruiter.not_assessed")
        parts.append(f'<h3 style="color:{p.accent}; margin-top:10px">{_e(t(f"recruiter.area.{area}"))}'
                     f' <span style="color:{p.muted}; font-weight:normal">· {_e(score_text)}</span></h3>')
        for f in findings:
            colour = severity_colour(p, f.severity)
            mark = SEVERITY_MARKS.get(f.severity, "●")
            label = t(f"recruiter.severity.{f.severity}")
            parts.append(f'<p style="margin:6px 0 2px 0"><span style="color:{colour}; font-weight:bold">'
                         f'{mark} {_e(label)}</span> <span style="color:{p.strong}">'
                         f'{_e(_text(f.key, f.params))}</span></p>')
            if f.evidence:
                lines = evidence_html(f.evidence, p, show_original)
                parts.append(f'<p style="margin:0 0 2px 22px; color:{p.muted}">'
                             f'{_e(t("recruiter.evidence"))}:<br>{lines}</p>')
            if f.advice_key:
                parts.append(f'<p style="margin:0 0 4px 22px; color:{p.text}"><b>{_e(t("recruiter.advice"))}:</b> '
                             f'{_e(_text(f.advice_key, f.params))}</p>')
        if coverage:
            parts.append(coverage_html(coverage, p))
    return "".join(parts)


def coverage_html(coverage, p: Palette) -> str:
    covered = [c for c in coverage if c.covered]
    missing = [c for c in coverage if not c.covered]
    rows = [f'<p style="margin:8px 0 2px 0; color:{p.strong}"><b>{_e(t("recruiter.coverage_list"))}</b></p>']
    for c in covered:
        method = t(f"recruiter.method.{c.method}") if c.method in METHODS else ""
        rows.append(f'<p style="margin:0 0 0 12px"><span style="color:{p.accent}">✓ {_e(display_text.display(c.skill))}</span>'
                    f' <span style="color:{p.muted}">{_e(method)}</span></p>')
    for c in missing:
        rows.append(f'<p style="margin:0 0 0 12px; color:{p.warning}">✗ {_e(display_text.display(c.skill))}'
                    f' <span style="color:{p.muted}">{_e(t("recruiter.missing"))}</span></p>')
    return "".join(rows)


def signals_html(signals, p: Palette, show_original: bool = False) -> str:
    if signals is None:
        return f'<p style="color:{p.muted}">{_e(t("recruiter.empty"))}</p>'
    if not signals.signals:
        return f'<p style="color:{p.accent}">{_e(t("ai.none"))}</p>'
    parts = []
    for s in signals.signals:
        parts.append(f'<p style="margin:6px 0 2px 0; color:{p.strong}">● {_e(_text(s.key, s.params))}</p>')
        if s.evidence:
            lines = evidence_html(s.evidence, p, show_original)
            parts.append(f'<p style="margin:0 0 2px 16px; color:{p.muted}">{_e(t("ai.evidence"))}:<br>{lines}</p>')
        advice = s.advice_key or f"{s.key}.advice"
        if t(advice) != advice:
            parts.append(f'<p style="margin:0 0 4px 16px; color:{p.text}"><b>{_e(t("recruiter.advice"))}:</b> '
                         f'{_e(_text(advice, s.params))}</p>')
    return "".join(parts)


def estimate_html(estimate, p: Palette, show_original: bool = False) -> str:
    """The signals behind the AI estimate: each with its share, the sentences
    concerned and one rewrite suggestion per sentence."""
    if estimate is None or not estimate.signals:
        return ""
    parts = [f'<p style="margin:4px 0 2px 0; color:{p.strong}"><b>{_e(t("ai.estimate.drivers"))}</b></p>']
    for s in estimate.signals:
        share = t("ai.estimate.share", share=round(s.share * 100))
        parts.append(f'<p style="margin:6px 0 2px 0; color:{p.strong}">● {_e(_text(s.key, s.params))}'
                     f' <span style="color:{p.muted}">· {_e(share)}</span></p>')
        for advice in s.sentences:
            shown = display_text.show(advice.text)
            rows = [f"“{_e(shown.text)}”"]
            if show_original and shown.original is not None:
                rows.append(f'<span style="color:{p.muted}"><i>'
                            f'{_e(t("display.original", text=shown.original))}</i></span>')
            parts.append(f'<p style="margin:0 0 0 16px; color:{p.muted}">{"<br>".join(rows)}</p>')
            parts.append(f'<p style="margin:0 0 4px 28px; color:{p.text}">→ <b>{_e(t("ai.estimate.rewrite"))}:</b> '
                         f'{_e(_text(advice.suggestion_key, advice.params))}</p>')
    parts.append(f'<p style="margin:10px 0 2px 0; color:{p.strong}"><b>{_e(t("ai.estimate.all_signals"))}</b></p>')
    return "".join(parts)


class BandBar(QWidget):
    """A 0..100 % bar with the confidence band (lighter) and the estimate (marker)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.value: float | None = None
        self.low = 0.0
        self.high = 0.0
        self._palette: Palette = active_palette()
        self.setMinimumHeight(self.fontMetrics().height())
        self.setMinimumWidth(self.fontMetrics().averageCharWidth() * 20)

    def set_estimate(self, value: float | None, low: float = 0.0, high: float = 0.0) -> None:
        self.value, self.low, self.high = value, low, high
        self.update()

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt name
        from PySide6.QtGui import QColor, QPainter

        p = self._palette
        painter = QPainter(self)
        try:
            rect = self.rect().adjusted(0, 2, -1, -3)
            painter.setPen(QColor(p.border))
            painter.setBrush(QColor(p.surface2))
            painter.drawRect(rect)
            if self.value is None:
                return
            width = rect.width()
            band = QColor(p.warning)
            band.setAlpha(90)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(band)
            x0 = rect.left() + round(width * self.low)
            x1 = rect.left() + round(width * self.high)
            painter.drawRect(x0, rect.top() + 1, max(1, x1 - x0), rect.height() - 1)
            marker = rect.left() + round(width * self.value)
            painter.setBrush(QColor(p.strong))
            painter.drawRect(max(rect.left(), marker - 1), rect.top() - 1, 3, rect.height() + 3)
        finally:
            painter.end()


class RecruiterPanel(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._review = None
        self._signals = None
        self._estimate = None
        self._palette: Palette = active_palette()

        self.title_label = QLabel()
        self.title_label.setProperty("role", "title")
        self.intro_label = wrapped_label(role="muted")
        self.score_label = QLabel()
        self.score_label.setProperty("role", "score")
        self.capped_label = wrapped_label(role="warning")
        self.area_labels: dict[str, QLabel] = {}
        self.area_bars: dict[str, QProgressBar] = {}
        self.area_scores: dict[str, QLabel] = {}
        areas = QGridLayout()
        areas.setContentsMargins(0, 0, 0, 0)
        for row, area in enumerate(AREAS):
            label = QLabel()
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(False)
            bar.setMinimumWidth(self.fontMetrics().averageCharWidth() * 12)
            bar.setMaximumHeight(self.fontMetrics().height())
            score = QLabel()
            score.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            areas.addWidget(label, row, 0)
            areas.addWidget(bar, row, 1)
            areas.addWidget(score, row, 2)
            self.area_labels[area] = label
            self.area_bars[area] = bar
            self.area_scores[area] = score
        areas.setColumnStretch(1, 1)
        self.findings = QTextBrowser()
        self.findings.setOpenLinks(False)
        # Evidence translated into English: the CV wording on demand.
        self.original_box = QCheckBox()
        self.original_box.toggled.connect(lambda _on: self._redraw())

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 4, 0)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(self.score_label)
        head.addStretch(1)
        head.addWidget(self.original_box)
        left_layout.addLayout(head)
        left_layout.addWidget(self.capped_label)
        left_layout.addLayout(areas)
        left_layout.addWidget(self.findings, 1)

        self.ai_box = QGroupBox()
        self.level_label = QLabel()
        self.level_label.setProperty("role", "score")
        self.notice_label = wrapped_label(role="warning")
        self.unreliable_label = wrapped_label(role="muted")
        self.signals_view = QTextBrowser()
        self.signals_view.setOpenLinks(False)
        # Probability that the text was generated with AI: a bar with its band and the
        # fixed caption (an estimate from writing signals, not proof).
        self.estimate_title = QLabel()
        self.estimate_title.setProperty("role", "strong")
        self.estimate_bar = BandBar()
        self.estimate_value = QLabel()
        self.estimate_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.estimate_caption = wrapped_label(role="warning")
        estimate_row = QHBoxLayout()
        estimate_row.setContentsMargins(0, 0, 0, 0)
        estimate_row.addWidget(self.estimate_bar, 1)
        estimate_row.addWidget(self.estimate_value)
        ai_layout = QVBoxLayout(self.ai_box)
        ai_layout.addWidget(self.estimate_title)
        ai_layout.addLayout(estimate_row)
        ai_layout.addWidget(self.estimate_caption)
        ai_layout.addWidget(self.level_label)
        ai_layout.addWidget(self.notice_label)
        ai_layout.addWidget(self.unreliable_label)
        ai_layout.addWidget(self.signals_view, 1)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        # both sides scroll on a small window instead of squeezing their texts
        self.left_scroll = QScrollArea()
        self.left_scroll.setWidgetResizable(True)
        self.left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.left_scroll.setWidget(left)
        self.left_scroll.setMinimumWidth(left.minimumSizeHint().width() + 16)
        self.splitter.addWidget(self.left_scroll)
        # the AI-writing box scrolls on a small window instead of squeezing its notes
        self.ai_scroll = QScrollArea()
        self.ai_scroll.setWidgetResizable(True)
        self.ai_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.ai_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.ai_scroll.setWidget(self.ai_box)
        self.ai_scroll.setMinimumWidth(self.ai_box.minimumSizeHint().width() + 16)
        self.splitter.addWidget(self.ai_scroll)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([600, 400])

        layout = QVBoxLayout(self)
        layout.addWidget(self.title_label)
        layout.addWidget(self.intro_label)
        layout.addWidget(self.splitter, 1)
        self.retranslate()

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.estimate_bar.apply_palette(palette)
        self._redraw()

    def set_results(self, review, signals, estimate=None) -> None:
        """`review`: cv.recruiter.RecruiterReview; `signals`: cv.ai_signals.AISignals;
        `estimate`: cv.ai_estimate.AiEstimate (None = not available)."""
        self._review = review
        self._signals = signals
        self._estimate = estimate
        self._redraw()

    def clear(self) -> None:
        self.set_results(None, None, None)

    @property
    def estimate(self):
        return self._estimate

    def estimate_text(self) -> str:
        return self.estimate_value.text()

    @property
    def review(self):
        return self._review

    @property
    def signals(self):
        return self._signals

    def findings_text(self) -> str:
        return self.findings.toPlainText()

    def signals_text(self) -> str:
        return self.signals_view.toPlainText()

    def _redraw(self) -> None:
        p = self._palette
        review = self._review
        if review is None:
            self.score_label.setText(t("recruiter.score", score="–"))
        else:
            self.score_label.setText(t("recruiter.score", score=review.score))
        capped = review is not None and getattr(review, "capped", False)
        self.capped_label.setText(t("recruiter.capped", cap=review.score) if capped else "")
        self.capped_label.setVisible(capped)
        for area in AREAS:
            self.area_labels[area].setText(t(f"recruiter.area.{area}"))
            bar = self.area_bars[area]
            score = review.area_scores.get(area) if review is not None else None
            if score is None:
                bar.setValue(0)
                bar.setStyleSheet("")
                self.area_scores[area].setText(t("recruiter.not_assessed"))
            else:
                bar.setValue(int(score))
                colour = p.accent if score >= 75 else p.warning if score >= 50 else p.error
                bar.setStyleSheet(f"QProgressBar::chunk {{ background-color: {colour}; }}")
                self.area_scores[area].setText(f"{score}/100")
        original = self.original_box.isChecked()
        self.findings.setHtml(findings_html(review, p, original))
        signals = self._signals
        if signals is None:
            self.level_label.setText(t("ai.title"))
            self.level_label.setStyleSheet("")
        else:
            self.level_label.setText(t(f"ai.level.{signals.level}"))
            self.level_label.setStyleSheet(f"color: {severity_colour(p, signals.level)};")
        reliable = signals is None or getattr(signals, "reliable", True)
        self.unreliable_label.setText("" if reliable else t("ai.notice.unreliable", words=signals.words,
                                                               minimum=_minimum_words()))
        self.unreliable_label.setVisible(not reliable)
        notice = getattr(signals, "notice_key", "ai.notice.limits") if signals is not None else "ai.notice.limits"
        self.notice_label.setText(t(notice))
        estimate = self._estimate
        self.estimate_title.setText(t("ai.estimate.title"))
        self.estimate_caption.setText(t(getattr(estimate, "caption_key", None) or "ai.estimate.caption"))
        if estimate is None:
            self.estimate_bar.set_estimate(None)
            self.estimate_value.setText("–")
            self.estimate_value.setToolTip("")
        else:
            low, high = estimate.band_percent
            self.estimate_bar.set_estimate(estimate.probability, estimate.low, estimate.high)
            self.estimate_value.setText(t("ai.estimate.value", percent=estimate.percent, low=low, high=high))
            self.estimate_value.setToolTip(t("ai.estimate.band_tooltip", low=low, high=high))
        self.signals_view.setHtml(estimate_html(estimate, p, original) + signals_html(signals, p, original))
        self.original_box.setVisible(display_text.interface_is_english() and self._has_translations())

    def _has_translations(self) -> bool:
        texts = display_text.analysis_texts(None, self._review, self._signals)
        return any(display_text.show(x).original is not None for x in texts)

    def retranslate(self) -> None:
        self.title_label.setText(t("recruiter.title"))
        self.intro_label.setText(t("recruiter.intro"))
        self.ai_box.setTitle(section(t("ai.title")))
        self.original_box.setText(t("display.show_original"))
        self._redraw()


def _minimum_words() -> int:
    try:
        from ..cv.ai_signals import MIN_WORDS

        return MIN_WORDS
    except Exception:  # pragma: no cover
        return 80
