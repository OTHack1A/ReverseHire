"""Pure builders: company card / application -> ExportDocument. No Qt, no I/O except the
page count of the CV PDF (read with pypdf, failures ignored).

Rules: a field that is unknown stays blank (""), nothing is invented; every value that has
a provenance fact carries its source (name + link) and a numbered footnote.
"""

from __future__ import annotations

import datetime as _dt
import time
from pathlib import Path
from typing import Callable

from .model import (
    Bullets, ExportDocument, Footnote, Image, Paragraph, Row, Rows, Section, safe_stem,
)
from .texts import Translate, make_t

MAX_POSTINGS = 30
REGISTRY_FIELDS = ("legal_form", "registered_office", "operating_site", "town", "province", "postcode",
                   "pec", "phone", "email", "share_capital", "founded", "employees", "lei")
HEADER_FIELDS = ("legal_name", "rea", "ateco", "tax_code", "vat")
CARD_SECTIONS = ("header", "registry", "hiring", "products", "ats", "screening", "profile", "people", "financials")
Chart = tuple[bytes, str]  # (PNG of the financial chart, caption)


class _Notes:
    """Numbered footnotes, one per distinct (source, link)."""

    def __init__(self, t: Callable[..., str]):
        self.t = t
        self._items: list[Footnote] = []

    def add(self, source: str, url: str = "", name: str | None = None) -> Footnote | None:
        if not source and not url:
            return None
        name = name or (self.t(f"source.{source}", source) if source else "")
        for note in self._items:
            if note.text == name and note.url == url:
                return note
        note = Footnote(len(self._items) + 1, name, url)
        self._items.append(note)
        return note

    @property
    def items(self) -> list[Footnote]:
        return list(self._items)


def _date(stamp) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(stamp)) if stamp else ""


def _s(value) -> str:
    return "" if value is None else str(value)


def _fact_row(label: str, value: str, fact: dict | None, notes: _Notes) -> Row:
    value = _s(value)
    if not value or not fact:
        return Row(label, value)
    source = _s(fact.get("source"))
    url = _s(fact.get("url"))
    note = notes.add(source, url)
    return Row(label, value, note.text if note else "", url, note.number if note else None)


def _links_rows(card, t, notes: _Notes) -> list[Row]:
    """The company's links (web/social.py) as rows: the network, the URL and where the link
    was found (a footnote); searches are labelled as such and carry no source."""
    from ..web import social

    try:
        links = social.links_for_card(card)
    except Exception:  # noqa: BLE001 - a link list must never break an export
        return []
    rows = []
    for link in links:
        name = t(f"social.network.{link.network}", social.NAMES.get(link.network, link.network))
        if link.is_search:
            rows.append(Row(t("social.search_label", "{name} (search)", name=name), link.url))
            continue
        note = notes.add(link.source, link.source_url) if link.source not in ("", "card") else None
        rows.append(Row(name, link.url, note.text if note else "", link.source_url, note.number if note else None))
    return rows


def _checked(t, stamp) -> str:
    day = _date(stamp)
    return t("card.checked_on", date=day) if day else ""


def _card_sections(card, t, notes: _Notes, chart: Chart | None = None) -> list[Section]:
    sections: list[Section] = []

    # 1. header ----------------------------------------------------------------------------
    status = card.identity_status or "unverified"
    blocks: list = [Paragraph(t("card.identity_line", status=t(f"card.status.{status}", status.upper())), "bold")]
    identity = card.identity or {}
    reasons = [t(f"identity.reason.{r}", str(r)) for r in identity.get("reasons", [])]
    if reasons:
        blocks.append(Bullets(reasons))
    conflicts = [c.get("field", "") for c in identity.get("conflicts", []) if isinstance(c, dict)]
    if conflicts:
        names = ", ".join(t(f"card.field.{c}", c) for c in conflicts)
        blocks.append(Paragraph(t("card.conflicts", fields=names), "note"))
    rows = [Row(t("card.field.name"), card.name)]
    for name in HEADER_FIELDS:
        value = card.value_of(name)
        rows.append(_fact_row(t(f"card.field.{name}"), value, card.source_of(name) if value else None, notes))
    blocks.append(Rows(rows))
    sections.append(Section("header", t("card.section.header"), blocks, checked=_checked(t, card.identity_checked)))
    # the company's links, as in the card header (own section: the header keeps only the identity)
    links = _links_rows(card, t, notes)
    if links:
        sections.append(Section("links", t("social.title", "Links"), [Rows(links)]))

    # 2. registry --------------------------------------------------------------------------
    rows = [Row(t("card.field.website"), card.website or card.site or "")]
    for name in REGISTRY_FIELDS:
        item = (card.registry or {}).get(name)
        fact = item if isinstance(item, dict) else (item[0] if isinstance(item, list) and item else None)
        rows.append(_fact_row(t(f"card.field.{name}"), card.registry_value(name), fact, notes))
    blocks = [Rows(rows)]
    missing = card.missing_paid
    if missing:
        names = ", ".join(t(f"card.field.{m}", m) for m in missing)
        blocks.append(Paragraph(t("card.missing_paid", fields=names), "muted"))
    sections.append(Section("registry", t("card.section.registry"), blocks,
                            checked=_checked(t, card.registry_checked)))

    # 3. profiles sought -------------------------------------------------------------------
    hiring = card.hiring or {}
    blocks = [Rows([Row(t("card.field.careers"), _s(hiring.get("careers_url") or card.careers_url))])]
    postings = card.postings
    if postings:
        items = []
        for posting in postings[:MAX_POSTINGS]:
            attrs = [_s(posting.get(k)).replace("_", " ") for k in ("contract", "remote", "salary", "travel")
                     if posting.get(k)]
            parts = [p for p in [_s(posting.get("location"))] + attrs if p]
            text = _s(posting.get("title"))
            if parts:
                text += " (" + ", ".join(parts) + ")"
            if posting.get("url"):
                text += " - " + _s(posting.get("url"))
            items.append(text)
        blocks.append(Bullets(items))
    elif card.hiring_checked or card.hiring:
        blocks.append(Paragraph(t("card.no_postings"), "muted"))
    else:
        blocks.append(Paragraph(t("card.not_built"), "muted"))
    if hiring.get("spontaneous"):
        blocks.append(Paragraph(t("card.spontaneous")))
    sections.append(Section("hiring", t("card.section.hiring"), blocks, checked=_checked(t, card.hiring_checked)))

    # 4. products --------------------------------------------------------------------------
    products = card.products or {}
    blocks = []
    if products.get("summary"):
        text = _s(products["summary"])
        if products.get("summary_url"):
            text += f" ({products['summary_url']})"
        blocks.append(Paragraph(text))
    if products.get("items"):
        blocks.append(Bullets([(_s(i.get("name")) + (f" - {i['url']}" if i.get("url") else "")).strip()
                               for i in products["items"] if isinstance(i, dict)]))
    if not products:
        blocks.append(Paragraph(t("card.not_built"), "muted"))
    sections.append(Section("products", t("card.section.products"), blocks,
                            checked=_checked(t, card.products_checked)))

    # 5. selection system ------------------------------------------------------------------
    platforms = (card.ats or {}).get("platforms", [])
    blocks = []
    if not card.ats:
        blocks.append(Paragraph(t("card.not_built"), "muted"))
    elif not platforms:
        blocks.append(Paragraph(t("card.ats_none")))
    items = []
    for platform in platforms:
        key = platform.get("key", "")
        if key in ("email", "form"):
            text = t(f"card.channel.{key}")
        else:
            text = _s(platform.get("name") or key)
            if platform.get("offer"):
                text += f": {platform['offer']}"
        if platform.get("url"):
            text += f" - {platform['url']}"
        if platform.get("sources"):
            text += " [" + t("card.vendor_sources", date=_s(platform.get("reviewed")),
                              urls=", ".join(platform["sources"])) + "]"
        items.append(text)
    if items:
        blocks.append(Bullets(items))
    if any(p.get("offer") for p in platforms):
        blocks.append(Paragraph(t("card.platform_note"), "muted"))
    sections.append(Section("ats", t("card.section.ats"), blocks, checked=_checked(t, card.ats_checked)))

    # 6. CV screening ----------------------------------------------------------------------
    screening = card.screening or {}
    blocks = []
    if not screening:
        blocks.append(Paragraph(t("card.not_built"), "muted"))
    else:
        parsing = screening.get("parsing", "unknown")
        ai = screening.get("ai", "no_evidence")
        blocks.append(Paragraph(t("card.parsing", value=t(f"card.parsing.{parsing}", parsing))))
        blocks.append(Paragraph(t("card.ai", value=t(f"card.ai.{ai}", ai.replace("_", " "))), "bold"))
        statements = screening.get("statements", [])
        if statements:
            blocks.append(Paragraph(t("card.statements"), "bold"))
            blocks.append(Bullets([
                f"“{_s(s.get('quote'))}” ({t('card.statement.' + _s(s.get('kind') or 'mentions'), 'mentions')}"
                + (f", {s['url']}" if s.get("url") else "") + ")" for s in statements]))
        else:
            blocks.append(Paragraph(t("card.no_statements"), "muted"))
        advice = screening.get("advice", [])
        if advice:
            blocks.append(Paragraph(t("card.advice"), "bold"))
            blocks.append(Bullets([t(f"card.advice.{a}", a) for a in advice]))
    sections.append(Section("screening", t("card.section.screening"), blocks,
                            checked=_checked(t, card.screening_checked)))
    sections.append(_profile_section(card, t))
    sections.append(_people_section(card, t, notes))
    sections.append(_financials_section(card, t, notes, chart))
    return sections


def _with_url(text: str, url) -> str:
    return f"{text} ({url})" if url else text


def _profile_section(card, t) -> Section:
    """7. Presentation and history: the company's own sentences, verbatim, with their page."""
    blocks: list = []
    if not getattr(card, "profile", None):
        blocks.append(Paragraph(t("card.not_built"), "muted"))
    else:
        for key, entry, empty in (("card.presentation", card.presentation, "card.no_presentation"),
                                  ("card.history", card.history, "card.no_history")):
            blocks.append(Paragraph(t(key), "bold"))
            text = " ".join(_s(entry.get("text")).split())
            blocks.append(Paragraph(_with_url(text, entry.get("url")) if text else t(empty), "normal" if text
                                    else "muted"))
        blocks.append(Paragraph(t("card.profile_note"), "note"))
    return Section("profile", t("card.section.profile"), blocks,
                   checked=_checked(t, getattr(card, "profile_checked", None)))


def _people_section(card, t, notes: _Notes) -> Section:
    """8. People and contacts: one row per contact, kind as label, the page as source."""
    blocks: list = []
    contacts = getattr(card, "contacts_list", []) or []
    if not getattr(card, "people", None):
        blocks.append(Paragraph(t("card.not_built"), "muted"))
    elif not contacts:
        blocks.append(Paragraph(t("card.people_none"), "muted"))
    else:
        rows = []
        for contact in contacts:
            kind = _s(contact.get("kind") or "general")
            value = " · ".join(_s(contact.get(k)).strip() for k in ("name", "role", "email", "phone")
                               if _s(contact.get(k)).strip())
            origin = {"source": contact.get("origin") or "website", "url": contact.get("source_url") or ""}
            rows.append(_fact_row(t(f"card.contact.kind.{kind}", kind), value, origin, notes))
        blocks.append(Rows(rows))
    return Section("people", t("card.section.people"), blocks,
                   checked=_checked(t, getattr(card, "people_checked", None)))


def _financials_section(card, t, notes: _Notes, chart: Chart | None) -> Section:
    """9. Financials: one row per fiscal year (revenue, profit, employees, source) and the
    chart image; nothing is estimated: without figures the reason is printed."""
    from ..financials.chart import format_amount

    def fin_t(key: str, default: str) -> str:
        return t(key, default)

    blocks: list = []
    series = card.financial_series()
    if series.empty:
        result = card.financial_result()
        why = result.why_missing(fin_t) if result.reports else ""
        text = why or (t("card.not_built") if not getattr(card, "financials_checked", None)
                       else t("fin.chart.empty"))
        blocks.extend(Paragraph(line, "muted") for line in text.splitlines() if line.strip())
    else:
        scopes = series.scopes()
        stored = (card.financials or {}).get("scope")
        chosen = stored if stored in scopes else (scopes[0] if scopes else None)
        rows = []
        for record in reversed(series.view(chosen).rows):
            parts = []
            if record.revenue is not None:
                parts.append(f"{t('fin.chart.revenue')}: {format_amount(record.revenue, record.currency, fin_t)}")
            if record.profit is not None:
                parts.append(f"{t('fin.chart.profit')}: {format_amount(record.profit, record.currency, fin_t)}")
            if record.employees is not None:
                parts.append(f"{t('fin.chart.employees')}: {record.employees:.0f}")
            source = record.source.split(":")[0]
            name = t(f"fin.source.{source}", record.source, name=record.source)
            note = notes.add(source, _s(record.source_url), name=name)
            rows.append(Row(f"{t('fin.dialog.year')} {record.year}", " · ".join(parts), name,
                            _s(record.source_url), note.number if note else None))
        blocks.append(Rows(rows))
        if chosen and len(scopes) > 1:
            blocks.append(Paragraph(f"{t('card.fin_scope', 'Scope:')} {t('fin.scope.' + chosen, chosen)}", "muted"))
        for line in card.financial_result().notes(fin_t):
            blocks.append(Paragraph(line, "note"))
    if chart and chart[0]:
        blocks.append(Image(chart[0], chart[1]))
    employees = card.registry_value("employees")
    if employees:
        blocks.append(Paragraph(t("card.fin_employees", value=employees)))
    return Section("financials", t("card.section.financials"), blocks,
                   checked=_checked(t, getattr(card, "financials_checked", None)))


def _labels(t) -> dict[str, str]:
    return {
        "field": t("export.col.field"), "value": t("export.col.value"), "source": t("export.col.source"),
        "link": t("export.col.link"), "sources": t("export.sources"),
        "sheet_letter": t("export.sheet.letter"), "sheet_cv": t("export.sheet.cv"),
        "sheet_sources": t("export.sheet.sources"),
        "footer": t("export.footer", date=_dt.date.today().isoformat()),
    }


def build_company_card_document(card, *, tr: Translate | None = None, today: _dt.date | None = None,
                                images: list[tuple[bytes, str]] | None = None,
                                chart: Chart | None = None) -> ExportDocument:
    """All nine card sections in the owner's order. `chart`: (png bytes, caption) of the
    financial chart, shown in section 9 (e.g. `CompanyCardView.chart_png/chart_caption`);
    `images`: optional further images appended after the sections."""
    t = make_t(tr)
    notes = _Notes(t)
    sections = _card_sections(card, t, notes, chart)
    for png, caption in images or []:
        sections.append(Section("image", "", [Image(png, caption)]))
    day = (today or _dt.date.today()).isoformat()
    return ExportDocument(
        title=t("export.card_title", name=card.name), kind="card", sections=sections,
        footnotes=notes.items, file_stem=safe_stem(f"company-card-{card.name}", "company-card"),
        created=day, labels=_labels(t))


def _cv_pages(path: Path) -> int | None:
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:
        return None


def _address_of(card) -> str:
    return (card.registry_value("registered_office") or card.address or "").strip()


def build_application_document(card, letter_subject: str, letter_body: str, cv_pdf_path: str | Path | None = None,
                               *, signature_name: str = "", contact: str | None = None,
                               sender_lines: tuple[str, ...] | list[str] = (), date_text: str | None = None,
                               tr: Translate | None = None, today: _dt.date | None = None,
                               chart: Chart | None = None) -> ExportDocument:
    """Letter (page 1), CV, then the company card as a private appendix.

    `contact`: recipient contact line; None takes the card's own "contacts" field.
    `date_text`: the date as printed in the letter; None uses today's ISO date.
    """
    t = make_t(tr)
    day = (today or _dt.date.today()).isoformat()

    # --- letter
    blocks: list = []
    if sender_lines:
        blocks.append(Paragraph("\n".join(x for x in sender_lines if x), "normal"))
    blocks.append(Paragraph(date_text if date_text is not None else day, "right"))
    recipient = [card.legal_name or card.name]
    address = _address_of(card)
    if address:
        recipient.append(address)
    contact_text = (card.contacts if contact is None else contact) or ""
    contact_text = " ".join(contact_text.split())
    if contact_text:
        recipient.append(t("export.letter_contact", contact=contact_text))
    blocks.append(Paragraph("\n".join(recipient), "normal"))
    if letter_subject.strip():
        blocks.append(Paragraph(t("export.letter_subject", subject=letter_subject.strip()), "subject"))
    for chunk in [c.strip("\n") for c in (letter_body or "").replace("\r\n", "\n").split("\n\n")]:
        if chunk.strip():
            blocks.append(Paragraph(chunk.strip()))
    if signature_name.strip():
        blocks.append(Paragraph(signature_name.strip(), "bold"))
    sections = [Section("letter", "", blocks, kind="letter")]

    # --- CV (the PDF pages are appended by the PDF/print renderers)
    cv_path = Path(cv_pdf_path) if cv_pdf_path else None
    if cv_path is None:
        cv_blocks = [Paragraph(t("export.cv_none"), "muted")]
    elif not cv_path.is_file():
        cv_blocks = [Paragraph(t("export.cv_note_missing", file=cv_path.name), "muted")]
    else:
        pages = _cv_pages(cv_path)
        cv_blocks = [Paragraph(t("export.cv_note_pages", file=cv_path.name, pages=pages) if pages
                               else t("export.cv_note", file=cv_path.name), "muted")]
    sections.append(Section("cv", t("export.cv_title"), cv_blocks, kind="cv"))

    # --- private appendix: the card
    notes = _Notes(t)
    card_sections = _card_sections(card, t, notes, chart)
    card_sections[0].banner = t("export.private_banner")
    card_sections[0].title = t("export.private_title")
    card_sections[0].page_break_before = True
    sections.extend(card_sections)

    document = ExportDocument(
        title=t("export.application_title", name=card.name), kind="application", sections=sections,
        footnotes=notes.items, cv_pdf_path=cv_path if cv_path and cv_path.is_file() else None,
        file_stem=safe_stem(f"application-{card.name}", "application"), created=day, labels=_labels(t))
    # the header section keeps its original heading as a subtitle row of the appendix
    card_sections[0].blocks.insert(0, Paragraph(t("card.section.header"), "bold"))
    return document
