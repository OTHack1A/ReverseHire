"""The local discovery sources: OSM extract (A), imported lists (E), the user's own list (F)."""

from __future__ import annotations

import logging
from typing import Callable

from ..geo.overpass import search_pairs
from ..geo.regions import Region
from ..models import Company
from . import imports, osm_extract
from .merge import info
from .sources import (AREA_PROVINCE, AREA_RADIUS, AREA_REGION, IMPORTS, OSM_EXTRACT, OWN, UNAVAILABLE,
                      DiscoveryQuery, DiscoverySource, SourceSkipped)

log = logging.getLogger(__name__)


def matches_pairs(company: Company, pairs: list[tuple[str, str]]) -> bool:
    tags = company.osm_tags or {}
    for key, value in pairs:
        present = tags.get(key)
        if present is not None and (value == "*" or present == value):
            return True
    return False


class OsmExtractSource(DiscoverySource):
    """Source A. Never downloads during a search (the panel's "Download map data" does);
    builds the index when the file is there but the index is missing or outdated."""

    name = OSM_EXTRACT
    network = False

    def __init__(self, region: Region | None):
        self.region = region
        self.note = ""

    def _index(self, cancel, progress) -> osm_extract.OsmIndex:
        if self.region is None:
            raise SourceSkipped("no_region", "no region chosen for the map data", UNAVAILABLE)
        state = osm_extract.status(self.region)
        if not state.ready:
            if not state.pbf_exists:
                raise SourceSkipped("not_downloaded", "the map data of the region is not downloaded yet",
                                    UNAVAILABLE)
            osm_extract.build_index(osm_extract.pbf_path(self.region), osm_extract.index_path(self.region),
                                    cancel=cancel, progress=progress, region_key=self.region.key)
            state = osm_extract.status(self.region)
        if state.stale and state.index_age_days is not None:
            self.note = f"map data {state.index_age_days:.0f} days old"
        return osm_extract.OsmIndex(osm_extract.index_path(self.region))

    def _query(self, index: osm_extract.OsmIndex, query: DiscoveryQuery) -> list[Company]:
        if query.area == AREA_PROVINCE:
            return index.province(query.province, query.lat, query.lon)
        if query.area == AREA_REGION:
            return index.region(query.lat, query.lon)
        if not index.bbox_contains(query.lat, query.lon):
            raise SourceSkipped("outside_region", "the starting point is outside the downloaded region",
                                UNAVAILABLE)
        return index.radius(query.lat, query.lon, query.radius_km)

    def find(self, query, gateway, cancel, progress=None) -> list[Company]:
        index = self._index(cancel, progress)
        companies = self._query(index, query)
        pairs = search_pairs(query.sectors, query.include_generic)
        return [c for c in companies if matches_pairs(c, pairs)]

    def count(self, query, gateway, cancel) -> int | None:
        try:
            state = osm_extract.status(self.region)
            if not state.ready:
                return None
            return len(self.find(query, gateway, cancel))
        except SourceSkipped:
            return None


class ImportSource(DiscoverySource):
    """Source E: the lists imported by the user, placed by town (local map data) or province."""

    name = IMPORTS
    network = False

    def __init__(self, store, region: Region | None = None):
        self.store = store
        self.region = region

    def _place_lookup(self) -> Callable | None:
        if self.region is None:
            return None
        try:
            if not osm_extract.status(self.region).ready:
                return None
            index = osm_extract.OsmIndex(osm_extract.index_path(self.region))
        except Exception:
            return None
        cache: dict = {}

        def lookup(town: str, province: str):
            key = (town.lower(), province.upper())
            if key not in cache:
                cache[key] = index.find_place(town, province)
            return cache[key]

        return lookup

    def find(self, query, gateway, cancel, progress=None) -> list[Company]:
        if self.store is None:
            raise SourceSkipped("no_store")
        rows = imports.all_records(self.store)
        if not rows:
            raise SourceSkipped("no_imports", "no company list imported")
        codes = list(getattr(query, "ateco", None) or [])
        kept = [record for _, record in rows if imports.ateco_allowed(record, codes)]
        if len(kept) < len(rows):
            log.info("Imported lists: %d of %d companies outside the chosen ATECO categories left out",
                     len(rows) - len(kept), len(rows))
        lookup = self._place_lookup()
        return [imports.to_company(record, lookup) for record in kept]


class OwnListSource(DiscoverySource):
    """Source F: the user's favourite company cards (manual URLs are added by the pipeline)."""

    name = OWN
    network = False

    def __init__(self, store):
        self.store = store

    def find(self, query, gateway, cancel, progress=None) -> list[Company]:
        if self.store is None or not hasattr(self.store, "card_rows"):
            raise SourceSkipped("no_store")
        from ..companies import CompanyCard

        result = []
        for row in self.store.card_rows():
            if not row.get("favourite"):
                continue
            try:
                company = CompanyCard.from_row(row).to_company()
            except Exception:  # an odd row: the basic fields are enough
                company = Company(name=row.get("name") or "", lat=row.get("lat"), lon=row.get("lon"),
                                  address=row.get("address") or "", site=row.get("site"),
                                  osm_tags=dict(row.get("osm_tags") or {}), origin=row.get("origin") or "osm")
            legacy = info(company)  # cards saved by older versions keep the data in a tag
            extra = dict(legacy.get("fields") or {})
            for key, value in (legacy.get("ids") or {}).items():
                company.ids.setdefault("tax_code" if key == "cf" else key, value)
            company.osm_tags.pop("_discovery", None)
            province = (row.get("registry") or {}).get("province") if isinstance(row.get("registry"), dict) else None
            if isinstance(province, dict) and province.get("value"):
                extra.setdefault("province", str(province["value"]))
            company.fields = {**extra, **company.fields}
            company.sources = [f for f in company.sources if f.get("source") != "card"]
            if company.name:
                result.append(company)
        if not result:
            raise SourceSkipped("no_favourites", "no favourite company")
        return result


__all__ = ["ImportSource", "OsmExtractSource", "OwnListSource", "matches_pairs", "AREA_RADIUS"]
