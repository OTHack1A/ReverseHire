"""Companies within a radius of a point, via the Overpass API (OpenStreetMap).

Moderate use of the public service: 7-day cache, pause between queries,
retries with increasing wait on 429/504, splitting of large radii.
Data (c) OpenStreetMap contributors (ODbL license).
"""

import math
import re
import threading
import time
from typing import Callable

import httpx

from ..store import Store
from ..config import Settings
from ..models import Company
from .distance import haversine_km
from .sectors import SECTORS, normalize_text, search_tags

CACHE_NAMESPACE = "overpass"
CACHE_TTL_S = 7 * 24 * 3600
KEY_VERSION = "v1"

SPLIT_THRESHOLD_KM = 25.0   # beyond this radius the area is split into several boxes
BOX_SIDE_KM = 20.0
TIMEOUT_QUERY_S = 60            # declared in the query ([timeout:N])
HTTP_MARGIN_S = 20.0           # HTTP client margin beyond the query timeout
PAUSE_BETWEEN_QUERIES_S = 3.0
ATTEMPTS = 2  # a busy public server rarely recovers within a few seconds
BASE_WAIT_S = 5.0
DUPLICATE_DISTANCE_KM = 0.1

RETRY_CODES = {429, 502, 503, 504}

# Generic set (sectors=None): all useful company types.
GENERIC_AMENITIES = (
    "bank", "pharmacy", "clinic", "doctors", "dentist", "hospital", "veterinary",
    "school", "kindergarten", "college", "university", "research_institute",
    "townhall", "courthouse", "social_facility", "restaurant",
)
GENERIC_TAGS: list[tuple[str, str]] = (
    [("office", "*"), ("craft", "*"), ("industrial", "*"), ("man_made", "works"),
     ("shop", "*")]
    + [("amenity", v) for v in GENERIC_AMENITIES]
)

# OSM tags kept on the Company (those useful for the assessment).
USEFUL_TAGS = (
    "office", "craft", "industrial", "man_made", "shop", "amenity", "healthcare",
    "tourism", "power", "leisure", "landuse", "description", "operator", "brand",
    "product",  # what a works/factory makes (ranking signal)
)


class OverpassError(Exception):
    """Overpass unreachable or response unusable after the retries."""


def _wait(seconds: float, cancel: threading.Event | None = None) -> None:
    if cancel is not None:
        cancel.wait(seconds)
    else:
        time.sleep(seconds)


# --- Query building ---------------------------------------------------


def tags_for_sectors(sectors: list[str] | None) -> list[tuple[str, str]]:
    """(key, value) pairs to search for; generic set if `sectors` is empty/None."""
    if sectors:
        pairs: list[tuple[str, str]] = []
        for key in sectors:
            if key in SECTORS:
                for pair in SECTORS[key].osm_tags:
                    if pair not in pairs:
                        pairs.append(pair)
        if pairs:
            return pairs
    return list(GENERIC_TAGS)


def search_pairs(sectors: list[str] | None, include_generic: bool = False) -> list[tuple[str, str]]:
    """Pairs to search: the sector tags, plus the generic company tags with
    `include_generic` (sectors.GENERIC_COMPANY_TAGS); generic set without sectors."""
    if include_generic:
        pairs = search_tags(sectors, include_generic=True)
        if pairs:
            return pairs
    return tags_for_sectors(sectors)


def _selectors(pairs: list[tuple[str, str]]) -> list[str]:
    """Overpass filters per key: `["k"]` if any value matches, otherwise a regex."""
    by_key: dict[str, list[str]] = {}
    for key, value in pairs:
        by_key.setdefault(key, []).append(value)
    selectors = []
    for key, values in by_key.items():
        if "*" in values:
            selectors.append(f'["{key}"]')
        else:
            unique = sorted(set(values))
            selectors.append(f'["{key}"~"^({"|".join(unique)})$"]')
    return selectors


def build_query(
    pairs: list[tuple[str, str]],
    area: str,
    count: bool = False,
    timeout_s: int = TIMEOUT_QUERY_S,
) -> str:
    """Overpass QL query. `area` is `(around:m,lat,lon)` or `(s,w,n,e)`."""
    rows = [f"[out:json][timeout:{timeout_s}];", "("]
    for selector in _selectors(pairs):
        rows.append(f'  nwr{selector}["name"]{area};')
    rows.append(");")
    rows.append("out count;" if count else "out center tags;")
    return "\n".join(rows)


def _radius_area(lat: float, lon: float, radius_km: float) -> str:
    return f"(around:{int(round(radius_km * 1000))},{lat:.6f},{lon:.6f})"


def compute_boxes(
    lat: float, lon: float, radius_km: float, side_km: float = BOX_SIDE_KM
) -> list[tuple[float, float, float, float]]:
    """Boxes (south, west, north, east) covering the circle; boxes outside it are excluded."""
    lat_deg = radius_km / 111.32
    lon_deg = radius_km / (111.32 * max(0.05, math.cos(math.radians(lat))))
    cells = max(1, math.ceil(2 * radius_km / side_km))
    lat_step = 2 * lat_deg / cells
    lon_step = 2 * lon_deg / cells
    boxes = []
    for i in range(cells):
        for j in range(cells):
            south_lat = lat - lat_deg + i * lat_step
            west = lon - lon_deg + j * lon_step
            north, east = south_lat + lat_step, west + lon_step
            # Box point closest to the centre: if it is outside the radius, skip the box.
            p_lat = min(max(lat, south_lat), north)
            p_lon = min(max(lon, west), east)
            if haversine_km(lat, lon, p_lat, p_lon) <= radius_km:
                boxes.append((max(-90.0, south_lat), max(-180.0, west),
                                 min(90.0, north), min(180.0, east)))
    return boxes


def _box_area(r: tuple[float, float, float, float]) -> str:
    return f"({r[0]:.6f},{r[1]:.6f},{r[2]:.6f},{r[3]:.6f})"


# --- Element conversion -------------------------------------------------


def normalize_site(value: str | None) -> str | None:
    """URL with a scheme (https if missing); None if it does not look like a web address."""
    if not value:
        return None
    site = value.split(";")[0].strip()
    if not site or " " in site:
        return None
    if site.startswith("//"):
        site = "https:" + site
    elif "://" not in site:
        site = "https://" + site
    if not site.lower().startswith(("http://", "https://")):
        return None
    host = site.split("://", 1)[1].split("/", 1)[0]
    if "." not in host:
        return None
    return site


def _site_key(site: str) -> str:
    rest = site.split("://", 1)[1].lower()
    if rest.startswith("www."):
        rest = rest[4:]
    return rest.rstrip("/")


_LEGAL_FORM_SUFFIXES = {"srl", "spa", "snc", "sas", "sa", "ltd", "llc", "gmbh", "inc", "srls"}


_DOTTED_FORMS = re.compile(r"\b(s r l s|s r l|s p a|s n c|s a s)\b")


def _normalized_name(name: str) -> str:
    text = _DOTTED_FORMS.sub(lambda m: m.group().replace(" ", ""), normalize_text(name))
    words = [p for p in text.split() if p not in _LEGAL_FORM_SUFFIXES]
    return " ".join(words)


def _address(tag: dict) -> str:
    via = " ".join(p for p in (tag.get("addr:street"), tag.get("addr:housenumber")) if p)
    locality = " ".join(p for p in (tag.get("addr:postcode"), tag.get("addr:city")) if p)
    return ", ".join(p for p in (via, locality) if p)


def element_to_company(element: dict, lat0: float, lon0: float) -> Company | None:
    """Convert an Overpass element into a Company; None if it has no name or no position."""
    tag = element.get("tags") or {}
    name = (tag.get("name") or "").strip()
    if not name:
        return None
    if "lat" in element and "lon" in element:
        lat, lon = element["lat"], element["lon"]
    else:
        center = element.get("center") or {}
        lat, lon = center.get("lat"), center.get("lon")
    if lat is None or lon is None:
        return None
    site = None
    for key in ("website", "contact:website", "url"):
        site = normalize_site(tag.get(key))
        if site:
            break
    useful_tags = {k: tag[k] for k in USEFUL_TAGS if k in tag}
    if "type" in element and "id" in element:
        # Not an OSM tag: identifies the source object (e.g. "node/123").
        useful_tags["osm_id"] = f"{element['type']}/{element['id']}"
    return Company(
        name=name,
        lat=float(lat),
        lon=float(lon),
        address=_address(tag),
        site=site,
        osm_tags=useful_tags,
        distance_km=round(haversine_km(lat0, lon0, float(lat), float(lon)), 3),
        origin="osm",
    )


def remove_duplicates(companies: list[Company]) -> list[Company]:
    """Merge companies with the same normalised name within ~100 m or the same site.

    Keeps the first one (the nearest, if the list is sorted) and fills in its
    missing data (site, address) from the duplicates.
    """
    kept: list[Company] = []
    by_name: dict[str, list[Company]] = {}
    by_site: dict[str, Company] = {}
    for co in companies:
        name_key = _normalized_name(co.name)
        site_key = _site_key(co.site) if co.site else None
        original = by_site.get(site_key) if site_key else None
        if original is None and name_key:
            for other in by_name.get(name_key, ()):
                if (
                    co.lat is not None and other.lat is not None
                    and haversine_km(co.lat, co.lon, other.lat, other.lon) <= DUPLICATE_DISTANCE_KM
                ):
                    original = other
                    break
        if original is not None:
            if not original.site and co.site:
                original.site = co.site
                by_site.setdefault(_site_key(co.site), original)
            if not original.address and co.address:
                original.address = co.address
            continue
        kept.append(co)
        if name_key:
            by_name.setdefault(name_key, []).append(co)
        if site_key:
            by_site[site_key] = co
    return kept


# --- Client ---------------------------------------------------------------------


class OverpassClient:
    """Search companies within a radius with Overpass, with cache and error handling."""

    def __init__(
        self,
        settings: Settings,
        store: Store | None = None,
        client: httpx.Client | None = None,
        wait: Callable[..., None] = _wait,
        pause_between_queries_s: float = PAUSE_BETWEEN_QUERIES_S,
        attempts: int = ATTEMPTS,
        base_wait_s: float = BASE_WAIT_S,
    ):
        self._settings = settings
        self._store = store
        self._client = client
        self._wait = wait
        self._pause_s = pause_between_queries_s
        self._attempts = max(1, attempts)
        self._base_wait_s = base_wait_s

    # --- Public API

    def count(self, lat: float, lon: float, radius_km: float, sectors: list[str] | None,
              include_generic: bool = False) -> int:
        """Estimated number of named elements within the radius (may count a few
        duplicates and, for large radii, include box corners outside the circle).
        `include_generic` also counts the companies with only a generic tag."""
        pairs = search_pairs(sectors, include_generic)
        key = self._cache_key("count", lat, lon, radius_km, sectors, include_generic)
        if self._store is not None:
            in_cache = self._store.cache_read(CACHE_NAMESPACE, key, CACHE_TTL_S)
            if isinstance(in_cache, int) and not isinstance(in_cache, bool):
                return in_cache
        total = 0
        for index, area in enumerate(self._areas(lat, lon, radius_km)):
            if index:
                self._wait(self._pause_s, None)
            query = build_query(pairs, area, count=True)
            data = self._run(query, None)
            total += self._read_count(data)
        if self._store is not None:
            self._store.cache_write(CACHE_NAMESPACE, key, total)
        return total

    def find(
        self,
        lat: float,
        lon: float,
        radius_km: float,
        sectors: list[str] | None,
        max_companies: int = 0,
        cancel: threading.Event | None = None,
        include_generic: bool = False,
    ) -> list[Company]:
        """Companies within the radius, sorted by distance and without duplicates.

        With `include_generic` the companies tagged only generically (office=company,
        industrial=*...) are searched too; the pipeline ranks them below the sector
        matches through `sectors.preliminary_relevance`.

        If `cancel` is set, returns what was collected up to that point
        (without caching it). `max_companies=0` means no limit.
        """
        if cancel is not None and cancel.is_set():
            return []
        key = self._cache_key("find", lat, lon, radius_km, sectors, include_generic)
        if self._store is not None:
            in_cache = self._store.cache_read(CACHE_NAMESPACE, key, CACHE_TTL_S)
            if isinstance(in_cache, list):
                try:
                    return self._cap([Company.from_dict(d) for d in in_cache], max_companies)
                except (KeyError, TypeError):
                    pass  # malformed cache: download again

        pairs = search_pairs(sectors, include_generic)
        elements: dict[tuple, dict] = {}
        complete = True
        for index, area in enumerate(self._areas(lat, lon, radius_km)):
            if cancel is not None and cancel.is_set():
                complete = False
                break
            if index:
                self._wait(self._pause_s, cancel)
                if cancel is not None and cancel.is_set():
                    complete = False
                    break
            data = self._run(build_query(pairs, area), cancel)
            if data is None:  # cancelled during the retries
                complete = False
                break
            for el in data.get("elements", []):
                if isinstance(el, dict):
                    elements.setdefault((el.get("type"), el.get("id")), el)

        companies = []
        for el in elements.values():
            co = element_to_company(el, lat, lon)
            if co is not None and co.distance_km <= radius_km:
                companies.append(co)
        companies.sort(key=lambda a: (a.distance_km, a.name))
        companies = remove_duplicates(companies)

        if complete and self._store is not None:
            self._store.cache_write(
                CACHE_NAMESPACE, key, [a.to_dict() for a in companies]
            )
        return self._cap(companies, max_companies)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    # --- internal details

    @staticmethod
    def _cap(companies: list[Company], maximum: int) -> list[Company]:
        return companies[:maximum] if maximum and maximum > 0 else companies

    @staticmethod
    def _cache_key(kind: str, lat, lon, radius_km, sectors, include_generic: bool = False) -> str:
        valid_items = sorted({s for s in (sectors or []) if s in SECTORS})
        joined = ",".join(valid_items) if valid_items else "*"
        generic = "|generic" if include_generic and valid_items else ""
        return f"{KEY_VERSION}|{kind}|{lat:.4f}|{lon:.4f}|{radius_km:g}|{joined}{generic}"

    @staticmethod
    def _areas(lat: float, lon: float, radius_km: float) -> list[str]:
        if radius_km <= SPLIT_THRESHOLD_KM:
            return [_radius_area(lat, lon, radius_km)]
        return [_box_area(r) for r in compute_boxes(lat, lon, radius_km)]

    @staticmethod
    def _read_count(data: dict | None) -> int:
        if not data:
            return 0
        for el in data.get("elements", []):
            if isinstance(el, dict) and el.get("type") == "count":
                try:
                    return int(el["tags"]["total"])
                except (KeyError, TypeError, ValueError):
                    return 0
        return 0

    def _run(self, query: str, cancel: threading.Event | None) -> dict | None:
        """Send the query with retries; None if cancelled while waiting."""
        last_error = "unknown error"
        for attempt in range(self._attempts):
            if cancel is not None and cancel.is_set():
                return None
            try:
                response = self._send(query)
            except httpx.HTTPError as e:
                last_error = f"network: {e.__class__.__name__}"
                data = None
            else:
                if response.status_code == 200:
                    try:
                        data = response.json()
                    except ValueError:
                        raise OverpassError("Invalid response from Overpass.") from None
                    remark = data.get("remark", "") if isinstance(data, dict) else ""
                    if isinstance(data, dict) and "runtime error" not in str(remark):
                        return data
                    last_error = f"server busy or query too heavy ({remark})"
                    data = None
                elif response.status_code in RETRY_CODES:
                    last_error = f"HTTP {response.status_code}"
                else:
                    raise OverpassError(
                        f"Overpass answered with HTTP {response.status_code}."
                    )
            if attempt < self._attempts - 1:
                self._wait(self._base_wait_s * (2**attempt), cancel)
        raise OverpassError(
            f"Overpass unavailable after {self._attempts} attempts ({last_error})."
        )

    def _send(self, query: str) -> httpx.Response:
        url = self._settings.overpass_server
        # Overpass operators ask for an identifying User-Agent with a contact URL: the
        # user's own when it has one, else the one the discovery gateway sends.
        from ..discovery.gateway import user_agent

        agent = (self._settings.user_agent or "").strip()
        if not re.search(r"https?://|@", agent):
            agent = user_agent()
        headers = {"User-Agent": agent}
        timeout = TIMEOUT_QUERY_S + HTTP_MARGIN_S
        if self._client is None:
            self._client = httpx.Client(follow_redirects=True)
        return self._client.post(
            url, data={"data": query}, headers=headers, timeout=timeout
        )
