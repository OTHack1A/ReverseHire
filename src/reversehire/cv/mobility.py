"""Mobility signals of a CV: business travel, relocation, driving licence, own car, home town.

Rules only, Italian, English and Ukrainian; the patterns are in `data/mobility.json` and are
applied to normalised rows (lowercase, no accents). Business travel ("trasferte":
trips of days or weeks, then back to the company) is kept separate from
relocation (moving home). In doubt the result is "unknown".
"""

import json
import re
from functools import lru_cache

from ..models import Evidence, Mobility
from .sections import DATA_FOLDER, normalize

SOURCE = "cv"
WORKING_DAYS_PER_YEAR = 220
MAX_EVIDENCE = 3


@lru_cache(maxsize=1)
def _data() -> dict:
    return json.loads((DATA_FOLDER / "mobility.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def _patterns(key: str) -> tuple[re.Pattern, ...]:
    return tuple(re.compile(p) for p in _data()[key])


def _remove(norm: str, key: str) -> tuple[str, bool]:
    """The row without the spans matched by the patterns `key`, and whether any matched."""
    found = False
    for pattern in _patterns(key):
        norm, count = pattern.subn(" ", norm)
        found = found or count > 0
    return norm, found


def _matches(norm: str, key: str) -> bool:
    return any(p.search(norm) for p in _patterns(key))


def _add(evidence: list[Evidence], row: str) -> None:
    if len(evidence) < MAX_EVIDENCE and all(e.text != row for e in evidence):
        evidence.append(Evidence(row, SOURCE))


# --- travel days -------------------------------------------------------------

_RE_PERCENT = re.compile(r"(\d{1,3})\s?%")
_RE_DAYS = re.compile(r"(\d{1,3})\s*(?:giorni|giornate|gg|days)\b")
_TRAVEL_WORDS = re.compile(r"trasfert|viagg|travel")


def travel_days(norm: str) -> int | None:
    """Days away per year stated in a normalised row about travel (% converted on 220 days)."""
    if not _TRAVEL_WORDS.search(norm):
        return None
    m = _RE_PERCENT.search(norm)
    if m and 0 < int(m.group(1)) <= 100:
        return round(int(m.group(1)) * WORKING_DAYS_PER_YEAR / 100)
    m = _RE_DAYS.search(norm)
    if m:
        days = int(m.group(1))
        after = norm[m.end():m.end() + 30]
        words = _data()["travel_period_words"]
        if re.search(words["month"], after):
            days *= 11  # about 11 working months a year
        elif not re.search(words["year"], after):
            return None  # days per trip, per week...: not a yearly figure
        if 0 < days <= WORKING_DAYS_PER_YEAR:
            return days
        return WORKING_DAYS_PER_YEAR if days > 0 else None
    return None


# --- driving licence and car ---------------------------------------------------

@lru_cache(maxsize=1)
def _licence_regex() -> re.Pattern:
    words = "|".join(_data()["licence_words"])
    categories = sorted(_data()["licence_categories"], key=len, reverse=True)
    cat = "(?:" + "|".join(c.lower() for c in categories) + r")(?![a-z0-9])"
    return re.compile(
        rf"\b(?:{words})\b\W*(?:(?:cat(?:egoria|egorie|egory|egories|\.)?|tipo|type|class|категорі\w*|кат\.?)\W+)?"
        rf"(?P<cats>{cat}(?:\s*(?:,|/|\+|&|-|\be\b|\band\b)?\s*{cat})*)"
    )


# Ukrainian CVs often write the categories with Cyrillic look-alike letters ("категорії В").
_CYRILLIC_CATEGORIES = str.maketrans({"а": "a", "в": "b", "с": "c", "д": "d", "е": "e", "і": "e"})
_UK_CATEGORY_RUN = re.compile(r"(категорі\w*\W+|кат\.?\W*)((?:[авсдеabcde][12]?(?:\W+|$)){1,5})")


def _latin_categories(norm: str) -> str:
    return _UK_CATEGORY_RUN.sub(lambda m: m.group(1) + m.group(2).translate(_CYRILLIC_CATEGORIES), norm)


def _licence_categories(norm: str) -> list[str]:
    norm = _latin_categories(norm)
    m = _licence_regex().search(norm)
    if not m:
        return []
    cats = re.findall(r"[a-z0-9]+", m.group("cats"))
    known = {c.lower() for c in _data()["licence_categories"]}
    return [c.upper() for c in cats if c in known and c not in ("e", "and")]


def _only_licence_label(norm: str) -> bool:
    words = "|".join(_data()["licence_words"])
    return bool(re.fullmatch(rf"\W*(?:{words})\W*", norm))


# --- public API ----------------------------------------------------------------

def find_mobility(all_rows: list[str], experience_rows: list[str]) -> Mobility:
    """Mobility from every row of the CV (explicit statements, licence, car) and from
    the rows of the work experience (indirect signs of travel -> "likely")."""
    result = Mobility()
    declared: list[str] = []
    excluded: list[str] = []
    days: int | None = None
    for i, row in enumerate(all_rows):
        norm = normalize(row)
        if not norm:
            continue
        # Travel: negative statements first, removed before looking for positive ones.
        rest, negative = _remove(norm, "travel_excluded")
        positive = _matches(rest, "travel_declared")
        if positive:
            declared.append(row)
            days = days if days is not None else travel_days(rest)
        elif negative:
            excluded.append(row)
        # Relocation.
        rest, negative = _remove(norm, "relocation_excluded")
        if _matches(rest, "relocation_declared"):
            if result.relocation != "declared":
                result.relocation, result.relocation_evidence = "declared", []
            _add(result.relocation_evidence, row)
        elif negative and result.relocation != "declared":
            result.relocation = "excluded"
            _add(result.relocation_evidence, row)
        # Licence (also the Europass label alone with the categories on the next row).
        categories = _licence_categories(norm)
        source_row = row
        if not categories and _only_licence_label(norm) and i + 1 < len(all_rows):
            following = all_rows[i + 1]
            categories = _licence_categories(norm + " " + normalize(following))
            source_row = f"{row} {following}"
        if categories:
            for c in categories:
                if c not in result.driving_licence:
                    result.driving_licence.append(c)
            _add(result.licence_evidence, source_row)
        # Own car: the negative form wins on the same row.
        if _matches(norm, "own_car_no"):
            result.own_car = False
            _add(result.licence_evidence, row)
        elif _matches(norm, "own_car_yes") and result.own_car is None:
            result.own_car = True
            _add(result.licence_evidence, row)

    if declared:
        result.travel = "declared"
        for row in declared + excluded:
            _add(result.travel_evidence, row)
        result.travel_days_per_year = days
    elif excluded:
        result.travel = "excluded"
        for row in excluded:
            _add(result.travel_evidence, row)
    else:
        likely = [r for r in experience_rows if _matches(normalize(r), "travel_likely")]
        if likely:
            result.travel = "likely"
            for row in likely:
                _add(result.travel_evidence, row)
    return result


# --- home town -------------------------------------------------------------------

_SEGMENT_SPLIT = re.compile(r"\s*(?:[|•·▪;\t]|\s{3,}|\s[–—-]\s)\s*")
# A town alone among the contacts: 1-3 capitalised words (particles allowed), optional
# province "(BO)" and country after a comma.
_RE_BARE_TOWN = re.compile(
    r"[A-ZÀ-Ý][a-zà-ÿ'’.\-]+(?:\s+(?:(?:di|del|della|dei|sul|sull'|on|upon|in|le|la|de)\s+)?[A-ZÀ-Ý][a-zà-ÿ'’.\-]+){0,2}"
    r"(?:\s*\([A-Z]{2}\))?(?:,\s*[A-ZÀ-Ý][A-Za-zà-ÿ ]{1,20})?"
)
_NOT_TOWNS = {
    "linkedin", "github", "gitlab", "portfolio", "website", "sito web", "skype", "curriculum vitae",
    "resume", "cv", "italian", "italiano", "italiana", "english", "inglese", "married", "single",
    "coniugato", "coniugata", "celibe", "nubile", "nationality", "nazionalita",
}
_JOB_WORDS = {
    "developer", "engineer", "manager", "designer", "analyst", "consultant", "specialist", "technician",
    "assistant", "accountant", "nurse", "director", "officer", "junior", "senior", "lead", "tecnico",
    "tecnica", "ingegnere", "sviluppatore", "sviluppatrice", "infermiere", "infermiera", "contabile",
    "impiegato", "impiegata", "operaio", "operaia", "responsabile", "consulente", "addetto", "addetta",
    "commesso", "commessa", "falegname", "programmatore", "programmatrice", "architetto", "geometra",
    "perito", "magazziniere", "autista", "cuoco", "cuoca", "professionale", "professional",
}


def _bare_town(segment: str) -> bool:
    norm = normalize(segment)
    return (bool(_RE_BARE_TOWN.fullmatch(segment)) and norm not in _NOT_TOWNS
            and not any(w.strip(".,()") in _JOB_WORDS for w in norm.split()))
_RE_LABEL = re.compile(
    r"^(?:luogo\s+di\s+residenza|residenza|residente\s+(?:a|in)|indirizzo(?:\s+di\s+residenza)?|"
    r"domicilio|domiciliat[oa]\s+(?:a|in)|citt[aà](?:\s+di\s+residenza)?|abito\s+a|vivo\s+a|"
    r"home\s+address|address|location|city|town|based\s+in|living\s+in|lives\s+in|"
    r"residence|resident\s+in|адреса(?:\s+проживання)?|місце\s+проживання|місто|проживаю\s+(?:в|у))\b"
    r"\s*[:\-–]?\s*(?P<value>.+)$",
    re.IGNORECASE,
)
_RE_NOT_ADDRESS = re.compile(r"@|https?://|www\.|\btel\b|\bcell|\bphone|\bmobile|\bfax\b|\bnat[oa]\b|\bborn\b"
                             r"|\bтел\b|\bтелефон|\bмоб|\bнародж",
                             re.IGNORECASE)
_RE_CAP = re.compile(
    r"(?<![\d+])(?<!\d\s)\b(?P<cap>\d{5})\s+"
    r"(?P<city>[^\W\d_](?:[^\W\d_]|['’.\-])*(?:\s+[^\W\d_](?:[^\W\d_]|['’.\-])*){0,3}?)"
    r"\s*(?:\((?P<prov>[A-Za-z]{2})\))?\s*(?=$|[,;)\-–]|\s+[A-Z]{2}\b|\s+(?:Italia|Italy)\b)"
)
_STREET_WORDS = (
    "via", "viale", "v.le", "piazza", "p.za", "p.zza", "piazzale", "corso", "c.so", "largo", "vicolo",
    "strada", "str.", "contrada", "borgo", "lungomare", "lungotevere", "frazione", "fraz.",
    "street", "st.", "road", "rd.", "avenue", "ave.", "lane", "drive", "boulevard", "blvd.",
    "way", "close", "court", "square", "place", "calle", "rue", "flat", "apt.", "apartment",
    "вул.", "вулиця", "просп.", "проспект", "пров.", "провулок", "бульв.", "бульвар", "пл.", "площа", "шосе",
)
_COUNTRIES = {
    "italia", "italy", "uk", "united kingdom", "england", "ireland", "ucraina", "ukraine", "germania",
    "germany", "francia", "france", "spagna", "spain", "svizzera", "switzerland", "austria",
    "poland", "polonia", "romania", "usa", "united states", "netherlands", "paesi bassi", "україна",
}


def _has_street_word(part: str) -> bool:
    words = [w.strip(",;:") for w in normalize(part).split()]
    return any(w in _STREET_WORDS or w.rstrip(".") in _STREET_WORDS for w in words[:3])


def _place_from_address(value: str) -> str | None:
    """Town (with postcode/province/country when present) from an address; None if unsure."""
    value = value.strip(" .,:;")
    if not value or _RE_NOT_ADDRESS.search(value):
        return None
    parts = [p.strip() for p in value.split(",") if p.strip()]
    country = next((p for p in parts[1:] if normalize(p) in _COUNTRIES), None)
    m = _RE_CAP.search(value)
    if m:
        place = f"{m.group('cap')} {m.group('city').strip()}"
        if m.group("prov"):
            place += f" ({m.group('prov').upper()})"
        return f"{place}, {country}" if country else place
    kept: list[str] = []
    for part in parts:
        if _has_street_word(part):
            continue
        text = re.sub(r"\([A-Za-z]{2}\)", lambda x: x.group(0).upper(), part)
        text = " ".join(t for t in text.split() if not re.search(r"\d", t)).strip(" -–")
        text = re.sub(r"^(?:м\.|місто|смт\.?|с\.)\s*", "", text)  # Ukrainian "м. Київ" (city of)
        if len(re.findall(r"[^\W\d_]", text)) < 2 or len(text.split()) > 4:
            continue
        kept.append(text)
    if not kept:
        return None
    if len(kept) > 1 and normalize(kept[-1]) in _COUNTRIES:
        return f"{kept[-2]}, {kept[-1]}" if normalize(kept[-2]) not in _COUNTRIES else kept[-1]
    return kept[0] if len(kept) == 1 else ", ".join(kept[:2])


def find_home(rows: list[str]) -> str | None:
    """Town/city of the candidate from the contact block (first rows of the CV).

    Accepts a labelled address ("Residenza: ...", "Address: ..."), an Italian
    postcode followed by the town ("40100 Bologna (BO)") or a street address with
    the town after a comma. Returns a short geocoding query, never the street.
    """
    labelled: str | None = None
    unlabelled: str | None = None
    bare: str | None = None
    for row in rows:
        segments = [s.strip() for s in _SEGMENT_SPLIT.split(row) if s.strip()]
        contact_row = len(segments) > 1 and any(_RE_NOT_ADDRESS.search(s) for s in segments)
        for index, segment in enumerate(segments):
            if contact_row and bare is None and index > 0 and _bare_town(segment):
                bare = segment  # "mail@example.com | +39 ... | Bologna" (the name comes first)
            m = _RE_LABEL.match(segment)
            if m:
                place = _place_from_address(m.group("value"))
                if place and labelled is None:
                    labelled = place
                continue
            if unlabelled is not None or _RE_NOT_ADDRESS.search(segment):
                continue
            if _RE_CAP.search(segment) or (_has_street_word(segment) and "," in segment):
                unlabelled = _place_from_address(segment)
    return labelled or unlabelled or bare
