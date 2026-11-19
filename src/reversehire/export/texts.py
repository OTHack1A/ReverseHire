"""English defaults of every text of the export package and the `tr` adapter.

`tr(key, default)` is the callable the integrator plugs in (the app's i18n). Keys that
start with `card.`, `source.` and `identity.reason.` are the ones the company card
already uses, so the locale files already translate them; the `export.*` keys are new.
"""

from __future__ import annotations

from typing import Callable

Translate = Callable[[str, str], str]

# --- new keys (export.*) -------------------------------------------------------------------
EXPORT_TEXTS: dict[str, str] = {
    "export.menu": "Export",
    "export.menu_card": "Export company card…",
    "export.menu_application": "Export application…",
    "export.preview_title": "Preview",
    "export.preview_card_title": "Preview: company card",
    "export.preview_application_title": "Preview: application",
    "export.print": "Print…",
    "export.save_pdf": "Save PDF",
    "export.save_excel": "Save Excel",
    "export.save_word": "Save Word",
    "export.close": "Close",
    "export.working": "Working…",
    "export.saved": "Saved: {path}",
    "export.printed": "Sent to the printer.",
    "export.failed": "Export failed: {error}",
    "export.print_failed": "Printing failed: {error}",
    "export.no_printer": "No default printer is available.",
    "export.dialog.pdf": "Save as PDF",
    "export.dialog.excel": "Save as Excel",
    "export.dialog.word": "Save as Word",
    "export.filter.pdf": "PDF files (*.pdf)",
    "export.filter.excel": "Excel workbooks (*.xlsx)",
    "export.filter.word": "Word documents (*.docx)",
    "export.card_title": "Company card: {name}",
    "export.application_title": "Application: {name}",
    "export.letter_to": "To: {name}",
    "export.letter_subject": "Subject: {subject}",
    "export.letter_contact": "Contact: {contact}",
    "export.cv_title": "Curriculum vitae",
    "export.cv_note": "CV file: {file}",
    "export.cv_note_pages": "CV file: {file} ({pages} pages). In the PDF and in print the CV pages follow the letter.",
    "export.cv_note_missing": "CV file not found: {file}",
    "export.cv_none": "No CV attached.",
    "export.private_banner": "PRIVATE REMINDER: NOT TO BE SENT",
    "export.private_title": "Company card (private reminder, not to be sent)",
    "export.sources": "Sources",
    "export.col.field": "Field",
    "export.col.value": "Value",
    "export.col.source": "Source",
    "export.col.link": "Link",
    "export.sheet.letter": "Letter",
    "export.sheet.cv": "CV",
    "export.sheet.sources": "Sources",
    "export.footer": "Created with ReverseHire on {date}",
}

# --- card texts used by the builders (same keys as gui/company_card.py) ----------------------
CARD_TEXTS: dict[str, str] = {
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
    "card.field.careers": "Careers page",
    "card.field.status": "Status",
    "card.missing_paid": "Missing: available from the paid register: {fields}",
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
    "card.conflicts": "Conflicts: {fields}",
    "card.not_built": "Not checked yet.",
    # sections 7-9 (same keys and texts as gui/company_card.py)
    "card.section.profile": "7. Presentation and history",
    "card.section.people": "8. People and contacts",
    "card.section.financials": "9. Financials",
    "card.presentation": "Presentation",
    "card.history": "History",
    "card.no_presentation": "No presentation text found on the pages read.",
    "card.no_history": "No history text found on the pages read.",
    "card.profile_note": "Sentences taken from the company's own pages, not rewritten.",
    "card.people_none": "No contact found on the pages read. Large companies usually take applications only "
                        "through their careers portal.",
    "card.contact.kind.hr": "HR",
    "card.contact.kind.management": "Management",
    "card.contact.kind.general": "General",
    "card.contact.kind.privacy": "Privacy / data protection (not for applications)",
    "card.fin_source": "Source",
    "fin.dialog.year": "Fiscal year",
    "card.fin_employees": "Employees: {value}",
    "source.website": "website",
    "source.website_jsonld": "website (schema.org)",
    "source.vies": "VIES",
    "source.gleif": "GLEIF",
    "source.wikidata": "Wikidata",
    "source.register": "register",
    "source.card": "card",
}

TEXTS: dict[str, str] = {**CARD_TEXTS, **EXPORT_TEXTS}


def _late_defaults() -> dict[str, str]:
    """Advice and identity-reason texts live next to the code that produces the keys."""
    out: dict[str, str] = {}
    try:
        from ..web.screening import ADVICE_TEXTS

        out.update({f"card.advice.{k}": v for k, v in ADVICE_TEXTS.items()})
    except Exception:  # pragma: no cover - optional
        pass
    try:
        from ..registry import confirm

        out.update({f"identity.reason.{k}": v for k, v in confirm.REASON_TEXTS.items()})
    except Exception:  # pragma: no cover - optional
        pass
    try:
        from ..financials.strings import STRINGS

        out.update({k: v for k, v in STRINGS.items() if k not in TEXTS})
    except Exception:  # pragma: no cover - optional
        pass
    return out


TEXTS.update(_late_defaults())


def default_text(key: str, fallback: str | None = None) -> str:
    return TEXTS.get(key, fallback if fallback is not None else key)


def make_t(tr: Translate | None) -> Callable[..., str]:
    """`t(key, fallback=None, **params)`: translation (or English default), formatted."""
    def t(key: str, fallback: str | None = None, **params) -> str:
        default = default_text(key, fallback)
        text = default
        if tr is not None:
            try:
                text = tr(key, default) or default
            except Exception:
                text = default
        if params:
            for candidate in (text, default):
                try:
                    return candidate.format(**params)
                except (KeyError, IndexError, ValueError):
                    continue
            return default
        return text

    return t
