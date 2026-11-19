"""Legal identifiers printed on a company's OWN website (sources study, section A.5).

Legal basis: art. 2250 c.c. para. 7 (S.p.A./S.r.l. websites must show registered office,
Registro Imprese office and number, share capital) and art. 35 DPR 633/1972 (VAT
number on the home page). The data is therefore a legitimate, first-party identity
source, usually in the footer, the contacts page or the "note legali" / privacy pages.

The study measured a home-page-only hit rate of about 2/5, so `scan_identity` reads the
home page plus at most MAX_EXTRA_PAGES internal pages chosen by link text and URL, one of
each kind first: legal notice / company information, contacts, about ("chi siamo",
"azienda"), history ("storia"), careers ("lavora con noi"), team / management and
privacy; or a few common paths when no such link exists. Legal and privacy pages are
read only while identifiers are still missing; about, history, careers, team and
contacts pages are read anyway (they feed the presentation, history and contacts of the
card, see web/profile.py and web/people.py). When the home page is not Italian and the
site declares an Italian version (hreflang="it" or a link "IT"/"Italiano"), the Italian
home is read too and its links come first: the data of the Italian legal entity of a
multinational usually sits in the Italian section. Pages go through the shared Fetcher
(robots.txt, per-host delay, page cache), so pages already read by the site analysis
cost nothing.

Extracted: partita IVA (checksum), codice fiscale (11 digits or 16 characters, checked),
Registro Imprese number (= CF), REA ("BO-472965"), share capital, PEC, e-mail, phone,
registered office (street, postcode, town, province), operating sites, founding year
and size hints, plus schema.org Organization JSON-LD (legalName, vatID, taxID, leiCode,
address, sameAs, foundingDate, numberOfEmployees), schema.org microdata, `<address>`
elements, the flags of art. 2250 c.c. ("unico socio", "in liquidazione") and the company
exercising "direzione e coordinamento" (art. 2497-bis c.c.), and the company's social
links (LinkedIn company page and others: kept as links only, never read). Each value keeps
the page URL and where on the page it was read ("footer", "header", "JSON-LD",
"microdata", "address element", "page text"): the provenance shown on the card.
A partita IVA or codice fiscale with a wrong check digit is discarded (logged, and kept
only as an "invalid_id" note).
"""

from __future__ import annotations

import html as html_module
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urljoin, urlsplit

from ..registry.checksums import PROVINCES, cf_ok, normalize_rea, normalize_vat, piva_ok
from .jsonld import _jsonld_blocks, _load
from .text import analyze_html, extract_links, normalize, same_site, url_key, visible_text

log = logging.getLogger(__name__)

MAX_EXTRA_PAGES = 6
SOURCE = "website"
SOURCE_JSONLD = "website_jsonld"
# Kinds of extra pages, in the order they are chosen (one of each kind first).
KIND_ORDER = ("legal", "contacts", "about", "history", "careers", "team", "privacy")
# Kinds read only while legal identifiers are still missing.
IDENTITY_KINDS = ("legal", "privacy")
# Story-telling pages: values of the legal block read in their body text are not trusted.
NARRATIVE_KINDS = ("history", "about", "team", "careers")
NARRATIVE_SKIP = ("share_capital", "registered_office", "operating_site", "pec", "rea", "single_member",
                  "in_liquidation", "parent_company")
# Where on a page a value was read (provenance).
WHERE_FOOTER, WHERE_HEADER, WHERE_TEXT = "footer", "header", "page text"
WHERE_JSONLD, WHERE_MICRODATA, WHERE_ADDRESS, WHERE_LINK = "JSON-LD", "microdata", "address element", "link"
WHERE_META = "meta description"

_PROV = "|".join(sorted(PROVINCES))
_LABEL_VAT = r"P(?:artita)?\.?\s*I(?:\.?\s*V\.?\s*A)?\.?|VAT(?:\s*(?:No|number|n|code|ID|Reg(?:istration)?))?\.?"
_LABEL_CF = r"C(?:odice)?\.?\s*F(?:iscale)?\.?|Tax\s*(?:code|ID)|Fiscal\s*code"
_LABELS = rf"(?:{_LABEL_VAT}|{_LABEL_CF})"
# "P.IVA 01234567890", "C.F. e P.IVA: IT01234567890", "VAT No. IT…", "CF/PIVA …"
_IDS = re.compile(
    rf"\b(?P<labels>{_LABELS}(?:\s*(?:e|and|/|&|-|,)\s*{_LABELS})*)\s*"
    r"(?:n\.?|n°|nr\.?|no\.?|number)?\s*[:.\-n°º#]*\s*(?:IT\s*)?(?P<number>\d{11})\b",
    re.I,
)
_PERSONAL_CF = re.compile(
    rf"\b(?:{_LABEL_CF})\s*[:.\-n°º]*\s*([A-Z]{{6}}[0-9LMNPQRSTUV]{{2}}[A-Z][0-9LMNPQRSTUV]{{2}}[A-Z]"
    r"[0-9LMNPQRSTUV]{3}[A-Z])\b",
    re.I,
)
_REG_IMP = re.compile(
    r"(?:Reg(?:istro)?\.?\s*Imp(?:rese)?\.?|Registro delle Imprese|companies register|"
    r"Register of Companies|Business Register)[^0-9]{0,40}?(\d{11})\b",
    re.I,
)
_REA = re.compile(
    r"\bR\.?\s*E\.?\s*A\.?\s*(?:n\.?|n°|nr\.?|num(?:ero)?\.?|no\.?)?\s*[:.\-]?\s*"
    rf"(?:({_PROV})\s*[-–/ ]?\s*)?(\d{{4,7}})\b",
    re.I,
)
_REA_OF_TOWN = re.compile(
    r"\bR\.?\s*E\.?\s*A\.?\s*(?:di|of|presso(?: la)? CCIAA di)\s+([A-Za-zÀ-ÿ' -]{3,30}?)\s*"
    r"(?:n\.?|n°|nr\.?|num(?:ero)?\.?|no\.?)?\s*[:.\-]?\s*(\d{4,7})\b",
    re.I,
)
_CAPSOC = re.compile(
    r"(?:Cap(?:itale)?\.?\s*Soc(?:iale)?\.?|Share capital)[^0-9€]{0,30}(?:€|euro|eur)?\s*"
    r"(\d{1,3}(?:[.,'’]\d{3})+(?:,\d{2})?|\d+(?:,\d{2})?)\s*(?:€|euro|eur)?\s*(i\.?\s*v\.?|interamente versato|fully paid)?",
    re.I,
)
_CAPITAL_LABEL = re.compile(r"Cap(?:itale)?\.?\s*Soc(?:iale)?\.?|Share capital", re.I)
_AMOUNT = re.compile(
    r"(?:€|euro|eur)?\s*(\d{1,3}(?:,\d{3})+(?:\.\d{2})?(?!\d)|\d{1,3}(?:[.'’]\d{3})+(?:,\d{2})?|\d+(?:[,.]\d{2})?)"
    r"\s*(?:€|euro|eur)?\s*"
    r"(i\.?\s*v\.?(?=[\s,;.)|]|$)|interamente versato|fully paid(?:[- ]up)?)?", re.I)
_PEC_DOMAINS = (r"pec|legalmail|postacert|cert|pecimprese|arubapec|registerpec|sicurezzapostale|"
                r"gigapec|mypec|pec-mail|cgn|pec\.it|casellapec|messaggipec")
_PEC = re.compile(rf"[\w.+-]+@(?:[\w-]+\.)*(?:{_PEC_DOMAINS})\.[a-z.]+", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,10}\b", re.I)
_PHONE = re.compile(
    r"(?:\bTel(?:efono)?\.?|\bPhone|\bT\.|\bPh\.)\s*[:.]?\s*(\+?\s?\d[\d\s./()-]{6,18}\d)", re.I
)
_OFFICE = re.compile(
    r"(?:Sede\s+(?:legale|sociale)(?:\s+e\s+(?:amministrativa|operativa))?|Registered\s+(?:office|address)|"
    r"Head\s*quarters?|Headquarters|Head\s+office)\s*[:\-–]?\s*(.{5,160}?)"
    r"(?=\s*(?:[-–|•·,;]\s*)?(?:P\.?\s*IVA|Partita|C\.?\s*F\.?|Cod(?:ice)?\.?\s*Fisc|Cap(?:itale)?\.?\s*Soc|"
    r"Share capital|Tel|Phone|Fax|R\.?\s*E\.?\s*A\b|Reg(?:istro)?\.?\s*Imp|Iscri|PEC\b|E-?mail|VAT|"
    r"Tax\s*code|Fiscal\s*code|Codice\s*fiscale|"
    r"Sede\s+operativa|Stabilimento|Copyright|©|$))",
    re.I,
)
_SITE = re.compile(
    r"(?:Sede\s+operativa|Sede\s+amministrativa|Stabilimento|Unit[aà]\s+locale|Operational\s+(?:site|office)|"
    r"Plant)\s*[:\-–]?\s*(.{5,120}?)(?=\s*(?:[-–|•·;]\s*)?(?:P\.?\s*IVA|Tel|Phone|Fax|Sede|"
    r"Stabilimento|C\.?\s*F\.?|R\.?\s*E\.?\s*A\b|PEC\b|E-?mail|$))",
    re.I,
)
_POSTCODE_TOWN = re.compile(r"\b(\d{5})\s*[-–,]?\s*([A-Za-zÀ-ÿ'][A-Za-zÀ-ÿ' .-]{1,40}?)\s*"
                            rf"(?:\(\s*({_PROV})\s*\)|[-–,]\s*({_PROV})\b|\s({_PROV})\b|(?=[,;.]|\s*$|\s+-))")
# The first word of an Italian street address ("Via", "V.le", "Piazza", "Loc.", "Strada"...).
_STREET = re.compile(r"\b(?:Via|Viale|V\.\s?le|Piazza|Piazzale|P\.\s?za|P\.le|Corso|C\.so|Largo|Vicolo|Strada|"
                     r"Str\.|Località|Localita|Loc\.|Frazione|Fraz\.|Contrada|C\.da|Borgo|Galleria|Lungo\w+|"
                     r"Zona\s+Industriale|Z\.\s?I\.|Circonvallazione|Traversa|Salita|Rotonda)\b", re.I)
# A privacy notice quotes the supervisory authority and its PEC: never the company's PEC.
_AUTHORITY = re.compile(r"Garante|autorit[aà]\s+(?:di\s+controllo|garante)|supervisory\s+authority|"
                        r"data\s+protection\s+authority", re.I)
_AUTHORITY_PEC = re.compile(r"@pec\.gpdp\.it$|@gpdp\.it$", re.I)


def street_part(text: str) -> str:
    """'EVO. Vai alle certificazioni Bonfiglioli S.P.A. Via Cav. X 1, 40012 Calderara' ->
    'Via Cav. X 1, 40012 Calderara': text in front of the first street word is dropped
    (menu words, company name); the text is kept as it is when it has no street word."""
    match = _STREET.search(text or "")
    if match and match.start() > 0:
        return text[match.start():].strip(" ,-–|")
    return (text or "").strip()
_FOUNDED = re.compile(
    r"\b(?:fondat[aoie]|nata|nato|costituit[aoie]|founded|established|since|dal)\s+"
    r"(?:nel\s+|in\s+)?(1[89]\d{2}|20[0-2]\d)\b",
    re.I,
)
_EMPLOYEES = re.compile(
    r"\b(?:oltre|più di|piu di|circa|over|more than|about|around)?\s*"
    r"(\d{1,3}(?:[.,]\d{3})+|\d{2,6})\s*\+?\s*(?:dipendenti|collaboratori|addetti|persone|employees|people)\b",
    re.I,
)
_LEGAL_FORM = re.compile(
    r"\b(S\.?\s?p\.?\s?A\.?|S\.?\s?r\.?\s?l\.?\s?s\.?|S\.?\s?r\.?\s?l\.?|S\.?\s?n\.?\s?c\.?|S\.?\s?a\.?\s?s\.?|"
    r"S\.?\s?a\.?\s?p\.?\s?a\.?|S\.?\s?c\.?\s?a\.?\s?r\.?\s?l\.?|"
    r"Soc(?:ietà|ieta'?|\.)\s+Coop(?:erativa)?\.?(?:\s+a\s+r\.?\s?l\.?)?)"
    r"(?=$|[\s,;)\-–])",
    re.I,
)
_NAME_BEFORE_ID = re.compile(
    r"([A-Z0-9À-Ý][\w&'.À-ÿ -]{1,80}?\s(?i:S\.?\s?p\.?\s?A\.?|S\.?\s?r\.?\s?l\.?(?:\s?s\.?)?|S\.?\s?n\.?\s?c\.?|"
    r"S\.?\s?a\.?\s?s\.?|S\.?\s?a\.?\s?p\.?\s?a\.?))(?=[\s,;\-–|]|$)",
)
# Words that precede a company name in footers and legal pages but are not part of it.
_NAME_PREFIX_WORDS = {
    "note", "legali", "legal", "notice", "notes", "copyright", "rights", "reserved", "tutti",
    "diritti", "riservati", "home", "contatti", "contacts", "privacy", "policy", "cookie", "informativa",
    "dati", "societari", "imprint", "credits", "powered", "realizzato", "chi", "siamo", "about",
}
_FORMS = {"spa": "S.p.A.", "srl": "S.r.l.", "srls": "S.r.l.s.", "snc": "S.n.c.", "sas": "S.a.s.",
          "sapa": "S.a.p.a.", "scarl": "S.c.a.r.l."}

# Town (as printed in "R.E.A. di Bologna") -> province code, for the most common cases.
_TOWN_PROVINCE = {
    "bologna": "BO", "modena": "MO", "reggio emilia": "RE", "reggio nell'emilia": "RE", "parma": "PR",
    "ferrara": "FE", "ravenna": "RA", "forli": "FC", "forli-cesena": "FC", "forli cesena": "FC",
    "cesena": "FC", "rimini": "RN", "piacenza": "PC", "milano": "MI", "torino": "TO", "roma": "RM",
    "napoli": "NA", "firenze": "FI", "venezia": "VE", "verona": "VR", "padova": "PD", "vicenza": "VI",
    "treviso": "TV", "brescia": "BS", "bergamo": "BG", "genova": "GE", "bari": "BA", "palermo": "PA",
    "ancona": "AN", "pesaro": "PU", "pesaro e urbino": "PU", "mantova": "MN", "cremona": "CR",
    "trento": "TN", "bolzano": "BZ", "udine": "UD", "trieste": "TS", "como": "CO", "varese": "VA",
    "monza": "MB", "monza e brianza": "MB", "lecco": "LC", "pavia": "PV", "novara": "NO", "cuneo": "CN",
    "alessandria": "AL", "asti": "AT", "perugia": "PG", "pisa": "PI", "livorno": "LI", "lucca": "LU",
    "prato": "PO", "arezzo": "AR", "siena": "SI", "lodi": "LO", "sondrio": "SO",
}

# Link text / path -> score: pages that usually carry the company data.
_LINK_WORDS: list[tuple[str, int]] = [
    ("note legali", 6), ("legal notice", 6), ("legal notes", 6), ("legal information", 6),
    ("informazioni legali", 6), ("dati societari", 6), ("company data", 6), ("company information", 5),
    ("corporate information", 5), ("informazioni societarie", 6), ("imprint", 6), ("impressum", 6),
    ("colophon", 5), ("societa trasparente", 6), ("trasparenza", 4), ("legal", 4), ("credits", 2),
    ("contatti", 4), ("contattaci", 4), ("contacts", 4), ("contact us", 4), ("contact", 3),
    ("dove siamo", 3), ("privacy policy", 3), ("privacy", 3), ("informativa", 2),
    ("chi siamo", 4), ("about us", 4), ("about", 3), ("azienda", 3), ("l'azienda", 4), ("company", 3),
    ("who we are", 4), ("profilo aziendale", 4), ("il gruppo", 3), ("the group", 3),
    ("storia", 4), ("la nostra storia", 5), ("history", 4), ("our history", 5), ("heritage", 3),
    ("our story", 4), ("lavora con noi", 4), ("careers", 4), ("career", 3), ("carriere", 4),
    ("jobs", 3), ("work with us", 4), ("join us", 3), ("join our team", 3), ("candidature", 3),
    ("team", 3), ("management", 3), ("leadership", 3), ("organigramma", 3), ("governance", 3),
    ("cookie policy", 1),
]
_LINK_PATH = re.compile(
    r"(?:^|[/_.-])(note-?legali|legal(?:-?notice|-?notes|-?info)?|imprint|impressum|dati-?societari|colophon|"
    r"company-?(?:data|info(?:rmation)?)|corporate-?info(?:rmation)?|societa-?trasparente|contatti|contacts?|"
    r"contact-?us|privacy(?:-?policy)?|informativa|chi-?siamo|about(?:-?us)?|azienda|storia|history|heritage|"
    r"lavora-?con-?noi|careers?|carriere|jobs|team|management|leadership)(?:$|[/_.-])",
    re.I,
)
# Path segments of a language section ("/en/", "/it-it/").
_LANG_SEGMENT = re.compile(r"^/([a-z]{2})(?:[-_][a-z]{2})?(?:/|$)", re.I)
_ITALIAN_SWITCH = re.compile(r"^(?:it|ita|italiano|italian)$", re.I)
COMMON_PATHS = ("/note-legali", "/contatti", "/chi-siamo", "/privacy-policy", "/legal", "/contacts", "/privacy")
_NON_PAGE = (".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".svg", ".mp4", ".xml")


@dataclass
class Found:
    """One identifier or datum read on a page."""

    field: str
    value: str
    url: str
    source: str = SOURCE
    context: str = ""
    where: str = ""


@dataclass
class IdentityScan:
    """Everything found on the site, with the pages read (text kept for the screening scan)."""

    site: str
    found: list[Found] = field(default_factory=list)
    pages: list[dict] = field(default_factory=list)  # {"url", "kind", "text", "html"}
    pages_read: int = 0
    date: str = field(default_factory=lambda: date.today().isoformat())

    def values(self, name: str) -> list[str]:
        result: list[str] = []
        for item in self.found:
            if item.field == name and item.value not in result:
                result.append(item.value)
        return result

    def first(self, name: str) -> str:
        values = self.values(name)
        return values[0] if values else ""

    def url_of(self, name: str, value: str | None = None) -> str:
        for item in self.found:
            if item.field == name and (value is None or item.value == value):
                return item.url
        return ""

    def facts(self) -> list[dict]:
        """Provenance facts (field, value, source, url, date), one per distinct value."""
        seen: set[tuple[str, str, str]] = set()
        result = []
        for item in self.found:
            key = (item.field, item.value, item.source)
            if key in seen:
                continue
            seen.add(key)
            fact = {"field": item.field, "value": item.value, "source": item.source,
                    "url": item.url, "date": self.date}
            if item.where:
                fact["where"] = item.where
            result.append(fact)
        return result

    @property
    def complete(self) -> bool:
        """VAT, REA, PEC and registered office all found: no need to read more pages."""
        return all(self.values(n) for n in ("vat", "rea", "pec", "registered_office"))


def legal_form_of(name: str) -> str:
    """'I.M.A. Industria Macchine Automatiche S.p.A.' -> 'S.p.A.'; '' if not stated."""
    found = None
    for found in _LEGAL_FORM.finditer(name or ""):
        pass
    if found is None:
        return ""
    key = re.sub(r"[^a-z]", "", found.group(1).lower())
    if key.startswith("soc"):
        return "Soc. Coop."
    return _FORMS.get(key, found.group(1))


def clean_company_name(text: str) -> str:
    """'© 2026 Note legali Esempio S.r.l.' -> 'Esempio S.r.l.': drops years, symbols and the
    menu/footer words in front of the name; '' when only the legal form is left."""
    words = " ".join((text or "").split()).strip(" -–,|").split(" ")
    while words and (re.fullmatch(r"[\W\d_]+", words[0]) or normalize(words[0]).strip(".:") in _NAME_PREFIX_WORDS):
        words.pop(0)
    name = " ".join(words)
    return name if name and not _LEGAL_FORM.fullmatch(name) else ""


def _unique_add(found: list[Found], item: Found) -> None:
    if item.value and not any(f.field == item.field and f.value == item.value and f.source == item.source
                              for f in found):
        found.append(item)


def _town_and_province(text: str) -> tuple[str, str, str]:
    """(postcode, town, province) from an address text; empty strings when absent."""
    for match in _POSTCODE_TOWN.finditer(text or ""):
        town = " ".join(match.group(2).split()).strip(" .-,")
        if not town or re.fullmatch(r"(?i)(?:tel|fax|p|c|via|italy|italia)", town):
            continue
        province = (match.group(3) or match.group(4) or match.group(5) or "").upper()
        return match.group(1), town, province
    return "", "", ""


def _clean_capital(number: str, paid: str | None) -> str:
    value = " ".join(number.split())
    return f"EUR {value}" + (" fully paid" if paid else "")


def _jsonld_organizations(html: str, tree=None) -> list[dict]:
    items: list[dict] = []

    def visit(node, depth=0):
        if depth > 10:
            return
        if isinstance(node, list):
            for element in node:
                visit(element, depth + 1)
        elif isinstance(node, dict):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            names = {str(k).split("/")[-1].lower() for k in kinds if k}
            if names & {"organization", "corporation", "localbusiness", "onlinebusiness",
                        "manufacturer", "professionalservice", "store"}:
                items.append(node)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    visit(value, depth + 1)

    for block in _jsonld_blocks(html, tree):
        visit(_load(block))
    return items


def _text_of(value) -> str:
    if isinstance(value, str):
        return " ".join(html_module.unescape(value).split())
    if isinstance(value, dict):
        return _text_of(value.get("name") or value.get("@value") or value.get("value") or "")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def social_link(url: str) -> tuple[str, str] | None:
    """('linkedin', 'https://www.linkedin.com/company/esempio') for a company profile link,
    None for anything else (share buttons, personal profiles, posts). The networks and the
    profile-vs-share rules are in web/social.py (also GitHub, XING, Glassdoor/Indeed company
    pages, Wikipedia); the Wikidata item is collected separately ("wikidata")."""
    from .social import classify

    found = classify(url)
    if found is None or found[0] == "wikidata":
        return None
    return found


def extract_from_jsonld(html: str, url: str, tree=None) -> list[Found]:
    """Identifiers and data of the schema.org Organization blocks of a page."""
    found: list[Found] = []
    for org in _jsonld_organizations(html, tree):
        add = lambda f, v: _unique_add(found, Found(f, v, url, SOURCE_JSONLD, where=WHERE_JSONLD))  # noqa: E731
        legal = _text_of(org.get("legalName"))
        if legal:
            add("name", legal)
            if legal_form_of(legal):
                add("legal_form", legal_form_of(legal))
        for key in ("vatID", "taxID", "identifier", "iso6523Code"):
            raw = _text_of(org.get(key))
            number = normalize_vat(raw)
            if piva_ok(number):
                add("vat" if key == "vatID" else "tax_code" if key == "taxID" else "vat", number)
            elif key == "taxID" and cf_ok(raw.strip().upper()):
                add("tax_code", raw.strip().upper())
            elif key in ("vatID", "taxID") and re.fullmatch(r"(?:IT)?\s*\d{11}", raw.strip(), re.I):
                log.info("Website identifier discarded (wrong check digit): %s on %s (JSON-LD)", number, url)
                add("invalid_id", number)
        lei = _text_of(org.get("leiCode")).upper()
        if re.fullmatch(r"[A-Z0-9]{18}\d{2}", lei):
            add("lei", lei)
        address = org.get("address")
        if isinstance(address, list):
            address = address[0] if address else None
        if isinstance(address, dict):
            street = _text_of(address.get("streetAddress"))
            postcode = _text_of(address.get("postalCode"))
            town = _text_of(address.get("addressLocality"))
            region = _text_of(address.get("addressRegion")).upper()
            country = _text_of(address.get("addressCountry")).upper()
            foreign = country not in ("", "IT", "ITA", "ITALY", "ITALIA")
            if street and not foreign:
                add("registered_office", street)
            if re.fullmatch(r"\d{5}", postcode) and not foreign:
                add("postcode", postcode)
            if town and not foreign:
                add("town", town)
            if region in PROVINCES:
                add("province", region)
        for key, name in (("email", "email"), ("telephone", "phone")):
            value = _text_of(org.get(key)).replace("mailto:", "").replace("tel:", "")
            if value:
                add("pec" if name == "email" and _PEC.fullmatch(value) else name, value)
        founded = re.search(r"(1[89]\d{2}|20[0-2]\d)", _text_of(org.get("foundingDate")))
        if founded:
            add("founded", founded.group(1))
        employees = org.get("numberOfEmployees")
        if isinstance(employees, dict):
            employees = employees.get("value") or employees.get("maxValue") or employees.get("minValue")
        if _text_of(employees):
            add("employees", _text_of(employees))
        same_as = org.get("sameAs")
        for link in same_as if isinstance(same_as, list) else [same_as]:
            if not isinstance(link, str):
                continue
            if "wikidata.org/" in link:
                qid = re.search(r"(Q\d+)", link)
                if qid:
                    add("wikidata", qid.group(1))
            social = social_link(link)
            if social:
                add(social[0] if social[0] == "linkedin" else "social", social[1])
    return found


_MICRODATA = {"vatid": "vat", "taxid": "tax_code", "legalname": "name", "streetaddress": "registered_office",
              "postalcode": "postcode", "addresslocality": "town", "addressregion": "province",
              "telephone": "phone", "email": "email", "foundingdate": "founded", "numberofemployees": "employees",
              "leicode": "lei"}


def extract_microdata(tree, url: str) -> list[Found]:
    """schema.org microdata (itemprop="vatID", "streetAddress"...) of a page."""
    found: list[Found] = []
    if tree is None:
        return found
    for element in tree.xpath("//*[@itemprop]"):
        name = _MICRODATA.get(str(element.get("itemprop") or "").strip().lower())
        if not name:
            continue
        raw = element.get("content") or element.get("href") or " ".join(element.itertext())
        value = " ".join(html_module.unescape(str(raw)).split()).replace("mailto:", "").replace("tel:", "")
        if not value:
            continue
        if name in ("vat", "tax_code"):
            number = normalize_vat(value)
            if piva_ok(number):
                value = number
            elif name == "tax_code" and cf_ok(value.upper()):
                value = value.upper()
            else:
                log.info("Website identifier discarded (wrong check digit): %s on %s (microdata)", value, url)
                continue
        elif name == "postcode" and not re.fullmatch(r"\d{5}", value):
            continue
        elif name == "province":
            if value.upper() not in PROVINCES:
                continue
            value = value.upper()
        elif name == "founded":
            year = re.search(r"(1[89]\d{2}|20[0-2]\d)", value)
            if not year:
                continue
            value = year.group(1)
        elif name == "email" and _PEC.fullmatch(value):
            name = "pec"
        elif name == "lei" and not re.fullmatch(r"[A-Z0-9]{18}\d{2}", value.upper()):
            continue
        _unique_add(found, Found(name, value, url, SOURCE_JSONLD, where=WHERE_MICRODATA))
    return found


def compact_text(text: str) -> str:
    return " ".join(html_module.unescape(text or "").split())


def _region_texts(tree) -> tuple[str, str]:
    """(footer text, header text) of a page, whitespace collapsed for `in` tests."""
    if tree is None:
        return "", ""
    footer: list[str] = []
    header: list[str] = []
    for element in tree.iter():
        tag = element.tag if isinstance(element.tag, str) else ""
        if tag in ("script", "style", "noscript", "template"):
            continue
        marker = f"{element.get('id') or ''} {element.get('class') or ''} {element.get('role') or ''}".lower()
        if tag == "footer" or re.search(r"\bfooter\b|contentinfo|colophon|site-?info", marker):
            footer.append(" ".join(element.itertext()))
        elif tag == "header" or re.search(r"(?:^|\s)(?:site-)?header(?:\s|$)|\bbanner\b", marker):
            header.append(" ".join(element.itertext()))
    return compact_text(" ".join(footer)), compact_text(" ".join(header))


# "Società soggetta all'attività di direzione e coordinamento di Alfa S.p.A." (art. 2497-bis c.c.)
_DIRECTION = re.compile(
    r"(?:direzione\s+e\s+coordinamento\s+(?:(?:da\s+parte\s+)?(?:di|della|del|dello)\s+|:\s*)|"
    r"(?:management\s+(?:control\s+)?and\s+coordination|direction\s+and\s+coordination)\s+(?:of|by)\s+)"
    r"([A-Z0-9][\w&'.\- ]{1,80}?[\s.](?:S\.?\s?p\.?\s?A\.?|S\.?\s?r\.?\s?l\.?|S\.?\s?a\.?\s?p\.?\s?a\.?|"
    r"plc|AG|SE|GmbH|Inc\.?|Ltd\.?|N\.?V\.?|B\.?V\.?))(?=[\s,;.)|]|$)",
    re.I,
)
_SINGLE_MEMBER = re.compile(r"\b(?:con\s+)?(?:unico\s+socio|socio\s+unico)\b|\bunipersonale\b|single[- ]member|"
                            r"sole[- ]shareholder", re.I)
_LIQUIDATION = re.compile(r"\bin\s+liquidazione\b|\bin\s+liquidation\b", re.I)
_SDI = re.compile(r"\b(?:Codice\s+(?:Destinatario|SDI|Univoco)|SDI)\s*[:.\-]?\s*([A-Z0-9]{7})\b")
# A group text names ANOTHER company right before an identifier: "controllata da X S.p.A., P.IVA ..."
_GROUP_ID_BEFORE = re.compile(
    r"(?:direzione\s+e\s+coordinamento|controllat[ao]\s+da|capogruppo|parent\s+company|socio\s+unico|unico\s+socio)"
    r"\b[^.;|]{0,90}?(?:S\.?\s?p\.?\s?A\.?|S\.?\s?r\.?\s?l\.?|S\.?\s?a\.?\s?p\.?\s?a\.?|plc|AG|SE|GmbH|Inc\.?|"
    r"Ltd\.?|N\.?V\.?|B\.?V\.?)\s*[,;:\-–(]*\s*(?:con\s+sede\s+[^,]{0,40},\s*)?$", re.I)


def extract_identifiers(html: str, url: str, text: str | None = None, tree=None) -> list[Found]:
    """Every identifier and company datum found on one page (visible text, JSON-LD,
    microdata, `<address>` elements and social links)."""
    if tree is None:
        tree = analyze_html(html)
    text = text if text is not None else visible_text(html)
    found: list[Found] = []
    footer, header = _region_texts(tree)

    def where_of(snippet: str) -> str:
        probe = compact_text(snippet)
        if probe and footer and probe in footer:
            return WHERE_FOOTER
        if probe and header and probe in header:
            return WHERE_HEADER
        return WHERE_TEXT

    def add(name: str, value: str, context: str = "", where: str | None = None) -> None:
        _unique_add(found, Found(name, value, url, SOURCE, context[:200],
                                 where if where is not None else WHERE_TEXT))

    def around(match: re.Match, width: int = 120) -> str:
        return text[max(0, match.start() - width): match.end() + 20]

    def at(match: re.Match) -> str:
        return where_of(match.group(0))

    for match in _IDS.finditer(text):
        labels, number = match.group("labels"), match.group("number")
        if not piva_ok(number):
            log.info("Website identifier discarded (wrong check digit): %s on %s", number, url)
            add("invalid_id", number, around(match), at(match))
            continue
        has_vat = bool(re.search(_LABEL_VAT, labels, re.I))
        has_cf = bool(re.search(_LABEL_CF, labels, re.I))
        before = text[max(0, match.start() - 160): match.start()]
        # the identifier of ANOTHER company of the group: printed right after the name of the
        # parent company / sole shareholder ("... coordinamento di Holding S.p.A., P.IVA ...").
        # "... coordinamento di Holding S.p.A. - Cap. Soc. ... - P.I. <n>" is the company's own.
        if _GROUP_ID_BEFORE.search(before):
            add("group_vat", number, around(match), at(match))
            continue
        if has_vat:
            add("vat", number, around(match), at(match))
        if has_cf:
            add("tax_code", number, around(match), at(match))
        name = None
        for name in _NAME_BEFORE_ID.finditer(text[max(0, match.start() - 140): match.start()]):
            pass
        if name is not None:
            cleaned = clean_company_name(name.group(1))
            if cleaned:
                add("name", cleaned, around(match), at(match))
    for match in _PERSONAL_CF.finditer(text):
        code = match.group(1).upper()
        if cf_ok(code):
            add("tax_code", code, around(match), at(match))
    for match in _REG_IMP.finditer(text):
        if piva_ok(match.group(1)):
            add("tax_code", match.group(1), around(match), at(match))
    for match in _REA.finditer(text):
        rea = normalize_rea(match.group(2), (match.group(1) or "").upper())
        if rea:
            add("rea", rea, around(match), at(match))
    for match in _REA_OF_TOWN.finditer(text):
        town = normalize(match.group(1)).strip()
        rea = normalize_rea(match.group(2), _TOWN_PROVINCE.get(town, ""))
        if rea:
            add("rea", rea, around(match), at(match))
    for label in _CAPITAL_LABEL.finditer(text):
        window = text[label.end(): label.end() + 170]
        amounts = [m for m in _AMOUNT.finditer(window)
                   if (re.search(r"[.,'’]\d{3}", m.group(1)) or re.search(r"€|eur", m.group(0), re.I))
                   and not re.match(r"\s*(?:di\s+)?(?:lire|lira|ITL|L\.)\b", window[m.end():], re.I)]
        if not amounts or amounts[0].start() > 80:
            continue
        # "deliberato 2.000.000, sottoscritto e versato 1.500.000 i.v.": the paid-up amount
        paid = [m for m in amounts if m.group(2)] or [
            m for m in amounts if re.search(r"versat[oa]|paid", window[max(0, m.start() - 30): m.start()], re.I)]
        match = (paid or amounts)[0]
        snippet = text[label.start(): label.end() + match.end()]
        # "Share capital fully paid-in: € 22,470,504.68": the wording before the amount
        fully = match.group(2) or (re.search(r"fully\s+paid|interamente\s+versato|\bi\.?\s?v\.?\b",
                                             window[: match.start()], re.I) and "i.v.")
        add("share_capital", _clean_capital(match.group(1), fully), snippet, where_of(snippet))
        break
    for match in _PEC.finditer(text):
        context = text[max(0, match.start() - 160): match.start()]
        if _AUTHORITY.search(context) or _AUTHORITY_PEC.search(match.group(0)):
            continue  # the PEC of the privacy authority (Garante) quoted in a privacy notice
        add("pec", match.group(0).lower().rstrip("."), around(match), at(match))
    for match in _OFFICE.finditer(text):
        office = " ".join(match.group(1).split()).strip(" -–,:;|")
        office = re.sub(r"^(?:at|in|a|presso)\s+", "", office, flags=re.I).strip(" ,")
        office = street_part(office)
        if len(office) >= 5 and re.search(r"\d", office):
            where = at(match)
            add("registered_office", office, around(match), where)
            postcode, town, province = _town_and_province(office)
            if postcode:
                add("postcode", postcode, where=where)
            if town:
                add("town", town, where=where)
            if province:
                add("province", province, where=where)
    for match in _SITE.finditer(text):
        site = street_part(" ".join(match.group(1).split()).strip(" -–,:;|"))
        if len(site) >= 5 and re.search(r"\d", site) and _STREET.search(site):
            add("operating_site", site, around(match), at(match))
    for match in _PHONE.finditer(text):
        add("phone", " ".join(match.group(1).split()), where=at(match))
        break
    for match in _FOUNDED.finditer(text):
        add("founded", match.group(1), around(match), at(match))
        break
    for match in _EMPLOYEES.finditer(text):
        add("employees", match.group(1), around(match), at(match))
        break
    if tree is not None and not any(f.field == "employees" for f in found):
        # "... circa 14.000 dipendenti ..." stated only in the page description
        for element in tree.iter("meta"):
            key = (element.get("name") or element.get("property") or "").lower()
            if key in ("description", "og:description"):
                content = compact_text(element.get("content") or "")
                match = _EMPLOYEES.search(content)
                if match:
                    add("employees", match.group(1), content[:200], WHERE_META)
                    break
    for match in _DIRECTION.finditer(text):
        parent = clean_company_name(match.group(1))
        if parent:
            add("parent_company", parent, around(match), at(match))
            break
    if any(f.field in ("vat", "tax_code", "rea", "share_capital") for f in found):
        # the flags of art. 2250 c.c. belong to the company data block
        for match in _SINGLE_MEMBER.finditer(text):
            add("single_member", "yes", around(match), at(match))
            break
        for match in _LIQUIDATION.finditer(text):
            add("in_liquidation", "yes", around(match), at(match))
            break
    for match in _SDI.finditer(text):
        code = match.group(1)
        if re.search(r"\d", code) and re.search(r"[A-Z]", code) or code == "0000000":
            add("sdi_code", code, around(match), at(match))
            break
    # <address> elements: an address shown by the company (not necessarily its registered office)
    if tree is not None:
        for element in tree.iter("address"):
            value = compact_text(" ".join(element.itertext()))
            value = re.split(rf"\s*[-–|,]?\s*\b(?:{_LABELS}|R\.?\s*E\.?\s*A\b|Cap(?:itale)?\.?\s*Soc)", value,
                             maxsplit=1, flags=re.I)[0].strip(" ,-–|")
            if 8 <= len(value) <= 300 and _POSTCODE_TOWN.search(value):
                add("address", street_part(value)[:200], value, WHERE_ADDRESS)
    # e-mail addresses from mailto: links and text (PEC ones already listed)
    mails = [(m.group(0), at(m)) for m in _EMAIL.finditer(text)]
    if tree is not None:
        for a in tree.iter("a"):
            href = html_module.unescape(a.get("href") or "").strip()
            if href.lower().startswith("mailto:"):
                mails.append((href[7:].split("?")[0], WHERE_LINK))
            social = social_link(href)
            if social:
                add(social[0] if social[0] == "linkedin" else "social", social[1], where=WHERE_LINK)
    for mail, where in mails:
        mail = mail.strip().strip(".").lower()
        if _EMAIL.fullmatch(mail) and not _PEC.fullmatch(mail):
            add("email", mail, where=where)
    if not any(f.field == "legal_form" for f in found):
        for f in list(found):
            if f.field == "name" and legal_form_of(f.value):
                add("legal_form", legal_form_of(f.value), where=f.where)
                break
    for item in extract_from_jsonld(html, url, tree) + extract_microdata(tree, url):
        _unique_add(found, item)
    return found


def _language_of_path(url: str) -> str:
    match = _LANG_SEGMENT.match(urlsplit(url).path or "/")
    return match.group(1).lower() if match else ""


def _link_score(url: str, text: str) -> int:
    norm = normalize(text)
    score = 0
    if norm and len(norm) <= 60:
        for phrase, weight in _LINK_WORDS:
            if re.search(r"(?:^|\W)" + re.escape(phrase) + r"(?:$|\W)", norm):
                score = max(score, weight)
    path = normalize(urlsplit(url).path.replace("_", "-"))
    if _LINK_PATH.search(path):
        score += 2
    language = _language_of_path(url)
    if language == "it":
        score += 1
    elif language and score:
        score -= 1
    return score


def page_kind(url: str, text: str = "") -> str:
    """'legal', 'contacts', 'about', 'history', 'careers', 'team', 'privacy' or 'other'
    from a link text/URL."""
    path = normalize(urlsplit(url).path)
    words = normalize(text)
    # a long link text ("Learn more about the CE1 machine") says nothing about the page:
    # then only the address counts
    probe = f"{words} {path}" if len(words.split()) <= 4 else path
    if re.search(r"privacy|informativa|cookie|gdpr", probe):
        return "privacy"
    if re.search(r"legal|imprint|impressum|societari|trasparen|company-?data|company[- ]?info|corporate[- ]?info|"
                 r"colophon|credits", probe):
        return "legal"
    if re.search(r"contatt|contact|dove-?siamo|locations?\b|\bsedi\b|\boffices\b|where[- ]we[- ]are", probe):
        return "contacts"
    if re.search(r"storia|histor|heritage|our[- ]story", probe):
        return "history"
    if re.search(r"lavora|career|carriere|\bjobs?\b|work[- ]with[- ]us|join|candidatur", probe):
        return "careers"
    if re.search(r"\bteam\b|management|leadership|organigramma|governance|\bboard\b", probe):
        return "team"
    if re.search(r"chi[- ]?siamo|about|azienda|company|who[- ]we[- ]are|profil|gruppo|group", probe):
        return "about"
    return "other"


def identity_links(html: str, base_url: str, tree=None, maximum: int = MAX_EXTRA_PAGES) -> list[tuple[str, str]]:
    """Internal links most likely to show the company data and its presentation:
    [(url, kind)], one of each kind first (KIND_ORDER), then the best of the rest."""
    candidates: dict[str, tuple[int, str, str]] = {}
    home = url_key(base_url)
    for url, text in extract_links(html, base_url, tree):
        if not same_site(url, base_url) or urlsplit(url).path.lower().endswith(_NON_PAGE):
            continue
        key = url_key(url)
        if key == home:
            continue
        score = _link_score(url, text)
        if score < 3:
            continue
        kind = page_kind(url, text)
        if kind == "other":
            continue
        if key not in candidates or score > candidates[key][0]:
            candidates[key] = (score, url, kind)
    ranked = sorted(candidates.values(), key=lambda c: (-c[0], len(c[1])))
    chosen: list[tuple[str, str]] = []
    for kind in KIND_ORDER:
        for _score, url, found_kind in ranked:
            if found_kind == kind:
                chosen.append((url, kind))
                break
    for _score, url, kind in ranked:
        if (url, kind) not in chosen:
            chosen.append((url, kind))
    return chosen[:maximum]


def italian_alternate(html: str, url: str, tree=None) -> str:
    """The Italian version of a page when the page itself is not Italian and the site
    declares one (hreflang="it" or a language switch "IT" / "Italiano"); '' otherwise."""
    if tree is None:
        tree = analyze_html(html)
    if tree is None:
        return ""
    lang = str(tree.get("lang") or "").lower()
    path_language = _language_of_path(url)
    if lang.startswith("it") or path_language == "it" or (not lang and not path_language):
        return ""
    for link in tree.iter("link"):
        if str(link.get("rel") or "").lower() == "alternate" and \
                str(link.get("hreflang") or "").lower() in ("it", "it-it"):
            target = urljoin(url, link.get("href") or "")
            if target and same_site(target, url) and url_key(target) != url_key(url):
                return target
    for target, text in extract_links(html, url, tree):
        if _ITALIAN_SWITCH.match(text.strip()) and same_site(target, url) and url_key(target) != url_key(url) \
                and _language_of_path(target) == "it":
            return target
    return ""


def scan_identity(site_url: str, fetcher, *, cancel: threading.Event | None = None,
                  max_extra: int = MAX_EXTRA_PAGES, home=None) -> IdentityScan:
    """Reads the home page (and its Italian version, see `italian_alternate`) and at most
    `max_extra` further pages (legal, contacts, about, history, careers, team, privacy).

    `home` may be an already fetched Page. Legal and privacy pages are skipped once the
    VAT, REA, PEC and the registered office are all known. Never raises for an
    unreachable site.
    """
    scan = IdentityScan(site_url)
    url = (site_url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    if not url:
        return scan
    if not urlsplit(url).path:
        url += "/"
    page = home or fetcher.fetch_page(url)
    if page is None:
        return scan
    scan.pages_read += 1
    extra_read = 0

    def read(page_, kind: str) -> None:
        tree_ = analyze_html(page_.html)
        text = visible_text(page_.html)
        scan.pages.append({"url": page_.final_url, "kind": kind, "text": text, "html": page_.html})
        for item in extract_identifiers(page_.html, page_.final_url, text, tree_):
            if kind in NARRATIVE_KINDS and item.field in NARRATIVE_SKIP and item.where not in (WHERE_FOOTER, WHERE_HEADER):
                continue  # "nel 1919 ... capitale sociale di 4.500 lire": a story, not the company data
            _unique_add(scan.found, item)

    read(page, "home")
    tree = analyze_html(page.html)
    seen = {url_key(page.final_url)}
    targets: list[tuple[str, str]] = []
    italian = italian_alternate(page.html, page.final_url, tree)
    if italian and max_extra > 0 and not (cancel is not None and cancel.is_set()):
        seen.add(url_key(italian))
        other = fetcher.fetch_page(italian)
        if other is not None and url_key(other.final_url) != url_key(page.final_url):
            scan.pages_read += 1
            extra_read += 1
            seen.add(url_key(other.final_url))
            read(other, "home_it")
            targets += identity_links(other.html, other.final_url, maximum=max_extra)
    for target in identity_links(page.html, page.final_url, tree, maximum=max_extra):
        if target not in targets:
            targets.append(target)
    if not targets:
        root = f"{urlsplit(page.final_url).scheme}://{urlsplit(page.final_url).netloc}"
        targets = [(urljoin(root, p), page_kind(p)) for p in COMMON_PATHS[:max(0, max_extra - extra_read)]]
    if italian:
        # the Italian section covers a kind: the same kind in another language is not read
        italian_kinds = {kind for url_, kind in targets if _language_of_path(url_) == "it"}
        targets = [(u, k) for u, k in targets
                   if not (k in italian_kinds and _language_of_path(u) not in ("", "it"))]
    # one of each kind first (the Italian section's links first), then the rest
    ordered: list[tuple[str, str]] = []
    for kind in KIND_ORDER:
        ordered += [t for t in targets if t[1] == kind and t not in ordered][:1]
    ordered += [t for t in targets if t not in ordered]
    for target, kind in ordered:
        if extra_read >= max_extra:
            break
        if cancel is not None and cancel.is_set():
            break
        if kind in IDENTITY_KINDS and scan.complete:
            continue
        if url_key(target) in seen:
            continue
        seen.add(url_key(target))
        extra = fetcher.fetch_page(target)
        if extra is None:
            continue
        scan.pages_read += 1
        extra_read += 1
        if url_key(extra.final_url) == url_key(page.final_url):
            continue  # a redirect back to the home page
        seen.add(url_key(extra.final_url))
        read(extra, kind)
    log.info("Identity scan of %s: %d pages read, %d values found", site_url, scan.pages_read, len(scan.found))
    return scan
