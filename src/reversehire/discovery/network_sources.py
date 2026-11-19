"""The network discovery sources: Overpass with instance rotation (B), Wikidata (C),
Overture Maps places (D). All requests go through the PoliteGateway."""

from __future__ import annotations

import json
import logging
import threading
import time

import httpx

from ..geo.overpass import (OverpassClient, _selectors, element_to_company, remove_duplicates,
                            search_pairs)
from ..geo.regions import Region
from ..models import Company
from .gateway import GatewayError, PoliteGateway, SourceBlocked
from .merge import normalize_vat
from .sources import (AREA_PROVINCE, AREA_REGION, OVERPASS, OVERTURE, UNAVAILABLE, WIKIDATA,
                      DiscoveryQuery, DiscoverySource, SourceSkipped)

log = logging.getLogger(__name__)

# --- B: Overpass -------------------------------------------------------------------------------
#
# Order from the sources study (C.1/C.2, checked 2026-11-17: only dresel answered).
OVERPASS_ENDPOINTS = (
    "https://ovp.dresel.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
LEGACY_DEFAULT_SERVER = "https://overpass-api.de/api/interpreter"
OVERPASS_QUERY_TIMEOUT_S = 25
OVERPASS_BUDGET_S = 60.0
OVERPASS_ALONE_BUDGET_S = 180.0
OVERPASS_COUNT_BUDGET_S = 30.0
OVERPASS_SKIP_S = 30 * 60      # an endpoint that failed is skipped for 30 minutes
OVERPASS_CACHE_TTL_S = 7 * 24 * 3600

_health_lock = threading.Lock()
_unhealthy_until: dict[str, float] = {}


def mark_unhealthy(url: str, seconds: float = OVERPASS_SKIP_S) -> None:
    with _health_lock:
        _unhealthy_until[url] = time.monotonic() + seconds


def healthy(url: str) -> bool:
    with _health_lock:
        return _unhealthy_until.get(url, 0.0) <= time.monotonic()


def reset_health() -> None:
    with _health_lock:
        _unhealthy_until.clear()


def endpoints_for(settings) -> list[str]:
    """The user's server first (when it is not the old default), then the study's order."""
    custom = (getattr(settings, "overpass_server", "") or "").strip()
    result = []
    if custom and custom != LEGACY_DEFAULT_SERVER:
        result.append(custom)
    for url in OVERPASS_ENDPOINTS:
        if url not in result:
            result.append(url)
    return result


def overpass_query(query: DiscoveryQuery, region: Region | None) -> list[str]:
    """Overpass QL texts for the query (several boxes for a large radius)."""
    pairs = search_pairs(query.sectors, query.include_generic)
    header = f"[out:json][timeout:{OVERPASS_QUERY_TIMEOUT_S}];"
    if query.area in (AREA_PROVINCE, AREA_REGION):
        iso = f"IT-{query.province.upper()}" if query.area == AREA_PROVINCE else (region.iso if region else "")
        if not iso:
            raise SourceSkipped("no_region", "unknown region for Overpass", UNAVAILABLE)
        body = "\n".join(f'  nwr{s}["name"](area.a);' for s in _selectors(pairs))
        return [f'{header}\narea["ISO3166-2"="{iso}"]->.a;\n(\n{body}\n);\nout center tags;']
    texts = []
    for area in OverpassClient._areas(query.lat, query.lon, query.radius_km):
        body = "\n".join(f'  nwr{s}["name"]{area};' for s in _selectors(pairs))
        texts.append(f"{header}\n(\n{body}\n);\nout center tags;")
    return texts


class OverpassSource(DiscoverySource):
    """Source B: tries the endpoints in order, skips the ones that failed recently,
    never waits minutes: overall budget OVERPASS_BUDGET_S (OVERPASS_ALONE_BUDGET_S when
    it is the only source)."""

    name = OVERPASS
    network = True

    def __init__(self, settings, region: Region | None = None, alone: bool = False, forced: bool = False):
        self.settings = settings
        self.region = region
        self.budget_s = OVERPASS_ALONE_BUDGET_S if alone else OVERPASS_BUDGET_S
        self.forced = forced
        self.note = ""

    def _run(self, text: str, gateway: PoliteGateway, cancel, deadline: float, failures: list[str]) -> dict:
        for url in endpoints_for(self.settings):
            if not healthy(url):
                failures.append(f"{httpx.URL(url).host}: skipped (failed recently)")
                continue
            remaining = deadline - time.monotonic()
            if remaining < 5:
                break
            read = min(OVERPASS_QUERY_TIMEOUT_S + 15.0, remaining)
            try:
                reply = gateway.post(url, data={"data": text}, timeout=httpx.Timeout(read, connect=10.0),
                                     cache_ttl_s=OVERPASS_CACHE_TTL_S, cancel=cancel, retry_backoff=False)
            except SourceBlocked as error:
                mark_unhealthy(url)
                failures.append(str(error))
                continue
            except GatewayError as error:
                mark_unhealthy(url)
                failures.append(str(error))
                continue
            if reply.status == 200:
                try:
                    data = reply.json()
                except ValueError:
                    data = None
                remark = str((data or {}).get("remark", "")) if isinstance(data, dict) else ""
                if isinstance(data, dict) and "runtime error" not in remark:
                    self.note = httpx.URL(url).host or ""
                    return data
                failures.append(f"{httpx.URL(url).host}: query rejected ({remark[:80]})")
            else:
                failures.append(f"{httpx.URL(url).host}: HTTP {reply.status}")
            mark_unhealthy(url)
        raise GatewayError("no Overpass server answered: " + "; ".join(failures))

    def count(self, query, gateway, cancel) -> int | None:
        """Named elements in the area ("out count"), within OVERPASS_COUNT_BUDGET_S; None
        when no server answers in time."""
        deadline = time.monotonic() + OVERPASS_COUNT_BUDGET_S
        total = 0
        try:
            for text in overpass_query(query, self.region):
                data = self._run(text.replace("out center tags;", "out count;"), gateway, cancel,
                                 deadline, [])
                for element in data.get("elements", []):
                    if isinstance(element, dict) and element.get("type") == "count":
                        total += int((element.get("tags") or {}).get("total", 0) or 0)
        except (GatewayError, SourceSkipped, ValueError, TypeError):
            return None
        return total

    def find(self, query, gateway, cancel, progress=None) -> list[Company]:
        deadline = time.monotonic() + self.budget_s
        failures: list[str] = []
        elements: dict = {}
        for text in overpass_query(query, self.region):
            data = self._run(text, gateway, cancel, deadline, failures)
            for element in data.get("elements", []):
                if isinstance(element, dict):
                    elements.setdefault((element.get("type"), element.get("id")), element)
        companies = []
        for element in elements.values():
            company = element_to_company(element, query.lat, query.lon)
            if company is not None:
                companies.append(company)
        companies.sort(key=lambda c: (c.distance_km, c.name))
        return remove_duplicates(companies)


# --- C: Wikidata --------------------------------------------------------------------------------

WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
WIKIDATA_CACHE_TTL_S = 30 * 24 * 3600
WIKIDATA_LIMIT = 3000

SPARQL = """SELECT ?c ?cLabel ?cDescription ?web ?vat ?lei ?coord ?hqLabel ?hqCoord ?industryLabel ?employees WHERE {{
  ?c wdt:P31/wdt:P279* wd:Q4830453 ; wdt:P159 ?hq ; wdt:P856 ?web .
  ?hq wdt:P131* wd:{region} .
  OPTIONAL {{ ?c wdt:P3608 ?vat }}
  OPTIONAL {{ ?c wdt:P1278 ?lei }}
  OPTIONAL {{ ?c wdt:P625 ?coord }}
  OPTIONAL {{ ?hq wdt:P625 ?hqCoord }}
  OPTIONAL {{ ?c wdt:P452 ?industry }}
  OPTIONAL {{ ?c wdt:P1128 ?employees }}
  FILTER NOT EXISTS {{ ?c wdt:P576 ?dissolved }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "it,en". }}
}} LIMIT {limit}"""


def _point(text: str | None) -> tuple[float, float] | None:
    """"Point(11.34 44.49)" -> (lat, lon)."""
    if not text or not text.startswith("Point("):
        return None
    try:
        lon, lat = text[6:-1].split()
        return float(lat), float(lon)
    except ValueError:
        return None


def parse_wikidata(data: dict) -> list[Company]:
    """Companies from SPARQL JSON results (one per entity; first website, VAT, LEI)."""
    by_id: dict[str, dict] = {}
    for binding in (data.get("results") or {}).get("bindings") or []:
        def value(name: str) -> str:
            return (binding.get(name) or {}).get("value", "")
        entity = value("c").rsplit("/", 1)[-1]
        if not entity:
            continue
        item = by_id.setdefault(entity, {"name": value("cLabel"), "web": "", "vat": "", "lei": "",
                                         "coord": None, "hq": value("hqLabel"), "hq_coord": None,
                                         "industry": [], "employees": 0,
                                         "about": value("cDescription")})
        try:  # several statements (years): keep the largest
            item["employees"] = max(item["employees"], int(float(value("employees") or 0)))
        except ValueError:
            pass
        item["web"] = item["web"] or value("web")
        item["vat"] = item["vat"] or value("vat")
        item["lei"] = item["lei"] or value("lei")
        item["coord"] = item["coord"] or _point(value("coord"))
        item["hq_coord"] = item["hq_coord"] or _point(value("hqCoord"))
        industry = value("industryLabel")
        if industry and industry not in item["industry"]:
            item["industry"].append(industry)
    from ..geo.overpass import normalize_site

    companies = []
    for entity, item in by_id.items():
        name = item["name"]
        if not name or name == entity:  # no label in it/en
            continue
        position = item["coord"] or item["hq_coord"]
        company = Company(name=name, lat=position[0] if position else None,
                          lon=position[1] if position else None, address=item["hq"] or "",
                          site=normalize_site(item["web"]), origin="wikidata")
        extra = {"wikidata": entity, "town": item["hq"] or ""}
        vat = normalize_vat(item["vat"])
        if vat:
            extra["vat"] = vat
        if item["lei"]:
            extra["lei"] = item["lei"]
        if item["industry"]:
            extra["industry"] = ", ".join(item["industry"][:3])
        if item["employees"] > 0:
            extra["employees"] = str(item["employees"])
        if item.get("about"):  # Wikidata short description ("Italian manufacturer of gearmotors")
            extra["about"] = item["about"][:200]
        if position and not item["coord"]:
            extra["position"] = "town"
        company.discovery_fields = extra
        companies.append(company)
    return companies


class WikidataSource(DiscoverySource):
    """Source C: companies with headquarters in the region and an official website
    (mid/large companies; VAT and LEI when present). One query per region, cached 30 days."""

    name = WIKIDATA
    network = True

    def __init__(self, region: Region | None, province_lookup=None):
        self.region = region
        self.province_lookup = province_lookup   # (town) -> province code, from the local map data

    def find(self, query, gateway, cancel, progress=None) -> list[Company]:
        if self.region is None or not self.region.wikidata:
            raise SourceSkipped("no_region", "no Wikidata id for this region", UNAVAILABLE)
        text = SPARQL.format(region=self.region.wikidata, limit=WIKIDATA_LIMIT)
        reply = gateway.get(WIKIDATA_ENDPOINT, params={"query": text, "format": "json"},
                            headers={"Accept": "application/sparql-results+json"},
                            timeout=httpx.Timeout(70.0, connect=10.0),
                            cache_ttl_s=WIKIDATA_CACHE_TTL_S, cancel=cancel)
        if reply.status != 200:
            raise GatewayError(f"query.wikidata.org answered HTTP {reply.status}", reply.status)
        try:
            data = reply.json()
        except ValueError as error:
            raise GatewayError("query.wikidata.org: invalid answer") from error
        companies = parse_wikidata(data)
        if self.province_lookup is not None:
            for company in companies:
                town = company.discovery_fields.get("town", "")
                code = self.province_lookup(town) if town else ""
                if code:
                    company.discovery_fields["province"] = code
        return companies


# --- D: Overture Maps places --------------------------------------------------------------------
#
# GeoParquet on public cloud storage, read with DuckDB (optional dependency). Off by
# default: see the measures in the final report. Licence CDLA-Permissive-2.0 / Apache-2.0
# depending on the source; no OSM data inside.

OVERTURE_RELEASE = "2026-10-21.0"
OVERTURE_URL = ("s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*")
OVERTURE_CACHE_TTL_S = 30 * 24 * 3600
OVERTURE_MIN_CONFIDENCE = 0.6


def duckdb_available() -> tuple[bool, str]:
    try:
        import duckdb  # noqa: F401
    except ImportError as error:
        return False, f"duckdb not installed ({error})"
    except OSError as error:  # a blocked DLL (Windows application control)
        return False, f"duckdb cannot be loaded ({error})"
    return True, ""


class OvertureSource(DiscoverySource):
    """Source D: places with websites in the bounding box of the query. The result is
    cached in the Store (30 days) so the remote files are read once per area."""

    name = OVERTURE
    network = True

    def __init__(self, store=None, release: str = OVERTURE_RELEASE):
        self.store = store
        self.release = release

    def find(self, query, gateway, cancel, progress=None) -> list[Company]:
        ok, reason = duckdb_available()
        if not ok:
            raise SourceSkipped("duckdb_missing", reason, UNAVAILABLE)
        if query.area != "radius":
            raise SourceSkipped("radius_only", "Overture is searched by radius only")
        import math

        # NOTE: DuckDB reads the remote files itself (HTTP range requests), outside the
        # PoliteGateway; that is why this source is off by default.

        dlat = query.radius_km / 111.32
        dlon = query.radius_km / (111.32 * max(0.05, math.cos(math.radians(query.lat))))
        box = (query.lon - dlon, query.lon + dlon, query.lat - dlat, query.lat + dlat)
        key = f"{self.release}|{box[0]:.3f}|{box[1]:.3f}|{box[2]:.3f}|{box[3]:.3f}"
        rows = self.store.cache_read("overture", key, OVERTURE_CACHE_TTL_S) if self.store is not None else None
        if not isinstance(rows, list):
            rows = self._query(box)
            if self.store is not None:
                self.store.cache_write("overture", key, rows)
        return [overture_company(r) for r in rows]

    def _query(self, box) -> list[dict]:
        import duckdb

        connection = duckdb.connect()
        try:
            connection.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
            sql = f"""
                SELECT names.primary AS name, websites, phones, emails, addresses,
                       taxonomy.primary AS category, confidence, bbox.xmin AS lon, bbox.ymin AS lat
                FROM read_parquet('{OVERTURE_URL.format(release=self.release)}', hive_partitioning=1)
                WHERE bbox.xmin BETWEEN ? AND ? AND bbox.ymin BETWEEN ? AND ?
                  AND confidence >= ? AND websites IS NOT NULL AND len(websites) > 0
            """
            result = connection.execute(sql, [box[0], box[1], box[2], box[3], OVERTURE_MIN_CONFIDENCE])
            columns = [d[0] for d in result.description]
            rows = [dict(zip(columns, row)) for row in result.fetchall()]
        finally:
            connection.close()
        return json.loads(json.dumps(rows, default=str))


def overture_company(row: dict) -> Company:
    from ..geo.overpass import normalize_site

    websites = row.get("websites") or []
    address = ""
    addresses = row.get("addresses") or []
    if addresses and isinstance(addresses[0], dict):
        address = addresses[0].get("freeform") or ""
    company = Company(name=row.get("name") or "", lat=row.get("lat"), lon=row.get("lon"),
                      address=address, site=normalize_site(websites[0]) if websites else None,
                      origin="overture")
    extra = {"category": row.get("category") or ""}
    phones = row.get("phones") or []
    if phones:
        extra["phone"] = phones[0]
    company.discovery_fields = {k: v for k, v in extra.items() if v}
    return company
