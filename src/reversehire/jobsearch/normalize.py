"""Shared finishing of a posting from any source: contract / remote / salary (as
published) through `web.posting_attrs`, working time, skills, language, and the
employer hints used to estimate the client behind an agency ad."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from . import lexicon
from ._util import fold, log
from .models import Posting

_CLIENT_PHRASE = re.compile(
    r"[^.\n]*(?:per (?:conto di |importante |nota |solida |azienda |realta |storica |prestigios)|"
    r"nostro cliente|azienda cliente|on behalf of|our client)[^.\n]*", re.I)
_HOURS_STRUCTURED = {"FULL_TIME": "full_time", "PART_TIME": "part_time"}
# Legal / agency boilerplate: never a skill the ad asks for.
_BOILERPLATE = re.compile(
    r"privacy|gdpr|regolamento \(?ue\)?|d\.?\s?lgs|dlgs|decreto legislativo|legge n|l\.\s?\d+/\d+|"
    r"aut\.?\s?min|autorizzazione ministeriale|art\.?\s?1[34]\b|informativa|diversity|inclusiv|"
    r"pari opportunit|equal opportunit|ricerca e selezione|agenzia per il lavoro|divisione specializzata|"
    r"rivolt[oa] a (?:persone|candidat)|ambo i sessi|both sexes|pdr 125", re.I)


def skill_text(text: str) -> str:
    """The text without legal / agency boilerplate sentences (for skill extraction)."""
    parts = re.split(r"(?<=[.;!?])\s+|\n+", text or "")
    return "\n".join(p for p in parts if p and not _BOILERPLATE.search(p))


def employer_hints(text: str) -> dict:
    """Sector, CCNL, size and "client" phrases of an ad (only what it says)."""
    hints: dict = {}
    sectors = lexicon.sector_hints(text)
    if sectors:
        hints["sectors"] = [label for label, _ in sectors]
        hints["sector_divisions"] = sorted({d for _, divisions in sectors for d in divisions})
    ccnl = lexicon.ccnl_hint(text)
    if ccnl:
        hints["ccnl"] = ccnl[0]
    band, employees = lexicon.size_hint(text)
    if band:
        hints["size"] = band
    if employees:
        hints["employees"] = employees
    phrases = [" ".join(m.group(0).split())[:200] for m in _CLIENT_PHRASE.finditer(text or "")][:3]
    if phrases:
        hints["client_phrases"] = phrases
    return hints


def hours_from(employment_type, text: str = "") -> str | None:
    values = employment_type if isinstance(employment_type, list) else [employment_type]
    for value in values:
        key = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
        if key in _HOURS_STRUCTURED:
            return _HOURS_STRUCTURED[key]
    folded = fold(text)
    for phrase, value in lexicon.HOURS_WORDS:
        if phrase in folded:
            return value
    return None


def finish(posting: Posting, full_text: str | None = None, *, employment_type=None, workplace=None,
           salary=None, hours_text: str = "") -> Posting:
    """Fill the empty attributes of `posting` from structured values and its text."""
    text = full_text if full_text is not None else posting.description
    try:
        from ..web.posting_attrs import detect_attributes

        attributes = detect_attributes(text or "", title=posting.title, location=posting.location,
                                       employment_type=employment_type, workplace=workplace, salary=salary)
        for name in ("contract", "remote", "salary", "salary_min", "salary_max", "salary_period",
                     "salary_currency"):
            value = getattr(attributes, name, None)
            if value is not None and getattr(posting, name) is None:
                setattr(posting, name, value)
    except Exception as error:  # attribute detection is best effort
        log.info("Posting attributes not read: %s", type(error).__name__)
    if posting.hours is None:
        posting.hours = hours_from(employment_type, hours_text + "\n" + posting.title + "\n" + (text or "")[:3000])
    if not posting.skills:
        posting.skills = lexicon.extract_skills(posting.title + "\n" + skill_text(text or ""))
    if posting.language is None:
        posting.language = lexicon.detect_language(text or posting.description)
    if not posting.employer_hints:
        posting.employer_hints = employer_hints(text or "")
    if posting.company and lexicon.is_agency_name(posting.company) and not posting.is_agency:
        posting.is_agency = True
        posting.agency = posting.agency or posting.company
        posting.company = None
    if posting.fetched_at is None:
        posting.fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return posting
