"""Dialogs of the job search: "Who might be behind this ad?" (the employer hypotheses of
an agency ad that does not name its client) and "Paste posting / company page…" (text
copied by hand from a site the app must not read, parsed and reviewed before saving).

Both are non-blocking (`open()`/`show()`), every slow step runs on a worker.
"""

from __future__ import annotations

import html
import logging
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea,
    QSplitter, QTextBrowser, QVBoxLayout, QWidget,
)

from ..i18n import t
from ..jobsearch.texts import text as js_text
from .common import open_url
from .theme import Palette, active_palette
from .widgets import wrapped_label

log = logging.getLogger(__name__)

# Fields of the review form, in order (the paste parser may give others too).
JOB_FIELDS = ("title", "company", "location", "workplace_address", "posted", "workplace", "contract", "hours",
              "salary", "experience", "language", "benefits", "industry", "size", "applicants", "description")
COMPANY_FIELDS = ("company", "website", "industry", "size", "headquarters", "founded", "specialties", "phone",
                  "locations", "followers", "overview")
MULTILINE = ("description", "overview", "benefits", "specialties")
# Fields with fixed values: shown with their labels, stored as the codes the parser uses.
CHOICES = {"workplace": ("onsite", "hybrid", "remote"), "hours": ("full_time", "part_time"),
           "contract": ("permanent", "permanent_possible", "fixed_term", "internship", "apprenticeship",
                        "freelance", "temporary_agency")}


def _choice_label(name: str, code: str) -> str:
    from .common import enum_label

    if name == "workplace":
        return enum_label("remote", code)
    if name == "contract":
        return enum_label("contract", code)
    text = t(f"jobs.hours.{code}")
    return code.replace("_", " ") if text == f"jobs.hours.{code}" else text


def _e(value) -> str:
    return html.escape(str(value or ""))


# --- employer hypotheses -------------------------------------------------------------------


def employers_html(estimate, posting, p: Palette) -> str:
    """The hypotheses (at most 5) with their reason chips, the fixed "unverified
    hypothesis" label and the share of "another company not in the list" (always shown)."""
    out = [f"<p style='color:{p.warning}'><b>{_e(js_text(estimate.header_key))}</b></p>"]
    title = getattr(posting, "title", "") or ""
    agency = getattr(posting, "agency", "") or getattr(posting, "publisher", "") or ""
    out.append(f"<p>{_e(t('jobs.employers.ad', title=title, agency=agency or '–'))}</p>")
    if estimate.note_key:
        out.append(f"<p style='color:{p.muted}'>{_e(js_text(estimate.note_key))}</p>")
    for hypothesis in estimate.hypotheses:
        percent = f"{100 * hypothesis.probability:.0f}"
        chips = " ".join(f"<span style='background-color:{p.surface2}; color:{p.strong}'>&nbsp;{_e(c.text)}&nbsp;</span>"
                         for c in hypothesis.reasons)
        out.append(f"<p style='margin:8px 0 2px 0'><b style='color:{p.strong}'>{_e(hypothesis.name)}</b> "
                   f"<span style='color:{p.accent}'>{_e(percent)}%</span> · {_e(hypothesis.label)} "
                   f"<span style='color:{p.warning}'>[{_e(hypothesis.unverified)}]</span><br>{chips}</p>")
    other = js_text("jobsearch.employers.other", percent=f"{100 * estimate.other_share:.0f}")
    out.append(f"<p style='margin-top:10px; color:{p.strong}'><b>{_e(other)}</b></p>")
    out.append(f"<p style='color:{p.muted}'><i>{_e(t('jobs.employers.method', count=estimate.considered))}</i></p>")
    return "".join(out)


class EmployersDialog(QDialog):
    """Non-modal window with the estimate of `jobsearch.estimate_employers`."""

    def __init__(self, parent: QWidget | None, posting, estimate, *, palette: Palette | None = None,
                 on_create_card: Callable[[str], None] | None = None):
        super().__init__(parent)
        self.setWindowTitle(js_text("jobsearch.result.possible_employers"))
        self.posting = posting
        self.estimate = estimate
        self._palette = palette or active_palette()
        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)
        self.browser.setHtml(employers_html(estimate, posting, self._palette))
        self.close_button = QPushButton(t("common.close"))
        self.close_button.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.browser, 1)
        layout.addLayout(buttons)
        metrics = self.fontMetrics()
        self.resize(metrics.averageCharWidth() * 90, metrics.lineSpacing() * 28)

    def text(self) -> str:
        return self.browser.toPlainText()


# --- paste and review ----------------------------------------------------------------------


class PasteDialog(QDialog):
    """Paste a page copied by hand (or read the clipboard, HTML when present), read it with
    `jobsearch.parse_pasted` (no network, people's names dropped), review the fields
    (guesses highlighted) and save only on confirmation.

    `on_save_posting(posting)` and `on_save_company(fields)` are called by the two save
    buttons with the REVIEWED values."""

    def __init__(self, parent: QWidget | None, *, on_save_posting: Callable, on_save_company: Callable,
                 palette: Palette | None = None, clipboard=None):
        super().__init__(parent)
        self.setWindowTitle(js_text("jobsearch.paste.title"))
        self._on_save_posting = on_save_posting
        self._on_save_company = on_save_company
        self._palette = palette or active_palette()
        self._clipboard = clipboard
        self._html: str | None = None
        self.draft = None
        self.fields: dict[str, QWidget] = {}

        self.help_label = wrapped_label(js_text("jobsearch.paste.help"), "muted")
        self.paste_box = QPlainTextEdit()
        self.paste_box.setPlaceholderText(t("jobs.paste.placeholder"))
        self.paste_box.textChanged.connect(self._text_edited)
        self.url_label = QLabel(js_text("jobsearch.paste.url"))
        self.url_field = QLineEdit()
        self.url_field.setPlaceholderText("https://…")
        self.clipboard_button = QPushButton(t("jobs.paste.from_clipboard"))
        self.clipboard_button.clicked.connect(lambda: self.paste_clipboard())
        self.read_button = QPushButton(js_text("jobsearch.paste.parse"))
        self.read_button.setProperty("role", "accent")
        self.read_button.clicked.connect(lambda: self.read())
        url_row = QHBoxLayout()
        url_row.setContentsMargins(0, 0, 0, 0)
        url_row.addWidget(self.url_label)
        url_row.addWidget(self.url_field, 1)
        top_buttons = QHBoxLayout()
        top_buttons.setContentsMargins(0, 0, 0, 0)
        top_buttons.addWidget(self.clipboard_button)
        top_buttons.addWidget(self.read_button)
        top_buttons.addStretch(1)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 4, 0)
        left_layout.addWidget(self.help_label)
        left_layout.addWidget(self.paste_box, 1)
        left_layout.addLayout(url_row)
        left_layout.addLayout(top_buttons)

        self.review_title = QLabel(t("jobs.paste.review"))
        self.review_title.setProperty("role", "title")
        self.kind_label = wrapped_label(role="muted")
        self.note_label = wrapped_label(js_text("jobsearch.paste.review_note"), "warning")
        self.people_label = wrapped_label(role="muted")
        self.form_host = QWidget()
        self.form = QFormLayout(self.form_host)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.form_scroll = QScrollArea()
        self.form_scroll.setWidgetResizable(True)
        self.form_scroll.setWidget(self.form_host)
        self.legend_label = wrapped_label(t("jobs.paste.legend"), "muted")
        self.save_posting_button = QPushButton(js_text("jobsearch.paste.save_posting"))
        self.save_posting_button.setProperty("role", "accent")
        self.save_posting_button.clicked.connect(lambda: self.save_posting())
        self.save_company_button = QPushButton(js_text("jobsearch.paste.save_company"))
        self.save_company_button.clicked.connect(lambda: self.save_company())
        self.close_button = QPushButton(t("common.close"))
        self.close_button.clicked.connect(self.reject)
        self.result_label = wrapped_label(role="muted")
        save_row = QHBoxLayout()
        save_row.setContentsMargins(0, 0, 0, 0)
        save_row.addWidget(self.save_posting_button)
        save_row.addWidget(self.save_company_button)
        save_row.addStretch(1)
        save_row.addWidget(self.close_button)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 0, 0, 0)
        right_layout.addWidget(self.review_title)
        right_layout.addWidget(self.kind_label)
        right_layout.addWidget(self.note_label)
        right_layout.addWidget(self.people_label)
        right_layout.addWidget(self.form_scroll, 1)
        right_layout.addWidget(self.legend_label)
        right_layout.addWidget(self.result_label)
        right_layout.addLayout(save_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([420, 520])
        layout = QVBoxLayout(self)
        layout.addWidget(splitter, 1)
        self._update_buttons()
        metrics = self.fontMetrics()
        self.resize(metrics.averageCharWidth() * 140, metrics.lineSpacing() * 34)

    # --- input

    def _text_edited(self) -> None:
        # typed or pasted by hand: the clipboard HTML no longer describes this text
        if self._html is not None and not getattr(self, "_setting_text", False):
            self._html = None

    def paste_clipboard(self) -> bool:
        """Reads the clipboard: its HTML when present (better structure), else the text."""
        clipboard = self._clipboard or QApplication.clipboard()
        try:
            mime = clipboard.mimeData()
            text = mime.text() if mime is not None else ""
            markup = mime.html() if mime is not None and mime.hasHtml() else None
        except Exception:  # noqa: BLE001 - an odd clipboard owner
            log.warning("Clipboard not readable", exc_info=True)
            return False
        if not text and not markup:
            self.result_label.setText(js_text("jobsearch.paste.empty"))
            return False
        self._setting_text = True
        try:
            self.paste_box.setPlainText(text or "")
        finally:
            self._setting_text = False
        self._html = markup or None
        log.info("Paste window: clipboard read (%d characters, html %s)", len(text or ""), bool(markup))
        return True

    def set_input(self, text: str, html_text: str | None = None, url: str = "") -> None:
        self._setting_text = True
        try:
            self.paste_box.setPlainText(text)
        finally:
            self._setting_text = False
        self._html = html_text
        self.url_field.setText(url)

    def read(self) -> bool:
        """Parses the pasted text (local, fast) and fills the review form."""
        from ..jobsearch import parse_pasted

        text = self.paste_box.toPlainText()
        if not text.strip() and not self._html:
            self.result_label.setText(js_text("jobsearch.paste.empty"))
            return False
        self.draft = parse_pasted(text, html=self._html, hint_url=self.url_field.text().strip() or None)
        log.info("Paste window: %s read, %d fields, %d lines with people dropped", self.draft.kind,
                 len(self.draft.fields), self.draft.removed_people)
        self._fill_form()
        return True

    def _fill_form(self) -> None:
        draft = self.draft
        p = self._palette
        while self.form.rowCount():
            self.form.removeRow(0)
        self.fields = {}
        self.kind_label.setText(js_text(f"jobsearch.paste.kind.{draft.kind}"))
        self.people_label.setText(js_text("jobsearch.paste.removed_people", n=draft.removed_people)
                                  if draft.removed_people else "")
        names = COMPANY_FIELDS if draft.kind == "linkedin_company" else JOB_FIELDS
        extra = [n for n in draft.fields if n not in names and not n.endswith("_text") and n != "company_type"]
        for name in [*names, *extra]:
            item = draft.fields.get(name)
            value = item.value if item is not None else ""
            confidence = item.confidence if item is not None else "low"
            if name in MULTILINE:
                widget = QPlainTextEdit(value)
                widget.setMinimumHeight(self.fontMetrics().lineSpacing() * 5)
            elif name in CHOICES:
                widget = QComboBox()
                widget.addItem("–", "")
                for code in CHOICES[name]:
                    widget.addItem(_choice_label(name, code), code)
                index = widget.findData(value)
                if index < 0 and value:
                    widget.addItem(value, value)  # an unknown value is kept as it was read
                    index = widget.count() - 1
                widget.setCurrentIndex(max(0, index))
            else:
                widget = QLineEdit(value)
            label_text = js_text(f"jobsearch.paste.field.{name}")
            if label_text == f"jobsearch.paste.field.{name}":
                label_text = name.replace("_", " ")
            label = QLabel(label_text)
            if value and confidence != "high":
                colour = p.warning if confidence == "low" else p.strong
                widget.setStyleSheet(f"border: 1px solid {colour};")
                widget.setToolTip(js_text(f"jobsearch.paste.confidence.{confidence}"))
                label.setStyleSheet(f"color: {colour};")
                label.setToolTip(widget.toolTip())
            widget.setProperty("confidence", confidence if value else "")
            self.form.addRow(label, widget)
            self.fields[name] = widget
        self._update_buttons()

    def _update_buttons(self) -> None:
        has = self.draft is not None
        self.save_posting_button.setEnabled(has and self.draft.kind != "linkedin_company")
        self.save_company_button.setEnabled(has)

    def reviewed_values(self) -> dict[str, str]:
        values = {}
        for name, widget in self.fields.items():
            if isinstance(widget, QComboBox):
                value = str(widget.currentData() or "")
            else:
                value = widget.toPlainText() if isinstance(widget, QPlainTextEdit) else widget.text()
            values[name] = value.strip()
        return values

    def _apply_review(self) -> None:
        """The user's corrections go back into the draft (confirmed fields are "high")."""
        from ..jobsearch import DraftField

        for name, value in self.reviewed_values().items():
            if value:
                self.draft.fields[name] = DraftField(value, "high", None)
            else:
                self.draft.fields.pop(name, None)
        self.draft.url = self.url_field.text().strip()

    # --- saving (only on confirmation)

    def save_posting(self) -> bool:
        if self.draft is None:
            return False
        self._apply_review()
        posting = self.draft.to_posting()
        if not posting.title:
            self.result_label.setText(t("jobs.paste.need_title"))
            return False
        self._on_save_posting(posting)
        self.result_label.setText(t("jobs.paste.saved_posting", title=posting.title))
        return True

    def save_company(self) -> bool:
        if self.draft is None:
            return False
        self._apply_review()
        fields = self.draft.to_company_fields()
        if not fields.get("company"):
            self.result_label.setText(t("jobs.paste.need_company"))
            return False
        self._on_save_company(fields)
        self.result_label.setText(t("jobs.paste.saved_company", name=fields["company"]))
        return True


def open_in_browser(url: str, opener) -> bool:
    return open_url(url, opener) if opener is not None else open_url(url)
