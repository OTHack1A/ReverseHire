"""Merge and deduplicate the companies found by several discovery sources.

Identity keys, strongest first:
1. codice fiscale / partita IVA (from a register import, Wikidata, OSM `ref:vatin`);
2. the registrable domain of the website ("www.shop.example.co.uk" -> "example.co.uk");
3. normalised name (legal forms removed) + town, or + position within MERGE_DISTANCE_KM.

Provenance is kept per field (which source said what), so the company card can show
where sources agree and where they conflict. A company's ATECO code is only ever
copied from a register import: nothing here guesses it.

The merged data go into the `Company` fields the company card reads:
- `company.ids`: legal identifiers (keys of models.COMPANY_ID_KEYS: vat, tax_code, rea,
  lei, wikidata, pec, ateco);
- `company.sources`: per-field provenance in the card's format ({"field", "value",
  "source", "url", "date"}; "source" is the card's source name: "osm", "wikidata",
  "register" for an imported register list...; "via" keeps the discovery source);
- `company.fields`: the other values (town, province, phone, industry...); internal
  keys start with "_" ("_discovered_by", "_conflicts") and are never shown or scored.
`info(company)` reads them back in the discovery naming (also from the JSON string
`osm_tags["_discovery"]` written by older versions).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..geo.distance import haversine_km
from ..geo.overpass import _normalized_name
from ..geo.sectors import normalize_text
from ..models import Company

DISCOVERY_TAG = "_discovery"
MERGE_DISTANCE_KM = 0.3
# Identity / contact fields tracked with provenance.
TRACKED = ("name", "site", "address", "vat", "cf", "rea", "ateco", "pec", "phone", "email",
           "wikidata", "lei", "province", "town", "employees", "legal_form")
ID_FIELDS = ("vat", "cf", "rea", "lei", "wikidata")

# Second-level public suffixes common enough to matter (no network list needed).
_TWO_LEVEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "ltd.uk", "plc.uk", "me.uk", "com.au", "net.au",
    "org.au", "co.nz", "co.jp", "ne.jp", "com.br", "com.ar", "com.mx", "com.tr", "com.cn",
    "com.hk", "co.za", "co.in", "co.kr", "com.sg", "com.ua", "co.il", "com.pl", "co.at",
    "or.at", "gv.at", "com.es", "com.pt", "co.it",
}


def registrable_domain(url: str | None) -> str:
    """"https://www.shop.example.co.uk/x" -> "example.co.uk"; "" when not a web address."""
    if not url:
        return ""
    text = url.strip()
    if "://" not in text:
        text = "https://" + text
    try:
        host = (urlsplit(text).hostname or "").lower().strip(".")
    except ValueError:
        return ""
    if not host or "." not in host or re.fullmatch(r"[\d.]+", host):
        return host
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def normalize_vat(value: str | None) -> str:
    """Italian VAT / company tax code: 11 digits ("IT 001 234..." -> "001234..."); a
    16-character personal tax code stays upper case; "" otherwise."""
    text = re.sub(r"[\s.\-]", "", (value or "").upper())
    if text.startswith("IT") and len(text) == 13:
        text = text[2:]
    if re.fullmatch(r"\d{11}", text):
        return text
    if re.fullmatch(r"[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]", text):
        return text
    return ""


def piva_ok(value: str) -> bool:
    """Checksum of an Italian partita IVA (11 digits)."""
    if not re.fullmatch(r"\d{11}", value or ""):
        return False
    total = 0
    for i, char in enumerate(value[:10]):
        digit = int(char)
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return (10 - total % 10) % 10 == int(value[10])


def name_key(name: str) -> str:
    return _normalized_name(name or "")


def town_key(text: str) -> str:
    return normalize_text(text or "")


# --- discovery info on a Company ------------------------------------------------------------


def fields_of(company: Company) -> dict:
    """Extra fields attached by a source (`company.fields`), internal "_" keys removed."""
    value = getattr(company, "fields", None)
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if not str(k).startswith("_")}


# Discovery source -> source name of the company card's provenance (registry/confirm.py).
CARD_SOURCE = {"osm_extract": "osm", "overpass": "osm", "wikidata": "wikidata", "imports": "register",
               "regional_register": "register", "overture": "overture"}
# Sources whose exact ATECO code and legal name are trusted (register data, never guessed):
# the regional register (official lists, bought and imported lists, the user's edits) and
# the imported lists of older versions.
REGISTER_SOURCES = ("regional_register", "imports")
# Discovery field -> card field (the card calls the codice fiscale "tax_code" and the site "website").
CARD_FIELD = {"cf": "tax_code", "site": "website"}
DISCOVERY_FIELD = {v: k for k, v in CARD_FIELD.items()}
# Fields copied into Company.ids (card naming).
CARD_ID_FIELDS = ("vat", "tax_code", "rea", "lei", "wikidata", "pec", "ateco")
# Fields whose provenance goes to Company.sources (shown on the card with their source).
CARD_FACT_FIELDS = ("name", "site", "vat", "cf", "rea", "ateco", "pec", "phone", "email", "wikidata",
                    "lei", "town", "province", "postcode", "employees", "legal_form")


def _source_url(source: str, field_name: str, value: str, company: Company) -> str:
    if source == "wikidata" and field_name == "wikidata":
        return f"https://www.wikidata.org/wiki/{value}"
    if source == "wikidata":
        entity = (company.ids or {}).get("wikidata") or fields_of(company).get("wikidata")
        return f"https://www.wikidata.org/wiki/{entity}" if entity else ""
    if source in ("osm_extract", "overpass"):
        osm_id = (company.osm_tags or {}).get("osm_id", "")
        return f"https://www.openstreetmap.org/{osm_id}" if osm_id else ""
    return ""


def _legacy(company: Company) -> dict:
    raw = (company.osm_tags or {}).get(DISCOVERY_TAG)
    if not raw:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def info(company: Company) -> dict:
    """{"sources": [...], "ids": {...}, "fields": {...}, "provenance": {field: {source: value}},
    "conflicts": [...]} in the discovery naming (ids "vat", "cf", "rea", "lei", "wikidata");
    empty dict for a company no discovery merge touched."""
    old = _legacy(company)
    if old:
        return old
    extra = getattr(company, "fields", None) or {}
    by = [s for s in str(extra.get("_discovered_by", "") or "").split(",") if s]
    if not by:
        return {}
    ids = {}
    for key, value in (company.ids or {}).items():
        name = DISCOVERY_FIELD.get(key, key)
        if name in ID_FIELDS and value:
            ids[name] = value
    fields = fields_of(company)
    for key in ("ateco", "pec"):
        if company.ids.get(key):
            fields.setdefault(key, company.ids[key])
    provenance: dict = {}
    for fact in company.sources or []:
        via = fact.get("via")
        if not via:
            continue
        name = DISCOVERY_FIELD.get(str(fact.get("field")), str(fact.get("field")))
        provenance.setdefault(name, {}).setdefault(via, fact.get("value"))
    conflicts = [c for c in str(extra.get("_conflicts", "") or "").split(",") if c]
    return {"sources": by, "ids": ids, "fields": fields, "provenance": provenance, "conflicts": conflicts}


def ateco_of(company: Company) -> str:
    """The register ATECO code of a company ("" when unknown: it is never guessed)."""
    return str((company.ids or {}).get("ateco", "") or info(company).get("fields", {}).get("ateco", "")
               or fields_of(company).get("ateco", "") or "")


def sources_of(company: Company) -> list[str]:
    return list(info(company).get("sources") or [])


# --- merging ------------------------------------------------------------------------------------


@dataclass
class _Entry:
    company: Company
    sources: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)   # field -> {source: value}


def _values(company: Company) -> dict:
    extra = fields_of(company)
    for key, value in (company.ids or {}).items():
        extra.setdefault(DISCOVERY_FIELD.get(key, key), value)
    values = {"name": company.name, "site": company.site or "", "address": company.address or ""}
    for key in TRACKED:
        if key not in values and extra.get(key):
            values[key] = str(extra[key])
    if extra.get("municipality") and not values.get("town"):
        values["town"] = str(extra["municipality"])
    vat = normalize_vat(values.get("vat", ""))
    if vat:
        values["vat"] = vat
    cf = normalize_vat(values.get("cf", ""))
    if cf:
        values["cf"] = cf
    return {k: v for k, v in values.items() if v}


def _same(field_name: str, a: str, b: str) -> bool:
    if field_name == "site":
        return registrable_domain(a) == registrable_domain(b)
    if field_name == "name":
        return name_key(a) == name_key(b)
    if field_name in ("vat", "cf"):
        return normalize_vat(a) == normalize_vat(b)
    return normalize_text(a) == normalize_text(b)


class Merger:
    """Feed the companies of each source in priority order with `add(source, companies)`;
    `companies()` returns the merged list. `add` returns how many were new."""

    def __init__(self, distance_km: float = MERGE_DISTANCE_KM):
        self.distance_km = distance_km
        self._entries: list[_Entry] = []
        self._by_id: dict[str, _Entry] = {}
        self._by_domain: dict[str, _Entry] = {}
        self._by_name_town: dict[tuple[str, str], _Entry] = {}
        self._by_name: dict[str, list[_Entry]] = {}

    def _find(self, company: Company, values: dict) -> _Entry | None:
        for key in ("vat", "cf"):
            if values.get(key) and values[key] in self._by_id:
                return self._by_id[values[key]]
        domain = registrable_domain(company.site)
        if domain and domain in self._by_domain:
            return self._by_domain[domain]
        key = name_key(company.name)
        if not key:
            return None
        town = town_key(values.get("town", ""))
        if town and (key, town) in self._by_name_town:
            return self._by_name_town[(key, town)]
        if company.lat is not None and company.lon is not None:
            for entry in self._by_name.get(key, ()):
                other = entry.company
                if other.lat is not None and other.lon is not None and haversine_km(
                        company.lat, company.lon, other.lat, other.lon) <= self.distance_km:
                    return entry
        return None

    def _index(self, entry: _Entry, values: dict) -> None:
        for key in ("vat", "cf"):
            if values.get(key):
                self._by_id.setdefault(values[key], entry)
        domain = registrable_domain(entry.company.site)
        if domain:
            self._by_domain.setdefault(domain, entry)
        key = name_key(entry.company.name)
        if key:
            town = town_key(values.get("town", ""))
            if town:
                self._by_name_town.setdefault((key, town), entry)
            if entry not in self._by_name.setdefault(key, []):
                self._by_name[key].append(entry)

    def add(self, source: str, companies: list[Company]) -> int:
        new = 0
        for company in companies:
            values = _values(company)
            entry = self._find(company, values)
            if entry is None:
                entry = _Entry(company)
                self._entries.append(entry)
                new += 1
            else:
                self._fill(entry.company, company)
            if source not in entry.sources:
                entry.sources.append(source)
            for key, value in values.items():
                entry.provenance.setdefault(key, {}).setdefault(source, value)
            # Index with every value known so far (a later source may match on any of them).
            merged = {k: next(iter(v.values())) for k, v in entry.provenance.items()}
            self._index(entry, merged)
        return new

    @staticmethod
    def _fill(target: Company, other: Company) -> None:
        if not target.site and other.site:
            target.site = other.site
        if not target.address and other.address:
            target.address = other.address
        if target.lat is None and other.lat is not None:
            target.lat, target.lon = other.lat, other.lon
        if other.distance_km is not None and (target.distance_km is None):
            target.distance_km = other.distance_km
        for key, value in (other.osm_tags or {}).items():
            if key != DISCOVERY_TAG and key not in target.osm_tags:
                target.osm_tags[key] = value
        extra = dict(target.fields or {})
        for key, value in fields_of(other).items():
            extra.setdefault(key, value)
        target.fields = extra
        for key, value in (other.ids or {}).items():
            target.ids.setdefault(key, value)

    def companies(self) -> list[Company]:
        result = []
        for entry in self._entries:
            attach(entry.company, entry.sources, entry.provenance)
            result.append(entry.company)
        return result


def attach(company: Company, sources: list[str], provenance: dict, when: str | None = None) -> None:
    """Fill `company.ids`, `company.sources` (card provenance) and `company.fields` from
    the merged values (see the module docstring); nothing goes to `osm_tags`."""
    from datetime import date

    company.osm_tags.pop(DISCOVERY_TAG, None)
    today = when or date.today().isoformat()
    chosen: dict[str, str] = {}
    conflicts: list[str] = []
    for key, by_source in provenance.items():
        values = list(by_source.values())
        if not values:
            continue
        chosen[key] = values[0]
        if any(not _same(key, values[0], v) for v in values[1:]):
            conflicts.append(key)
    ids = dict(company.ids or {})
    for key in ID_FIELDS + ("ateco", "pec"):
        value = chosen.get(key)
        card_key = CARD_FIELD.get(key, key)
        if value and card_key in CARD_ID_FIELDS:
            if key == "ateco":
                # ATECO only from a register list: never from OSM or Wikidata
                by_source = provenance.get("ateco", {})
                register = next((by_source[s] for s in REGISTER_SOURCES if by_source.get(s)), None)
                if not register:
                    continue
                ids[card_key] = str(register)   # the register's exact code is trusted over any other
                continue
            ids.setdefault(card_key, str(value))
    company.ids = ids
    extra = dict(company.fields or {})
    for key, value in chosen.items():
        if key in ("name", "site", "address") or key in ID_FIELDS or key in ("ateco", "pec"):
            continue
        extra.setdefault(key, value)
    for key in ("ateco", "pec"):
        extra.pop(key, None)
    extra["_discovered_by"] = ",".join(sources)
    if conflicts:
        extra["_conflicts"] = ",".join(conflicts)
    else:
        extra.pop("_conflicts", None)
    company.fields = extra
    facts = [dict(f) for f in company.sources or [] if not f.get("via")]
    seen = {(f.get("field"), str(f.get("value")), f.get("source")) for f in facts}
    for key, by_source in provenance.items():
        if key not in CARD_FACT_FIELDS:
            continue
        for source, value in by_source.items():
            card_source = CARD_SOURCE.get(source)
            if not card_source or not value:
                continue
            if key in ("ateco", "name") and source not in REGISTER_SOURCES:
                continue  # ATECO and the legal name only from a register list
            card_field = CARD_FIELD.get(key, key)
            marker = (card_field, str(value), card_source)
            if marker in seen:
                continue
            seen.add(marker)
            facts.append({"field": card_field, "value": str(value), "source": card_source,
                          "url": _source_url(source, key, str(value), company), "date": today,
                          "via": source})
    company.sources = facts


def merge(results: list[tuple[str, list[Company]]]) -> tuple[list[Company], dict[str, int]]:
    """Merge (source, companies) pairs given in priority order: (companies, new per source)."""
    merger = Merger()
    new = {source: merger.add(source, companies) for source, companies in results}
    return merger.companies(), new
