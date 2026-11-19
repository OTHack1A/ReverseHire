"""Card of the selected company: scores, reasons with their source, gaps, job ads."""

from __future__ import annotations

import html
import logging
import re
import webbrowser

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QTextBrowser, QVBoxLayout, QWidget,
)

from .. import display_text
from ..i18n import t
from ..models import Company, Assessment
from . import table_filter, table_sort
from .common import PERMANENT_CONTRACTS, enum_label, format_number, is_openable_url, open_url
from .results_table import fit_first_column, is_blocked, score_of, status_of, status_text, watch_resize
from .theme import Palette, active_palette

log = logging.getLogger(__name__)

AD_COLUMNS = ("title", "location", "contract", "remote", "salary", "travel", "match")


def posting_terms(posting) -> str:
    """"PERMANENT · Hybrid · Salary: ... · Travel: occasional" (only what is stated)."""
    parts = []
    contract = getattr(posting, "contract", None)
    if contract:
        label = enum_label("contract", contract)
        parts.append(label.upper() if contract in PERMANENT_CONTRACTS else label)
    if getattr(posting, "remote", None):
        parts.append(enum_label("remote", posting.remote))
    if getattr(posting, "salary", None):
        parts.append(t("detail.salary", salary=display_text.display(posting.salary)))
    if getattr(posting, "travel", None):
        parts.append(t("detail.travel", travel=enum_label("travel", posting.travel)))
    return " · ".join(parts)

_URL = re.compile(r"https?://[^\s<>\"']+")


def _shown(text: str, show_original: bool) -> list[str]:
    """`text` through the display layer; with `show_original` the source wording
    follows a translated text on its own line."""
    shown = display_text.show(text)
    lines = [shown.text]
    if show_original and shown.original is not None:
        lines.append("    " + t("display.original", text=shown.original))
    return lines


def detail_text(company: Company, evaluation: Assessment | None, show_original: bool = False) -> str:
    """Readable text of the card (also used by the tests). Texts taken from the
    websites, the postings and the CV go through the display layer
    (`display_text`): English with an English interface."""
    D = display_text.display  # noqa: N806
    lines = [company.name]
    if company.address:
        lines.append(company.address)
    if company.distance_km is not None:
        lines.append(t("detail.distance", km=format_number(company.distance_km, 2)))
    lines.append(t("detail.origin", origin=t(f"origin.{company.origin}")))
    lines.append(t("detail.site", site=company.site or t("detail.no_site")))
    lines.append("")
    lines.append(t("detail.status", status=status_text(status_of(evaluation))))
    if evaluation is None:
        lines.append(t("detail.not_assessed"))
        return "\n".join(lines)
    match = score_of(evaluation, "match")
    desirability = score_of(evaluation, "desirability")
    if match is not None or desirability is not None:
        lines.append(t("detail.match", match=format_number(match) if match is not None else "-",
                       desirability=format_number(desirability) if desirability is not None else "-"))
    lines.append(t("detail.scores",
                   relevance=format_number(evaluation.relevance) if evaluation.relevance is not None else "-",
                   fit=format_number(evaluation.fit) if evaluation.fit is not None else "-",
                   confidence=format_number(evaluation.confidence * 100)))
    if is_blocked(evaluation):
        reason = getattr(evaluation, "blocked_reason", None)
        lines.append(t("detail.blocked_reason", reason=D(reason)) if reason else t("detail.blocked"))
    spontaneous = score_of(evaluation, "spontaneous")
    if not evaluation.postings and spontaneous is not None:
        lines.append(t("detail.spontaneous", score=format_number(spontaneous)))
    if evaluation.careers_url:
        lines.append(t("detail.careers", url=evaluation.careers_url))
    if evaluation.postings:
        lines.append("")
        lines.append(t("detail.best_posting", title=D(evaluation.postings[0].title)))
        terms = posting_terms(evaluation.postings[0])
        if terms:
            lines.append("  " + terms)
        for posting in evaluation.postings[1:]:
            if getattr(posting, "salary", None):
                lines.append("  " + t("detail.salary_of", title=D(posting.title), salary=D(posting.salary)))
        if not any(getattr(p, "salary", None) for p in evaluation.postings):
            lines.append("  " + t("detail.no_salary"))
    lines.append("")
    lines.append(t("detail.reasons"))
    if evaluation.reasons:
        for reason in evaluation.reasons:
            first, *rest = _shown(reason.text, show_original)
            lines.append(f"  • {first}")
            lines.extend(rest)
            if reason.source:
                lines.append(f"      {t('detail.source', source=reason.source)}")
    else:
        lines.append("  " + t("detail.none"))
    lines.append("")
    lines.append(t("detail.gaps"))
    if evaluation.gaps:
        for gap in evaluation.gaps:
            first, *rest = _shown(gap, show_original)
            lines.append(f"  • {first}")
            lines.extend(rest)
    else:
        lines.append("  " + t("detail.none"))
    return "\n".join(lines)


def detail_html(text: str, accent: str) -> str:
    """The card as HTML: same text, first line in bold, http(s) addresses clickable."""
    rows = []
    for i, line in enumerate(text.split("\n")):
        indent = len(line) - len(line.lstrip(" "))
        body = line.lstrip(" ")
        parts, last = [], 0
        for match in _URL.finditer(body):
            parts.append(html.escape(body[last:match.start()]))
            url = match.group(0).rstrip(".,;)")
            rest = match.group(0)[len(url):]
            parts.append(f'<a href="{html.escape(url, quote=True)}" style="color:{accent}">{html.escape(url)}</a>'
                         + html.escape(rest))
            last = match.end()
        parts.append(html.escape(body[last:]))
        content = "&nbsp;" * indent + "".join(parts)
        if i == 0:
            content = f"<b>{content}</b>"
        rows.append(content or "&nbsp;")
    return "<div>" + "<br>".join(rows) + "</div>"


class DetailPanel(QWidget):
    def __init__(self, opener=webbrowser.open, parent: QWidget | None = None):
        super().__init__(parent)
        self._opener = opener
        self._row: tuple[Company, Assessment | None] | None = None
        self._accent = "#39D98A"
        self._palette: Palette = active_palette()

        # Texts translated into English: the original wording on demand.
        self.original_box = QCheckBox()
        self.original_box.toggled.connect(lambda _on: self.show_row(self._row))
        self.site_button = QPushButton()
        self.careers_button = QPushButton()
        self.linkedin_button = QPushButton()
        # "Write e-mail…": the main window sets `on_mail` (opens the mail dialog for this row)
        self.mail_button = QPushButton()
        self.on_mail = lambda: None
        self.mail_button.clicked.connect(lambda: self.on_mail())
        self.site_button.clicked.connect(self.open_site)
        self.careers_button.clicked.connect(self.open_careers)
        self.linkedin_button.clicked.connect(self.open_linkedin)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self.site_button)
        buttons.addWidget(self.careers_button)
        buttons.addWidget(self.linkedin_button)
        buttons.addWidget(self.mail_button)
        buttons.addStretch(1)
        buttons.addWidget(self.original_box)

        self.text = QTextBrowser()
        self.text.setOpenLinks(False)
        self.text.anchorClicked.connect(self._link_clicked)

        self.ads_label = QLabel()
        self.ads_label.setProperty("role", "title")
        self.ads = QTableWidget(0, len(AD_COLUMNS))
        self.ads.setObjectName("detailAdsTable")
        table_filter.attach(self.ads, kinds=table_filter.named_kinds(AD_COLUMNS))
        self.ads.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.ads.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.ads.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.ads.verticalHeader().setVisible(False)
        self.ads.setShowGrid(False)
        self.ads.setWordWrap(False)
        self.ads.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        header = self.ads.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        for i in range(len(AD_COLUMNS)):
            self.ads.setHorizontalHeaderItem(i, QTableWidgetItem())
        watch_resize(self.ads, lambda: fit_first_column(self.ads, 0, 18))
        self.ads.itemSelectionChanged.connect(self._update_buttons)
        self.ads.cellDoubleClicked.connect(lambda *_: self.open_ad())
        self.ad_button = QPushButton()
        self.ad_button.clicked.connect(self.open_ad)
        # "Simulate screening": the main window sets `on_simulate(posting)` (Recruiter analysis tab)
        self.simulate_button = QPushButton()
        self.on_simulate = lambda _posting: None
        self.simulate_button.clicked.connect(lambda: self.simulate_ad())
        self.match_service = None  # gui.cv_match.MatchService, set by the main window
        ad_row = QHBoxLayout()
        ad_row.setContentsMargins(0, 0, 0, 0)
        ad_row.addWidget(self.ad_button)
        ad_row.addWidget(self.simulate_button)
        ad_row.addStretch(1)

        # the company's links (website, social networks, searches); `saved_card(company)` is set
        # by the main window: the saved card's links when the company is in "My companies"
        from .social_links import SocialLinksBar

        self.social_bar = SocialLinksBar(self, opener=opener)
        self.saved_card = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(buttons)
        layout.addWidget(self.social_bar)
        layout.addWidget(self.text, 3)
        layout.addWidget(self.ads_label)
        layout.addWidget(self.ads, 2)
        layout.addLayout(ad_row)
        self.show_row(None)

    def set_accent(self, colour: str) -> None:
        self._accent = colour
        self.show_row(self._row)

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.social_bar.apply_palette(palette)
        self.set_accent(palette.accent)

    def _update_links(self) -> None:
        company = self._row[0] if self._row else None
        card = None
        if company is not None and self.saved_card is not None:
            try:
                card = self.saved_card(company)
            except Exception:  # noqa: BLE001 - the result's own links are shown instead
                log.warning("Saved card not read for the links", exc_info=True)
        if card is not None:
            self.social_bar.set_card(card)
        else:
            self.social_bar.set_company(company)
        self.social_bar.setVisible(company is not None)

    def _ad_cells(self, ad) -> list[QTableWidgetItem]:
        """Cells of one posting: remote/hybrid in the remote colour, permanent in bold."""
        p = self._palette
        title = display_text.show(ad.title)
        salary = display_text.show(getattr(ad, "salary", None) or "")
        match = self._match_cell(ad)
        values = [title.text, ad.location or "", enum_label("contract", getattr(ad, "contract", None)),
                  enum_label("remote", getattr(ad, "remote", None)), salary.text,
                  enum_label("travel", getattr(ad, "travel", None)), match[0]]
        originals = {"title": title.original, "salary": salary.original}
        cells = []
        remote = getattr(ad, "remote", None) in ("remote", "hybrid")
        for column, value in zip(AD_COLUMNS, values):
            item = QTableWidgetItem(value)
            if column == "match":
                from .table_filter import FILTER_ROLE, parse_number

                item.setData(FILTER_ROLE, parse_number(match[0]))  # 0-100 % (None: no value)
                item.setToolTip(match[1])
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if match[2]:
                    item.setForeground(QBrush(QColor(match[2])))
            if column in ("title", "salary") and value:
                original = originals.get(column)
                item.setToolTip(value + ("\n" + t("display.original", text=original) if original else ""))
            if column == "travel" and getattr(ad, "travel_note", None):
                note = display_text.show(ad.travel_note)
                item.setToolTip(note.text + ("\n" + t("display.original", text=note.original)
                                             if note.original else ""))
            bold = False
            if remote and column in ("title", "remote"):
                item.setBackground(QBrush(QColor(p.remote_bg)))
                item.setForeground(QBrush(QColor(p.remote_fg)))
                bold = True
            if column == "contract" and getattr(ad, "contract", None) in PERMANENT_CONTRACTS:
                item.setForeground(QBrush(QColor(p.accent)))
                bold = True
            if bold:
                font = QFont(self.ads.font())
                font.setBold(True)
                item.setFont(font)
            cells.append(item)
        return cells

    def _match_cell(self, ad) -> tuple[str, str, str]:
        """(text, tooltip, colour) of "Match with my CV" for one posting."""
        from .cv_match import PENDING, badge_text, tooltip_text

        service = self.match_service
        if service is None or not service.has_cv():
            return "–", t("match.no_cv"), ""
        result = service.cached(ad)
        if result is None:
            return ("–", t("match.not_computable"), "") if service.is_known(ad) else (PENDING, t("match.computing"), "")
        p = self._palette
        colour = p.accent if result.score >= 60 else (p.warning if result.score >= 35 else p.error)
        return badge_text(result), tooltip_text(result), colour

    def refresh_matches(self) -> None:
        """Redraws the postings with the match values computed meanwhile."""
        if self._row is not None:
            selected = table_sort.source_row(self.ads, self.ads.currentRow())
            self.show_row(self._row)
            row = table_sort.view_row(self.ads, selected) if selected is not None and selected >= 0 else None
            if row is not None and 0 <= row < self.ads.rowCount():
                self.ads.selectRow(row)

    def show_row(self, row) -> None:
        self._row = row
        self.ads.setRowCount(0)
        if row is not None and row[1] is not None and self.match_service is not None:
            self.match_service.request(row[1].postings, self.refresh_matches)
        if row is None:
            self.text.setPlainText(t("detail.select"))
        else:
            company, evaluation = row
            self.text.setHtml(detail_html(detail_text(company, evaluation, self.original_box.isChecked()),
                                          self._accent))
            ads = evaluation.postings if evaluation else []
            self.ads.setRowCount(len(ads))
            for i, ad in enumerate(ads):
                for c, item in enumerate(self._ad_cells(ad)):
                    self.ads.setItem(i, c, item)
            table_sort.resort(self.ads)
        fit_first_column(self.ads, 0, 18)
        self._update_buttons()
        self._update_links()
        evaluation = row[1] if row else None
        self.original_box.setVisible(display_text.interface_is_english() and any(
            display_text.show(x).original is not None for x in display_text.assessment_texts(evaluation)))

    @property
    def row(self):
        """The (company, assessment) shown, or None."""
        return self._row

    def plain_text(self) -> str:
        return self.text.toPlainText()

    def _chosen_ad(self):
        if self._row is None or self._row[1] is None:
            return None
        indexes = self.ads.selectionModel().selectedRows()
        if not indexes:
            return None
        return self._row[1].postings[table_sort.source_row(self.ads, indexes[0].row())]

    def _update_buttons(self) -> None:
        company, evaluation = self._row if self._row else (None, None)
        self.site_button.setEnabled(bool(company and is_openable_url(company.site)))
        careers = evaluation.careers_url if evaluation else None
        self.careers_button.setEnabled(is_openable_url(careers))
        self.linkedin_button.setEnabled(bool(company and company.name.strip()))
        self.mail_button.setEnabled(bool(company and company.name.strip()))
        ad = self._chosen_ad()
        self.ad_button.setEnabled(ad is not None and is_openable_url(ad.url))
        self.simulate_button.setEnabled(ad is not None)

    def _link_clicked(self, url: QUrl) -> None:
        open_url(url.toString(), self._opener)

    def open_site(self) -> bool:
        return bool(self._row) and open_url(self._row[0].site, self._opener)

    def open_careers(self) -> bool:
        return bool(self._row and self._row[1]) and open_url(self._row[1].careers_url, self._opener)

    def open_linkedin(self) -> bool:
        """LinkedIn's company search for the selected company (browser only)."""
        from ..companies import linkedin_search_url

        return bool(self._row) and open_url(linkedin_search_url(self._row[0].name), self._opener)

    def open_ad(self) -> bool:
        ad = self._chosen_ad()
        return ad is not None and open_url(ad.url, self._opener)

    def simulate_ad(self) -> bool:
        ad = self._chosen_ad()
        if ad is None:
            return False
        self.on_simulate(ad)
        return True

    def retranslate(self) -> None:
        self.social_bar.retranslate()
        self.site_button.setText(t("detail.open_site"))
        self.careers_button.setText(t("detail.open_careers"))
        self.linkedin_button.setText(t("detail.open_linkedin"))
        self.linkedin_button.setToolTip(t("detail.open_linkedin_tooltip"))
        self.mail_button.setText(t("mail.write"))
        self.mail_button.setToolTip(t("mail.write_tooltip"))
        self.ad_button.setText(t("detail.open_posting"))
        self.simulate_button.setText(t("jobs.simulate"))
        self.simulate_button.setToolTip(t("analysis.simulate_tooltip"))
        self.ads_label.setText(t("detail.postings"))
        self.original_box.setText(t("display.show_original"))
        keys = {"title": "detail.posting_title", "location": "detail.location", "contract": "table.contract",
                "remote": "table.remote", "salary": "table.salary", "travel": "detail.travel_column",
                "match": "match.column"}
        for i, column in enumerate(AD_COLUMNS):
            self.ads.horizontalHeaderItem(i).setText(t(keys[column]))
        self.ads.horizontalHeaderItem(AD_COLUMNS.index("match")).setToolTip(t("match.column_tooltip"))
        self.show_row(self._row)
