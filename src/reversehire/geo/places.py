"""From a messy place text to structured fields: country, region, province, municipality.

    normalise_location("CASTEL MAGGIORE (BO)")   -> Castel Maggiore (BO), Emilia-Romagna, Italy
    normalise_location("Milan, Italy")           -> Milano (MI)
    normalise_location("Lugano, Svizzera")       -> Lugano, Switzerland (country only)
    normalise_location("Dogana")                 -> Dogana, San Marino

Only the app's own data is used (geo/italy.py tables, the built-in towns of the job
search, the OpenStreetMap place index of the regions downloaded in tab 2): no network.
Every result says how it was found (`provenance`): "sigla", "province-name", "town",
"osm-index", "region", "country", "foreign-town", "san-marino", "coordinates",
"text" (an unknown town kept as written) or "" (nothing found).

Foreign places keep the country only (and the town as written); San Marino is a
country of its own. Results are cached (texts repeat a lot in the lists).
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Callable

from . import italy
from .italy import fold

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Location:
    country: str = ""            # display name (Italian table name; see display())
    country_code: str = ""       # ISO alpha-2, "" unknown
    region_key: str = ""         # geo/regions.py key, Italy only
    region_name: str = ""
    province_code: str = ""      # sigla, Italy only
    province_name: str = ""
    municipality: str = ""
    provenance: str = ""

    @property
    def known(self) -> bool:
        return bool(self.country_code or self.municipality)

    @property
    def italian(self) -> bool:
        return self.country_code == "IT"

    def display(self, language: str | None = None) -> str:
        """"Castel Maggiore (BO)" in Italy, "Lugano, Svizzera" abroad, "Forlì-Cesena (FC)"
        for a province only, the region or country name when that is all there is."""
        language = language or _language()
        if self.country_code == "IT" or (not self.country_code and self.province_code):
            if self.municipality and self.province_code:
                return f"{self.municipality} ({self.province_code})"
            if self.province_code:
                return f"{self.province_name} ({self.province_code})"
            if self.municipality:
                return self.municipality
            if self.region_name:
                return self.region_name
            return italy.country_name("IT", language)
        country = italy.country_name(self.country_code, language) if self.country_code else ""
        if self.municipality and country and fold(self.municipality) != fold(country):
            return f"{self.municipality}, {country}"
        return self.municipality or country

    def country_label(self, language: str | None = None) -> str:
        return italy.country_name(self.country_code, language or _language()) if self.country_code else ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "Location":
        known = cls.__dataclass_fields__
        return cls(**{k: str(v or "") for k, v in (data or {}).items() if k in known})


EMPTY = Location()


def _language() -> str:
    try:
        from ..i18n import current_language

        return current_language()
    except Exception:  # noqa: BLE001
        return "it"


# --- the town lookups ---------------------------------------------------------------------

_place_lookup: Callable[[str], tuple | None] | None = None
_osm_lock = threading.Lock()
_osm_indexes: dict = {}


def set_place_lookup(lookup: Callable[[str], tuple | None] | None) -> None:
    """Replace the OpenStreetMap index lookup (name -> (lat, lon, province) | None);
    tests pass a fake, None restores the default."""
    global _place_lookup
    _place_lookup = lookup
    clear_cache()


def clear_cache() -> None:
    _normalise_cached.cache_clear()
    _town.cache_clear()


def _osm_lookup(name: str) -> tuple | None:
    """The place index of every region already downloaded (tab 2), largest place first."""
    try:
        from ..discovery import osm_extract
        from .regions import REGIONS

        for region in REGIONS.values():
            path = osm_extract.index_path(region)
            if not path.exists():
                continue
            with _osm_lock:
                index = _osm_indexes.get(path)
                if index is None:
                    index = _osm_indexes[path] = osm_extract.OsmIndex(path)
            found = index.find_place(name)
            if found and found[2]:
                return found
    except Exception:  # noqa: BLE001 - no index: built-in towns only
        log.debug("OSM place index not usable", exc_info=True)
    return None


@lru_cache(maxsize=1)
def _builtin_towns() -> dict[str, str]:
    """Folded town name -> province code: the job search's built-in towns, their aliases
    and every province capital."""
    towns: dict[str, str] = {}
    try:
        from ..jobsearch.lexicon import TOWN_ALIASES, TOWNS

        for name, (_lat, _lon, code) in TOWNS.items():
            towns[fold(name)] = code
        for alias, name in TOWN_ALIASES.items():
            if name in TOWNS:
                towns[fold(alias)] = TOWNS[name][2]
    except Exception:  # noqa: BLE001
        pass
    for province in italy.PROVINCES.values():
        towns.setdefault(fold(province.capital), province.code)
    return towns


@lru_cache(maxsize=4096)
def _town(name_folded: str) -> tuple[str, str] | None:
    """(province code, provenance) of an Italian town name; None if unknown."""
    if not name_folded or len(name_folded) < 2:
        return None
    found = _builtin_towns().get(name_folded)
    if found:
        return found, "town"
    lookup = _place_lookup or _osm_lookup
    try:
        found = lookup(name_folded)
    except Exception:  # noqa: BLE001
        found = None
    if found and len(found) > 2 and found[2] and italy.province(found[2]):
        return italy.province(found[2]).code, "osm-index"
    return None


# --- text helpers --------------------------------------------------------------------------

_LOWER_WORDS = {"di", "del", "dei", "della", "delle", "degli", "dello", "e", "ed", "in", "sul", "sulla",
                "al", "alla", "a", "da", "con", "sotto", "sopra", "nel", "nella", "lo", "la", "le", "de"}
_ACCENTS = {"a'": "à", "e'": "è", "i'": "ì", "o'": "ò", "u'": "ù"}
_STREET = re.compile(r"^(via|viale|v\.le|piazza|p\.za|piazzale|corso|c\.so|largo|strada|vicolo|loc\.?|"
                     r"localita|località|frazione|fraz\.?|zona industriale|z\.i\.?|str\.|street|road|"
                     r"unione|comunita|comunità|citta metropolitana|città metropolitana)\b", re.IGNORECASE)
_SIGLA = re.compile(r"\(\s*([A-Za-z]{2})\s*\)")
_POSTCODE = re.compile(r"\b(?:I-|IT-|CH-|D-|A-|F-)?\d{4,5}\b")
_REMOTE = re.compile(r"\b(full[ -]?remote|fully remote|remote|remoto|da remoto|smart[ -]?working|telelavoro|"
                     r"hybrid|ibrido|home[ -]?office|anywhere|ovunque|homeworking)\b", re.IGNORECASE)
_PROVINCE_OF = re.compile(r"^(?:provincia di|prov\.? di|prov\.?|province of|provincia|area di|zona di)\s+(.+)$")
_AND_PROVINCE = re.compile(r"^(.+?)\s+(?:e|and)\s+(?:provincia|province|dintorni|hinterland)$")


def pretty_town(text: str) -> str:
    """"CASTEL MAGGIORE" -> "Castel Maggiore", "FORLI'" -> "Forlì", "anzola dell'emilia"
    -> "Anzola dell'Emilia". Mixed-case input is kept."""
    value = " ".join(str(text or "").split()).strip(" ,;-")
    if not value:
        return ""
    if value != value.upper() and value != value.lower():
        out = value
    else:
        words = []
        for index, word in enumerate(value.lower().split(" ")):
            if index and word in _LOWER_WORDS:
                words.append(word)
            elif "'" in word[:-1] and not word.endswith("'"):
                head, _, tail = word.partition("'")
                head = head if (index and head in ("dell", "dall", "nell", "sull", "d", "l", "all")) \
                    else head.capitalize()
                words.append(f"{head}'{tail.capitalize()}")
            else:
                words.append(word.capitalize())
        out = " ".join(words)
    for plain, accented in _ACCENTS.items():
        if out.lower().endswith(plain):
            out = out[:-2] + (accented.upper() if out[-2].isupper() and out[:-2].isupper() else accented)
    return out


def _parts(text: str) -> list[str]:
    """Pieces of the text: separated by commas, " - ", "/", "|", "·", ";" (not inside
    names like "Forlì-Cesena")."""
    pieces = re.split(r"\s*[,;|·•]\s*|\s+[-–—/]\s+|\n", text)
    return [p.strip(" .()[]") for p in pieces if p and p.strip(" .()[]")]


# --- the normaliser ------------------------------------------------------------------------

def normalise_location(text: str | None, *, lat: float | None = None, lon: float | None = None) -> Location:
    """Structured place of a text (and/or coordinates). Never raises."""
    try:
        return _normalise_cached(" ".join(str(text or "").split()),
                                 None if lat is None else round(float(lat), 4),
                                 None if lon is None else round(float(lon), 4))
    except Exception:  # noqa: BLE001 - a place that cannot be read stays unknown
        log.debug("Place not normalised", exc_info=True)
        return EMPTY


def _italian(province_code: str = "", municipality: str = "", region: str = "", provenance: str = "") -> Location:
    found = italy.province(province_code) if province_code else None
    region = found.region if found is not None else region
    return Location(country=italy.country_name("IT", "it"), country_code="IT", region_key=region or "",
                    region_name=italy.region_name(region), province_code=found.code if found else "",
                    province_name=found.name if found else "", municipality=municipality, provenance=provenance)


def _foreign(code: str, municipality: str = "", provenance: str = "country") -> Location:
    return Location(country=italy.country_name(code, "it"), country_code=code, municipality=municipality,
                    provenance=provenance)


@lru_cache(maxsize=8192)
def _normalise_cached(text: str, lat: float | None, lon: float | None) -> Location:
    result = _from_text(text) if text else EMPTY
    if lat is not None and lon is not None and not (result.province_code or
                                                    (result.country_code and result.country_code != "IT")):
        by_point = _from_point(lat, lon)
        if by_point.known:
            if result.municipality and by_point.country_code == "IT" and not by_point.municipality:
                by_point = _italian(by_point.province_code, result.municipality, by_point.region_key,
                                    result.provenance + "+coordinates")
            return by_point
    return result


def _from_point(lat: float, lon: float) -> Location:
    if italy.in_san_marino(lat, lon):
        return _foreign("SM", provenance="coordinates")
    if not italy.in_italy_box(lat, lon):
        return EMPTY
    from .distance import haversine_km

    nearest = min(italy.PROVINCES.values(), key=lambda p: haversine_km(lat, lon, p.lat, p.lon))
    if haversine_km(lat, lon, nearest.lat, nearest.lon) > 45:
        return _italian(provenance="coordinates")
    return _italian(nearest.code, provenance="coordinates")


def _from_text(text: str) -> Location:
    sigla = ""
    match = _SIGLA.search(text)
    if match and italy.province(match.group(1)):
        sigla = italy.province(match.group(1)).code
        text = (text[:match.start()] + " " + text[match.end():]).strip()
    text = _REMOTE.sub(" ", text)
    text = _POSTCODE.sub(" ", text)
    parts = _parts(text)

    country = ""
    country_provenance = ""
    region = ""
    province_code = sigla
    provenance = "sigla" if sigla else ""
    municipality = ""
    unknown: list[str] = []
    foreign_town = ""

    for part in parts:
        folded = fold(part)
        if not folded:
            continue
        # a trailing sigla without brackets: "CASTEL MAGGIORE BO", "Milano MI"
        tail = part.rsplit(" ", 1)
        if len(tail) == 2 and len(tail[1]) == 2 and tail[1].isupper() and italy.province(tail[1]) \
                and not italy.country_code(tail[1]):
            if not province_code:
                province_code, provenance = italy.province(tail[1]).code, "sigla"
            part, folded = tail[0], fold(tail[0])
        code = italy.country_code(part)
        if code and (len(folded) > 2 or part.isupper()):
            country = country or code
            country_provenance = country_provenance or "country"
            continue
        key = italy.region_key(part)
        if key:
            region = region or key
            continue
        match_of = _PROVINCE_OF.match(folded) or _AND_PROVINCE.match(folded)
        if match_of:
            found = italy.province_by_name(match_of.group(1)) or italy.province(match_of.group(1))
            if found is not None:
                province_code = province_code or found.code
                provenance = provenance or "province-name"
                continue
        foreign = italy.foreign_town(part)
        if foreign is not None:
            foreign_town = foreign_town or foreign[0]
            country = country or foreign[1]
            country_provenance = country_provenance or ("san-marino" if foreign[1] == "SM" else "foreign-town")
            continue
        found = italy.province_by_name(part)
        if found is not None:
            province_code = province_code or found.code
            provenance = provenance or "province-name"
            if folded == fold(found.capital) or _builtin_towns().get(folded) == found.code:
                municipality = municipality or found.capital   # "Milan" -> "Milano"
            continue
        if _STREET.match(part) or any(ch.isdigit() for ch in part) or len(folded) < 2:
            continue
        unknown.append(part)

    # foreign country stated: keep the country (and a town written with it)
    if country and country != "IT":
        town = foreign_town or (pretty_town(unknown[0]) if unknown else "")
        return _foreign(country, town, country_provenance or "country")

    # an unknown piece that is an Italian town
    for part in unknown:
        town = _town(fold(part))
        if town is not None:
            code, how = town
            if province_code and code != province_code and provenance == "sigla":
                continue   # "Castel Maggiore (BO)": the sigla wins, the name stays below
            municipality = municipality or pretty_town(part)
            if not province_code:
                province_code, provenance = code, how
            elif provenance in ("sigla", "province-name"):
                provenance = provenance + "+" + how
            break
    if not municipality and unknown and (province_code or region or country == "IT"):
        municipality = pretty_town(unknown[0])
        provenance = provenance or "text"

    if province_code or region or country == "IT":
        return _italian(province_code, municipality, region,
                        provenance or ("region" if region else "country"))
    if unknown:
        return Location(municipality=pretty_town(unknown[0]), provenance="text")
    return EMPTY


# --- the places of the app's objects --------------------------------------------------------

def location_of_company(company=None, *, card=None, assessment=None) -> Location:
    """The workplace of a result / card: the best posting's location when it states
    one, else the company's town and province (registry, discovery, OSM tags), with the
    coordinates as the last resort."""
    from .. import companies as _companies

    text = _companies.workplace_raw(card, company=company, assessment=assessment)
    obj = card if card is not None else company
    lat = getattr(obj, "lat", None) if obj is not None else None
    lon = getattr(obj, "lon", None) if obj is not None else None
    return normalise_location(text, lat=lat, lon=lon)


def location_of_posting(posting) -> Location:
    """A job posting: its town / province / region / country fields and location text."""
    if posting is None:
        return EMPTY
    get = (lambda key: posting.get(key)) if isinstance(posting, dict) else (lambda key: getattr(posting, key, None))
    pieces = []
    town, province_value = get("town"), get("province")
    if town:
        pieces.append(str(town) + (f" ({province_value})" if province_value and len(str(province_value)) == 2 else ""))
    if province_value and not (town and len(str(province_value)) == 2):
        pieces.append(str(province_value))
    for key in ("region", "country"):
        if get(key):
            pieces.append(str(get(key)))
    location = get("location") or ""
    text = ", ".join(pieces) if pieces else str(location)
    found = normalise_location(text, lat=get("lat"), lon=get("lon"))
    if not found.known and location and pieces:
        found = normalise_location(str(location), lat=get("lat"), lon=get("lon"))
    return found


def location_of_search(search: dict | None) -> Location:
    """A saved company search: its place text (Nominatim's long name) and centre."""
    if not search:
        return EMPTY
    return normalise_location(search.get("place") or "", lat=search.get("lat"), lon=search.get("lon"))


def geocode_query(name: str) -> str:
    """The text to geocode for a posting's place: a bare Italian town is looked up in
    Italy ("Viadana" -> "Viadana, Italia"); a foreign town or country is NOT forced into
    Italy ("Vienna" -> "Vienna, Austria", "London" -> "London, United Kingdom"); a text
    with a comma ("Town, Region, Country") is used as it is."""
    name = " ".join(str(name or "").split())
    if not name or "," in name:
        return name
    found = normalise_location(name)
    if found.country_code and found.country_code != "IT":
        country = italy.country_name(found.country_code, "en")
        town = found.municipality or ""
        return f"{town}, {country}" if town and fold(town) != fold(country) else country
    return f"{name}, Italia"
