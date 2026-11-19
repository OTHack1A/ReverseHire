"""Tab "Recruiter analysis": simulators of the recruiters' screening systems run on the
CV currently loaded (recruiter_sim package).

Target: a role typed by the user, a posting (from Results, Job search or a pasted ad)
or a company (its detected selection system is then preselected). System: the
detected one, any simulator profile, or "Compare all systems". For one system the
pipeline is shown as ordered cards ("what recruiters do -> what we found, with the CV
line -> fix", with a status colour), "What the recruiter sees", the simulated score
with its explanation, the top fixes with the points each would add, and the match
with the posting. The fixed banner says that every simulator is an approximation
built from public documentation, with the profile's confidence and its sources.

The analysis runs on a worker with progress ("done/estimated" steps) and Cancel; a
change of CV or target only marks the result as out of date ("Run analysis" again).
"""

from __future__ import annotations

import html
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QComboBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPushButton, QRadioButton, QScrollArea, QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem, QTextBrowser,
    QVBoxLayout, QWidget,
)

from ..i18n import t
from .common import open_url, run_job
from .theme import Palette, active_palette
from . import table_filter, table_sort
from .widgets import ElidedLabel, wrapped_label

log = logging.getLogger(__name__)

MODE_ROLE, MODE_POSTING, MODE_COMPANY = "role", "posting", "company"
SYSTEM_DETECTED = "__detected__"
SYSTEM_ALL = "__all__"
ANALYSIS_TIMEOUT_S = 600.0
TOP_FIXES = 5
STATUS_MARKS = {"ok": "✓", "warn": "▲", "fail": "✗", "unknown": "●"}
TABLE_COLUMNS = ("system", "score", "scale", "band", "losses", "confidence")


# --- texts ---------------------------------------------------------------------------------


def _plain(value) -> str:
    if isinstance(value, float):
        return f"{value:.1f}".rstrip("0").rstrip(".")
    if isinstance(value, (list, tuple)):
        return ", ".join(_plain(v) for v in value)
    return str(value)


_DEFAULT_KEYS: dict[str, str] | None = None


def _param_text(value) -> str:
    """A parameter in the interface language: the simulators pass some labels (band,
    component, knock-out topic) as their English default text; those are translated back
    through their key."""
    global _DEFAULT_KEYS
    if hasattr(value, "key") and hasattr(value, "params"):  # a nested Message ("simulated 61/100 (...)")
        return message_text(value)
    if isinstance(value, str) and value:
        if _DEFAULT_KEYS is None:
            from ..recruiter_sim import texts as rsim_texts

            _DEFAULT_KEYS = {text: key for key, text in rsim_texts.TEXTS.items() if "{" not in text}
        key = _DEFAULT_KEYS.get(value)
        if key is not None:
            text = t(key)
            return value if text == key else text
    return _plain(value)


def message_text(message) -> str:
    """A recruiter_sim Message in the interface language (its English default when the
    locale files have no text for the key)."""
    if message is None:
        return ""
    params = {k: _param_text(v) for k, v in (getattr(message, "params", None) or {}).items()}
    text = t(message.key, **params)
    return message.default if text == message.key else text


def status_colour(p: Palette, status: str) -> str:
    return {"ok": p.accent, "warn": p.warning, "fail": p.error}.get(status, p.muted)


def _e(text) -> str:
    return html.escape(str(text or ""))


# --- the simulated system for a selection-system hit ------------------------------------


def profile_key_for_platform(key: str | None, url: str | None = None) -> str | None:
    """Simulator profile of a detected platform (card "Selection system" entry, a posting
    host): the platform key first, then the host of the URL; None when unknown."""
    from ..recruiter_sim import profile_for_host, profile_for_platform
    from ..recruiter_sim.profiles import BASELINE_KEY
    from ..web.ats import platform_for_host

    candidates = []
    if key:
        candidates.append(str(key))
        host_platform = platform_for_host(str(key)) if "." in str(key) else None
        if host_platform:
            candidates.append(host_platform)
    if url:
        host_platform = platform_for_host(url)
        if host_platform:
            candidates.append(host_platform)
    for candidate in candidates:
        profile = profile_for_platform(candidate)
        if profile.key != BASELINE_KEY:
            return profile.key
    if url:
        from urllib.parse import urlsplit

        try:
            host = urlsplit(url if "://" in url else "https://" + url).hostname or ""
        except ValueError:
            host = ""
        profile = profile_for_host(host)
        if profile.key != BASELINE_KEY:
            return profile.key
    return None


def detected_for_card(card) -> tuple[str | None, str]:
    """(profile key, where it was seen) for a company card: its first selection-system
    entry that a simulator knows."""
    platforms = (getattr(card, "ats", None) or {}).get("platforms", []) if card is not None else []
    for platform in platforms:
        key = profile_key_for_platform(platform.get("key"), platform.get("url"))
        if key:
            return key, str(platform.get("url") or "")
    return None, ""


def detected_for_posting(posting) -> tuple[str | None, str]:
    """(profile key, URL) for a posting: its ATS source ("ats:workday") or its host."""
    if posting is None:
        return None, ""
    get = posting.get if isinstance(posting, dict) else (lambda name, default="": getattr(posting, name, default))
    source = str(get("source", "") or "")
    url = str(get("url", "") or "")
    platform = source.split(":", 1)[1] if source.startswith("ats:") else None
    if get("is_agency", False) and get("agency", None):
        platform = platform or str(get("agency")).split()[0].lower()
    return profile_key_for_platform(platform, url), url


# --- the request and its outcome ---------------------------------------------------------


@dataclass
class TargetChoice:
    """What the user chose as target, with its label for the status line."""

    mode: str
    label: str
    target: object  # recruiter_sim.Target
    detected_key: str | None = None
    detected_from: str = ""


@dataclass
class AnalysisOutcome:
    target: TargetChoice
    system: str  # profile key or SYSTEM_ALL
    results: dict = field(default_factory=dict)  # profile key -> SimulationResult



def system_name(key: str, name: str) -> str:
    """The name of a simulated system in the interface language: the generic ones ("Italian
    staffing agency (back office not documented)", "E-mail to HR (no ATS)"…) have a
    translation; product names stay as they are."""
    text = t(f"rsim.profile.{key}.name")
    return name if text == f"rsim.profile.{key}.name" else text

class _Bridge(QObject):
    progress = Signal(int, int, str)


# --- HTML of one simulated system ----------------------------------------------------------


def _evidence_html(evidence, p: Palette) -> str:
    rows = []
    for ref in evidence[:4]:
        where = t("analysis.evidence.line", line=ref.line) if ref.line else t(f"analysis.evidence.source.{ref.source}")
        rows.append(f"<span style='color:{p.muted}'>{_e(where)}:</span> "
                    f"<span style='color:{p.strong}'>“{_e(ref.text)}”</span>")
    return "<br>".join(rows)


def pipeline_html(result, p: Palette) -> str:
    """The checks in order, one card each: what recruiters do -> what we found (with
    the evidence line) -> fix, with the status colour."""
    from ..recruiter_sim import CHECK_IDS

    out = []
    for check in CHECK_IDS:
        if check == "score":
            continue
        findings = result.by_check(check)
        if not findings:
            continue
        status = result.check_status(check)
        colour = status_colour(p, status)
        mark = STATUS_MARKS.get(status, "●")
        title = message_text(_msg(f"rsim.check.{check}"))
        word = message_text(_msg(f"rsim.status.{status}"))
        rows = [f"<tr><td style='padding:4px 6px'><span style='color:{colour}; font-weight:bold'>{mark} {_e(title)}"
                f"</span> <span style='color:{colour}'>· {_e(word)}</span></td></tr>"]
        seen_do = set()
        for finding in findings:
            parts = []
            do_text = message_text(finding.what_recruiters_do)
            if do_text and do_text not in seen_do:
                seen_do.add(do_text)
                parts.append(f"<span style='color:{p.muted}'>{_e(t('analysis.card.do'))}</span> {_e(do_text)}")
            mark_f = STATUS_MARKS.get(finding.status, "●")
            parts.append(f"<span style='color:{status_colour(p, finding.status)}'>{mark_f}</span> "
                         f"<span style='color:{p.muted}'>{_e(t('analysis.card.found'))}</span> "
                         f"<span style='color:{p.strong}'>{_e(message_text(finding.what_we_found))}</span>")
            if finding.evidence:
                parts.append(_evidence_html(finding.evidence, p))
            if finding.fix is not None:
                parts.append(f"<span style='color:{p.muted}'>{_e(t('analysis.card.fix'))}</span> "
                             f"<span style='color:{p.text}'>{_e(message_text(finding.fix))}</span>")
            rows.append(f"<tr><td style='padding:2px 6px 6px 18px'>{'<br>'.join(parts)}</td></tr>")
        out.append(f"<table width='100%' cellspacing='0' style='background-color:{p.surface2}; "
                   f"margin-bottom:8px; border-left:3px solid {colour}'>" + "".join(rows) + "</table>")
    return "".join(out)


def _msg(key: str, **params):
    from ..recruiter_sim.model import msg

    return msg(key, **params)


def record_html(result, p: Palette) -> str:
    """"What the recruiter sees": the candidate record as the system would fill it."""
    record = result.record
    out = [f"<h3 style='color:{p.accent}'>{_e(t('analysis.record.title'))}</h3>",
           f"<p style='color:{p.muted}'>{_e(t('analysis.record.note'))}</p><table cellspacing='0'>"]
    for item in record.fields:
        name = message_text(_msg(f"rsim.record.field.{item.name}"))
        colour = p.accent if item.status == "found" else (p.warning if item.status == "garbled" else p.error)
        state = t(f"analysis.field_status.{item.status}")
        value = _e(item.value) if item.value else f"<span style='color:{p.muted}'>–</span>"
        out.append(f"<tr><td style='padding:1px 10px 1px 0; color:{p.muted}'>{_e(name)}</td>"
                   f"<td style='padding:1px 10px 1px 0; color:{p.strong}'>{value}</td>"
                   f"<td style='color:{colour}'>{_e(state)}</td></tr>")
    out.append("</table>")
    if record.positions:
        out.append(f"<p style='margin-bottom:2px'><b>{_e(t('analysis.record.positions'))}</b></p><ul>")
        for position in record.positions[:6]:
            text = " · ".join(str(position.get(k) or "") for k in ("title", "employer", "dates") if position.get(k))
            out.append(f"<li>{_e(text)}</li>")
        out.append("</ul>")
    if record.skills:
        out.append(f"<p><b>{_e(t('analysis.record.skills'))}</b> {_e(', '.join(record.skills[:20]))}</p>")
    if record.text_lines:
        lines = "<br>".join(_e(line) for line in record.text_lines[:8])
        out.append(f"<p style='margin-bottom:2px'><b>{_e(t('analysis.record.text'))}</b></p>"
                   f"<p style='color:{p.muted}; font-family:monospace'>{lines}</p>")
    return "".join(out)


def score_html(result, p: Palette) -> str:
    score = result.score
    band = message_text(_msg(f"rsim.score.band.{score.band}"))
    colour = {"top": p.accent, "middle": p.warning}.get(score.band, p.error)
    out = [f"<h3 style='color:{p.accent}'>{_e(t('analysis.score.title'))}</h3>",
           f"<p><span style='font-size:large; color:{colour}'>"
           f"<b>{_e(t('analysis.score.percent', score=score_percent(score)))}</b></span><br>"
           f"<span style='color:{p.muted}'>{_e(scale_note(score))}</span><br>"
           f"<span style='color:{colour}'>{_e(band)}</span></p><ul>"]
    for line in score.explanation:
        out.append(f"<li>{_e(message_text(line))}</li>")
    out.append("</ul>")
    out.append(f"<p style='color:{p.muted}'><i>{_e(t('analysis.score.estimate'))}</i></p>")
    return "".join(out)


def fixes_html(result, p: Palette, edits) -> str:
    out = [f"<h3 style='color:{p.accent}'>{_e(t('analysis.fixes.title'))}</h3>"]
    if not edits:
        out.append(f"<p style='color:{p.muted}'>{_e(t('analysis.fixes.none'))}</p>")
        return "".join(out)
    out.append("<ol>")
    for edit in edits:
        points = t("analysis.fixes.points", points=_plain(round(edit.points, 1)))
        extra = f" <span style='color:{p.muted}'>({_e(t('analysis.fixes.conditional'))})</span>" \
            if edit.conditional else ""
        out.append(f"<li><span style='color:{p.accent}'><b>{_e(points)}</b></span> "
                   f"{_e(message_text(edit.message))}{extra}</li>")
    out.append("</ol>")
    try:
        from ..recruiter_sim import score_after

        after = score_after(result, list(edits))
        out.append(f"<p style='color:{p.muted}'>{_e(t('analysis.fixes.after', score=after))}</p>")
    except Exception:  # noqa: BLE001 - the total is a convenience
        log.debug("score_after failed", exc_info=True)
    return "".join(out)


def match_html(match, p: Palette) -> str:
    out = [f"<h3 style='color:{p.accent}'>{_e(t('analysis.match.title'))}</h3>"]
    if match is None:
        out.append(f"<p style='color:{p.muted}'>{_e(t('analysis.match.none'))}</p>")
        return "".join(out)
    out.append(f"<p><b>{_e(t('analysis.match.score', score=round(match.score)))}</b> &nbsp;"
               f"{_e(t('match.common', n=match.common, m=match.total))}</p>")
    for summary in match.summary()[1:]:
        out.append(f"<p style='color:{p.muted}; margin:0'>{_e(message_text(summary))}</p>")

    def items(key: str, values: list[str], colour: str) -> None:
        if values:
            out.append(f"<p style='margin:4px 0 0 0'><span style='color:{colour}'><b>{_e(t(key))}</b></span> "
                       f"{_e(', '.join(values))}</p>")

    items("analysis.match.in_common", [i.text for i in match.in_common], p.accent)
    items("analysis.match.partial", [i.text for i in match.partial], p.warning)
    items("analysis.match.missing_must", [i.text for i in match.missing_must], p.error)
    items("analysis.match.missing_nice", [i.text for i in match.missing_nice], p.warning)
    items("analysis.match.not_stated", [i.text for i in match.uncertain], p.muted)
    items("analysis.match.extra", [s.name for s in match.extra_strengths][:12], p.muted)
    return "".join(out)


def result_html(result, p: Palette, edits=None) -> str:
    """The whole single-system view (pure function, tested)."""
    from ..recruiter_sim import what_if

    edits = list(edits) if edits is not None else what_if(result, TOP_FIXES)
    out = [f"<h2 style='margin-bottom:2px'>{_e(system_name(result.profile_key, result.profile_name))}</h2>",
           f"<p style='color:{p.warning}'><i>{_e(message_text(result.approximation))}</i></p>"]
    if result.degraded:
        out.append(f"<p style='color:{p.muted}'>{_e(message_text(_msg('rsim.degraded.semantic')))}</p>")
    out.append(score_html(result, p))
    out.append(fixes_html(result, p, edits))
    out.append(match_html(result.match, p))
    out.append(f"<h3 style='color:{p.accent}'>{_e(t('analysis.pipeline.title'))}</h3>")
    out.append(pipeline_html(result, p))
    out.append(record_html(result, p))
    return "".join(out)


def score_percent(score) -> int:
    """The simulated score as 0..100 % (whatever scale the system shows)."""
    from ..recruiter_sim.percent import score_percent as percent

    return percent(score)


def scale_note(score) -> str:
    """"This system shows the recruiter grades A–D: grade B." (the secondary note)."""
    from ..recruiter_sim.percent import scale_kind

    return t(f"analysis.scale.{scale_kind(score)}", display=short_display(score.display))


def scale_help(score) -> str:
    """Plain words on the scale this system uses ("this system uses letters A–D"), the tooltip of
    the "How this system shows you" cells."""
    from ..recruiter_sim.percent import scale_kind

    return t(f"analysis.scale_help.{scale_kind(score)}")


def short_display(display) -> str:
    """The score on the system's own scale, short enough for a table cell."""
    if display is not None and display.key == "rsim.score.display.none":
        return t("analysis.compare.no_score", met=display.params.get("met", 0), total=display.params.get("total", 0))
    return message_text(display)


def losses_text(result) -> tuple[str, str]:
    """(cell text, tooltip) of the "parse losses" column: record fields the system would
    lose or garble, plus the parse check's own problems."""
    lost = [f for f in result.record.fields if f.status in ("lost", "garbled", "missing")]
    names = [message_text(_msg(f"rsim.record.field.{f.name}")) for f in lost]
    problems = [message_text(f.what_we_found) for f in result.by_check("parse") if f.status in ("warn", "fail")]
    if not lost and not problems:
        return t("analysis.losses.none"), ""
    text = t("analysis.losses.count", n=len(lost) + len(problems))
    return text, "\n".join(names + problems)


def banner_html(profile, p: Palette) -> str:
    """The fixed banner: approximation, confidence of the profile and its sources."""
    if profile is None:
        return f"<span style='color:{p.warning}'><b>{_e(t('analysis.banner.all'))}</b></span>"
    confidence = message_text(_msg(f"rsim.confidence.{profile.confidence}"))
    links = []
    for source in profile.sources:
        if source.url:
            links.append(f"<a href='{_e(source.url)}' style='color:{p.accent}'>{_e(source.title)}</a>")
        else:
            links.append(_e(source.title))
    text = f"<span style='color:{p.warning}'><b>{_e(t('analysis.banner', confidence=confidence))}</b></span>"
    note = message_text(profile.confidence_note)
    sources = _e(t("analysis.banner.sources")) + " " + ("; ".join(links) if links else "–")
    return f"{text}<br><span style='color:{p.muted}'>{_e(note)}</span><br>{sources}"


# --- the panel -----------------------------------------------------------------------------


class RecruiterAnalysisPanel(QWidget):
    """See the module docstring. Providers are callables set by the main window."""

    def __init__(self, run_in_background: Callable, *, opener=None, parent: QWidget | None = None):
        super().__init__(parent)
        self._run = run_in_background
        self._opener = opener
        self._palette: Palette = active_palette()
        self.cv_provider: Callable[[], object] = lambda: None  # -> recruiter_sim.CvDocument | None
        self.ai_estimate_provider: Callable[[], object] = lambda: None
        self.postings_provider: Callable[[], list] = lambda: []  # -> [(label, posting)]
        self.companies_provider: Callable[[], list] = lambda: []  # -> [CompanyCard]
        self.answers_provider: Callable[[], object] = lambda: None  # -> KnockoutAnswers
        self.embedder_provider: Callable[[], object] = lambda: None
        self.on_progress: Callable[[int, int, str], None] = lambda done, total, text: None
        self.on_finished: Callable[[str], None] = lambda text: None
        self.on_open_answers: Callable[[], None] = lambda: None
        self._postings: list = []
        self._cards: list = []
        self._cancel: threading.Event | None = None
        self._running = False
        self._stale = False
        self.outcome: AnalysisOutcome | None = None
        self.shown_key: str | None = None
        self._bridge = _Bridge()
        self._bridge.progress.connect(lambda done, total, text: self.on_progress(done, total, text))

        self.title_label = QLabel()
        self.title_label.setProperty("role", "title")
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setTextFormat(Qt.TextFormat.RichText)
        self.banner.setProperty("role", "warning")
        self.banner.setOpenExternalLinks(False)
        self.banner.linkActivated.connect(lambda url: open_url(url, self._opener) if self._opener else open_url(url))

        # --- target
        self.target_box = QGroupBox()
        self.role_radio = QRadioButton()
        self.posting_radio = QRadioButton()
        self.company_radio = QRadioButton()
        self.mode_group = QButtonGroup(self)
        for radio in (self.role_radio, self.posting_radio, self.company_radio):
            self.mode_group.addButton(radio)
            radio.toggled.connect(lambda _on: self._mode_changed())
        self.role_radio.setChecked(True)
        self.role_field = QLineEdit()
        self.skills_field = QLineEdit()
        self.role_field.textChanged.connect(lambda _t: self._mark_stale())
        self.skills_field.textChanged.connect(lambda _t: self._mark_stale())
        self.posting_combo = QComboBox()
        self.posting_combo.setMinimumContentsLength(18)
        self.posting_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.posting_combo.currentIndexChanged.connect(lambda _i: self._target_changed())
        self.company_combo = QComboBox()
        self.company_combo.setMinimumContentsLength(18)
        self.company_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.company_combo.currentIndexChanged.connect(lambda _i: self._target_changed())
        self.target_pages = QStackedWidget()
        role_page = QWidget()
        role_grid = QGridLayout(role_page)
        role_grid.setContentsMargins(0, 0, 0, 0)
        self.role_label = QLabel()
        self.skills_label = QLabel()
        role_grid.addWidget(self.role_label, 0, 0)
        role_grid.addWidget(self.role_field, 0, 1)
        role_grid.addWidget(self.skills_label, 1, 0)
        role_grid.addWidget(self.skills_field, 1, 1)
        role_grid.setColumnStretch(1, 1)
        posting_page = QWidget()
        posting_layout = QVBoxLayout(posting_page)
        posting_layout.setContentsMargins(0, 0, 0, 0)
        posting_layout.addWidget(self.posting_combo)
        self.posting_hint = wrapped_label(role="muted")
        posting_layout.addWidget(self.posting_hint)
        company_page = QWidget()
        company_layout = QVBoxLayout(company_page)
        company_layout.setContentsMargins(0, 0, 0, 0)
        company_layout.addWidget(self.company_combo)
        self.company_hint = wrapped_label(role="muted")
        company_layout.addWidget(self.company_hint)
        for page in (role_page, posting_page, company_page):
            self.target_pages.addWidget(page)
        modes = QHBoxLayout()
        modes.setContentsMargins(0, 0, 0, 0)
        for radio in (self.role_radio, self.posting_radio, self.company_radio):
            modes.addWidget(radio)
        modes.addStretch(1)
        target_layout = QVBoxLayout(self.target_box)
        target_layout.addLayout(modes)
        target_layout.addWidget(self.target_pages)

        # --- system and actions
        self.system_label = QLabel()
        self.system_combo = QComboBox()
        self.system_combo.setMinimumContentsLength(16)
        self.system_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.system_combo.currentIndexChanged.connect(lambda _i: self._system_changed())
        self.run_button = QPushButton()
        self.run_button.setProperty("role", "accent")
        self.run_button.clicked.connect(lambda: self.run())
        self.cancel_button = QPushButton()
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(lambda: self.cancel())
        self.answers_button = QPushButton()
        self.answers_button.clicked.connect(lambda: self.on_open_answers())
        system_row = QHBoxLayout()
        system_row.setContentsMargins(0, 0, 0, 0)
        system_row.addWidget(self.system_label)
        system_row.addWidget(self.system_combo, 1)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.addWidget(self.run_button)
        actions.addWidget(self.cancel_button)
        actions.addStretch(1)
        actions.addWidget(self.answers_button)
        self.state_label = ElidedLabel()
        self.state_label.setProperty("role", "muted")

        # --- results
        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.anchorClicked.connect(self._anchor)
        self.table = QTableWidget(0, len(TABLE_COLUMNS))
        self.table.setObjectName("compareTable")
        table_filter.attach(self.table, kinds=table_filter.named_kinds(TABLE_COLUMNS, score="number"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(False)  # never squeeze the last header below its text
        for i in range(len(TABLE_COLUMNS)):
            self.table.setHorizontalHeaderItem(i, QTableWidgetItem())
        self.table.cellDoubleClicked.connect(lambda row, _c: self.show_system_row(row))
        self.table_hint = wrapped_label(role="muted")
        # "?": both columns explained in plain words (a popup that never blocks the window)
        self.compare_help_button = QPushButton("?")
        self.compare_help_button.setObjectName("compareHelpButton")
        self.compare_help_button.setFixedWidth(self.fontMetrics().horizontalAdvance("?") * 4)
        self.compare_help_button.clicked.connect(lambda: self.show_compare_help())
        self.compare_help = None
        hint_row = QHBoxLayout()
        hint_row.setContentsMargins(0, 0, 0, 0)
        hint_row.addWidget(self.table_hint, 1)
        hint_row.addWidget(self.compare_help_button, 0, Qt.AlignmentFlag.AlignTop)
        compare_page = QWidget()
        compare_layout = QVBoxLayout(compare_page)
        compare_layout.setContentsMargins(0, 0, 0, 0)
        compare_layout.addLayout(hint_row)
        compare_layout.addWidget(self.table, 1)
        self.results = QStackedWidget()
        self.results.addWidget(self.view)
        self.results.addWidget(compare_page)

        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 4, 0)
        controls_layout.addWidget(self.target_box)
        controls_layout.addLayout(system_row)
        controls_layout.addLayout(actions)
        controls_layout.addWidget(self.state_label)
        controls_layout.addStretch(1)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        # the controls scroll on a small window instead of being squeezed
        self.controls_scroll = QScrollArea()
        self.controls_scroll.setWidgetResizable(True)
        self.controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.controls_scroll.setWidget(controls)
        self.controls_scroll.setMinimumWidth(controls.minimumSizeHint().width() + 16)
        self.splitter.addWidget(self.controls_scroll)
        self.splitter.addWidget(self.results)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 5)
        self.splitter.setSizes([360, 820])

        layout = QVBoxLayout(self)
        layout.addWidget(self.title_label)
        layout.addWidget(self.banner)
        layout.addWidget(self.splitter, 1)
        self._fill_systems()
        self.retranslate()

    # --- providers and lists

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self._render()

    def refresh_sources(self) -> None:
        """Reloads the postings and companies offered as targets (keeps the choice)."""
        current_posting = self.posting_combo.currentData()
        current_card = self.company_combo.currentData()
        try:
            self._postings = list(self.postings_provider() or [])
        except Exception:  # noqa: BLE001
            log.warning("Postings for the recruiter analysis not read", exc_info=True)
            self._postings = []
        try:
            self._cards = list(self.companies_provider() or [])
        except Exception:  # noqa: BLE001
            log.warning("Companies for the recruiter analysis not read", exc_info=True)
            self._cards = []
        self.posting_combo.blockSignals(True)
        self.posting_combo.clear()
        for index, (label, _posting) in enumerate(self._postings):
            self.posting_combo.addItem(label, index)
        if not self._postings:
            self.posting_combo.addItem(t("analysis.target.no_postings"), None)
        if current_posting is not None and current_posting < len(self._postings):
            self.posting_combo.setCurrentIndex(current_posting)
        self.posting_combo.blockSignals(False)
        self.company_combo.blockSignals(True)
        self.company_combo.clear()
        for card in self._cards:
            self.company_combo.addItem(card.name, card.id)
        if not self._cards:
            self.company_combo.addItem(t("analysis.target.no_companies"), None)
        index = self.company_combo.findData(current_card)
        if index >= 0:
            self.company_combo.setCurrentIndex(index)
        self.company_combo.blockSignals(False)
        self._update_hints()

    def _fill_systems(self) -> None:
        from ..recruiter_sim import load_profiles

        current = self.system_combo.currentData()
        self.system_combo.blockSignals(True)
        self.system_combo.clear()
        detected = self.detected()
        if detected[0]:
            name = load_profiles()[detected[0]].name
            self.system_combo.addItem(t("analysis.system.detected", name=name), SYSTEM_DETECTED)
        for key, profile in load_profiles().items():
            self.system_combo.addItem(system_name(key, profile.name), key)
        self.system_combo.addItem(t("analysis.system.all"), SYSTEM_ALL)
        index = self.system_combo.findData(current)
        self.system_combo.setCurrentIndex(index if index >= 0 else 0)
        self.system_combo.blockSignals(False)
        self._update_banner()

    # --- target

    def mode(self) -> str:
        if self.posting_radio.isChecked():
            return MODE_POSTING
        if self.company_radio.isChecked():
            return MODE_COMPANY
        return MODE_ROLE

    def _mode_changed(self) -> None:
        if not hasattr(self, "system_combo"):
            return  # still being built
        self.target_pages.setCurrentIndex({MODE_ROLE: 0, MODE_POSTING: 1, MODE_COMPANY: 2}[self.mode()])
        self._target_changed()

    def _target_changed(self) -> None:
        self._update_hints()
        self._fill_systems()
        self._mark_stale()

    def selected_posting(self):
        index = self.posting_combo.currentData()
        if index is None or not (0 <= index < len(self._postings)):
            return None
        return self._postings[index][1]

    def selected_card(self):
        card_id = self.company_combo.currentData()
        return next((c for c in self._cards if c.id == card_id), None)

    def detected(self) -> tuple[str | None, str]:
        mode = self.mode()
        if mode == MODE_POSTING:
            return detected_for_posting(self.selected_posting())
        if mode == MODE_COMPANY:
            return detected_for_card(self.selected_card())
        return None, ""

    def _update_hints(self) -> None:
        key, where = self.detected()
        from ..recruiter_sim import load_profiles

        name = load_profiles()[key].name if key else ""
        text = t("analysis.target.detected", name=name, where=where or "–") if key else \
            t("analysis.target.not_detected")
        self.posting_hint.setText(text if self.mode() == MODE_POSTING else "")
        card = self.selected_card()
        if self.mode() == MODE_COMPANY and card is not None:
            postings = getattr(card, "postings", []) or []
            usable = card_target_postings(card)
            if usable:
                extra = t("analysis.target.company_posting", title=usable[0].get("title", ""))
            elif postings:
                extra = t("analysis.target.company_untexted", title=postings[0].get("title", ""))
            else:
                extra = t("analysis.target.company_role")
            self.company_hint.setText(text + "\n" + extra)
        else:
            self.company_hint.setText("")

    def set_role(self, role: str, skills: list[str] | None = None) -> None:
        self.role_radio.setChecked(True)
        self.role_field.setText(role)
        if skills is not None:
            self.skills_field.setText(", ".join(skills))

    def select_posting(self, posting) -> bool:
        """Preselects a posting (e.g. "Simulate screening" in Job search)."""
        self.refresh_sources()
        for index, (_label, item) in enumerate(self._postings):
            if item is posting or _same_posting(item, posting):
                self.posting_combo.setCurrentIndex(index)
                self.posting_radio.setChecked(True)
                self._target_changed()
                return True
        self._postings.append((_posting_label(posting, t("analysis.target.from_selection")), posting))
        self.posting_combo.addItem(self._postings[-1][0], len(self._postings) - 1)
        self.posting_combo.setCurrentIndex(self.posting_combo.count() - 1)
        self.posting_radio.setChecked(True)
        self._target_changed()
        return True

    def select_company(self, card_id: int, system_key: str | None = None) -> bool:
        """Preselects a company and its detected (or the given) system."""
        self.refresh_sources()
        index = self.company_combo.findData(card_id)
        if index < 0:
            return False
        self.company_combo.setCurrentIndex(index)
        self.company_radio.setChecked(True)
        self._target_changed()
        if system_key:
            self.select_system(system_key)
        return True

    def select_system(self, key: str) -> bool:
        detected, _ = self.detected()
        if detected == key and self.system_combo.findData(SYSTEM_DETECTED) >= 0:
            self.system_combo.setCurrentIndex(self.system_combo.findData(SYSTEM_DETECTED))
            return True
        index = self.system_combo.findData(key)
        if index >= 0:
            self.system_combo.setCurrentIndex(index)
            return True
        return False

    def target_choice(self) -> TargetChoice | None:
        """The target as chosen now, or None when it is incomplete."""
        from ..recruiter_sim import Target

        mode = self.mode()
        key, where = self.detected()
        if mode == MODE_ROLE:
            role = self.role_field.text().strip()
            if not role:
                return None
            skills = [s.strip() for s in self.skills_field.text().split(",") if s.strip()]
            return TargetChoice(mode, role, Target.from_role(role, skills))
        if mode == MODE_POSTING:
            posting = self.selected_posting()
            if posting is None:
                return None
            return TargetChoice(mode, self.posting_combo.currentText(), _posting_target(posting), key, where)
        card = self.selected_card()
        if card is None:
            return None
        postings = card_target_postings(card)
        if postings:
            target = _posting_target(postings[0])
            label = f"{card.name}: {postings[0].get('title', '')}"
        else:
            cv = self.cv_provider()
            roles = [r.value for r in (getattr(getattr(cv, "profile", None), "roles", None) or [])]
            if not roles:
                return None
            target = Target.from_role(roles[0])
            label = f"{card.name}: {roles[0]}"
        return TargetChoice(mode, label, target, key, where)

    def system_key(self) -> str | None:
        data = self.system_combo.currentData()
        if data == SYSTEM_DETECTED:
            return self.detected()[0]
        return data

    def _system_changed(self) -> None:
        self._update_banner()
        key = self.system_key()
        if self.outcome is None:
            return
        if key == SYSTEM_ALL and self.outcome.system == SYSTEM_ALL:
            self.shown_key = None
            self._render()
        elif key in self.outcome.results:
            self.shown_key = key
            self._render(single=True)
        else:
            self._mark_stale()

    def _update_banner(self) -> None:
        from ..recruiter_sim import load_profiles

        key = self.system_key()
        profile = load_profiles().get(key) if key and key != SYSTEM_ALL else None
        if self.outcome is not None and self.results.currentIndex() == 0 and self.shown_key:
            profile = load_profiles().get(self.shown_key, profile)
        self.banner.setText(banner_html(profile, self._palette))

    # --- running

    @property
    def running(self) -> bool:
        return self._running

    def _mark_stale(self) -> None:
        if self.outcome is not None and not self._running:
            self._stale = True
            self.state_label.setText(t("analysis.state.stale"))

    def cv_changed(self) -> None:
        """A new CV was read: the shown analysis is of the previous one."""
        self._mark_stale()
        self._fill_systems()

    def run(self) -> bool:
        """Starts the analysis on a worker (False when something is missing)."""
        if self._running:
            return False
        cv = self.cv_provider()
        if cv is None:
            self.state_label.setText(t("analysis.state.no_cv"))
            return False
        choice = self.target_choice()
        if choice is None:
            self.state_label.setText(t("analysis.state.no_target"))
            return False
        system = self.system_key()
        if system is None:
            from ..recruiter_sim.profiles import BASELINE_KEY

            system = BASELINE_KEY
        from ..recruiter_sim import load_profiles
        from ..recruiter_sim.engine import STEPS

        STEPS_PER_PROFILE = len(STEPS)  # noqa: N806 - steps of one simulated system
        profiles = list(load_profiles().values()) if system == SYSTEM_ALL else [load_profiles()[system]]
        answers = self.answers_provider()
        ai_estimate = self.ai_estimate_provider()
        cancel = threading.Event()
        self._cancel = cancel
        self._running = True
        self._update_buttons()
        total = len(profiles) * STEPS_PER_PROFILE
        bridge = self._bridge
        embedder_provider = self.embedder_provider
        names = {p.key: p.name for p in profiles}
        order = [p.key for p in profiles]
        log.info("Recruiter analysis started: %s, %d system(s), target %s", system, len(profiles), choice.mode)
        self.state_label.setText(t("analysis.state.running"))
        bridge.progress.emit(0, total, t("analysis.progress", done=0, total=total, system=names[order[0]]))

        def work():
            from ..recruiter_sim import simulate
            from ..recruiter_sim.layout import analyse_layout
            from ..recruiter_sim.model import CvDocument

            embedder = None
            try:
                embedder = embedder_provider()
            except Exception:  # noqa: BLE001 - the simulators fall back to lexical matching
                log.info("No embedder for the recruiter analysis", exc_info=True)
            document = cv
            if document.layout is None:
                document = CvDocument(cv.profile, cv.read, cv.path, analyse_layout(cv.file_path))
            results = {}
            for position, profile in enumerate(profiles):
                def step(name, done, _total, _base=position * STEPS_PER_PROFILE, _name=profile.name):
                    current = min(total, _base + done)
                    bridge.progress.emit(current, total, t("analysis.progress", done=current, total=total,
                                                           system=_name))
                results[profile.key] = simulate(document, choice.target, profile, answers=answers, embedder=embedder,
                                                ai_estimate=ai_estimate, progress=step, cancel=cancel.is_set)
            bridge.progress.emit(total, total, t("analysis.progress", done=total, total=total, system=""))
            return results

        def done(results) -> None:
            self._running = False
            self._cancel = None
            self._stale = False
            self.outcome = AnalysisOutcome(choice, system, results)
            self.shown_key = order[0] if system != SYSTEM_ALL else None
            self._update_buttons()
            self._render()
            text = t("analysis.state.done", target=choice.label, count=len(results))
            self.state_label.setText(text)
            log.info("Recruiter analysis finished: %d system(s)", len(results))
            self.on_finished(text)

        def failed(error) -> None:
            self._running = False
            self._cancel = None
            self._update_buttons()
            from ..recruiter_sim import SimulationCancelled

            if isinstance(error, SimulationCancelled) or cancel.is_set():
                text = t("analysis.state.cancelled")
                log.info("Recruiter analysis cancelled")
            else:
                text = t("analysis.state.failed")
                log.warning("Recruiter analysis failed: %s", type(error).__name__)
            self.state_label.setText(text)
            self.on_finished(text)

        run_job(self._run, work, done, failed, name="recruiter analysis", timeout=ANALYSIS_TIMEOUT_S, cancel=cancel)
        return True

    def cancel(self) -> None:
        if self._cancel is not None:
            log.info("Recruiter analysis: cancel requested")
            self._cancel.set()
            self.state_label.setText(t("analysis.state.cancelling"))

    def _update_buttons(self) -> None:
        self.run_button.setEnabled(not self._running)
        self.cancel_button.setEnabled(self._running)

    # --- showing

    def show_system_row(self, row: int) -> None:
        """A row of the comparison table: that system's details."""
        if self.outcome is None:
            return
        keys = list(self.outcome.results)
        row = table_sort.source_row(self.table, row)
        if 0 <= row < len(keys):
            self.shown_key = keys[row]
            self.select_system(keys[row])
            self._render(single=True)

    def _render(self, single: bool | None = None) -> None:
        p = self._palette
        outcome = self.outcome
        if outcome is None:
            self.results.setCurrentIndex(0)
            self.view.setHtml(f"<p style='color:{p.muted}'>{_e(t('analysis.empty'))}</p>")
            self._update_banner()
            return
        compare = outcome.system == SYSTEM_ALL and not single and (
            self.system_combo.currentData() == SYSTEM_ALL or self.shown_key is None)
        if compare:
            self._fill_table()
            self.results.setCurrentIndex(1)
        else:
            key = self.shown_key or next(iter(outcome.results))
            result = outcome.results.get(key)
            position = self.view.verticalScrollBar().value()
            self.view.setHtml(result_html(result, p) if result is not None else "")
            self.view.verticalScrollBar().setValue(position)
            self.results.setCurrentIndex(0)
        self._update_banner()

    def _fill_table(self) -> None:
        from ..recruiter_sim import load_profiles

        profiles = load_profiles()
        p = self._palette
        results = list(self.outcome.results.values())
        self.table.setRowCount(len(results))
        for row, result in enumerate(results):
            profile = profiles.get(result.profile_key)
            losses, tip = losses_text(result)
            band = result.score.band
            percent = score_percent(result.score)
            values = [system_name(result.profile_key, result.profile_name), t("analysis.score.percent", score=percent),
                      short_display(result.score.display),
                      message_text(_msg(f"rsim.score.band.{band}")), losses,
                      message_text(_msg(f"rsim.confidence.{profile.confidence}")) if profile else ""]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setToolTip(value)
                if column == 1:  # always 0-100 %: numeric for sorting and the per-column filter
                    item.setData(Qt.ItemDataRole.UserRole, percent)
                    item.setData(table_filter.FILTER_ROLE, percent)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    item.setToolTip(scale_note(result.score) + "\n" + (
                        message_text(result.score.explanation[0]) if result.score.explanation else ""))
                if column == 2:  # the system's own presentation, as a secondary note
                    item.setToolTip(scale_help(result.score) + "\n" + scale_note(result.score))
                if column == 3:
                    from PySide6.QtGui import QBrush, QColor

                    colour = {"top": p.accent, "middle": p.warning}.get(band, p.error)
                    item.setForeground(QBrush(QColor(colour)))
                if column == 4 and tip:
                    item.setToolTip(tip)
                self.table.setItem(row, column, item)
        # long names and bands: capped (the user can widen them), the full text in the tooltip
        char = self.fontMetrics().averageCharWidth()
        header = self.table.horizontalHeader()
        for column, chars in ((0, 28), (2, 24), (3, 24)):
            # never narrower than the column's own title ("How this system shows you" must be readable)
            title = self.table.horizontalHeaderItem(column)
            title_width = header.fontMetrics().horizontalAdvance(title.text() if title else "") + char * 4
            limit = max(char * chars, title_width)
            if self.table.columnWidth(column) > limit or self.table.columnWidth(column) < title_width:
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
                self.table.setColumnWidth(column, limit if self.table.columnWidth(column) > limit else title_width)
        table_sort.resort(self.table)   # the order chosen with a header click stays
        for row in range(self.table.rowCount()):
            band = self.table.item(row, 3)
            if band is not None:
                band.setToolTip(band.text())

    def show_compare_help(self):
        """The "?" of the comparison: what the two score columns mean (non-modal)."""
        from PySide6.QtWidgets import QMessageBox

        if self.compare_help is None:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Information)
            box.setStandardButtons(QMessageBox.StandardButton.Ok)
            box.setModal(False)
            box.finished.connect(lambda _r: setattr(self, "compare_help", None))
            self.compare_help = box
        self.compare_help.setWindowTitle(t("analysis.compare.help_title"))
        self.compare_help.setText(t("analysis.compare.help_text"))
        self.compare_help.show()
        self.compare_help.raise_()
        log.info("Recruiter analysis: comparison help shown")
        return self.compare_help

    def shown_html(self) -> str:
        return self.view.toPlainText()

    def _anchor(self, url: QUrl) -> None:
        open_url(url.toString(), self._opener) if self._opener else open_url(url.toString())

    def retranslate(self) -> None:
        self.title_label.setText(t("analysis.title"))
        self.target_box.setTitle(t("analysis.target"))
        self.role_radio.setText(t("analysis.target.role"))
        self.posting_radio.setText(t("analysis.target.posting"))
        self.company_radio.setText(t("analysis.target.company"))
        self.role_label.setText(t("analysis.target.role_label"))
        self.skills_label.setText(t("analysis.target.skills_label"))
        self.role_field.setPlaceholderText(t("analysis.target.role_placeholder"))
        self.skills_field.setPlaceholderText(t("analysis.target.skills_placeholder"))
        self.system_label.setText(t("analysis.system"))
        self.run_button.setText(t("analysis.run"))
        self.cancel_button.setText(t("analysis.cancel"))
        self.answers_button.setText(t("analysis.answers"))
        self.answers_button.setToolTip(t("analysis.answers_tooltip"))
        self.table_hint.setText(t("analysis.compare.hint"))
        self.compare_help_button.setToolTip(t("analysis.compare.help_title"))
        self.compare_help_button.setAccessibleName(t("analysis.compare.help_title"))
        for i, column in enumerate(TABLE_COLUMNS):
            self.table.horizontalHeaderItem(i).setText(t(f"analysis.compare.{column}"))
            tip = t(f"analysis.compare.tip.{column}")
            self.table.horizontalHeaderItem(i).setToolTip("" if tip == f"analysis.compare.tip.{column}" else tip)
        if self.compare_help is not None:
            self.compare_help.setWindowTitle(t("analysis.compare.help_title"))
            self.compare_help.setText(t("analysis.compare.help_text"))
        self.refresh_sources()
        self._fill_systems()
        if not self.state_label.text():
            self.state_label.setText(t("analysis.state.idle"))
        self._render()
        self._fit_controls()

    def _fit_controls(self) -> None:
        """The controls column is never narrower than its content: measured after the
        texts are set (at construction they are empty), so nothing is clipped on the left
        when the scroll area keeps a focused field in view."""
        controls = self.controls_scroll.widget()
        if controls is None:
            return
        controls.adjustSize()
        bar = self.controls_scroll.verticalScrollBar().sizeHint().width()
        self.controls_scroll.setMinimumWidth(controls.minimumSizeHint().width() + bar + 4)
        self.controls_scroll.horizontalScrollBar().setValue(0)


def card_target_postings(card) -> list:
    """The card's postings usable as a screening target: those with a text to compare. A
    title alone ("SALES", from a careers-page link) would make the simulators suggest the
    title as a headline: the CV's own role is then the target."""
    return [p for p in (getattr(card, "postings", []) or [])
            if isinstance(p, dict) and str(p.get("description") or "").strip()]


def _posting_target(posting):
    from ..recruiter_sim import Target

    if isinstance(posting, dict):
        return Target(title=str(posting.get("title") or ""), description=str(posting.get("description") or ""),
                      location=str(posting.get("location") or ""), kind="posting")
    return Target.from_posting(posting)


def _same_posting(a, b) -> bool:
    from .cv_match import posting_identity

    return posting_identity(a) == posting_identity(b)


def _posting_label(posting, origin: str) -> str:
    get = posting.get if isinstance(posting, dict) else (lambda name, default="": getattr(posting, name, default))
    who = get("company", "") or get("agency", "") or ""
    title = get("title", "") or "?"
    return f"{origin}: {title}" + (f" ({who})" if who else "")
