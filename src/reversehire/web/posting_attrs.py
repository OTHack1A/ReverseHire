"""Attributes of a job posting: contract type, remote work, salary, business travel.

Pure functions over structured data (JSON-LD, ATS fields) and the posting text,
Italian and English. Structured fields are used first; the text rules only fill
what the structured data does not say. A salary is reported only when it is
published: it is never estimated.
"""

import re
from dataclasses import dataclass, fields

from ..models import Posting
from .text import compact, normalize

# --- keyword tables (patterns on normalised text: lowercase, no accents) -------------

_PERMANENT_POSSIBLE = [
    r"\b(?:possibilita|possibile|prospettiv\w*|finalizzat\w*|volt\w|mirat\w|scopo|in\s+vista|con\s+vista|"
    r"opportunita|trasformazione|trasformabile|stabilizzazione|inserimento|obiettivo)\W+(?:\w+\W+){0,5}?"
    r"(?:tempo\s+indeterminato|indeterminato|assunzione(?:\s+diretta)?)\b",
    r"\b(?:temp|contract)[- ]to[- ](?:perm|permanent|hire)\b",
    r"\b(?:possibility|prospect|view|chance|opportunity|option|potential)\W+(?:\w+\W+){0,4}?"
    r"(?:permanent|perm|becoming\s+permanent|conversion)\b",
    r"\bpotentially\s+permanent\b",
]
_CONTRACT_RULES: dict[str, list[str]] = {
    "temporary_agency": [
        r"\bsomministrazione\b", r"\binterinal\w*\b", r"\bstaff\s+leasing\b",
        r"\btemp(?:orary)?\s+(?:work\s+)?agency\b", r"\bagency\s+work(?:er)?\b",
    ],
    "internship": [
        r"\btirocini\w*\b", r"\bstagist\w*\b", r"\binternships?\b", r"\bintern\b",
        r"\bstage\s+(?:curriculare|extracurriculare|extra-curriculare|retribuito|formativo|"
        r"di\s+\d+\s+mesi|in\s+azienda)\b",
        r"\b(?:offerta|proposta|percorso|periodo|contratto|opportunita)\s+di\s+stage\b",
        r"\bwork\s+placement\b", r"\bpraticantato\b",
    ],
    "apprenticeship": [r"\bapprendist\w*\b", r"\bapprenticeships?\b", r"\bapprentice\b"],
    "permanent": [
        r"\b(?:tempo|contratto)\s+(?:\w+\s+){0,2}?indeterminato\b",
        r"\bindeterminato\b",
        r"\bpermanent\s+(?:position|role|contract|job|employment|post|basis|hire|opportunity|"
        r"full[- ]time|part[- ]time|vacancy)\b",
        r"\b(?:full[- ]time|part[- ]time|contract(?:\s+type)?)\W+permanent\b",
        r"\bpermanent\s*$",
        r"\bunbefristet\w*\b",
    ],
    "fixed_term": [
        r"\b(?:tempo|contratto)\s+(?:\w+\s+){0,2}?determinato\b",
        r"\bcontratto\s+a\s+termine\b", r"\bfixed[- ]term\b", r"\btemporary\s+(?:contract|position|role)\b",
        r"\bmaternity\s+(?:cover|leave\s+cover)\b", r"\bsostituzione\s+maternita\b", r"\bbefristet\w*\b",
    ],
    "freelance": [
        r"\bpartita\s+iva\b", r"\bp\.?\s?iva\b", r"\bfreelance\w*\b", r"\blibero\s+professionista\b",
        r"\bcollaborazione\s+(?:occasionale|coordinata|esterna)\b", r"\bco\.?co\.?co\b",
        r"\bcontractor\b", r"\bcontract\s+(?:role|position|basis|assignment)\b", r"\bb2b\s+contract\b",
        r"\bday\s+rate\b", r"\bself[- ]employed\b",
    ],
}
# Structured employment types (JSON-LD employmentType, ATS fields), normalised.
_STRUCTURED_CONTRACT = {
    "permanent": "permanent", "regular": "permanent", "unbefristet": "permanent",
    "full_time_permanent": "permanent", "cdi": "permanent",
    "temporary": "fixed_term", "fixed_term": "fixed_term", "fixedterm": "fixed_term",
    "fixed-term": "fixed_term", "befristet": "fixed_term", "seasonal": "fixed_term",
    "intern": "internship", "internship": "internship", "working_student": "internship",
    "apprenticeship": "apprenticeship", "apprentice": "apprenticeship", "trainee": "apprenticeship",
    "contractor": "freelance", "contract": "freelance", "freelance": "freelance",
    "freelancer": "freelance", "self_employed": "freelance",
}
_STRUCTURED_SUBSTRINGS = [
    ("permanent", "permanent"), ("fixedterm", "fixed_term"), ("temporary", "fixed_term"),
    ("internship", "internship"), ("apprentice", "apprenticeship"), ("freelance", "freelance"),
    ("contractor", "freelance"),
]

_REMOTE_NEGATIVE = [
    r"\bno\s+(?:full\s+)?(?:remote|remote\s+work(?:ing)?|smart\s+working|telelavoro|wfh)\b",
    r"\bnot\s+(?:a\s+)?(?:fully\s+)?remote\b",
    r"\bnon\s+(?:e\s+)?(?:previst\w+|possibile|disponibile)\W+(?:\w+\W+){0,3}?(?:smart\s+working|remoto|telelavoro)\b",
    r"\bnessuna\s+possibilita\s+di\s+(?:smart\s+working|lavoro\s+da\s+remoto|telelavoro)\b",
    r"\bsenza\s+(?:smart\s+working|telelavoro)\b",
]
# "Remote" as a technique, not a way of working: "assistenza da remoto", "avviamento degli
# impianti da remoto", "remote support/diagnostics" (automation and IT service ads).
# Removed from the sentence before the remote patterns are tried.
_REMOTE_TECHNICAL = [
    r"\b(?:assistenza|teleassistenza|supporto|support|avviament\w*|avviare|collaud\w*|diagnos\w*|"
    r"manutenzione|monitoraggio|controllo|controllare|accesso|collegamento|collegarsi|connessione|"
    r"intervent\w*|aggiornament\w*|programmazione|configurazione|troubleshooting|messa\s+in\s+servizio)"
    r"(?:\W+\w+){0,6}?\W+(?:da|in)\s+remoto\b",
    r"\bremote\s+(?:support|assistance|access|diagnostics?|monitoring|commissioning|maintenance|control|"
    r"troubleshooting|connection|service|services|desktop|i/?o|sensors?)\b",
]
_ONSITE = [
    r"\b(?:100\s?%|interamente|esclusivamente|solo|totalmente)\s+in\s+(?:presenza|sede|ufficio)\b",
    r"\bon[- ]?site\s+only\b", r"\bonly\s+on[- ]?site\b", r"\b(?:fully|100\s?%)\s+(?:on[- ]?site|in[- ]office)\b",
    r"\boffice[- ]based\b", r"\bin[- ]office\s+(?:role|position)\b",
    r"\b(?:lavoro|attivita|posizione|ruolo|modalita)\s+in\s+presenza\b",
    r"\b(?:workplace|work\s+mode|modalita(?:\s+di\s+lavoro)?)\W+(?:on[- ]?site|in\s+presenza|in\s+sede)\b",
]
_HYBRID = [
    r"\bhybrid\b", r"\bibrid[oa]\b",
    r"\bsmart\s+working\b(?!\s+(?:al\s+)?100)",
    r"\b(?:\d|un|uno|due|tre|quattro)\s+(?:giorni|giorno|gg)\W+(?:\w+\W+){0,4}?"
    r"(?:smart\s+working|da\s+remoto|in\s+remoto|da\s+casa)\b",
    r"\b(?:smart\s+working|lavoro\s+da\s+remoto|remoto)\s+(?:parziale|\d\s+(?:giorni|gg))\b",
    r"\b(?:\d|one|two|three|four)\s+days?\W+(?:\w+\W+){0,4}?(?:remote|from\s+home|wfh)\b",
    r"\b(?:partly|partially|part)\s+remote\b", r"\bflexible\s+(?:remote|home)\s+working\b",
]
_REMOTE = [
    r"\b(?:full|fully|100\s?%|completely|totally|entirely)\W*(?:remote|remoto|smart\s+working)\b",
    r"\bsmart\s+working\s+(?:al\s+)?100\s?%",
    r"\bremote(?:[- ]first|[- ]friendly|[- ]only)\b",
    # Not "remote team(s)": collaborating with a remote team says nothing about this job.
    r"\bremote\s+(?:work|working|position|role|job|opportunity|contract|employee|based)\b",
    r"\bwork(?:ing)?\s+(?:remotely|from\s+home|from\s+anywhere)\b",
    r"\b(?:lavoro|lavorare|lavorerai|posizione|attivita)\s+(?:\w+\s+)?(?:da|in)\s+remoto\b",
    r"\bda\s+remoto\b", r"\bin\s+remoto\b", r"\btelelavoro\b", r"\btelecommut\w*\b", r"\bwfh\b",
    r"^remote\s*(?:$|[,(/|:-])", r"\bremote\s*[,(/-]\s*(?:\w+\s*)?(?:italy|italia|europe|europa|emea|eu|uk|us|usa|worldwide|global)\b",
    r"\b(?:italy|italia|europe|emea|eu|uk|us|usa)\s*[,(/-]\s*remote\b", r"\(remote\)",
]

_TRAVEL_NONE = [
    r"\bno\s+(?:business\s+)?travel(?:l?ing)?\b(?!\s+(?:expenses|allowance))",
    r"\btravel\s+(?:is\s+)?not\s+required\b",
    r"\b(?:nessuna|niente)\s+trasferta\b", r"\bno\s+trasferte\b",
    r"\bnon\s+(?:sono\s+|e\s+)?(?:previst\w+|richiest\w+)\W+(?:\w+\W+){0,2}?trasfert\w*\b",
    r"\bsenza\s+trasferte\b",
]
_TRAVEL_OCCASIONAL = [
    r"\b(?:occasional\w*|saltuari\w*|sporadic\w*|rar\w|brevi\s+e\s+saltuarie|limitat\w)\s+trasfert\w*\b",
    r"\btrasfert\w*\s+(?:occasional\w*|saltuari\w*|sporadic\w*|limitat\w|rar\w)\b",
    r"\b(?:occasional|occasionally|some|limited|minimal|infrequent|light)\s+(?:business\s+)?travel\w*\b",
    r"\btravel\s+(?:occasionally|is\s+occasional)\b",
]
_TRAVEL_REQUIRED = [
    r"\bdisponibil\w*\W+(?:\w+\W+){0,3}?(?:a|ad|alle|per)\W+(?:\w+\W+){0,2}?(?:trasfert\w*|viagg\w*)",
    r"\b(?:frequent\w*|continu\w*|regolari)\s+trasfert\w*\b", r"\btrasfert\w*\s+(?:frequent\w*|continu\w*)\b",
    r"\btrasferte\s+(?:in\s+italia|all\W?estero|nazionali|internazionali|in\s+europa|presso\s+(?:i\s+)?clienti)\b",
    r"\b(?:willing\w*|availab\w*|able|ready|ability)\s+to\s+travel\w*\b",
    r"\btravel(?:ling|ing)?\s+(?:is\s+)?(?:required|necessary|expected|mandatory)\b",
    r"\b(?:frequent|extensive|regular|international|national|domestic|significant)\s+(?:business\s+)?travel\w*\b",
    r"\btravel\s+up\s+to\b", r"\b\d{1,3}\s?%\s+(?:of\s+)?(?:travel\w*|trasfert\w*)",
    r"\b(?:requires?|involves?)\s+(?:\w+\s+){0,2}?travel\w*\b", r"\btrasfertist\w*\b",
    r"\b(?:on[- ]site|customer\s+site)\s+(?:visits|interventions)\b",
]
_RE_TRAVEL_PERCENT = re.compile(r"(\d{1,3})\s?%")
OCCASIONAL_MAX_PERCENT = 25

# --- salary ----------------------------------------------------------------------

_CURRENCIES = {
    "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR", "£": "GBP", "gbp": "GBP",
    "$": "USD", "usd": "USD", "chf": "CHF", "pln": "PLN", "uah": "UAH",
}
_CUR = r"(?:€|£|\$|\b(?:eur|euro|euros|gbp|usd|chf|pln|uah)\b)"
_NUM = r"\d{1,3}(?:[.,'’  ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_AMOUNT = rf"(?:{_CUR}\s?)?(?:{_NUM})(?:\s?[kK]\b)?(?:\s?{_CUR})?"
_RE_SALARY = re.compile(
    rf"(?P<lead>(?:\b(?:ral|r\.a\.l\.|retribuzione(?:\s+annua)?(?:\s+lorda)?|stipendio|compenso|"
    rf"salario|salary|pay|compensation|base\s+pay|wage|range\s+retributivo|fascia\s+retributiva)"
    rf"\b[^\d€£$]{{0,50}}))?"
    rf"(?P<low>{_AMOUNT})"
    rf"(?:\s*(?:-|–|—|to|a|e|and|fino\s+a|/)\s*(?P<high>{_AMOUNT}))?"
    rf"(?P<tail>(?:\s*\(?\s*(?:/\s?(?:anno|year|yr|annum|mese|month|mo|ora|hour|hr|h)\b|"
    rf"(?:lord[oi]|nett[oi]|gross|net)\b|"
    rf"(?:annu[oi]|annual[ie]?|mensil[ei]|orari[oe]?)\b|al\s+mese|all'?\s?anno|l'?\s?anno|all'?\s?ora|"
    rf"per\s+(?:year|annum|month|hour|anno|mese)|a\s+(?:year|month|hour)|an\s+hour|p\.?a\.?\b|"
    rf"yearly|monthly|hourly|\+\s?(?:bonus|benefits)))*)",
    re.IGNORECASE,
)
_GROUPED = re.compile("\\d{1,3}(?:[.,'\u2019\u00a0\u202f ]\\d{3})+")
MIN_BARE_YEARLY = 10000  # smallest figure of a salary range published without currency
_SALARY_EXCLUDED = re.compile(
    r"fatturato|revenue|turnover|budget|welfare|buoni\s+pasto|ticket|rimborso\s+(?:chilometric|km|viagg|"
    r"trasfert|delle\s+spese\s+di\s+viaggio)|investiment|capitale|"
    r"funding|raised|milion|million|mln|miliard|billion|dipendenti|employees|clienti|customers|"
    r"people|persone|colleagues|colleghi|users|utenti|merchants|"
    r"premio|bonus\s+di\s+benvenuto|referral",
    re.IGNORECASE,
)
_PERIOD_WORDS = [
    ("hour", r"/\s?(?:ora|hour|hr|h)\b|\b(?:orari[oe]?|all'?\s?ora|per\s+hour|an\s+hour|a\s+hour|hourly)\b"),
    ("month", r"/\s?(?:mese|month|mo)\b|\b(?:mensil[ei]|al\s+mese|per\s+(?:month|mese)|a\s+month|monthly)\b"),
    ("year", r"/\s?(?:anno|year|yr|annum)\b|\b(?:annu[oi]|annual[ie]?|all'?\s?anno|l'?\s?anno|"
             r"per\s+(?:year|annum|anno)|a\s+year|p\.?a\.?|yearly|ral|r\.a\.l\.)(?!\w)"),
]
_STRUCTURED_PERIOD = {
    "year": "year", "yearly": "year", "annual": "year", "annually": "year", "1 year": "year",
    "per-year-salary": "year", "month": "month", "monthly": "month", "1 month": "month",
    "per-month-salary": "month", "hour": "hour", "hourly": "hour", "1 hour": "hour",
    "per-hour-wage": "hour",
}


@dataclass
class PostingAttributes:
    contract: str | None = None
    remote: str | None = None
    salary: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_period: str | None = None
    salary_currency: str | None = None
    travel: str | None = None
    travel_note: str | None = None


def _sentences(text: str) -> list[str]:
    """Sentences of the text (rows, and periods followed by a space)."""
    parts = re.split(r"\n+|(?<=[.!?;])\s+|\s[•·▪]\s", text or "")
    return [compact(p) for p in parts if p and compact(p)]


def _any(norm: str, patterns: list[str]) -> re.Match | None:
    for pattern in patterns:
        m = re.search(pattern, norm)
        if m:
            return m
    return None


def _structured_values(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [v for x in value for v in _structured_values(x)]
    if isinstance(value, dict):
        return _structured_values(value.get("id") or value.get("name") or value.get("label"))
    return [str(value)]


# --- contract ----------------------------------------------------------------------

def contract_from_structured(employment_type) -> str | None:
    """Contract from structured values (JSON-LD employmentType, ATS fields); None for
    values that say only the schedule (FULL_TIME, PART_TIME)."""
    found = []
    for raw in _structured_values(employment_type):
        key = re.sub(r"[\s-]+", "_", normalize(raw)).strip("_")
        compact_key = key.replace("_", "")
        value = _STRUCTURED_CONTRACT.get(key) or _STRUCTURED_CONTRACT.get(compact_key)
        if value is None:  # composite codes, e.g. Recruitee "fulltime_permanent"
            value = next((v for word, v in _STRUCTURED_SUBSTRINGS if word in compact_key), None)
        if value and value not in found:
            found.append(value)
    if not found:
        return None
    if "permanent" in found and len(found) > 1:
        return "permanent_possible"
    return found[0]


def contract_from_text(text: str) -> str | None:
    norm = normalize(text or "")
    if not norm:
        return None
    if _any(norm, _PERMANENT_POSSIBLE):
        return "permanent_possible"
    hits = {kind for kind, patterns in _CONTRACT_RULES.items() if _any(norm, patterns)}
    if "temporary_agency" in hits:
        return "temporary_agency"
    permanent = "permanent" in hits
    if permanent and hits & {"internship", "fixed_term"}:
        return "permanent_possible"
    if "apprenticeship" in hits:
        return "apprenticeship"
    if permanent:
        return "permanent"
    for kind in ("internship", "fixed_term", "freelance"):
        if kind in hits:
            return kind
    return None


def _title_contract(title: str) -> str | None:
    """Words that are reliable only in a title ("Stage marketing", "Stage - Ufficio acquisti")."""
    if re.search(r"(?:^|\W)stage(?:$|\W)", normalize(title or "")) and not re.search(
            r"\b(?:early|late|final|next|first|second|this)\s+stage\b", normalize(title or "")):
        return "internship"
    return None


def detect_contract(employment_type, text: str, title: str = "") -> str | None:
    structured = contract_from_structured(employment_type)
    from_text = contract_from_text(f"{title}\n{text}") or _title_contract(title)
    if structured is None:
        return from_text
    if from_text == "permanent_possible" and structured in ("fixed_term", "internship", "apprenticeship",
                                                           "temporary_agency"):
        return "permanent_possible"
    return structured


# --- remote --------------------------------------------------------------------------

def remote_from_structured(workplace) -> str | None:
    """"remote"/"hybrid"/"onsite" from workplace fields (Lever, Ashby, JSON-LD TELECOMMUTE...)."""
    for raw in _structured_values(workplace):
        key = normalize(raw).replace("-", "").replace("_", "").replace(" ", "")
        if key in ("remote", "telecommute", "fullyremote", "remoteonly", "true"):
            return "remote"
        if key in ("hybrid", "flexible"):
            return "hybrid"
        if key in ("onsite", "office", "inoffice", "onpremise"):
            return "onsite"
    return None


_REMOTE_HINT = re.compile(r"remot|smart|hybrid|ibrid|telelavor|telecommut|home|wfh|presenza|site|office|"
                          r"ufficio|sede|anywhere|casa")
_TRAVEL_HINT = re.compile(r"travel|trasfert|viagg|site")


def remote_from_text(text: str) -> str | None:
    found = None
    negative = False
    for sentence in _sentences(text):
        norm = normalize(sentence)
        if not _REMOTE_HINT.search(norm):
            continue
        rest = norm
        for pattern in _REMOTE_TECHNICAL:
            rest = re.sub(pattern, " ", rest)
        for pattern in _REMOTE_NEGATIVE:
            rest, count = re.subn(pattern, " ", rest)
            negative = negative or count > 0
        if _any(rest, _HYBRID):
            return "hybrid"
        if found is None and _any(rest, _REMOTE):
            found = "remote"
        if _any(norm, _ONSITE):
            negative = True
    if found:
        return found
    return "onsite" if negative else None


def remote_from_location(location: str) -> str | None:
    """A location field naming the arrangement ("Milan (Hybrid)", "Remote - EU",
    "Italia, da remoto"): there the word is never prose about something else."""
    norm = normalize(location or "")
    if re.search(r"\bhybrid\b|\bibrid[oa]\b", norm):
        return "hybrid"
    if re.search(r"\bremote\b|\bremoto\b|\btelelavoro\b|\banywhere\b|\bwfh\b", norm):
        return "remote"
    return None


def detect_remote(workplace, text: str, location: str = "") -> str | None:
    structured = remote_from_structured(workplace) or remote_from_location(location)
    from_text = remote_from_text(f"{location}\n{text}")
    if structured == "remote" and from_text == "hybrid":
        return "hybrid"  # e.g. JSON-LD TELECOMMUTE with "2 days in the office"
    return structured or from_text


# --- salary ----------------------------------------------------------------------------

def parse_amount(text: str) -> float | None:
    """'35.000' -> 35000; '2,000.50' -> 2000.5; '1.500,00' -> 1500; '35k' -> 35000."""
    t = re.sub(_CUR, "", text or "", flags=re.IGNORECASE).strip()
    thousand = bool(re.search(r"[kK]\s*$", t))
    t = re.sub(r"[kK]\s*$", "", t).strip().replace(" ", " ").replace("’", "'")
    t = t.replace(" ", "").replace("'", "")
    if not re.fullmatch(r"[\d.,]+", t):
        return None
    if "." in t and "," in t:
        decimal = "." if t.rfind(".") > t.rfind(",") else ","
        t = t.replace("," if decimal == "." else ".", "").replace(decimal, ".")
    elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", t):
        t = t.replace(".", "").replace(",", "")
    else:
        t = t.replace(",", ".")
    try:
        value = float(t)
    except ValueError:
        return None
    return value * 1000 if thousand else value


def _currency(text: str) -> str | None:
    m = re.search(_CUR, text or "", re.IGNORECASE)
    return _CURRENCIES.get(m.group(0).lower()) if m else None


def _plausible(value: float | None, period: str | None) -> bool:
    if value is None or value <= 0:
        return False
    limits = {"hour": (3, 1000), "month": (200, 50000), "year": (3000, 2_000_000)}
    low, high = limits.get(period, (3, 2_000_000))
    return low <= value <= high


def _format_number(value: float) -> str:
    return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"


def salary_from_structured(data) -> PostingAttributes | None:
    """Salary from JSON-LD baseSalary/estimatedSalary (MonetaryAmount) or ATS ranges
    ({min, max, currency, interval|period|unit})."""
    if isinstance(data, list):
        data = next((d for d in data if isinstance(d, dict)), None)
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        data = {"value": data}
    if not isinstance(data, dict):
        return None
    value = data.get("value", data)
    currency = data.get("currency") or data.get("currencyCode") or ""
    unit = data.get("unitText") or data.get("interval") or data.get("period") or data.get("unit") or ""
    low = high = None
    if isinstance(value, dict):
        unit = value.get("unitText") or unit
        low = value.get("minValue", value.get("min"))
        high = value.get("maxValue", value.get("max"))
        if low is None and high is None:
            low = value.get("value")
    elif isinstance(value, (int, float, str)) and value is not data:
        low = value
    if low is None and high is None:
        low, high = data.get("minValue", data.get("min")), data.get("maxValue", data.get("max"))

    def number(x) -> float | None:
        if isinstance(x, bool):
            return None
        if isinstance(x, (int, float)):
            return float(x)
        if isinstance(x, str):
            return parse_amount(x)
        return None

    low, high = number(low), number(high)
    if low is None and high is None:
        return None
    if low is not None and high is not None and high < low:
        low, high = high, low
    if low == high:
        high = None
    period = _STRUCTURED_PERIOD.get(normalize(str(unit)).replace("_", " "), None)
    if period is None and str(unit):
        period = _STRUCTURED_PERIOD.get(normalize(str(unit)).replace("_", "-"))
    if not all(_plausible(v, period) for v in (low, high) if v is not None):
        return None
    currency = str(currency).upper().strip() or None
    amounts = "–".join(_format_number(v) for v in (low, high) if v is not None)
    label = " ".join(x for x in (currency, amounts) if x)
    if period:
        label += f" / {period}"
    return PostingAttributes(salary=label, salary_min=low, salary_max=high or low,
                             salary_period=period, salary_currency=currency)


def salary_from_text(text: str) -> PostingAttributes | None:
    """First salary published in the text, kept as written; None if only words
    ("RAL commisurata all'esperienza", "competitive salary")."""
    for sentence in _sentences(text):
        if not re.search(r"\d", sentence):
            continue
        for m in _RE_SALARY.finditer(sentence):
            lead, tail = m.group("lead") or "", m.group("tail") or ""
            low_text, high_text = m.group("low"), m.group("high") or ""
            span = m.group(0)
            has_currency = bool(re.search(_CUR, span, re.IGNORECASE))
            has_k = bool(re.search(r"\d\s?[kK]\b", low_text + " " + high_text))
            window = sentence[max(0, m.start() - 40):m.end() + 40]
            if _SALARY_EXCLUDED.search(window):
                continue
            period = next((p for p, pattern in _PERIOD_WORDS
                           if re.search(pattern, lead + " " + tail, re.IGNORECASE)), None)
            ral = bool(re.search(r"\bral\b|\br\.a\.l\.", normalize(lead)))
            if not lead and not period:
                continue  # an amount alone (prices, figures) is not a salary
            low = parse_amount(low_text)
            high = parse_amount(high_text) if high_text else None
            # "The salary band for this position is 32.000 - 44.000": no currency, but a
            # salary word and a range of grouped figures of a yearly size.
            grouped_range = bool(
                lead and high_text and _GROUPED.fullmatch(low_text.strip()) and _GROUPED.fullmatch(high_text.strip())
                and low is not None and high is not None and low >= MIN_BARE_YEARLY)
            if not (has_currency or has_k or ral or grouped_range):
                continue  # "salary review 2026": a number without currency is not a salary
            if high is not None and has_k and low is not None and low < 1000 <= high:
                low *= 1000  # "35-45k"
            if not _plausible(low, period) or (high is not None and not _plausible(high, period)):
                continue
            if high is not None and high < low:
                continue
            currency = _currency(span) or ("EUR" if ral else None)
            # The label keeps "RAL" (it says what the figure is) but not "Salary:" & co.
            label = compact(span if ral else span[len(lead):]).strip(" ,.;:-")
            return PostingAttributes(salary=label, salary_min=low, salary_max=high if high else low,
                                     salary_period=period, salary_currency=currency)
    return None


def detect_salary(structured, text: str) -> PostingAttributes | None:
    return salary_from_structured(structured) or salary_from_text(text)


# --- travel ------------------------------------------------------------------------------

def detect_travel(text: str) -> tuple[str | None, str | None]:
    """("required"|"occasional"|"none"|None, sentence about travel)."""
    for sentence in _sentences(text):
        norm = normalize(sentence)
        if not _TRAVEL_HINT.search(norm):
            continue
        if _any(norm, _TRAVEL_NONE):
            return "none", sentence[:300]
        if _any(norm, _TRAVEL_OCCASIONAL):
            return "occasional", sentence[:300]
        if _any(norm, _TRAVEL_REQUIRED):
            m = _RE_TRAVEL_PERCENT.search(norm)
            if m and re.search(r"travel|trasfert|viagg", norm) and int(m.group(1)) <= OCCASIONAL_MAX_PERCENT:
                return "occasional", sentence[:300]
            return "required", sentence[:300]
    return None, None


# --- assembly ------------------------------------------------------------------------------

def detect_attributes(text: str, *, title: str = "", location: str = "", employment_type=None,
                      workplace=None, salary=None) -> PostingAttributes:
    """All the attributes of a posting: structured values first, then the text."""
    full = "\n".join(x for x in (title, text) if x)
    result = PostingAttributes(
        contract=detect_contract(employment_type, text, title),
        remote=detect_remote(workplace, full, location),
    )
    found_salary = detect_salary(salary, full)
    if found_salary:
        for name in ("salary", "salary_min", "salary_max", "salary_period", "salary_currency"):
            setattr(result, name, getattr(found_salary, name))
    result.travel, result.travel_note = detect_travel(full)
    return result


def apply_attributes(posting: Posting, attributes: PostingAttributes, overwrite: bool = False) -> Posting:
    """Copies the attributes onto the posting (only the empty fields unless `overwrite`)."""
    for f in fields(PostingAttributes):
        value = getattr(attributes, f.name)
        if value is not None and (overwrite or getattr(posting, f.name) is None):
            setattr(posting, f.name, value)
    return posting


def complete_posting(posting: Posting) -> Posting:
    """Fills the attributes still empty from the posting's own title, description and location."""
    attributes = detect_attributes(posting.description, title=posting.title, location=posting.location)
    return apply_attributes(posting, attributes)
