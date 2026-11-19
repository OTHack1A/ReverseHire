"""Data model of the regional register: rows produced by the sources, merge priorities,
progress events and the company record returned by queries.

A source parser yields `RegisterRow`s: one company (11-digit tax code, never a 16-character
personal code) with the fields that source knows, in the canonical names of FIELDS.
The register keeps every source's value with its date and picks, per field, the value of
the first source in PRIORITY that has one; a field the user edited is locked to the
user's value.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

log = logging.getLogger(__name__)

# --- source ids -----------------------------------------------------------------------------

RNA, GLEIF, OPENCOESIONE, AGCM, RI_BOUGHT = "rna", "gleif", "opencoesione", "agcm", "ri_bought"
USER, CSV_IMPORT, DERIVED = "user", "csv_import", "derived"
DOWNLOADABLE = (RNA, GLEIF, OPENCOESIONE, AGCM)
ALL_SOURCES = DOWNLOADABLE + (RI_BOUGHT, CSV_IMPORT, DERIVED)

# --- fields ---------------------------------------------------------------------------------

FIELDS = (
    "name", "vat", "legal_form", "address", "postcode", "municipality", "municipality_istat",
    "province", "region", "ateco", "ateco_text", "nace_hints", "size_class", "employees_class",
    "founded", "lei", "website", "status", "legality_rating", "legality_until", "legality_state",
    "lat", "lon", "pec", "rea", "aid_count", "last_aid_date", "aid_measures", "note",
    "email", "phone",     # schema 2: from the user's imported lists (company contacts, never persons)
)
# Fields the user may edit (aid facts belong to the source, not editable).
EDITABLE = tuple(f for f in FIELDS if f not in ("aid_count", "last_aid_date", "aid_measures"))

_DEFAULT_ORDER = (USER, RI_BOUGHT, GLEIF, OPENCOESIONE, AGCM, RNA, CSV_IMPORT, DERIVED)
# Per-field priority (study A.3 step 6 and A.4). The user always wins.
PRIORITY: dict[str, tuple[str, ...]] = {
    "name": (USER, RI_BOUGHT, GLEIF, OPENCOESIONE, RNA, AGCM, CSV_IMPORT),
    "province": (USER, RI_BOUGHT, GLEIF, OPENCOESIONE, AGCM, CSV_IMPORT, DERIVED),
    "municipality": (USER, RI_BOUGHT, GLEIF, AGCM, OPENCOESIONE, CSV_IMPORT, DERIVED),
    "address": (USER, RI_BOUGHT, GLEIF, OPENCOESIONE, CSV_IMPORT),
    "postcode": (USER, RI_BOUGHT, GLEIF, OPENCOESIONE, CSV_IMPORT),
    "ateco": (USER, RI_BOUGHT, OPENCOESIONE, CSV_IMPORT),          # exact codes only
    "ateco_text": (USER, RI_BOUGHT, OPENCOESIONE, CSV_IMPORT),
    "nace_hints": (RNA, CSV_IMPORT),                                # RNA sector: a hint
    "legal_form": (USER, RI_BOUGHT, GLEIF, OPENCOESIONE, CSV_IMPORT),
    "founded": (USER, RI_BOUGHT, GLEIF, CSV_IMPORT),
    "employees_class": (USER, RI_BOUGHT, OPENCOESIONE, CSV_IMPORT),
    "size_class": (USER, RNA, OPENCOESIONE, RI_BOUGHT, CSV_IMPORT),
    "lei": (USER, GLEIF, RI_BOUGHT, CSV_IMPORT),
    "status": (USER, RI_BOUGHT, GLEIF, CSV_IMPORT),
    "legality_rating": (USER, AGCM, CSV_IMPORT),
    "legality_until": (USER, AGCM, CSV_IMPORT),
    "legality_state": (USER, AGCM, CSV_IMPORT),
    "lat": (USER, RI_BOUGHT, AGCM, CSV_IMPORT),
    "lon": (USER, RI_BOUGHT, AGCM, CSV_IMPORT),
    "region": (USER, RI_BOUGHT, GLEIF, OPENCOESIONE, AGCM, RNA, CSV_IMPORT, DERIVED),
    "aid_count": (RNA,),
    "last_aid_date": (RNA,),
    "aid_measures": (RNA,),
    "website": (USER, RI_BOUGHT, CSV_IMPORT),
}


def priority(field_name: str) -> tuple[str, ...]:
    return PRIORITY.get(field_name, _DEFAULT_ORDER)


# --- small normalisers ----------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean_text(value) -> str:
    """Collapse spaces, drop control characters and NBSP, repair common mojibake
    ("SocietÃ\xa0" -> "Società")."""
    text = str(value or "")
    if not text:
        return ""
    if "Ã" in text or "Â" in text:
        try:
            text = text.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    text = _CONTROL.sub("", text).replace("\xa0", " ").replace("�", "")
    return " ".join(text.split())


def company_cf(value) -> str:
    """An 11-digit company tax code ("IT 0030 7140376" -> "00307140376"); "" for anything
    else, including the 16-character personal codes (natural persons are never stored)."""
    text = re.sub(r"[\s.\-/]", "", str(value or "").upper())
    if text.startswith("IT") and len(text) == 13:
        text = text[2:]
    if re.fullmatch(r"\d{11}", text):
        return text
    if re.fullmatch(r"\d{9,10}", text):    # leading zeros lost by a spreadsheet
        return text.zfill(11)
    return ""


def fold(text) -> str:
    """Lower case, no accents, punctuation as spaces (for search and matching)."""
    decomposed = unicodedata.normalize("NFKD", str(text or "").casefold())
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    plain = re.sub(r"[^0-9a-z]+", " ", plain)
    return " ".join(plain.split())


_LEGAL_SUFFIXES = re.compile(
    r"\b(s ?r ?l ?s?|s ?p ?a|s ?a ?s|s ?n ?c|s ?s|s ?c ?a ?r ?l|s ?c ?r ?l|s ?c ?p ?a|s ?a ?p ?a|soc coop|"
    r"societa (a responsabilita limitata( semplificata)?|per azioni|in accomandita semplice|"
    r"in nome collettivo|cooperativa( a responsabilita limitata)?|consortile( a responsabilita limitata)?|"
    r"semplice)|a socio unico|unipersonale|in liquidazione|in breve .*|in sigla .*|o in forma abbreviata .*)\b")


def name_key(name) -> str:
    """Folded name without legal forms ("Rossi S.r.l." -> "rossi"): used only to SUGGEST
    possible duplicates to the user, never to merge."""
    folded = fold(name)
    return " ".join(_LEGAL_SUFFIXES.sub(" ", folded).split())


_FORM_RULES = (
    ("responsabilita limitata semplificata", "S.r.l.s."),
    ("consortile a responsabilita limitata", "Soc. consortile a r.l."),
    ("consortile per azioni", "Soc. consortile per azioni"),
    ("cooperativa", "Soc. coop."),
    ("accomandita per azioni", "S.a.p.a."),
    ("responsabilita limitata", "S.r.l."),
    ("per azioni", "S.p.A."),
    ("accomandita semplice", "S.a.s."),
    ("nome collettivo", "S.n.c."),
    ("consortile", "Soc. consortile"),
    ("consorzio", "Consorzio"),
    ("societa semplice", "S.s."),
    ("fondazione", "Fondazione"),
    ("associazione", "Associazione"),
    ("impresa individuale", "Impresa individuale"),
)
_FORM_ABBREVIATIONS = {
    "srl": "S.r.l.", "srls": "S.r.l.s.", "spa": "S.p.A.", "sas": "S.a.s.", "snc": "S.n.c.",
    "ss": "S.s.", "sapa": "S.a.p.a.", "scarl": "Soc. consortile a r.l.", "scrl": "Soc. coop.",
    "scpa": "Soc. consortile per azioni", "sc": "Soc. coop.", "coop": "Soc. coop.",
}


def legal_form(text) -> str:
    """Short Italian legal form ("Società a responsabilità limitata" -> "S.r.l.");
    the cleaned input when it is not recognised."""
    cleaned = clean_text(text)
    folded = fold(cleaned)
    if not folded:
        return ""
    compact = folded.replace(" ", "")
    if compact in _FORM_ABBREVIATIONS:
        return _FORM_ABBREVIATIONS[compact]
    for needle, short in _FORM_RULES:
        if needle in folded:
            return short
    return cleaned


_PERSON_FORMS = ("privato", "lavoratore autonomo", "persona fisica", "impresa individuale",
                 "imprenditore individuale", "ditta individuale", "libero professionista")


def is_natural_person_form(text) -> bool:
    """True for legal forms of natural persons (sole traders, professionals, private
    individuals): such rows are personal data and are never stored, even when they
    carry an 11-digit VAT number."""
    folded = fold(text)
    return any(form in folded for form in _PERSON_FORMS)


def iso_date(value) -> str:
    """ISO date from "2025-07-29", "29-07-2025", "29/07/2025", "1961-06-05T23:00:00Z"
    (UTC instants late in the evening are the next day in Italy); "" when not a date."""
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})[^Z+\-]*(Z|[+\-]\d{2}:?\d{2})?)?", text)
    if match:
        try:
            day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return ""
        hour, zone = match.group(4), match.group(6)
        if hour is not None and zone in ("Z", "+00:00", "+0000") and int(hour) >= 22:
            day += timedelta(days=1)   # Europe/Rome is UTC+1/+2
        return day.isoformat()
    match = re.match(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", text)
    if match:
        try:
            return date(int(match.group(3)), int(match.group(2)), int(match.group(1))).isoformat()
        except ValueError:
            return ""
    return ""


_NACE = re.compile(r"(?:^|[\s)(])([A-U])[.\-\s]?(\d{2}(?:\.\d{1,2}(?:\.\d{1,2})?)?)\b")


def nace_code(text) -> str:
    """"(NACE 2.1) C.28.2" -> "28.2"; "C-28.29.30" -> "28.29.30"; "28.29.30" -> itself."""
    value = clean_text(text)
    match = _NACE.search(" " + value)
    if match:
        return match.group(2)
    match = re.search(r"\b(\d{2}(?:\.\d{1,2}(?:\.\d{1,2})?)?)\b", value)
    return match.group(1) if match else ""


def division(code: str) -> str:
    match = re.match(r"(\d{2})", code or "")
    return match.group(1) if match else ""


def today_iso() -> str:
    return date.today().isoformat()


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


# --- rows from the sources ------------------------------------------------------------------


@dataclass
class Signal:
    """A dated fact about a company (kind "hiring": an RNA hiring incentive)."""

    kind: str
    date: str
    title: str = ""


@dataclass
class RegisterRow:
    """One company as one source sees it."""

    source: str
    cf: str
    fields: dict[str, str] = field(default_factory=dict)
    signals: list[Signal] = field(default_factory=list)
    source_date: str = ""

    def get(self, name: str, default: str = "") -> str:
        return self.fields.get(name) or default


# --- progress -------------------------------------------------------------------------------


@dataclass
class RegisterProgress:
    """Progress of a download, parse or merge: done/estimated bytes and rows."""

    source: str
    phase: str                       # listing | checking | download | parse | merge | done | cancelled
    file: str = ""
    done_bytes: int = 0
    total_bytes: int | None = None
    rows: int = 0
    rows_estimated: int | None = None
    message: str = ""

    @property
    def fraction(self) -> float | None:
        if self.total_bytes:
            return max(0.0, min(1.0, self.done_bytes / self.total_bytes))
        if self.rows_estimated:
            return max(0.0, min(1.0, self.rows / self.rows_estimated))
        return None


ProgressCallback = Callable[[RegisterProgress], None]


def notify(callback: ProgressCallback | None, event: RegisterProgress) -> None:
    """Calls a progress callback; a failing callback never stops the work."""
    if callback is None:
        return
    try:
        callback(event)
    except Exception:  # noqa: BLE001
        log.debug("Register progress callback failed", exc_info=True)


class CancelledError(Exception):
    """The user cancelled (internal: never raised to the caller of the public API)."""


def check_cancel(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise CancelledError()


__all__ = [
    "AGCM", "ALL_SOURCES", "CSV_IMPORT", "CancelledError", "DERIVED", "DOWNLOADABLE", "EDITABLE", "FIELDS",
    "GLEIF", "OPENCOESIONE", "PRIORITY", "ProgressCallback", "RI_BOUGHT", "RNA", "RegisterProgress",
    "RegisterRow", "Signal", "USER", "check_cancel", "clean_text", "company_cf", "division", "fold",
    "iso_date", "legal_form", "name_key", "nace_code", "notify", "priority",
]
