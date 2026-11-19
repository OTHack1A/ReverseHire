"""Discovery sources behind one interface, run together and merged.

Every source is optional and every failure is non-fatal: `discover` always returns
what the working sources gave, with one `SourceStatus` per source (shown to the user
as "Local map data: 1,240 companies in 0.4 s", "Overpass: skipped (not needed)"...).

Order: the local sources first (OSM extract, imports, the user's own list), then the
network ones (Overpass only when the local map data could not answer, Wikidata,
Overture), at most MAX_NETWORK_SOURCES at once. Everything outgoing goes through one
`PoliteGateway`.

Area modes (the register method is territorial): "radius" around the point,
"province" (a province code such as "BO"), "region" (the whole region of the extract).
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from ..geo.distance import haversine_km
from ..geo.regions import Region, region_for
from ..models import Company
from .gateway import Cancelled, GatewayError, PoliteGateway, SourceBlocked
from .merge import Merger

log = logging.getLogger(__name__)

MAX_NETWORK_SOURCES = 2
AREA_RADIUS, AREA_PROVINCE, AREA_REGION = "radius", "province", "region"
AREAS = (AREA_RADIUS, AREA_PROVINCE, AREA_REGION)

# Source names (settings.discovery_sources uses them).
OSM_EXTRACT, OVERPASS, WIKIDATA, OVERTURE, IMPORTS, OWN = (
    "osm_extract", "overpass", "wikidata", "overture", "imports", "own")
# The regional register: official open lists AND the lists the user imported (registers/).
# "imports" (the old source of imported lists) is an alias of it in settings values.
REGIONAL_REGISTER = "regional_register"
REGISTER_OFF = "-" + REGIONAL_REGISTER   # in a sources value: the user switched the register off
KNOWN_SOURCES = (OSM_EXTRACT, OVERPASS, WIKIDATA, OVERTURE, REGIONAL_REGISTER, IMPORTS, OWN)
LOCAL_SOURCES = (OSM_EXTRACT, REGIONAL_REGISTER, IMPORTS, OWN)
ALWAYS_ON = (OWN,)   # cheap and local: used whatever the settings say
# On unless switched off (REGISTER_OFF): imported lists were always used by older versions.
ON_BY_DEFAULT = (REGIONAL_REGISTER,)

# States of a source after a search.
OK, EMPTY, SKIPPED, FAILED, UNAVAILABLE, CANCELLED, DISABLED = (
    "ok", "empty", "skipped", "failed", "unavailable", "cancelled", "disabled")


@dataclass
class DiscoveryQuery:
    lat: float
    lon: float
    radius_km: float
    sectors: list[str] | None = None
    area: str = AREA_RADIUS
    province: str = ""
    region: Region | None = None
    ateco: list[str] = field(default_factory=list)
    include_generic: bool = True

    def contains(self, company: Company) -> bool:
        """True when the company lies in the requested area (by position, or by province
        for records without coordinates)."""
        extra = getattr(company, "discovery_fields", None) or {}
        if self.area == AREA_REGION:
            return True
        if self.area == AREA_PROVINCE:
            code = (extra.get("province") or "").upper()
            return bool(code) and code == self.province.upper()
        if company.lat is None or company.lon is None:
            return False
        return haversine_km(self.lat, self.lon, company.lat, company.lon) <= self.radius_km


@dataclass
class SourceStatus:
    name: str
    state: str
    count: int = 0
    new: int = 0
    seconds: float = 0.0
    reason: str = ""     # short machine reason ("not_downloaded", "not_needed", "blocked"...)
    detail: str = ""     # human detail (English), e.g. the HTTP error

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class SourceSkipped(Exception):
    """A source cannot or need not run now (not an error): `reason` says why."""

    def __init__(self, reason: str, detail: str = "", state: str = SKIPPED):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail
        self.state = state


class DiscoverySource:
    """Base class. `find` returns companies (distance_km filled when the query has a
    point), or raises SourceSkipped / GatewayError / any error (reported, not fatal)."""

    name = ""
    network = False

    def find(self, query: DiscoveryQuery, gateway: PoliteGateway, cancel,
             progress: Callable[[str, int, int], None] | None = None) -> list[Company]:
        raise NotImplementedError

    def count(self, query: DiscoveryQuery, gateway: PoliteGateway, cancel) -> int | None:
        """Fast count for the time estimate; None when unknown without a slow request."""
        return None


@dataclass
class DiscoveryResult:
    companies: list[Company]
    statuses: list[SourceStatus]

    @property
    def all_failed(self) -> bool:
        tried = [s for s in self.statuses if s.state not in (SKIPPED, DISABLED)]
        return bool(tried) and all(s.state in (FAILED, UNAVAILABLE) for s in tried) and not self.companies


def _with_distance(companies: list[Company], query: DiscoveryQuery) -> list[Company]:
    for company in companies:
        if company.distance_km is None and company.lat is not None and company.lon is not None:
            company.distance_km = round(haversine_km(query.lat, query.lon, company.lat, company.lon), 3)
    return companies


def discover(query: DiscoveryQuery, sources: list[DiscoverySource], gateway: PoliteGateway,
             cancel: threading.Event | None = None,
             on_status: Callable[[SourceStatus], None] | None = None,
             progress: Callable[[str, str, int, int], None] | None = None) -> DiscoveryResult:
    """Run the sources, merge the results (local first) and report each source's status."""
    cancel = cancel or threading.Event()
    statuses: dict[str, SourceStatus] = {}
    found: dict[str, list[Company]] = {}
    order = [s.name for s in sources]

    def report(status: SourceStatus) -> None:
        statuses[status.name] = status
        if on_status is not None:
            try:
                on_status(status)
            except Exception:
                log.exception("Error in the discovery status handler")

    def run(source: DiscoverySource) -> None:
        started = time.monotonic()
        if cancel.is_set():
            report(SourceStatus(source.name, CANCELLED))
            return

        def source_progress(phase: str, done: int, total: int) -> None:
            if progress is not None:
                progress(source.name, phase, done, total)

        try:
            companies = source.find(query, gateway, cancel, source_progress)
        except Cancelled:
            report(SourceStatus(source.name, CANCELLED, seconds=round(time.monotonic() - started, 1)))
            return
        except SourceSkipped as skipped:
            report(SourceStatus(source.name, skipped.state, seconds=round(time.monotonic() - started, 1),
                                reason=skipped.reason, detail=skipped.detail))
            return
        except SourceBlocked as error:
            report(SourceStatus(source.name, FAILED, seconds=round(time.monotonic() - started, 1),
                                reason="blocked", detail=str(error)))
            return
        except GatewayError as error:
            report(SourceStatus(source.name, FAILED, seconds=round(time.monotonic() - started, 1),
                                reason="network", detail=str(error)))
            return
        except Exception as error:  # a broken source never stops the search
            log.warning("Discovery source %s failed", source.name, exc_info=True)
            report(SourceStatus(source.name, FAILED, seconds=round(time.monotonic() - started, 1),
                                reason="error", detail=f"{type(error).__name__}: {error}"))
            return
        companies = [c for c in _with_distance(list(companies or []), query) if query.contains(c)]
        found[source.name] = companies
        statuses[source.name] = SourceStatus(source.name, OK if companies else EMPTY, len(companies),
                                             seconds=round(time.monotonic() - started, 1),
                                             detail=str(getattr(source, "note", "") or ""))

    local = [s for s in sources if not s.network]
    remote = [s for s in sources if s.network]
    for source in local:
        run(source)
    # Overpass is the fallback of the local map data: only when that could not answer.
    extract = statuses.get(OSM_EXTRACT)
    extract_ok = extract is not None and extract.state in (OK, EMPTY)
    to_run = []
    for source in remote:
        if source.name == OVERPASS and extract_ok and not getattr(source, "forced", False):
            report(SourceStatus(OVERPASS, SKIPPED, reason="not_needed",
                                detail="the local map data answered"))
            continue
        to_run.append(source)
    if to_run and not cancel.is_set():
        with ThreadPoolExecutor(max_workers=MAX_NETWORK_SOURCES, thread_name_prefix="discovery") as pool:
            list(pool.map(run, to_run))
    elif cancel.is_set():
        for source in to_run:
            report(SourceStatus(source.name, CANCELLED))

    merger = Merger()
    for name in order:
        if name in found:
            status = statuses[name]
            status.new = merger.add(name, found[name])
    companies = merger.companies()
    companies.sort(key=lambda c: (c.distance_km if c.distance_km is not None else float("inf"), c.name))
    # Report the successful sources once "new" is known (failures were reported at once).
    for name in order:
        status = statuses.get(name)
        if status is not None and status.state in (OK, EMPTY):
            report(status)
    ordered = [statuses[n] for n in order if n in statuses]
    return DiscoveryResult(companies, ordered)


def region_of(settings) -> Region | None:
    return region_for(getattr(settings, "osm_region", "emilia-romagna"))
