"""Company card view: the sections in the owner's order (header, registry
data, profiles sought, products, selection system, CV screening) plus presentation and
history, people and contacts, and financials; the identity badge with its "why" table
(field x source); blanks shown as "—" with a hint on where to find the value for free by
hand; clickable links; "Create full card" with progress (also for several cards in a row);
"Open Registro Imprese" with its paste form; the paid provider (only with the user's own
key, after a cost confirmation); "Copy as text" and "Export card (Markdown)".

The card is rendered as HTML in a QTextBrowser (it scrolls by itself, so the window keeps
its minimum size); the financial chart is drawn by `financials.chart.FinancialChart` into
an image resource of the document. Building runs in a worker thread through the
`run_in_background` callable of the main window (deadline, cancel); progress reaches the
GUI thread through a Qt signal.

All texts go through `tr(key, english_default)`: the English default is shown until the
key is present in the locale files.
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
import webbrowser
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QAction, QGuiApplication, QImage, QTextDocument
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLineEdit, QMenu, QMessageBox, QProgressBar,
    QPushButton, QScrollArea, QSpinBox, QTextBrowser, QVBoxLayout, QWidget,
)

from .. import companies, display_text
from ..display_text import display
from ..financials.strings import STRINGS as FIN_STRINGS
from ..i18n import t
from ..registry import confirm, paid
from ..registry.visura import FIELDS as REGISTER_FIELDS
from ..web.screening import ADVICE_TEXTS
from .common import is_openable_url, open_url, run_job
from .theme import Palette, active_palette
from .widgets import ElidedLabel, wrapped_label

from ..numbers import format_int  # noqa: E402 - thousands marks of the interface language

log = logging.getLogger(__name__)

BLANK = "—"
BUILD_TIMEOUT_S = 900.0  # the window stops waiting for a card build after this
BATCH_TIMEOUT_PER_CARD_S = 600.0
PAID_TIMEOUT_S = 90.0
CHART_URL = "reversehire://financials-chart.png"
CHART_SIZE = (640, 340)

# English defaults of every text of the card (key -> text). The locale files carry the
# English, Italian and Ukrainian texts of every key.
TEXTS: dict[str, str] = {
    "card.tab": "Card",
    "card.tab_notes": "Notes",
    "card.build": "Create full card",
    "card.refresh": "Refresh full card",
    "card.cancel": "Stop",
    "card.more": "More",
    "card.why": "Why?",
    "card.hide_why": "Hide details",
    "card.enter_register": "Enter register data…",
    "card.import_csv": "Import register CSV row…",
    "card.copy": "Copy as text",
    "card.copied": "Card copied to the clipboard.",
    "card.export": "Export card (Markdown)…",
    "card.exported": "Card saved: {path}",
    "card.paid_fetch": "Fetch from paid register",
    "card.refresh_section": "Refresh section",
    "card.select": "Select a company to see its card.",
    "card.not_built": "Not checked yet: use “Create full card”.",
    "card.building": "Building the card… ({step})",
    "card.built": "Card updated.",
    "card.cancelled": "Stopped: what was read so far is saved.",
    "card.failed": "The card could not be completed: {error}",
    "card.no_site": "No website: only the register data can be checked.",
    "card.step.site": "reading the website",
    "card.step.identity": "legal notice, contacts, about and careers pages",
    "card.step.registers": "VIES, GLEIF and Wikidata registers",
    "card.step.profile": "presentation and history",
    "card.step.people": "people and contacts",
    "card.step.financials": "financial figures (ESEF, Wikidata)",
    "card.step.ats": "selection system and privacy notices",
    "card.step.products": "products and services",
    "card.step.done": "done",
    "card.batch_step": "Card {index} of {count}: {name} ({step})",
    "card.batch_done": "{count} cards updated.",
    "card.batch_cancelled": "Stopped after {done} of {count} cards: what was read is saved.",
    "card.identity_line": "Identity: {status}",
    "card.status.confirmed": "CONFIRMED",
    "card.status.probable": "PROBABLE",
    "card.status.unverified": "UNVERIFIED",
    "card.status.conflict": "CONFLICT",
    "card.section.header": "1. Header",
    "card.section.registry": "2. Registry data",
    "card.section.hiring": "3. Profiles sought",
    "card.section.products": "4. Products and services",
    "card.section.ats": "5. Selection system",
    "card.section.screening": "6. CV screening",
    "card.section.profile": "7. Presentation and history",
    "card.section.people": "8. People and contacts",
    "card.section.financials": "9. Financials",
    "card.field.name": "Company",
    "card.field.legal_name": "Legal name",
    "card.field.rea": "REA",
    "card.field.ateco": "ATECO",
    "card.field.tax_code": "Tax code",
    "card.field.vat": "VAT number",
    "card.field.website": "Website",
    "card.field.legal_form": "Legal form",
    "card.field.registered_office": "Registered office",
    "card.field.operating_site": "Operating sites",
    "card.field.address": "Address shown on the website",
    "card.field.town": "Town",
    "card.field.province": "Province",
    "card.field.postcode": "Postcode",
    "card.field.pec": "PEC",
    "card.field.phone": "Phone",
    "card.field.email": "E-mail",
    "card.field.share_capital": "Share capital",
    "card.field.founded": "Founded",
    "card.field.employees": "Size hint (employees)",
    "card.field.lei": "LEI",
    "card.field.wikidata": "Wikidata",
    "card.field.parent_company": "Directed and coordinated by",
    "card.field.single_member": "Single shareholder",
    "card.field.in_liquidation": "In liquidation",
    "card.field.group_vat": "VAT numbers of other group companies",
    "card.field.linkedin": "LinkedIn page",
    "card.field.social": "Social profiles",
    "card.field.sdi_code": "SDI code (e-invoicing)",
    "card.field.careers": "Careers page",
    "card.field.status": "Status",
    "card.field.name_register": "Name",
    "card.yes": "yes",
    "card.capital_paid": "fully paid",
    "card.paid_hint": "not found in the free sources",
    "card.missing_paid": "Not found in the free automatic sources: {fields}. Each blank field says where to "
                         "find it for free by hand.",
    "card.hint_paid": "a paid key (Settings) can fill it automatically",
    "card.hint.vat": "usually printed on the company's home page or legal notice; free search on registroimprese.it",
    "card.hint.tax_code": "free on registroimprese.it after a free registration (“Codice Fiscale”)",
    "card.hint.rea": "free on registroimprese.it after a free registration (“Numero REA”)",
    "card.hint.ateco": "free on registroimprese.it: search the company, the result shows the ATECO code",
    "card.hint.legal_form": "free on registroimprese.it (search result); the full form after a free registration",
    "card.hint.pec": "free on registroimprese.it: search the company and press “MOSTRA” next to PEC",
    "card.hint.registered_office": "free on registroimprese.it (search result: address of the registered office)",
    "card.hint.share_capital": "in a Registro Imprese visura (5.00 EUR) or on the company's legal notice when "
                               "published",
    "card.hint.employees": "in the company's reports or a Registro Imprese visura; rarely published free",
    "card.hint.founded": "in a Registro Imprese visura (date of incorporation) or on the company's history page",
    "card.field_conflict": "Sources differ for {field}: {values}",
    "card.website_verified": "verified: the identifier printed on the site matches a register",
    "card.source": "source: {source}",
    "card.checked_on": "Checked on {date}",
    "card.spontaneous": "Spontaneous applications accepted",
    "card.no_postings": "No open positions found on the site.",
    "card.ats_none": "No applicant-tracking system detected.",
    "card.channel.email": "Applications by e-mail",
    "card.channel.form": "Application form on the website",
    "card.vendor_sources": "Vendor sources (reviewed {date}): {urls}",
    "card.platform_note": "What the platform offers is not proof that this employer uses it.",
    "card.parsing": "Automatic parsing: {value}",
    "card.parsing.likely": "likely",
    "card.parsing.unknown": "unknown",
    "card.ai": "AI screening: {value}",
    "card.ai.declared": "declared by the employer",
    "card.ai.platform_capable": "the platform is capable",
    "card.ai.no_evidence": "no evidence",
    "card.statements": "Statements on the employer's pages",
    "card.statement.states_ai": "states automated/AI processing",
    "card.statement.states_no_automated": "states NO automated decision",
    "card.statement.mentions": "mentions",
    "card.no_statements": "No statement about automated decisions or AI found on the pages checked.",
    "card.advice": "Advice for the CV",
    "card.why_title": "Agreement between sources",
    "card.why_field": "Field",
    "card.conflicts": "Conflicts: {fields}",
    "card.vies": "VIES: {state}",
    "card.gleif": "GLEIF: {state}",
    "card.wikidata": "Wikidata: {state}",
    "card.paid_note": "Paid register: {note}",
    # where on the page a value was read
    "card.where.footer": "footer",
    "card.where.header": "page header",
    "card.where.page_text": "page text",
    "card.where.json_ld": "schema.org data",
    "card.where.microdata": "schema.org microdata",
    "card.where.address_element": "address block",
    "card.where.link": "link",
    "card.where.wikidata_item": "Wikidata item",
    "card.where.about_first": "about page, first paragraphs",
    "card.where.json_ld_description": "schema.org description",
    "card.where.meta_description": "page description",
    "card.where.og_description": "page description (social)",
    "card.where.history_page": "history page",
    "card.where.dates": "sentences with dates",
    "card.where.job_posting": "job posting",
    "card.where.team_card": "team card",
    "card.page_kind.home": "home page",
    "card.page_kind.home_it": "Italian home page",
    "card.page_kind.legal": "legal notice",
    "card.page_kind.contacts": "contacts page",
    "card.page_kind.about": "about page",
    "card.page_kind.history": "history page",
    "card.page_kind.careers": "careers page",
    "card.page_kind.team": "team page",
    "card.page_kind.privacy": "privacy page",
    "card.page_kind.other": "page",
    # 7. presentation and history
    "card.presentation": "Presentation",
    "card.history": "History",
    "card.no_presentation": "No presentation text found on the pages read.",
    "card.no_history": "No history text found on the pages read.",
    "card.profile_note": "Sentences taken from the company's own pages, not rewritten.",
    # 8. people and contacts
    "card.people_none": "No contact found on the pages read. Large companies usually take applications only "
                        "through their careers portal.",
    "card.contact.kind.hr": "HR",
    "card.contact.kind.management": "Management",
    "card.contact.kind.general": "General",
    "card.contact.kind.privacy": "Privacy / data protection (not for applications)",
    "card.contact.delete": "delete",
    "card.contact.deleted": "Contact deleted.",
    "card.contact.confirm_delete": "Delete this contact from the card? It will not be added again.",
    "card.people_note": "Stored only on this computer; never sent anywhere. E-mail addresses are never guessed.",
    "card.linkedin_people": "Search on LinkedIn (opens your browser; the app reads nothing there):",
    "card.linkedin.hr": "HR people",
    "card.linkedin.recruiter": "recruiters",
    "card.linkedin.talent": "talent acquisition",
    "card.linkedin.risorse": "“risorse umane”",
    "card.linkedin.company_people": "company page: people in HR",
    # registro imprese
    "card.registro_open": "Open Registro Imprese",
    "card.registro_enter": "Enter data from Registro Imprese…",
    "card.registro_opened": "Registro Imprese opened in the browser; “{query}” copied: paste it in the search field.",
    "card.registro_title": "Data from Registro Imprese",
    "card.registro_intro": "Search the company on registroimprese.it (free), then copy here what the result "
                           "shows. Values are checked before saving and kept with the source “Registro Imprese "
                           "(entered by hand)” and today's date.",
    "card.registro_saved": "Registro Imprese data saved.",
    "source.registro_manual": "Registro Imprese (entered by hand)",
    "source.openapi": "Openapi (paid)",
    # 9. financials
    "card.fin_import": "Import balance-sheet CSV…",
    "card.fin_add": "Add figures by hand…",
    "card.fin_scope": "Scope:",
    "card.fin_source": "Source",
    "card.fin_employees": "Employees: {value}",
    "card.fin_csv_errors": "Some rows were not imported: {errors}",
    "card.fin_saved": "Figures saved.",
    "card.fin_csv_filter": "CSV files (*.csv *.txt)",
    "card.fin.sites": "Open company-information sites",
    "card.fin.paste": "Paste figures…",
    "card.fin.where": "Revenue, profit and employees of non-listed Italian companies are published in the "
                      "balance sheet filed with the Registro Imprese (bought there, about 2.50 EUR per year) "
                      "and summarised by public company-information sites, often as ranges. \"Open "
                      "company-information sites\" searches them in your browser (the app does not read them); "
                      "copy the page text into \"Paste figures…\", or import a CSV / type the figures by hand.",
    "card.fin.employees_years": "Employees per year: {values} (source: {sources})",
    "card.fin.size_value": "{value} ({year}, source: {source})",
    "card.fin.no_identity": "Neither the VAT number nor the name of this company is known.",
    "card.fin.sites_opened": "Search opened in the browser.",
    "fin.dialog.title": "Financial figures",
    "fin.dialog.intro": "Figures from a balance sheet you have (e.g. bought on Registro Imprese, 2.50 EUR per "
                        "year). They are shown with the source “Entered by hand”.",
    "fin.dialog.year": "Fiscal year",
    "fin.dialog.revenue": "Revenue",
    "fin.dialog.profit": "Net profit (negative for a loss)",
    "fin.dialog.employees": "Employees",
    "fin.dialog.scope": "Scope",
    "fin.dialog.currency": "Currency",
    "fin.dialog.unit": "Amounts in",
    "fin.dialog.unit.1": "units",
    "fin.dialog.unit.1000": "thousands",
    "fin.dialog.unit.1000000": "millions",
    "fin.dialog.note": "Document (e.g. bilancio 2024, Registro Imprese)",
    "fin.dialog.save": "Save",
    "fin.dialog.cancel": "Cancel",
    "fin.dialog.error": "Not saved: {error}",
    # paid provider
    "card.paid_confirm_title": "Paid lookup",
    "card.paid_confirm": "This asks {provider} for the data of “{name}” and costs {cost}, charged to your own "
                         "account. Continue?",
    "card.paid_cost": "{price} EUR per company (from {from_price} EUR with a plan), excl. VAT",
    "card.paid_running": "Asking the paid register…",
    "card.paid_done": "Paid register data saved.",
    "card.paid_failed": "Paid lookup not done: {error}",
    "card.paid_note_none": "No paid register configured (Settings). Openapi costs {cost}.",
    "card.paid_note_manual": "{provider}: buy it by hand and enter the data (not automated).",
    "card.paid_note_nokey": "{provider} ({cost}): type your key in Settings to use it.",
    "card.paid_note_ready": "{provider} ({cost}): asked only when you press the button and confirm.",
    "register.title": "Register data",
    "register.intro": "Data from a visura or a register list you bought. Invalid identifiers are not saved.",
    "register.save": "Save",
    "register.cancel": "Cancel",
    "register.errors": "Not saved: {errors}",
    "register.csv_filter": "CSV files (*.csv *.txt)",
    "register.csv_error": "The file cannot be used: {error}",
    "source.website": "website",
    "source.website_jsonld": "website (schema.org)",
    "source.vies": "VIES",
    "source.gleif": "GLEIF",
    "source.wikidata": "Wikidata",
    "source.register": "register",
    "source.card": "card",
}
for _key, _text in ADVICE_TEXTS.items():
    TEXTS[f"card.advice.{_key}"] = _text
for _key, _text in confirm.REASON_TEXTS.items():
    TEXTS[f"identity.reason.{_key}"] = _text
for _key, _text in FIN_STRINGS.items():
    TEXTS.setdefault(_key, _text)
for _key, _text in companies.FIELD_HINTS.items():
    TEXTS.setdefault(f"card.hint.{_key}", _text)

# Known "where" values (web/identity.py, web/profile.py, web/people.py) -> text keys.
_WHERE_KEYS = {
    "footer": "card.where.footer", "header": "card.where.header", "page text": "card.where.page_text",
    "JSON-LD": "card.where.json_ld", "microdata": "card.where.microdata",
    "address element": "card.where.address_element", "link": "card.where.link",
    "Wikidata item": "card.where.wikidata_item", "about page, first paragraphs": "card.where.about_first",
    "JSON-LD description": "card.where.json_ld_description", "meta description": "card.where.meta_description",
    "og:description": "card.where.og_description", "history page": "card.where.history_page",
    "job posting": "card.where.job_posting",
}


def tr(key: str, default: str | None = None, **params) -> str:
    """Translated text; the English default while the key is missing from the locales."""
    text = t(key, **params)
    if text != key:
        return text
    text = default if default is not None else TEXTS.get(key, key)
    if params:
        try:
            return text.format(**params)
        except (KeyError, IndexError):
            return text
    return text


def fin_tr(key: str, default: str) -> str:
    """The `tr(key, default)` callable of the financials package."""
    return tr(key, default)


def _e(text) -> str:
    return html.escape(str(text or ""))


def _date(stamp: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(stamp)) if stamp else ""


def _link(url: str, text: str | None = None) -> str:
    if not url:
        return _e(text or "")
    if not is_openable_url(url):
        return _e(text or url)
    return f'<a href="{_e(url)}">{_e(text or url)}</a>'


def _source_name(source: str) -> str:
    key = f"source.{source}"
    return tr(key, TEXTS.get(key, source or ""))


def where_text(where: str) -> str:
    """The display text of a provenance "where" value ('' for unknown ones)."""
    where = (where or "").strip()
    if not where:
        return ""
    if where in _WHERE_KEYS:
        return tr(_WHERE_KEYS[where])
    match = re.fullmatch(r"(\w+) page(, team card)?", where)
    if match:
        text = tr(f"card.page_kind.{match.group(1)}", TEXTS.get(f"card.page_kind.{match.group(1)}",
                                                                 TEXTS["card.page_kind.other"]))
        return f"{text}, {tr('card.where.team_card')}" if match.group(2) else text
    match = re.fullmatch(r"(about|home) page, sentences with dates", where)
    if match:
        return f"{tr('card.page_kind.' + match.group(1))}, {tr('card.where.dates')}"
    return ""


def fact_source_label(fact: dict | None) -> str:
    """'website, footer', 'Registro Imprese (entered by hand)', 'Openapi (paid)'…"""
    if not fact:
        return ""
    where = str(fact.get("where") or "")
    if where == companies.REGISTRO_IMPRESE_VIA:
        return tr("source.registro_manual")
    if where == paid.VIA:
        return tr("source.openapi")
    label = _source_name(str(fact.get("source", "")))
    extra = where_text(where)
    return f"{label}, {extra}" if extra else label


def _shown_value(name: str, value: str) -> str:
    """Values with a fixed English part, in the display language."""
    if name == "share_capital" and value.endswith(" fully paid"):
        return f"{value[: -len(' fully paid')]} {tr('card.capital_paid')}"
    if name in ("single_member", "in_liquidation") and value == "yes":
        return tr("card.yes")
    return value


def card_display_texts(card) -> list[str]:
    """Site-derived texts of a card that `display_text` may translate for an English
    interface (company names, addresses and identifiers are never translated)."""
    if card is None:
        return []
    products = card.products or {}
    texts = [products.get("summary", "")] + [i.get("name", "") for i in products.get("items", [])]
    texts += [p.get("title", "") for p in card.postings]
    texts += [p.get("salary", "") or "" for p in card.postings]
    texts += [s.get("quote", "") for s in (card.screening or {}).get("statements", [])]
    # the website sentences of the description and the OSM category names
    texts += [line for line in (card.description or "").splitlines()]
    texts += list(card.sectors or [])
    # presentation and history (verbatim from the site) and the roles of the contacts
    from ..web.profile import sentences

    for entry in (getattr(card, "presentation", {}) or {}, getattr(card, "history", {}) or {}):
        texts += sentences(entry.get("text", "")) if entry.get("text") else []
    texts += [c.get("role", "") for c in getattr(card, "contacts_list", [])]
    return [x for x in texts if x]


def badge_colours(status: str, palette: Palette) -> tuple[str, str]:
    """(background, text) of the identity badge."""
    return {
        confirm.CONFIRMED: (palette.hiring_bg, palette.hiring_fg),
        confirm.PROBABLE: (palette.maybe_bg, palette.maybe_fg),
        confirm.CONFLICT: (palette.surface2, palette.error),
    }.get(status, (palette.surface2, palette.muted))


def linkedin_people_links(card) -> list[tuple[str, str]]:
    """[(label, url)] of LinkedIn searches opened in the user's browser (never read by the app)."""
    name = companies.strip_legal_form(card.legal_name or card.name)
    base = "https://www.linkedin.com/search/results/people/?keywords="
    links = [(tr("card.linkedin.hr"), base + quote(f"HR {name}", safe="")),
             (tr("card.linkedin.recruiter"), base + quote(f"recruiter {name}", safe="")),
             (tr("card.linkedin.talent"), base + quote(f"talent acquisition {name}", safe="")),
             (tr("card.linkedin.risorse"), base + quote(f"risorse umane {name}", safe=""))]
    page = card.registry_value("linkedin")
    if page.startswith("https://www.linkedin.com/company/"):
        links.append((tr("card.linkedin.company_people"), page.rstrip("/") + "/people/?keywords=HR"))
    return links


def _shown_text(text: str, muted: str) -> str:
    """A site text in the display language, sentence by sentence (each one is translated
    and cached on its own), the original under it when translated."""
    from ..web.profile import sentences

    parts = sentences(text) or [text]
    shown = [display_text.show(s) for s in parts]
    # an untranslated sentence is shown as it is; the "(original language)" marker once at the end
    words = [s if item.missing else item.text for s, item in zip(parts, shown)]
    out = _e(" ".join(words))
    if any(item.missing for item in shown):
        out += f" <span style='color:{muted}'>{_e(t('display.original_marker'))}</span>"
    if any(item.original is not None for item in shown):
        out += f"<br><span style='color:{muted}'>{_e(text)}</span>"
    return out


def card_html(card, palette: Palette, *, show_why: bool = False, paid_note: str = "",
              chart_url: str = "", posting_match: Callable | None = None, simulate_links: bool = False) -> str:
    """The whole card as HTML for the QTextBrowser (pure function, tested). `chart_url` is
    the image resource of the financial chart (drawn by the view), '' for none.
    `posting_match(posting dict) -> (badge, tooltip, colour) | None` adds "Match with my
    CV" to the profiles sought; `simulate_links` adds "Simulate this system on my CV" to
    the selection systems a recruiter simulator knows."""
    muted = palette.muted
    out: list[str] = [f"<h2 style='margin-bottom:2px'>{_e(card.name)}</h2>"]
    status = card.identity_status or confirm.UNVERIFIED
    background, colour = badge_colours(status, palette)
    badge = (f"<span style='background-color:{background}; color:{colour}'>&nbsp;<b>"
             f"{_e(tr('card.status.' + status))}</b>&nbsp;</span>")
    why = tr("card.hide_why") if show_why else tr("card.why")
    checked = _date(card.identity_checked)
    line = f"{badge} &nbsp;<a href='action:why'>{_e(why)}</a>"
    if checked:
        line += f" &nbsp;<span style='color:{muted}'>{_e(tr('card.checked_on', date=checked))}</span>"
    out.append(f"<p>{line}</p>")
    if show_why:
        out.append(_why_html(card, palette))

    def source_note(fact: dict | None) -> str:
        if not fact:
            return ""
        label = fact_source_label(fact)
        url = str(fact.get("url", ""))
        note = _link(url, label) if url.startswith("http") else _e(label)
        return f" <span style='color:{muted}'>({_e(tr('card.source', source=''))}{note})</span>"

    def value_row(label: str, value: str, *, name: str = "", fact: dict | None = None, hint: bool = False,
                  link_value: bool = False) -> str:
        if value:
            shown = _link(value) if link_value else _e(_shown_value(name, value))
            shown += source_note(fact)
        else:
            shown = BLANK
            if hint and name in companies.FIELD_HINTS:
                text = f"{tr('card.hint.' + name)}; {tr('card.hint_paid')}"
                shown += f" <span style='color:{muted}'><i>{_e(text)}</i></span>"
        return f"<tr><td style='padding-right:12px; color:{muted}'>{_e(label)}</td><td>{shown}</td></tr>"

    def section(key: str, checked_stamp: float | None = None) -> None:
        stamp = _date(checked_stamp)
        extra = f" <span style='color:{muted}; font-size:small'>{_e(tr('card.checked_on', date=stamp))}</span>" \
            if stamp else ""
        out.append(f"<h3 style='margin-top:14px; margin-bottom:4px'>{_e(tr(key))}{extra}</h3>")

    # 1. header
    section("card.section.header", card.identity_checked)
    rows = [value_row(tr("card.field.name"), card.name)]
    for name in ("legal_name", "rea", "ateco", "tax_code", "vat"):
        value = card.value_of(name)
        rows.append(value_row(tr(f"card.field.{name}"), value, name=name,
                              fact=card.source_of(name) if value else None, hint=True))
    out.append("<table>" + "".join(rows) + "</table>")

    # 2. registry data
    section("card.section.registry", card.registry_checked)
    website = card.website or card.site or ""
    rows = [value_row(tr("card.field.website"), website, link_value=True)]
    if website and (card.identity or {}).get("website_verified"):
        rows[-1] = rows[-1].replace("</td></tr>", f" <span style='color:{palette.accent}'>✓ "
                                    f"{_e(tr('card.website_verified'))}</span></td></tr>")
    if card.name:  # opens LinkedIn's company search in the browser (the app never reads LinkedIn)
        rows.append(f"<tr><td style='padding-right:12px; color:{muted}'>LinkedIn</td><td>"
                    f"{_link(companies.linkedin_search_url(card.name), tr('detail.open_linkedin'))}</td></tr>")
    registry = card.registry or {}
    for name in ("legal_form", "registered_office", "operating_site", "address", "town", "province", "postcode",
                 "pec", "phone", "email", "share_capital", "founded", "employees", "lei", "wikidata",
                 "parent_company", "single_member", "in_liquidation", "group_vat", "linkedin", "social"):
        item = registry.get(name)
        if name in companies.LIST_FIELDS:
            entries = item if isinstance(item, list) else []
            if not entries:
                if name == "operating_site":
                    rows.append(value_row(tr(f"card.field.{name}"), ""))
                continue
            links = name in ("social",)
            shown = "<br>".join((_link(i.get("value", "")) if links else _e(i.get("value", ""))) + source_note(i)
                                for i in entries)
            rows.append(f"<tr><td style='padding-right:12px; color:{muted}'>{_e(tr(f'card.field.{name}'))}</td>"
                        f"<td>{shown}</td></tr>")
            continue
        fact = item if isinstance(item, dict) else None
        value = card.registry_value(name)
        if not value and name in ("address", "parent_company", "single_member", "in_liquidation", "linkedin",
                                  "wikidata"):
            continue  # shown only when stated
        if name == "wikidata" and value:
            rows.append(value_row(tr("card.field.wikidata"), f"https://www.wikidata.org/wiki/{value}",
                                  link_value=True))
            continue
        if name == "employees" and not value and card.latest_employees() is not None:
            # the size from the stored financial figures, with its year and source
            count, year, origin = card.latest_employees()
            rows.append(f"<tr><td style='padding-right:12px; color:{muted}'>{_e(tr('card.field.employees'))}</td>"
                        f"<td>{_e(tr('card.fin.size_value', value=format_int(count), year=year,
                                       source=_fin_source_name(origin)))}</td></tr>")
            continue
        rows.append(value_row(tr(f"card.field.{name}"), value, name=name, fact=fact,
                              hint=name in companies.PAID_FIELDS, link_value=name == "linkedin"))
    out.append("<table>" + "".join(rows) + "</table>")
    conflicts = (card.identity or {}).get("field_conflicts") or {}
    for name, values in conflicts.items():
        parts = ", ".join(f"{_e(_shown_value(name, str(v.get('value', ''))))} "
                          f"({_e(_source_name(str(v.get('source', ''))))})" for v in values)
        out.append(f"<p style='color:{palette.error}'>"
                   f"{_e(tr('card.field_conflict', field=tr('card.field.' + name), values=''))}{parts}</p>")
    missing = card.missing_paid
    if missing:
        names = ", ".join(tr(f"card.field.{m}") for m in missing)
        out.append(f"<p style='color:{muted}'>{_e(tr('card.missing_paid', fields=names))}</p>")
    out.append(f"<p><a href='action:registro_open'>{_e(tr('card.registro_open'))}</a> &nbsp;·&nbsp; "
               f"<a href='action:registro_enter'>{_e(tr('card.registro_enter'))}</a></p>")
    if paid_note:
        out.append(f"<p style='color:{muted}'><i>{_e(tr('card.paid_note', note=paid_note))}</i></p>")

    # 3. profiles sought
    section("card.section.hiring", card.hiring_checked)
    hiring = card.hiring or {}
    careers = hiring.get("careers_url") or card.careers_url or ""
    items = [f"<p>{_e(tr('card.field.careers'))}: {_link(careers) if careers else BLANK}</p>"]
    postings = card.postings
    if postings:
        lis = []
        for index, posting in enumerate(postings[: companies.MAX_POSTINGS_KEPT]):
            attrs = [str(posting.get(k)).replace("_", " ") for k in ("contract", "remote", "salary", "travel")
                     if posting.get(k)]
            location = posting.get("location") or ""
            title = display(posting.get("title", ""))
            text = _link(posting.get("url", ""), title) if posting.get("url") else _e(title)
            details = " · ".join([location] + attrs if location else attrs)
            badge = ""
            if posting_match is not None:
                shown = posting_match(posting)
                if shown is not None:
                    value, tip, colour = shown
                    style = f"color:{colour or palette.strong}; background-color:{palette.surface2}"
                    badge = (f" <a href='match:{index}' title='{_e(tip)}' style='{style}'>&nbsp;"
                             f"{_e(tr('match.badge', score=value))}&nbsp;</a>")
            lis.append(f"<li>{text}{badge}" + (f" <span style='color:{muted}'>{_e(details)}</span>" if details else "")
                       + "</li>")
        items.append("<ul>" + "".join(lis) + "</ul>")
    elif card.hiring_checked or card.hiring:
        items.append(f"<p style='color:{muted}'>{_e(tr('card.no_postings'))}</p>")
    else:
        items.append(f"<p style='color:{muted}'>{_e(tr('card.not_built'))}</p>")
    if hiring.get("spontaneous"):
        items.append(f"<p>{_e(tr('card.spontaneous'))}</p>")
    out.extend(items)

    # 4. products and services
    section("card.section.products", card.products_checked)
    products = card.products or {}
    if products.get("summary"):
        out.append(f"<p>{_e(display(products['summary']))} <span style='color:{muted}'>"
                   f"({_link(products.get('summary_url', ''), _source_name('website'))})</span></p>")
    if products.get("items"):
        out.append("<ul>" + "".join(f"<li>{_link(i.get('url', ''), display(i.get('name', '')))}</li>"
                                    for i in products["items"]) + "</ul>")
    if not products:
        out.append(f"<p style='color:{muted}'>{_e(tr('card.not_built'))}</p>")

    # 5. selection system
    section("card.section.ats", card.ats_checked)
    platforms = (card.ats or {}).get("platforms", [])
    if not card.ats:
        out.append(f"<p style='color:{muted}'>{_e(tr('card.not_built'))}</p>")
    elif not platforms:
        out.append(f"<p>{_e(tr('card.ats_none'))}</p>")
    for platform in platforms:
        key = platform.get("key", "")
        if key in ("email", "form"):
            out.append(f"<p><b>{_e(tr('card.channel.' + key))}</b> {_link(platform.get('url', ''), '↗')}</p>")
            if simulate_links and key == "email":
                out.append(f"<p><a href='action:simulate:email_no_ats'>{_e(tr('card.simulate_system'))}</a></p>")
            continue
        out.append(f"<p><b>{_e(platform.get('name', key))}</b> {_link(platform.get('url', ''), '↗')}<br>"
                   f"{_e(platform.get('offer', ''))}</p>")
        if simulate_links:
            from .recruiter_analysis import profile_key_for_platform

            profile_key = profile_key_for_platform(key, platform.get("url", ""))
            if profile_key:
                out.append(f"<p><a href='action:simulate:{_e(profile_key)}'>"
                           f"{_e(tr('card.simulate_system'))}</a></p>")
        if platform.get("sources"):
            links = ", ".join(_link(u, u.split('/')[2] if u.count('/') >= 2 else u) for u in platform["sources"])
            out.append(f"<p style='color:{muted}'>{_e(tr('card.vendor_sources', date=platform.get('reviewed', ''), urls=''))}"
                       f"{links}</p>")
    if any(p.get("offer") for p in platforms):
        out.append(f"<p style='color:{muted}'><i>{_e(tr('card.platform_note'))}</i></p>")

    # 6. CV screening
    section("card.section.screening", card.screening_checked)
    screening = card.screening or {}
    if not screening:
        out.append(f"<p style='color:{muted}'>{_e(tr('card.not_built'))}</p>")
    else:
        parsing = screening.get("parsing", "unknown")
        ai = screening.get("ai", "no_evidence")
        out.append(f"<p>{_e(tr('card.parsing', value=tr('card.parsing.' + parsing)))}<br>"
                   f"<b>{_e(tr('card.ai', value=tr('card.ai.' + ai)))}</b></p>")
        statements = screening.get("statements", [])
        if statements:
            out.append(f"<p><b>{_e(tr('card.statements'))}</b></p><ul>")
            for statement in statements:
                kind = tr("card.statement." + statement.get("kind", "mentions"))
                quote_text = statement.get("quote", "")
                shown = display_text.show(quote_text)
                # the verbatim quote stays visible under its English translation
                original = (f"<br><span style='color:{muted}'>“{_e(quote_text)}”</span>"
                            if shown.original is not None else "")
                out.append(f"<li><i>“{_e(shown.text)}”</i>{original} <span style='color:{muted}'>"
                           f"({_e(kind)}, {_link(statement.get('url', ''), '↗')})</span></li>")
            out.append("</ul>")
        else:
            out.append(f"<p style='color:{muted}'>{_e(tr('card.no_statements'))}</p>")
        advice = screening.get("advice", [])
        if advice:
            out.append(f"<p><b>{_e(tr('card.advice'))}</b></p><ul>"
                       + "".join(f"<li>{_e(tr('card.advice.' + a))}</li>" for a in advice) + "</ul>")

    # 7. presentation and history
    section("card.section.profile", card.profile_checked)
    if not card.profile:
        out.append(f"<p style='color:{muted}'>{_e(tr('card.not_built'))}</p>")
    else:
        for key, entry, empty in (("card.presentation", card.presentation, "card.no_presentation"),
                                  ("card.history", card.history, "card.no_history")):
            out.append(f"<p><b>{_e(tr(key))}</b></p>")
            if entry.get("text"):
                out.append(f"<p>{_shown_text(entry['text'], muted)}{source_note(entry)}</p>")
            else:
                out.append(f"<p style='color:{muted}'>{_e(tr(empty))}</p>")
        out.append(f"<p style='color:{muted}'><i>{_e(tr('card.profile_note'))}</i></p>")

    # 8. people and contacts
    section("card.section.people", card.people_checked)
    contacts = card.contacts_list
    if not card.people:
        out.append(f"<p style='color:{muted}'>{_e(tr('card.not_built'))}</p>")
    elif not contacts:
        out.append(f"<p style='color:{muted}'>{_e(tr('card.people_none'))}</p>")
    else:
        lis = []
        for contact in contacts:
            kind = tr(f"card.contact.kind.{contact.get('kind', 'general')}")
            parts = []
            if contact.get("name"):
                parts.append(f"<b>{_e(contact['name'])}</b>")
            if contact.get("role"):
                parts.append(_e(display(contact["role"])))
            if contact.get("email"):
                parts.append(f"<a href='mailto:{_e(contact['email'])}'>{_e(contact['email'])}</a>")
            if contact.get("phone"):
                parts.append(_e(contact["phone"]))
            origin = {"source": contact.get("origin", "website"), "url": contact.get("source_url", ""),
                      "where": contact.get("where", "")}
            lis.append(f"<li>[{_e(kind)}] " + " · ".join(parts) + source_note(origin)
                       + f" <a href='action:delete_contact:{_e(contact.get('id', ''))}'>"
                       f"{_e(tr('card.contact.delete'))}</a></li>")
        out.append("<ul>" + "".join(lis) + "</ul>")
    if card.name:
        links = " · ".join(_link(url, label) for label, url in linkedin_people_links(card))
        out.append(f"<p>{_e(tr('card.linkedin_people'))} {links}</p>")
    out.append(f"<p style='color:{muted}'><i>{_e(tr('card.people_note'))}</i></p>")

    # 9. financials
    section("card.section.financials", card.financials_checked)
    out.append(_financials_html(card, palette, chart_url))
    return "\n".join(out)


def _financials_html(card, palette: Palette, chart_url: str) -> str:
    muted = palette.muted
    series = card.financial_series()
    result = card.financial_result()
    parts: list[str] = []
    scopes = series.scopes()
    chosen = (card.financials or {}).get("scope") if (card.financials or {}).get("scope") in scopes else \
        (scopes[0] if scopes else None)
    if len(scopes) > 1:
        links = " · ".join(f"<b>{_e(tr('fin.scope.' + s))}</b>" if s == chosen else
                           f"<a href='action:fin_scope:{s}'>{_e(tr('fin.scope.' + s))}</a>" for s in scopes)
        parts.append(f"<p>{_e(tr('card.fin_scope'))} {links}</p>")
    if not series.empty:
        parts.append(_trend_badge_html(card, palette))
    # the chart frame is always there: with no figure it shows its empty state
    if chart_url:
        parts.append(f"<p><img src='{_e(chart_url)}' width='{CHART_SIZE[0]}' height='{CHART_SIZE[1]}'></p>")
    if series.empty:
        why = result.why_missing(fin_tr) if result.reports else ""
        text = why or (tr("card.not_built") if not card.financials_checked else tr("fin.chart.empty"))
        parts.append("<p style='color:%s'>%s</p>" % (muted, "<br>".join(_e(x) for x in text.splitlines())))
        parts.append(f"<p style='color:{muted}'><i>{_e(tr('card.fin.where'))}</i></p>")
    else:
        view = series.view(chosen)
        from ..financials.chart import format_amount

        def amount(record, name: str) -> str:
            value = getattr(record, name)
            if value is not None:
                return format_int(value) if name == "employees" else format_amount(value, record.currency, fin_tr)
            pair = (record.ranges or {}).get(name)
            if pair:
                low, high = pair
                if name == "employees":  # a range is shown as "low–high" in every language
                    return f"{format_int(low)}–{format_int(high)}"
                return f"{format_amount(low, '', fin_tr)}–{format_amount(high, record.currency, fin_tr)}"
            return BLANK

        rows = []
        for record in reversed(view.rows):
            source = _link(record.source_url, _fin_source_name(record.source))
            if record.source.split(":")[0] == "pasted" and record.note:
                source += f" <span style='color:{muted}'>({_e(record.note.removeprefix('source: '))})</span>"
            cells = [_e(str(record.year)), _e(amount(record, "revenue")), _e(amount(record, "profit")),
                     _e(amount(record, "employees")), source]
            rows.append("<tr>" + "".join(f"<td style='padding:1px 8px'>{c}</td>" for c in cells) + "</tr>")
        head = [tr("fin.dialog.year"), tr("fin.chart.revenue"), tr("fin.chart.profit"), tr("fin.chart.employees"),
                tr("card.fin_source")]
        parts.append("<table><tr>" + "".join(f"<th style='padding:1px 8px'>{_e(h)}</th>" for h in head) + "</tr>"
                     + "".join(rows) + "</table>")
        staff = _employees_line(series, chosen)
        if staff:
            parts.append(f"<p>{_e(staff)}</p>")
        for note in result.notes(fin_tr):
            parts.append(f"<p style='color:{muted}'><i>{_e(note)}</i></p>")
    employees = card.registry_value("employees")
    if employees:
        parts.append(f"<p>{_e(tr('card.fin_employees', value=employees))}</p>")
    actions = [("fin_sites", "card.fin.sites"), ("fin_paste", "card.fin.paste"), ("fin_import", "card.fin_import"),
               ("fin_add", "card.fin_add")]
    parts.append("<p>" + " &nbsp;·&nbsp; ".join(f"<a href='action:{name}'>{_e(tr(key))}</a>" for name, key in actions)
                 + "</p>")
    return "\n".join(parts)


def _fin_source_name(source: str) -> str:
    """Display name of a financial source ("manual" -> "Entered by hand"; any new source by its name)."""
    name = (source or "").split(":")[0]
    return tr(f"fin.source.{name}", FIN_STRINGS.get(f"fin.source.{name}", name))


def _employees_line(series, scope) -> str:
    """"Employees per year: 2021: 40 · 2022: 45 (source)" from the stored figures, or ""."""
    from ..financials.trend import employees_by_year

    rows = employees_by_year(series, scope)
    if not rows:
        return ""
    values = " · ".join(f"{year}: {format_int(value)}" for year, value, _source in rows)
    sources = ", ".join(dict.fromkeys(_fin_source_name(source) for _year, _value, source in rows))
    return tr("card.fin.employees_years", values=values, sources=sources)


def trend_badge_text(card) -> str:
    """The revenue-trend sentence of a card ("Revenue growing +6.3 %/year over 2021–2024 ·
    source: Entered by hand"), "" when no figure is stored."""
    from ..financials.trend import trend_text

    if card.financial_series().empty:
        return ""
    trend = card.revenue_trend()
    text = trend_text(trend, fin_tr)
    if trend.known and trend.sources:
        text += " · " + tr("fin.trend.source", sources=", ".join(_fin_source_name(s) for s in trend.sources))
    return text


def _trend_badge_html(card, palette: Palette) -> str:
    trend = card.revenue_trend()
    colour = {"growing": palette.accent, "declining": palette.warning}.get(trend.state, palette.muted)
    text = trend_badge_text(card)
    if not trend.known:
        return f"<p style='color:{palette.muted}'><i>{_e(text)}</i></p>"
    return f"<p><span style='color:{colour}'><b>{_e(text)}</b></span></p>"


def _why_html(card, palette: Palette) -> str:
    identity = card.identity or {}
    muted = palette.muted
    parts = [f"<p><b>{_e(tr('card.why_title'))}</b></p>"]
    reasons = identity.get("reasons", [])
    if reasons:
        parts.append("<ul>" + "".join(f"<li>{_e(tr('identity.reason.' + r))}</li>" for r in reasons) + "</ul>")
    table = identity.get("table") or {}
    sources: list[str] = []
    for per_source in table.values():
        for source in per_source:
            if source not in sources:
                sources.append(source)
    if table:
        agreed = {(a.get("field"), s) for a in identity.get("agreements", []) for s in a.get("sources", [])}
        head = "".join(f"<th style='padding:2px 8px'>{_e(_source_name(s))}</th>" for s in sources)
        rows = []
        for name, per_source in table.items():
            cells = []
            for source in sources:
                value = per_source.get(source, "")
                style = f"color:{palette.accent}" if (name, source) in agreed else ""
                cells.append(f"<td style='padding:2px 8px; {style}'>{_e(value) or BLANK}</td>")
            label = tr(f"card.field.{name}") if name != "name" else tr("card.field.name_register")
            rows.append(f"<tr><td style='color:{muted}; padding:2px 8px'>{_e(label)}</td>{''.join(cells)}</tr>")
        parts.append(f"<table border='1' cellspacing='0' style='border-color:{palette.border}'>"
                     f"<tr><th style='padding:2px 8px'>{_e(tr('card.why_field'))}</th>{head}</tr>"
                     + "".join(rows) + "</table>")
    conflicts = identity.get("conflicts", [])
    if conflicts:
        names = ", ".join(tr(f"card.field.{c.get('field')}") for c in conflicts)
        parts.append(f"<p style='color:{palette.error}'>{_e(tr('card.conflicts', fields=names))}</p>")
    notes = []
    for key in ("vies", "gleif", "wikidata"):
        if identity.get(key):
            notes.append(tr(f"card.{key}", state=str(identity[key].get("state", "")).replace("_", " ")))
    if notes:
        parts.append(f"<p style='color:{muted}'>{_e(' · '.join(notes))}</p>")
    return "\n".join(parts)


def paid_note_text(settings) -> str:
    """The note under the paid button, in the display language."""
    provider = paid.configured_provider(settings)
    cost = tr("card.paid_cost", price=f"{paid.PRICE_SINGLE_EUR:.2f}", from_price=f"{paid.PRICE_FROM_EUR:.2f}")
    if provider is None:
        return tr("card.paid_note_none", cost=cost)
    if not provider.implemented:
        return tr("card.paid_note_manual", provider=provider.name)
    if not paid.has_key(settings):
        return tr("card.paid_note_nokey", provider=provider.name, cost=cost)
    return tr("card.paid_note_ready", provider=provider.name, cost=cost)


# --- dialogs -------------------------------------------------------------------------------------


class RegisterDataDialog(QDialog):
    """Manual entry of register data (after a paid visura) and import of a CSV row."""

    def __init__(self, parent: QWidget, card, on_save: Callable[[dict], dict],
                 on_import: Callable[[str, str], dict]):
        super().__init__(parent)
        self._on_save = on_save
        self._on_import = on_import
        self.setWindowTitle(tr("register.title"))
        self.fields: dict[str, QLineEdit] = {}
        form = QFormLayout()
        for name in REGISTER_FIELDS:
            edit = QLineEdit()
            current = card.value_of(name) if name != "name" else card.legal_name
            if name == "status":
                current = ""
            edit.setText(current or "")
            self.fields[name] = edit
            label = tr(f"card.field.{name}") if name != "name" else tr("card.field.legal_name")
            form.addRow(label, edit)
        holder = QWidget()
        holder.setLayout(form)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        self.intro = wrapped_label(tr("register.intro"), role="muted")
        self.error_label = wrapped_label(role="error")
        self.error_label.hide()
        self.import_button = QPushButton(tr("card.import_csv"))
        self.import_button.clicked.connect(lambda: self.import_csv())
        self.save_button = QPushButton(tr("register.save"))
        self.save_button.setProperty("role", "accent")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(lambda: self.save())
        self.cancel_button = QPushButton(tr("register.cancel"))
        self.cancel_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.import_button)
        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.intro)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.error_label)
        layout.addLayout(buttons)
        metrics = self.fontMetrics()
        self.resize(metrics.averageCharWidth() * 80, metrics.height() * 34)

    def values(self) -> dict:
        return {name: edit.text() for name, edit in self.fields.items()}

    def _show_errors(self, errors: dict) -> bool:
        if not errors:
            self.error_label.hide()
            return False
        text = "; ".join(f"{tr('card.field.' + k)}: {v}" for k, v in errors.items())
        self.error_label.setText(tr("register.errors", errors=text))
        self.error_label.show()
        return True

    def save(self) -> bool:
        errors = self._on_save(self.values())
        if self._show_errors(errors):
            return False
        self.accept()
        return True

    def import_text(self, text: str, filename: str = "") -> bool:
        try:
            errors = self._on_import(text, filename)
        except ValueError as error:
            self.error_label.setText(tr("register.csv_error", error=str(error)))
            self.error_label.show()
            return False
        if self._show_errors(errors):
            return False
        self.accept()
        return True

    def import_csv(self) -> bool:
        path, _filter = QFileDialog.getOpenFileName(self, tr("card.import_csv"), "", tr("register.csv_filter"))
        if not path:
            return False
        raw = Path(path).read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
        return self.import_text(text, Path(path).name)


class RegistroImpreseDialog(QDialog):
    """Paste form for what the free Registro Imprese search shows (ATECO, PEC, REA, codice
    fiscale, legal form, registered office)."""

    def __init__(self, parent: QWidget, card, on_save: Callable[[dict], dict], on_open: Callable[[], None]):
        super().__init__(parent)
        self._on_save = on_save
        self.setWindowTitle(tr("card.registro_title"))
        self.fields: dict[str, QLineEdit] = {}
        form = QFormLayout()
        for name in companies.REGISTRO_FORM_FIELDS:
            edit = QLineEdit(card.value_of(name) or "")
            self.fields[name] = edit
            form.addRow(tr(f"card.field.{name}"), edit)
        self.intro = wrapped_label(tr("card.registro_intro"), role="muted")
        self.error_label = wrapped_label(role="error")
        self.error_label.hide()
        self.open_button = QPushButton(tr("card.registro_open"))
        self.open_button.clicked.connect(lambda: on_open())
        self.save_button = QPushButton(tr("register.save"))
        self.save_button.setProperty("role", "accent")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(lambda: self.save())
        self.cancel_button = QPushButton(tr("register.cancel"))
        self.cancel_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.open_button)
        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.intro)
        layout.addLayout(form)
        layout.addWidget(self.error_label)
        layout.addLayout(buttons)
        self.resize(self.fontMetrics().averageCharWidth() * 76, self.sizeHint().height())

    def values(self) -> dict:
        return {name: edit.text() for name, edit in self.fields.items()}

    def save(self) -> bool:
        errors = self._on_save(self.values())
        if errors:
            text = "; ".join(f"{tr('card.field.' + k)}: {v}" for k, v in errors.items())
            self.error_label.setText(tr("register.errors", errors=text))
            self.error_label.show()
            return False
        self.accept()
        return True


class FinancialsDialog(QDialog):
    """One fiscal year typed by hand (e.g. from a bought bilancio)."""

    UNITS = (1, 1000, 1000000)

    def __init__(self, parent: QWidget, on_save: Callable[[dict], str]):
        super().__init__(parent)
        self._on_save = on_save
        self.setWindowTitle(tr("fin.dialog.title"))
        self.year = QSpinBox()
        self.year.setRange(1900, 2100)
        self.year.setValue(time.localtime().tm_year - 1)
        self.revenue = QLineEdit()
        self.profit = QLineEdit()
        self.employees = QLineEdit()
        self.unit = QComboBox()
        for unit in self.UNITS:
            self.unit.addItem(tr(f"fin.dialog.unit.{unit}"), unit)
        self.scope = QComboBox()
        for scope in ("separate", "consolidated", "unknown"):
            self.scope.addItem(tr(f"fin.scope.{scope}"), scope)
        self.currency = QLineEdit("EUR")
        self.currency.setMaxLength(3)
        self.note = QLineEdit()
        form = QFormLayout()
        for label, widget in (("fin.dialog.year", self.year), ("fin.dialog.revenue", self.revenue),
                              ("fin.dialog.profit", self.profit), ("fin.dialog.unit", self.unit),
                              ("fin.dialog.employees", self.employees), ("fin.dialog.scope", self.scope),
                              ("fin.dialog.currency", self.currency), ("fin.dialog.note", self.note)):
            form.addRow(tr(label), widget)
        self.intro = wrapped_label(tr("fin.dialog.intro"), role="muted")
        self.error_label = wrapped_label(role="error")
        self.error_label.hide()
        self.save_button = QPushButton(tr("fin.dialog.save"))
        self.save_button.setProperty("role", "accent")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(lambda: self.save())
        self.cancel_button = QPushButton(tr("fin.dialog.cancel"))
        self.cancel_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.intro)
        layout.addLayout(form)
        layout.addWidget(self.error_label)
        layout.addLayout(buttons)

    def values(self) -> dict:
        return {"year": self.year.value(), "revenue": self.revenue.text(), "profit": self.profit.text(),
                "employees": self.employees.text(), "unit": self.unit.currentData(),
                "scope": self.scope.currentData(), "currency": self.currency.text().strip().upper() or "EUR",
                "note": self.note.text().strip()}

    def save(self) -> bool:
        error = self._on_save(self.values())
        if error:
            self.error_label.setText(tr("fin.dialog.error", error=error))
            self.error_label.show()
            return False
        self.accept()
        return True


# --- card view -----------------------------------------------------------------------------


class _Bridge(QObject):
    progress = Signal(str, int, int)
    batch = Signal(int, int, str, str)


class CompanyCardView(QWidget):
    """The card of the selected company, with its build, register, financials and export actions."""

    def __init__(self, store, run_in_background: Callable, *, settings_provider: Callable[[], object],
                 opener=webbrowser.open, on_changed: Callable[[int], None] | None = None,
                 fetcher_factory: Callable | None = None, ask: Callable[[str, str], bool] | None = None,
                 paid_client_factory: Callable | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self._store = store
        self._run = run_in_background
        self._settings_provider = settings_provider
        self._opener = opener
        self._on_changed = on_changed or (lambda _id: None)
        self._fetcher_factory = fetcher_factory
        self._ask = ask or self._ask_box
        self._paid_client_factory = paid_client_factory
        self._palette: Palette = active_palette()
        self._cancel: threading.Event | None = None
        # Called with the card (or None) every time the shown card is set or reloaded:
        # the boxes of other features (affinity, applications) follow the card this way.
        self.card_listeners: list[Callable] = []
        self._card = None
        self.show_why = False
        self.busy = False
        self.batch_count = 0
        self.register_dialog: RegisterDataDialog | None = None
        self.registro_dialog: RegistroImpreseDialog | None = None
        self.financials_dialog: FinancialsDialog | None = None
        self._bridge = _Bridge()
        self._bridge.progress.connect(self._progress)
        self._bridge.batch.connect(self._batch_progress)
        self._chart = None  # FinancialChart, created on first use (never shown: it draws the image)

        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)
        self.browser.setOpenExternalLinks(False)
        self.browser.anchorClicked.connect(self._anchor)
        # set by the main window: "Simulate this system on my CV" and "Match with my CV"
        self.on_simulate: Callable[[int, str], None] = lambda _card_id, _profile_key: None
        self.match_service = None
        self.build_button = QPushButton()
        self.build_button.setProperty("role", "accent")
        self.build_button.clicked.connect(lambda: self.build())
        self.cancel_button = QPushButton()
        self.cancel_button.clicked.connect(lambda: self.cancel())
        self.cancel_button.hide()
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setMaximumWidth(self.fontMetrics().averageCharWidth() * 14)
        self.progress.hide()
        self.more_button = QPushButton()
        # "Write e-mail…": the main window sets `on_mail` (opens the mail dialog for this card)
        self.mail_button = QPushButton()
        self.on_mail = lambda: None
        self.mail_button.clicked.connect(lambda: self.on_mail())
        self.menu = QMenu(self)
        self.more_button.setMenu(self.menu)
        self.register_action = QAction(self)
        self.register_action.triggered.connect(lambda: self.open_register_dialog())
        self.registro_open_action = QAction(self)
        self.registro_open_action.triggered.connect(lambda: self.open_registro_imprese())
        self.registro_enter_action = QAction(self)
        self.registro_enter_action.triggered.connect(lambda: self.open_registro_dialog())
        self.fin_import_action = QAction(self)
        self.fin_import_action.triggered.connect(lambda: self.import_financials())
        self.fin_add_action = QAction(self)
        self.fin_add_action.triggered.connect(lambda: self.open_financials_dialog())
        self.fin_sites_action = QAction(self)
        self.fin_sites_action.triggered.connect(lambda: self.open_info_sites())
        self.fin_paste_action = QAction(self)
        self.fin_paste_action.triggered.connect(lambda: self.open_paste_dialog())
        self.paste_dialog = None  # financials_paste.PasteFiguresDialog
        self.copy_action = QAction(self)
        self.copy_action.triggered.connect(lambda: self.copy_text())
        self.export_action = QAction(self)
        self.export_action.triggered.connect(lambda: self.export())
        self.refresh_menu = QMenu(self)
        self.refresh_actions: dict[str, QAction] = {}
        for section in companies.REFRESH_SECTIONS:
            action = QAction(self)
            action.triggered.connect(lambda _checked=False, s=section: self.build({s}))
            self.refresh_actions[section] = action
            self.refresh_menu.addAction(action)
        for action in (self.registro_open_action, self.registro_enter_action, self.register_action):
            self.menu.addAction(action)
        self.menu.addSeparator()
        self.menu.addAction(self.fin_sites_action)
        self.menu.addAction(self.fin_paste_action)
        self.menu.addAction(self.fin_import_action)
        self.menu.addAction(self.fin_add_action)
        self.menu.addSeparator()
        self.menu.addMenu(self.refresh_menu)
        self.menu.addSeparator()
        self.menu.addAction(self.copy_action)
        self.menu.addAction(self.export_action)
        self.paid_button = QPushButton()
        self.paid_button.setEnabled(False)  # only with the user's own key (Settings)
        self.paid_button.clicked.connect(lambda: self.fetch_paid())
        self.paid_note = ElidedLabel()
        self.paid_note.setProperty("role", "muted")
        self.message = ElidedLabel()
        self.message.setProperty("role", "muted")

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.build_button)
        row.addWidget(self.more_button)
        row.addWidget(self.mail_button)
        row.addWidget(self.cancel_button)
        row.addWidget(self.progress)
        row.addStretch(1)
        paid_row = QHBoxLayout()
        paid_row.setContentsMargins(0, 0, 0, 0)
        paid_row.addWidget(self.paid_button)
        paid_row.addWidget(self.paid_note, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(row)
        # the company name with its links right beside it (website, social networks, searches)
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QLabel

        from .social_links import SocialLinksBar

        self.title_label = QLabel()
        self.title_label.setProperty("role", "title")
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.social_bar = SocialLinksBar(self, opener=opener)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 2, 0, 2)
        title_row.addWidget(self.title_label)
        title_row.addSpacing(10)
        title_row.addWidget(self.social_bar, 1)
        layout.addLayout(title_row)
        layout.addWidget(self.message)
        # Boxes added by other features (add_extra_section), between the title and the card text.
        self.extra_holder = QWidget()
        self.extra_layout = QVBoxLayout(self.extra_holder)
        self.extra_layout.setContentsMargins(0, 0, 0, 0)
        self.extra_layout.setSpacing(4)
        self.extra_holder.hide()
        layout.addWidget(self.extra_holder)
        layout.addWidget(self.browser, 1)
        layout.addLayout(paid_row)
        self.retranslate()

    # --- data

    @property
    def card(self):
        return self._card

    @card.setter
    def card(self, value) -> None:
        self._card = value
        for listener in list(self.card_listeners):
            try:
                listener(value)
            except Exception:  # noqa: BLE001 - a feature box must never break the card
                log.warning("Card listener failed", exc_info=True)

    def add_extra_section(self, widget: QWidget, *, first: bool = False) -> None:
        """Shows `widget` above the card text (`first`: above the other boxes)."""
        if first:
            self.extra_layout.insertWidget(0, widget)
        else:
            self.extra_layout.addWidget(widget)
        self.extra_holder.show()

    def settings(self):
        try:
            return self._settings_provider()
        except Exception:
            from .. import config

            return config.load()

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.social_bar.apply_palette(palette)
        self.render()

    def _update_header(self) -> None:
        """The company name and its links (beside the name)."""
        card = self.card
        self.title_label.setText(card.name if card is not None else "")
        self.title_label.setVisible(card is not None)
        self.social_bar.setVisible(card is not None)
        self.social_bar.set_card(card)

    def show_card(self, card) -> None:
        if card is not None and self.card is not None and card.id != self.card.id:
            self.show_why = False
        self.card = card
        self._update_buttons()
        self.render()
        self._prewarm(card)

    def _prewarm(self, card) -> None:
        """Translates the card's site texts in a worker thread (English interface only),
        then draws the card again."""
        if card is None or not display_text.interface_is_english():
            return
        texts = card_display_texts(card)
        try:
            if not display_text.pending(texts):
                return
        except Exception:  # detection problems must never break the card
            return
        store = self._store
        card_id = card.id

        def redraw(_count) -> None:
            if self.card is not None and self.card.id == card_id:
                self.render()

        run_job(self._run, lambda: display_text.prewarm(texts, store), redraw, lambda _e: None,
                name="translation of a company card")

    def reload(self) -> None:
        if self.card is not None:
            self.card = companies.get(self._store, self.card.id)
        self._update_buttons()
        self.render()

    def chart(self):
        """The FinancialChart that draws the image of section 9 (not shown itself)."""
        if self._chart is None:
            from ..financials.chart import FinancialChart

            self._chart = FinancialChart(None, fin_tr)
        return self._chart

    def chart_png(self, card=None, *, empty_frame: bool = False) -> bytes:
        """PNG of the card's financial chart ('' bytes when there is no figure), e.g. for the
        card export (`export.builders.build_company_card_document(images=[(png, caption)])`).
        With `empty_frame` a card without figures gets the chart frame with its empty state
        (the card view always shows it; exports leave it out)."""
        card = card or self.card
        if card is None:
            return b""
        series = card.financial_series()
        if series.empty and not empty_frame:
            return b""
        result = card.financial_result()
        scope = (card.financials or {}).get("scope")
        chart = self.chart()
        chart.set_series(series, scope if scope in series.scopes() else None,
                         why_missing=result.why_missing(fin_tr), notes=result.notes(fin_tr))
        return chart.render_png(*CHART_SIZE)

    def chart_caption(self, card=None) -> str:
        card = card or self.card
        if card is None or card.financial_series().empty:
            return ""
        self.chart_png(card)
        return self.chart().caption_text()

    def render(self) -> None:
        position = self.browser.verticalScrollBar().value()
        if self.card is None:
            self.browser.setHtml(f"<p>{_e(tr('card.select'))}</p>")
            return
        note = paid_note_text(self.settings())
        chart_url = ""
        try:
            png = self.chart_png(empty_frame=True)
        except Exception:  # the chart must never break the card
            log.warning("Financial chart not drawn", exc_info=True)
            png = b""
        if png:
            image = QImage.fromData(png, "PNG")
            self.browser.document().addResource(QTextDocument.ResourceType.ImageResource, QUrl(CHART_URL), image)
            chart_url = CHART_URL
        service = self.match_service
        if service is not None and service.has_cv():
            service.request(self.card.postings[: companies.MAX_POSTINGS_KEPT], self.render)
        self.browser.setHtml(card_html(self.card, self._palette, show_why=self.show_why, paid_note=note,
                                       chart_url=chart_url, posting_match=self._posting_match,
                                       simulate_links=True))
        self.browser.verticalScrollBar().setValue(position)

    def _posting_match(self, posting: dict):
        """(badge value, tooltip, colour) of "Match with my CV" for a posting of the card."""
        from .cv_match import PENDING, badge_text, tooltip_text

        service = self.match_service
        if service is None:
            return None
        if not service.has_cv():
            return "–", t("match.no_cv"), self._palette.muted
        result = service.cached(posting)
        if result is None:
            return ("–", t("match.not_computable"), self._palette.muted) if service.is_known(posting) else \
                (PENDING, t("match.computing"), self._palette.muted)
        p = self._palette
        colour = p.accent if result.score >= 60 else (p.warning if result.score >= 35 else p.error)
        return badge_text(result), tooltip_text(result), colour

    def _update_buttons(self) -> None:
        has = self.card is not None
        self._update_header()
        self.build_button.setEnabled(has and not self.busy)
        self.more_button.setEnabled(has and not self.busy)
        self.mail_button.setEnabled(has)
        self.build_button.setText(tr("card.refresh") if has and self.card.built else tr("card.build"))
        settings = self.settings()
        self.paid_button.setEnabled(has and not self.busy and paid.can_fetch(settings))
        self.paid_note.setText(paid_note_text(settings))
        self.paid_button.setToolTip(paid_note_text(settings))
        self.cancel_button.setVisible(self.busy)
        self.progress.setVisible(self.busy)
        # the "More" actions are in the menu bar too: they follow the selection
        for action in (self.register_action, self.registro_enter_action, self.fin_import_action,
                       self.fin_add_action, self.fin_paste_action, *self.refresh_actions.values()):
            action.setEnabled(has and not self.busy)
        for action in (self.copy_action, self.export_action, self.registro_open_action, self.fin_sites_action):
            action.setEnabled(has)

    def _anchor(self, url: QUrl) -> None:
        text = url.toString()
        if text == "action:why":
            self.show_why = not self.show_why
            self.render()
            return
        if text == "action:registro_open":
            self.open_registro_imprese()
            return
        if text == "action:registro_enter":
            self.open_registro_dialog()
            return
        if text == "action:fin_import":
            self.import_financials()
            return
        if text == "action:fin_sites":
            self.open_info_sites()
            return
        if text == "action:fin_paste":
            self.open_paste_dialog()
            return
        if text == "action:fin_add":
            self.open_financials_dialog()
            return
        if text.startswith("action:fin_scope:"):
            self.set_financial_scope(text.split(":", 2)[2])
            return
        if text.startswith("action:delete_contact:"):
            self.delete_contact(text.split(":", 2)[2])
            return
        if text.startswith("action:simulate:"):
            log.info("Card: simulate the selection system on the CV")
            if self.card is not None:
                self.on_simulate(self.card.id, text.split(":", 2)[2])
            return
        if text.startswith("match:"):
            self._show_match_tip(text.split(":", 1)[1])
            return
        if text.startswith("mailto:"):
            try:
                self._opener(text)
            except Exception:
                log.warning("Mail program cannot be started", exc_info=True)
            return
        open_url(text, self._opener)

    def _show_match_tip(self, index_text: str) -> None:
        """A click on a "Match with my CV" badge: the skills in common and missing."""
        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QToolTip

        try:
            posting = self.card.postings[int(index_text)]
        except (AttributeError, ValueError, IndexError):
            return
        shown = self._posting_match(posting)
        if shown is not None:
            QToolTip.showText(QCursor.pos(), shown[1], self.browser)

    @staticmethod
    def _ask_box(title: str, text: str) -> bool:
        answer = QMessageBox.question(None, title, text)
        return answer == QMessageBox.StandardButton.Yes

    # --- build

    def _fetcher(self, settings):
        if self._fetcher_factory is not None:
            return self._fetcher_factory(settings, self._store)
        from ..web.fetch import Fetcher

        return Fetcher(settings, self._store)

    def _start(self, cancel: threading.Event) -> None:
        self.busy = True
        self._cancel = cancel
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setToolTip("")
        self._update_buttons()

    def build(self, sections: set[str] | None = None) -> bool:
        """Builds the card (or some sections) in a worker thread."""
        card = self.card
        if card is None or self.busy:
            return False
        cancel = threading.Event()
        self._start(cancel)
        self.batch_count = 0
        self.message.setText(tr("card.building", step=tr("card.step.site")))
        settings = self.settings()
        bridge = self._bridge
        ui_language = "en" if display_text.interface_is_english() else "other"

        def work():
            fetcher = self._fetcher(settings)
            try:
                built = companies.build_card(
                    self._store, card.id, fetcher, settings, cancel=cancel, sections=sections,
                    progress=lambda key, done, total: bridge.progress.emit(key, done, total))
                try:  # site texts in English for an English interface (no-op otherwise)
                    display_text.prewarm(card_display_texts(built), self._store, ui_language=ui_language)
                except Exception:
                    log.debug("Card texts not pre-translated", exc_info=True)
                return built
            finally:
                close = getattr(fetcher, "close", None)
                if callable(close):
                    close()

        log.info("Company card build started (card %s, sections: %s)", card.id,
                 ", ".join(sorted(sections)) if sections else "all")
        run_job(self._run, work, self._built, self._failed, name="company card build",
                timeout=BUILD_TIMEOUT_S, cancel=cancel)
        return True

    def build_many(self, card_ids: list[int]) -> bool:
        """"Create full card" for several cards, one after the other (polite), cancellable."""
        ids = [i for i in dict.fromkeys(card_ids) if i is not None]
        if not ids or self.busy:
            return False
        cancel = threading.Event()
        self._start(cancel)
        self.batch_count = len(ids)
        self._batch_progress(0, len(ids), "", "site")
        self.message.setText(tr("card.batch_step", index=1, count=len(ids), name="", step=tr("card.step.site")))
        settings = self.settings()
        bridge = self._bridge
        ui_language = "en" if display_text.interface_is_english() else "other"

        def work():
            fetcher = self._fetcher(settings)
            try:
                built = companies.build_cards(
                    self._store, ids, fetcher, settings, cancel=cancel,
                    progress=lambda index, count, name, key: bridge.batch.emit(index, count, name, key))
                try:
                    display_text.prewarm([x for c in built for x in card_display_texts(c)], self._store,
                                         ui_language=ui_language)
                except Exception:
                    log.debug("Card texts not pre-translated", exc_info=True)
                return built
            finally:
                close = getattr(fetcher, "close", None)
                if callable(close):
                    close()

        log.info("Batch card build started: %d cards", len(ids))
        run_job(self._run, work, self._batch_built, self._failed, name="company card batch build",
                timeout=BATCH_TIMEOUT_PER_CARD_S * len(ids), cancel=cancel)
        return True

    def _progress(self, key: str, done: int, total: int) -> None:
        if not self.busy or self.batch_count:
            return
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        self.message.setText(tr("card.building", step=tr(f"card.step.{key}")))

    def _batch_progress(self, index: int, count: int, name: str, key: str) -> None:
        if not self.busy:
            return
        # "3/12": the card being built of the batch (the bar shows the cards done).
        self.progress.setRange(0, max(1, count))
        self.progress.setValue(min(index, count))
        self.progress.setFormat(f"{min(index + 1, count)}/{count}")
        self.progress.setTextVisible(True)
        self.progress.setToolTip(tr("card.batch_progress", index=min(index + 1, count), count=count))
        self.message.setText(tr("card.batch_step", index=index + 1, count=count, name=name,
                                step=tr(f"card.step.{key}")))

    def _finish(self) -> None:
        self.busy = False
        self._update_buttons()

    def _built(self, card) -> None:
        cancelled = self._cancel is not None and self._cancel.is_set()
        self._finish()
        if card is not None and self.card is not None and card.id == self.card.id:
            self.card = card
        self.render()
        self._update_buttons()
        self.message.setText(tr("card.cancelled") if cancelled else tr("card.built"))
        log.info("Company card %s %s", getattr(card, "id", "?"), "build stopped" if cancelled else "built")
        if card is not None:
            self._on_changed(card.id)

    def _batch_built(self, cards) -> None:
        cancelled = self._cancel is not None and self._cancel.is_set()
        count = self.batch_count
        self.batch_count = 0
        self._finish()
        if self.card is not None:
            self.card = companies.get(self._store, self.card.id)
        self.render()
        done = len(cards or [])
        self.message.setText(tr("card.batch_cancelled", done=done, count=count) if cancelled
                             else tr("card.batch_done", count=done))
        for card in cards or []:
            self._on_changed(card.id)

    def _failed(self, error: BaseException) -> None:
        self.batch_count = 0
        self._finish()
        log.warning("Company card not built: %s", error)
        self.message.setText(tr("card.failed", error=type(error).__name__))

    def cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    # --- register data, Registro Imprese, paid provider

    def _changed(self, card) -> None:
        self.card = card
        self.render()
        self._on_changed(card.id)

    def save_register(self, values: dict) -> dict:
        if self.card is None:
            return {}
        card, errors = companies.apply_register_data(self._store, self.card.id, values)
        self._changed(card)
        return errors

    def import_register(self, text: str, filename: str = "") -> dict:
        if self.card is None:
            return {}
        card, errors = companies.import_register_csv(self._store, self.card.id, text, filename=filename)
        self._changed(card)
        return errors

    def open_register_dialog(self) -> RegisterDataDialog | None:
        if self.card is None:
            return None
        dialog = RegisterDataDialog(self, self.card, self.save_register, self.import_register)
        dialog.finished.connect(lambda _r: setattr(self, "register_dialog", None))
        self.register_dialog = dialog
        dialog.open()
        return dialog

    def open_registro_imprese(self) -> bool:
        """Opens the free Registro Imprese search in the browser and copies what to search."""
        if self.card is None:
            return False
        query = companies.registro_imprese_query(self.card)
        try:
            QGuiApplication.clipboard().setText(query)
        except Exception:
            log.debug("Clipboard not available", exc_info=True)
        opened = open_url(companies.REGISTRO_IMPRESE_URL, self._opener)
        self.message.setText(tr("card.registro_opened", query=query))
        log.info("Registro Imprese opened for card %s", self.card.id)
        return opened

    def save_registro(self, values: dict) -> dict:
        if self.card is None:
            return {}
        card, errors = companies.apply_registro_imprese(self._store, self.card.id, values)
        self._changed(card)
        if not errors:
            self.message.setText(tr("card.registro_saved"))
        return errors

    def open_registro_dialog(self) -> RegistroImpreseDialog | None:
        if self.card is None:
            return None
        dialog = RegistroImpreseDialog(self, self.card, self.save_registro, self.open_registro_imprese)
        dialog.finished.connect(lambda _r: setattr(self, "registro_dialog", None))
        self.registro_dialog = dialog
        dialog.open()
        return dialog

    def fetch_paid(self) -> bool:
        """The paid lookup: only with the user's key, after a confirmation that shows the cost."""
        card = self.card
        settings = self.settings()
        if card is None or self.busy or not paid.can_fetch(settings):
            return False
        provider = paid.configured_provider(settings)
        cost = tr("card.paid_cost", price=f"{paid.PRICE_SINGLE_EUR:.2f}", from_price=f"{paid.PRICE_FROM_EUR:.2f}")
        if not self._ask(tr("card.paid_confirm_title"),
                         tr("card.paid_confirm", provider=provider.name, name=card.legal_name or card.name,
                            cost=cost)):
            log.info("Paid lookup declined by the user (card %s)", card.id)
            return False
        cancel = threading.Event()
        self._start(cancel)
        self.message.setText(tr("card.paid_running"))
        factory = self._paid_client_factory

        def work():
            client = factory() if factory is not None else None
            try:
                return companies.fetch_paid(self._store, card.id, settings, client=client)
            finally:
                if client is not None and factory is not None:
                    client.close()

        def done(result) -> None:
            self._finish()
            fresh, _errors = result
            self._changed(fresh)
            self.message.setText(tr("card.paid_done"))

        def failed(error: BaseException) -> None:
            self._finish()
            log.warning("Paid lookup failed: %s", getattr(error, "kind", type(error).__name__))
            self.message.setText(tr("card.paid_failed", error=str(error) if isinstance(
                error, paid.PaidRegistryUnavailable) else type(error).__name__))

        run_job(self._run, work, done, failed, name="paid register lookup", timeout=PAID_TIMEOUT_S, cancel=cancel)
        return True

    # --- contacts

    def delete_contact(self, contact_id: str, *, confirm_first: bool = True) -> bool:
        if self.card is None or not contact_id:
            return False
        if confirm_first and not self._ask(tr("card.section.people"), tr("card.contact.confirm_delete")):
            return False
        card = companies.delete_contact(self._store, self.card.id, contact_id)
        self._changed(card)
        self.message.setText(tr("card.contact.deleted"))
        return True

    # --- financials

    def set_financial_scope(self, scope: str) -> None:
        if self.card is None:
            return
        financials = dict(self.card.financials or {})
        financials["scope"] = scope
        self._store.card_update(self.card.id, {"financials": financials})
        self.card = companies.get(self._store, self.card.id)
        self.render()

    def save_financials(self, values: dict) -> str:
        """Saves one hand-typed year; returns '' or the error text."""
        if self.card is None:
            return ""
        from ..financials import parse_amount

        try:
            unit = float(values.get("unit") or 1)
            revenue = parse_amount(values.get("revenue"))
            profit = parse_amount(values.get("profit"))
            employees = parse_amount(values.get("employees"))
            card = companies.add_financial_year(
                self._store, self.card.id, int(values["year"]),
                revenue=revenue * unit if revenue is not None else None,
                profit=profit * unit if profit is not None else None, employees=employees,
                scope=values.get("scope") or "separate", currency=values.get("currency") or "EUR",
                note=values.get("note") or "")
        except (ValueError, TypeError) as error:
            return str(error)
        self._changed(card)
        self.message.setText(tr("card.fin_saved"))
        return ""

    def open_financials_dialog(self) -> FinancialsDialog | None:
        if self.card is None:
            return None
        dialog = FinancialsDialog(self, self.save_financials)
        dialog.finished.connect(lambda _r: setattr(self, "financials_dialog", None))
        self.financials_dialog = dialog
        dialog.open()
        return dialog

    def open_info_sites(self) -> str:
        """"Open company-information sites": a web search for the company's VAT number / tax
        code / name on the public company-information sites, in the browser (the app does
        not read them). Returns the URL opened ("" when nothing identifies the company)."""
        if self.card is None:
            return ""
        from ..financials import FinancialsIdentity
        from ..financials.info_sites import info_search_url

        url = info_search_url(FinancialsIdentity.from_card(self.card))
        if not url:
            self.message.setText(tr("card.fin.no_identity"))
            return ""
        log.info("Company-information search opened for card %s", self.card.id)
        open_url(url, self._opener)
        self.message.setText(tr("card.fin.sites_opened"))
        return url

    def open_paste_dialog(self):
        """"Paste figures…": the paste-and-review window (non-modal)."""
        if self.card is None:
            return None
        from .financials_paste import PasteFiguresDialog

        if self.paste_dialog is not None:
            self.paste_dialog.raise_()
            self.paste_dialog.activateWindow()
            return self.paste_dialog
        card_id = self.card.id
        dialog = PasteFiguresDialog(self, self.card.name,
                                    lambda rows, source, currency, scope:
                                    self.save_pasted(card_id, rows, source, currency, scope),
                                    ask=lambda title, text: self._ask(title, text))
        dialog.finished.connect(lambda _r: setattr(self, "paste_dialog", None))
        self.paste_dialog = dialog
        dialog.open()
        return dialog

    def save_pasted(self, card_id: int, rows: list, source: str, currency: str = "EUR",
                    scope: str = "separate") -> str:
        """Stores the confirmed pasted figures on a worker; the card is redrawn when done."""
        store = self._store

        def work():
            return companies.add_pasted_years(store, card_id, rows, source=source, currency=currency, scope=scope)

        def done(card) -> None:
            if self.card is not None and self.card.id == card_id:
                self._changed(card)
            else:
                self._on_changed(card_id)
            self.message.setText(tr("card.fin_saved"))

        def failed(_error) -> None:
            log.warning("Pasted financial figures not saved")
            self.message.setText(tr("fin.dialog.error", error=tr("error.generic", "an error occurred")))

        run_job(self._run, work, done, failed, name="pasted financial figures")
        return ""

    def import_financials_text(self, text: str, filename: str = "") -> list[str]:
        if self.card is None:
            return []
        card, errors = companies.import_financials_csv(self._store, self.card.id, text, filename=filename)
        self._changed(card)
        self.message.setText(tr("card.fin_csv_errors", errors="; ".join(errors[:3])) if errors
                             else tr("card.fin_saved"))
        return errors

    def import_financials(self, path: str | None = None) -> bool:
        if self.card is None:
            return False
        if path is None:
            path, _filter = QFileDialog.getOpenFileName(self, tr("card.fin_import"), "", tr("card.fin_csv_filter"))
            if not path:
                return False
        raw = Path(path).read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
        self.import_financials_text(text, Path(path).name)
        return True

    # --- copy, export

    def card_text(self, markdown: bool = False) -> str:
        return companies.card_text(self.card, tr, markdown=markdown) if self.card is not None else ""

    def copy_text(self) -> bool:
        if self.card is None:
            return False
        QGuiApplication.clipboard().setText(self.card_text(markdown=False))
        self.message.setText(tr("card.copied"))
        return True

    def export(self, path: str | None = None) -> bool:
        if self.card is None:
            return False
        if path is None:
            safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in self.card.name).strip() or "card"
            path, _filter = QFileDialog.getSaveFileName(self, tr("card.export"), f"{safe}.md",
                                                        "Markdown (*.md);;Text (*.txt)")
            if not path:
                return False
        markdown = not str(path).lower().endswith(".txt")
        Path(path).write_text(self.card_text(markdown=markdown), encoding="utf-8")
        self.message.setText(tr("card.exported", path=str(path)))
        return True

    def retranslate(self) -> None:
        self.social_bar.retranslate()
        self.cancel_button.setText(tr("card.cancel"))
        self.more_button.setText(tr("card.more"))
        self.register_action.setText(tr("card.enter_register"))
        self.registro_open_action.setText(tr("card.registro_open"))
        self.registro_enter_action.setText(tr("card.registro_enter"))
        self.fin_import_action.setText(tr("card.fin_import"))
        self.fin_add_action.setText(tr("card.fin_add"))
        self.fin_sites_action.setText(tr("card.fin.sites"))
        self.fin_paste_action.setText(tr("card.fin.paste"))
        self.copy_action.setText(tr("card.copy"))
        self.export_action.setText(tr("card.export"))
        self.mail_button.setText(t("mail.write"))
        self.mail_button.setToolTip(t("mail.write_tooltip"))
        self.refresh_menu.setTitle(tr("card.refresh_section"))
        names = {"identity": tr("card.section.registry"), "hiring": tr("card.section.hiring"),
                 "products": tr("card.section.products"), "ats": tr("card.section.ats"),
                 "screening": tr("card.section.screening"), "profile": tr("card.section.profile"),
                 "people": tr("card.section.people"), "financials": tr("card.section.financials")}
        for section, action in self.refresh_actions.items():
            action.setText(names[section])
        self.paid_button.setText(tr("card.paid_fetch"))
        if self._chart is not None:
            self._chart.set_tr(fin_tr)
        self._update_buttons()
        self.render()
