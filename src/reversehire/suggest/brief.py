"""The anonymised profile brief sent to the assistant, and the search area.

`build_profile_brief(cv_profile, preferences, area)` keeps only what an assistant
needs to name employers: roles, skill families with years, sectors of experience,
spoken languages, seniority, the wanted directions and company types, and the area
(region / province / country, or a radius around a TOWN).

It never contains: the person's name, e-mail, phone, postal address or home town,
employers' names (each employer becomes "undisclosed employer", or "<sector>,
<size class>" when the integrator passes `employer_class`), dates (only durations in
years), the date of birth or any raw CV text. The brief is built only from structured
profile fields; every string is scrubbed (e-mail, phone, URL, digits, employer and
home words) and a final leak check drops any line that still contains a forbidden
string (fail closed).

`ProfileBrief.preview_text()` is EXACTLY the block put into the prompt, so the GUI
can show the user what will be sent before the first use.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

AREA_RADIUS, AREA_PROVINCE, AREA_REGION, AREA_COUNTRY = "radius", "province", "region", "country"
AREA_KINDS = (AREA_RADIUS, AREA_PROVINCE, AREA_REGION, AREA_COUNTRY)

MAX_ROLES = 6
MAX_FAMILIES = 8
MAX_EXPERIENCES = 6
MAX_LANGUAGES = 5
MAX_ITEM_CHARS = 60
SIZE_CLASSES = ("micro", "small", "medium", "large")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_PHONE = re.compile(r"\+?\d[\d\s./()-]{5,}\d")
_DIGITS = re.compile(r"\d")
_STREET = re.compile(r"\b(?:via|viale|piazza|piazzale|corso|largo|vicolo|strada|street|st\.|road|rd\.|avenue|"
                     r"ave\.|lane|platz|strasse|straße|rue|calle)\b", re.I)
_AT_EMPLOYER = re.compile(r"\s+(?:at|presso|c/o|@|for|per|da|bei|chez)\s+.*$", re.I)
# Words of an employer's name that say nothing about who the employer is.
_GENERIC_WORDS = {
    "srl", "spa", "srls", "snc", "sas", "ltd", "llc", "inc", "gmbh", "group", "gruppo", "societa", "company",
    "the", "and", "di", "del", "della", "dei", "e", "&", "co", "corp", "italia", "italy", "industries",
    "industrie", "industriale", "systems", "sistemi", "solutions", "soluzioni", "engineering", "automation",
    "automazione", "automazioni", "service", "services", "servizi", "technologies", "tecnologie", "tech",
    "international", "europe", "consulting", "software", "network", "networks", "electric", "elettrica",
    "meccanica", "machines", "macchine", "impianti", "plant", "plants", "studio", "digital",
}


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _fold(text))


@dataclass
class SearchArea:
    """Where the companies should be. Never the home address: a radius is around a TOWN.

    kind: "radius" (town + radius_km), "province" (province code, e.g. "FC", and its
    name), "region" (region name and, for the area check, its province codes) or
    "country" (ISO code)."""

    kind: str = AREA_PROVINCE
    country: str = "IT"
    region: str = ""
    province: str = ""            # code, e.g. "FC"
    province_name: str = ""       # e.g. "Forlì-Cesena"
    provinces: list[str] = field(default_factory=list)  # codes of the region (area check)
    town: str = ""                # radius mode: the centre town (never a street address)
    radius_km: float | None = None
    lat: float | None = None      # centre of the radius (area check only, never sent)
    lon: float | None = None

    def describe(self) -> str:
        """The area as sent to the assistant (English)."""
        country = _country_name(self.country)
        if self.kind == AREA_RADIUS and self.town and self.radius_km:
            where = f"within {int(round(self.radius_km))} km of the town of {self.town}"
            if self.province:
                where += f" (province {self.province})"
            return f"{where}, {country}"
        if self.kind == AREA_PROVINCE and (self.province or self.province_name):
            name = self.province_name or self.province
            code = f" ({self.province})" if self.province and self.province_name else ""
            region = f", region {self.region}" if self.region else ""
            return f"province of {name}{code}{region}, {country}"
        if self.kind == AREA_REGION and self.region:
            return f"region {self.region}, {country}"
        return country

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "SearchArea":
        d = dict(d or {})
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


_COUNTRIES = {"IT": "Italy", "DE": "Germany", "FR": "France", "ES": "Spain", "CH": "Switzerland",
              "AT": "Austria", "GB": "United Kingdom", "UK": "United Kingdom", "PL": "Poland",
              "PT": "Portugal", "NL": "Netherlands", "BE": "Belgium", "UA": "Ukraine"}


def _country_name(code: str) -> str:
    code = (code or "IT").strip().upper()
    return _COUNTRIES.get(code, code)


@dataclass
class FamilyYears:
    name: str
    years: float | None = None
    skills: list[str] = field(default_factory=list)


@dataclass
class ExperienceLine:
    title: str
    years: float | None = None
    employer: str = "undisclosed employer"


@dataclass
class ProfileBrief:
    """The anonymised summary (only these fields reach the assistant)."""

    roles: list[str] = field(default_factory=list)
    families: list[FamilyYears] = field(default_factory=list)
    experiences: list[ExperienceLine] = field(default_factory=list)
    sectors: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    seniority: str = ""
    years_experience: float | None = None
    travel: str = ""
    directions: list[tuple[str, str]] = field(default_factory=list)   # (id, name)
    company_types: list[str] = field(default_factory=list)            # type keys
    area: SearchArea = field(default_factory=SearchArea)
    dropped: int = 0                                                  # lines removed by the leak check

    @property
    def direction_ids(self) -> list[str]:
        return [d for d, _ in self.directions]

    def preview_text(self) -> str:
        """Exactly what is sent to the assistant (English, one fact per line)."""
        lines = ["Candidate (anonymised):"]
        if self.roles:
            lines.append("- Roles: " + "; ".join(self.roles))
        if self.seniority or self.years_experience is not None:
            parts = []
            if self.seniority:
                parts.append(self.seniority)
            if self.years_experience is not None:
                parts.append(f"{_years(self.years_experience)} years of experience in total")
            lines.append("- Seniority: " + ", ".join(parts))
        for family in self.families:
            years = f", about {_years(family.years)} years" if family.years else ""
            skills = f" ({', '.join(family.skills)})" if family.skills else ""
            lines.append(f"- Skills, {family.name}{years}{skills}")
        for item in self.experiences:
            years = f", {_years(item.years)} years" if item.years else ""
            lines.append(f"- Experience: {item.title}{years}, {item.employer}")
        if self.sectors:
            lines.append("- Sectors of experience: " + "; ".join(self.sectors))
        if self.languages:
            lines.append("- Languages: " + "; ".join(self.languages))
        if self.travel:
            lines.append(f"- Business travel: {self.travel}")
        if self.directions:
            lines.append("- Wanted directions (id: name): " + "; ".join(f"{i}: {n}" for i, n in self.directions))
        if self.company_types:
            from .prompt import TYPE_DESCRIPTIONS

            lines.append("- Wanted company types: " + "; ".join(
                f"{t} ({TYPE_DESCRIPTIONS.get(t, t)})" for t in self.company_types))
        lines.append(f"- Area: {self.area.describe()}")
        return "\n".join(lines)

    def fingerprint(self) -> str:
        return hashlib.sha256(self.preview_text().encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["area"] = self.area.to_dict()
        return data

    @classmethod
    def from_dict(cls, d: dict) -> "ProfileBrief":
        d = dict(d or {})
        return cls(roles=list(d.get("roles") or []),
                   families=[FamilyYears(**f) for f in d.get("families") or []],
                   experiences=[ExperienceLine(**e) for e in d.get("experiences") or []],
                   sectors=list(d.get("sectors") or []), languages=list(d.get("languages") or []),
                   seniority=d.get("seniority") or "", years_experience=d.get("years_experience"),
                   travel=d.get("travel") or "",
                   directions=[tuple(x) for x in d.get("directions") or []],
                   company_types=list(d.get("company_types") or []),
                   area=SearchArea.from_dict(d.get("area")), dropped=int(d.get("dropped") or 0))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


def _years(value: float | None) -> str:
    if value is None:
        return "?"
    rounded = round(float(value) * 2) / 2
    return str(int(rounded)) if rounded == int(rounded) else f"{rounded:.1f}"


# --- forbidden strings -----------------------------------------------------------------------


class _Scrubber:
    """Knows the identifying strings of one CV and removes them from short values."""

    def __init__(self, profile, extra_forbidden: list[str] | None = None):
        self.phrases: set[str] = set()   # folded multi-word strings (employer names, address...)
        self.tokens: set[str] = set()    # folded distinctive single words
        for experience in getattr(profile, "experiences", None) or []:
            self._add(getattr(experience, "employer", "") or "")
        # `home` is a town usable for geocoding; only its address-like parts (street,
        # house number, postcode) are forbidden: the town may be the chosen area centre.
        for part in re.split(r"[,;\n]", getattr(profile, "home", "") or ""):
            if _STREET.search(part):
                self._add(part, all_words=True)
            else:
                for word in _words(part):
                    if _DIGITS.search(word):
                        self.phrases.add(word)
        for text in extra_forbidden or []:
            self._add(text, all_words=True)
        raw = getattr(profile, "text", "") or ""
        # A CV starts with the person's name: the first short line of capitalised words.
        for line in raw.splitlines()[:5]:
            words = line.strip().split()
            if 2 <= len(words) <= 5 and all(w[:1].isupper() for w in words if w[:1].isalpha()) \
                    and not any(ch.isdigit() for ch in line) and "@" not in line:
                self._add(line, all_words=True)
                break
        self.emails = set(_EMAIL.findall(raw))

    def _add(self, text: str, all_words: bool = False) -> None:
        folded = " ".join(_words(text))
        if not folded:
            return
        self.phrases.add(folded)
        for word in folded.split():
            if len(word) >= 3 and (all_words or word not in _GENERIC_WORDS) and not word.isdigit():
                self.tokens.add(word)

    def clean(self, value: str, limit: int = MAX_ITEM_CHARS) -> str:
        """`value` without e-mails, URLs, phones, digits runs, employer or home words."""
        text = " ".join(str(value or "").split())
        text = _AT_EMPLOYER.sub("", text)
        text = _EMAIL.sub(" ", text)
        text = _URL.sub(" ", text)
        text = _PHONE.sub(" ", text)
        kept = []
        for raw_word in text.split():
            folded = _words(raw_word)
            if any(w in self.tokens for w in folded):
                continue
            kept.append(raw_word)
        text = " ".join(kept).strip(" ,;:-–/|").removesuffix("(").removeprefix(")").strip()
        if text.count("(") > text.count(")"):
            text += ")"
        if _STREET.search(text) and _DIGITS.search(text):
            return ""
        if len(text) > limit:
            text = text[:limit].rsplit(" ", 1)[0]
        return text

    def leaks(self, text: str) -> list[str]:
        """The forbidden strings found in `text` (empty when clean)."""
        folded_words = set(_words(text))
        folded = " ".join(_words(text))
        found = sorted(w for w in self.tokens if w in folded_words)
        found += sorted(p for p in self.phrases if len(p) >= 4 and f" {p} " in f" {folded} ")
        found += sorted(e for e in self.emails if e.casefold() in text.casefold())
        if _EMAIL.search(text):
            found.append("e-mail address")
        if _PHONE.search(text):
            found.append("phone number")
        return sorted(set(found))


# --- skill families ------------------------------------------------------------------------

_FAMILY_NAMES = {
    "automation": "industrial automation (PLC, SCADA, HMI)", "robotics": "robotics", "electrical": "electrical design",
    "dev": "software development", "data": "data and databases", "security": "cybersecurity",
    "network": "networking", "it": "IT systems administration", "management": "project and team management",
    "mechanical": "mechanical design", "cad": "CAD", "office": "office tools", "quality": "quality",
}


def _family_of(skill: str) -> str:
    try:
        from ..cv.skills import dictionary

        entry = dictionary().lookup(skill)
    except Exception:  # noqa: BLE001 - the dictionary is optional here
        entry = None
    return entry.family if entry is not None and entry.family else ""


def _skill_families(profile, scrub: _Scrubber) -> list[FamilyYears]:
    skills = [getattr(s, "value", "") or "" for s in getattr(profile, "skills", None) or []]
    by_family: dict[str, list[str]] = {}
    for skill in skills:
        clean = scrub.clean(skill, 40)
        if not clean:
            continue
        family = _family_of(skill) or "other"
        bucket = by_family.setdefault(family, [])
        if clean.casefold() not in (b.casefold() for b in bucket):
            bucket.append(clean)
    # years per family: the experiences whose title or description names one of its skills
    months: dict[str, int] = {}
    for experience in getattr(profile, "experiences", None) or []:
        length = getattr(experience, "months", None)
        if not length:
            continue
        blob = " ".join([getattr(experience, "title", "") or ""]
                        + [getattr(e, "text", "") or "" for e in getattr(experience, "evidence", None) or []])
        folded = f" {' '.join(_words(blob))} "
        touched = {family for family, names in by_family.items()
                   if any(f" {' '.join(_words(n))} " in folded for n in names if _words(n))}
        for family in touched:
            months[family] = months.get(family, 0) + int(length)
    result = []
    for family, names in sorted(by_family.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if family == "other" and len(by_family) > 1 and len(names) < 2:
            continue
        name = _FAMILY_NAMES.get(family, family.replace("_", " "))
        years = round(months[family] / 12, 1) if months.get(family) else None
        result.append(FamilyYears(name=name, years=years, skills=names[:6]))
    return result[:MAX_FAMILIES]


# --- the brief ---------------------------------------------------------------------------------


def _sector_names(keys: list[str]) -> list[str]:
    try:
        from ..geo.sectors import SECTORS
    except Exception:  # noqa: BLE001
        SECTORS = {}
    out = []
    for key in keys or []:
        sector = SECTORS.get(key)
        name = sector.name_en if sector is not None else str(key).replace("_", " ")
        if name not in out:
            out.append(name)
    return out


def _enabled_directions(preferences) -> list[tuple[str, str]]:
    if preferences is None:
        return []
    enabled = getattr(preferences, "enabled_directions", None)
    directions = enabled() if callable(enabled) else [d for d in getattr(preferences, "directions", None) or []
                                                     if getattr(d, "enabled", True)]
    return [(str(d.id), str(getattr(d, "name", d.id))) for d in directions]


def _wanted_types(preferences) -> list[str]:
    from .prompt import COMPANY_TYPES

    weights = getattr(preferences, "company_types", None) if preferences is not None else None
    if not isinstance(weights, dict):
        return [t for t in COMPANY_TYPES if t != "other"]
    return [t for t in COMPANY_TYPES if t != "other" and float(weights.get(t, 1.0) or 0) > 0]


def _clean_area(area: SearchArea | None, scrub: _Scrubber) -> SearchArea:
    area = SearchArea.from_dict(area.to_dict()) if area is not None else SearchArea(kind=AREA_COUNTRY)
    if area.kind not in AREA_KINDS:
        area.kind = AREA_COUNTRY
    if area.town and (_DIGITS.search(area.town) or _STREET.search(area.town) or "@" in area.town):
        log.info("Suggest brief: the radius centre looked like an address; the province is used instead")
        area.town = ""
    if area.kind == AREA_RADIUS and not (area.town and area.radius_km):
        area.kind = AREA_PROVINCE if area.province else AREA_REGION if area.region else AREA_COUNTRY
    return area


def build_profile_brief(cv_profile, preferences=None, area: SearchArea | None = None, *,
                        employer_class: Callable[[str], tuple[str, str] | None] | None = None,
                        forbidden: list[str] | None = None) -> ProfileBrief:
    """The anonymised brief of `cv_profile` (a CVProfile; read by attribute).

    `preferences` is an ``affinity.AffinityPreferences`` (directions, company_types) or
    None. `employer_class(employer_name) -> (sector, size class)` may describe each
    employer without naming it (the employer name never leaves this function).
    `forbidden` adds strings that must never be sent (e.g. the user's name from the
    settings). Never raises: a broken profile gives an almost empty brief."""
    try:
        scrub = _Scrubber(cv_profile, forbidden)
        brief = ProfileBrief(area=_clean_area(area, scrub))
        # the area must not reveal the home either: but the user chose it, so a town is fine
        for item in getattr(cv_profile, "roles", None) or []:
            value = scrub.clean(getattr(item, "value", "") or "")
            if value and value.casefold() not in (r.casefold() for r in brief.roles):
                brief.roles.append(value)
        brief.roles = brief.roles[:MAX_ROLES]
        brief.families = _skill_families(cv_profile, scrub)
        for experience in (getattr(cv_profile, "experiences", None) or [])[:MAX_EXPERIENCES]:
            title = scrub.clean(getattr(experience, "title", "") or "")
            if not title:
                continue
            months = getattr(experience, "months", None)
            employer = "undisclosed employer"
            name = getattr(experience, "employer", "") or ""
            if employer_class is not None and name:
                try:
                    described = employer_class(name)
                except Exception:  # noqa: BLE001
                    described = None
                if described:
                    sector, size = (scrub.clean(str(described[0]), 40), str(described[1] or "").lower())
                    size = size if size in SIZE_CLASSES else ""
                    if sector or size:
                        employer = "employer: " + ", ".join(x for x in (sector, f"{size} company" if size else "")
                                                            if x)
            brief.experiences.append(ExperienceLine(title=title, years=round(months / 12, 1) if months else None,
                                                    employer=employer))
        brief.sectors = [scrub.clean(s, 50) for s in _sector_names(getattr(cv_profile, "sectors", None) or [])]
        brief.sectors = [s for s in brief.sectors if s]
        for item in (getattr(cv_profile, "spoken_languages", None) or [])[:MAX_LANGUAGES]:
            value = scrub.clean(getattr(item, "value", "") or "", 30)
            if value:
                brief.languages.append(value)
        seniority = str(getattr(cv_profile, "seniority", "") or "").lower()
        brief.seniority = seniority if seniority in ("junior", "mid", "senior") else ""
        years = getattr(cv_profile, "years_experience", None)
        brief.years_experience = round(float(years) * 2) / 2 if isinstance(years, (int, float)) else None
        travel = getattr(getattr(cv_profile, "mobility", None), "travel", "unknown")
        brief.travel = {"declared": "willing to travel", "likely": "probably willing to travel",
                        "excluded": "does not want to travel"}.get(str(travel), "")
        brief.directions = [(i, scrub.clean(n, 60) or i) for i, n in _enabled_directions(preferences)]
        brief.company_types = _wanted_types(preferences)
        _drop_leaks(brief, scrub)
        return brief
    except Exception:  # noqa: BLE001 - never into the caller
        log.exception("Suggest: the profile brief could not be built")
        return ProfileBrief(area=SearchArea(kind=AREA_COUNTRY))


def _drop_leaks(brief: ProfileBrief, scrub: _Scrubber) -> None:
    """Fail closed: any value that still contains a forbidden string is removed."""
    def ok(text: str) -> bool:
        found = scrub.leaks(text)
        if found:
            brief.dropped += 1
            log.warning("Suggest brief: a value was dropped because it contained identifying text")
        return not found

    brief.roles = [r for r in brief.roles if ok(r)]
    brief.sectors = [s for s in brief.sectors if ok(s)]
    brief.languages = [x for x in brief.languages if ok(x)]
    brief.experiences = [e for e in brief.experiences if ok(e.title) and ok(e.employer)]
    for family in brief.families:
        family.skills = [s for s in family.skills if ok(s)]
    brief.directions = [(i, n) for i, n in brief.directions if ok(n)]
    # The area is the user's own choice (checked for address-like text in _clean_area);
    # the rest of the text is checked once more as a whole.
    body = "\n".join(line for line in brief.preview_text().splitlines() if not line.startswith("- Area:"))
    if scrub.leaks(body):  # last line of defence
        log.warning("Suggest brief: identifying text left after cleaning; only the area and directions are kept")
        brief.roles, brief.families, brief.experiences, brief.languages = [], [], [], []
        brief.sectors = []
        brief.dropped += 1


def find_leaks(text: str, cv_profile, forbidden: list[str] | None = None) -> list[str]:
    """Identifying strings of `cv_profile` found in `text` (tests, and a GUI self-check)."""
    return _Scrubber(cv_profile, forbidden).leaks(text)
