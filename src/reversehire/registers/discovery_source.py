"""The regional register as a discovery source (ready to register in discovery/__init__.py:
name "regional_register", local, no network during a search).

Mirrors discovery/local_sources.ImportSource: candidates are `models.Company` objects
(origin "register") with the register fields in `Company.fields`, placed at their
coordinates when a source gave them, else at their town from the local map data
(`fields["position"] = "town"`), so radius searches work too.
"""

from __future__ import annotations

import logging

from ..discovery.sources import AREA_PROVINCE, AREA_REGION, UNAVAILABLE, DiscoveryQuery, DiscoverySource, SourceSkipped
from ..models import Company
from .record import DISCOVERY_SOURCE

log = logging.getLogger(__name__)

MAX_CANDIDATES = 3000


class RegionalRegisterSource(DiscoverySource):
    name = DISCOVERY_SOURCE
    network = False

    def __init__(self, manager, region=None, max_candidates: int = MAX_CANDIDATES, only_active: bool = True, *,
                 store=None):
        self.manager = manager
        self.region = region
        self.max_candidates = max_candidates
        self.only_active = only_active
        self.store = store
        self.note = ""

    def _migrate(self, cancel) -> None:
        """The lists an older version kept in the Store join the register first (once)."""
        if self.store is None:
            return
        try:
            from .lists import migrate_imported_lists
            migrate_imported_lists(self.store, self.manager.db, cancel)
        except Exception:  # noqa: BLE001 - the register answers with what it has
            log.warning("Register: old imported lists not migrated", exc_info=True)

    def _filters(self, query: DiscoveryQuery) -> dict:
        from .lists import UNCODED_KEPT

        region_key = getattr(query.region, "key", None) or getattr(self.region, "key", None)
        kwargs = {"ateco_prefixes": tuple(query.ateco or ()), "only_active": self.only_active,
                  "keep_uncoded": UNCODED_KEPT}
        if query.area == AREA_PROVINCE and query.province:
            kwargs["province"] = query.province
        elif region_key:
            kwargs["region"] = region_key
            kwargs["include_unplaced"] = True   # list rows with only a town: placed by the town below
        return kwargs

    def _place_lookup(self):
        if self.region is None:
            return None
        try:
            from ..discovery import osm_extract
            if not osm_extract.status(self.region).ready:
                return None
            index = osm_extract.OsmIndex(osm_extract.index_path(self.region))
        except Exception:  # noqa: BLE001 - positions are optional
            return None
        cache: dict = {}

        def lookup(town: str, province: str):
            key = (town.lower(), (province or "").upper())
            if key not in cache:
                try:
                    cache[key] = index.find_place(town, province)
                except Exception:  # noqa: BLE001
                    cache[key] = None
            return cache[key]

        return lookup

    def find(self, query: DiscoveryQuery, gateway, cancel, progress=None) -> list[Company]:
        self._migrate(cancel)
        try:
            if not self.manager.db.count(only_active=False):
                raise SourceSkipped("not_downloaded", "the regional register is empty", UNAVAILABLE)
        except SourceSkipped:
            raise
        except Exception as error:  # noqa: BLE001
            raise SourceSkipped("unavailable", f"{type(error).__name__}: {error}", UNAVAILABLE) from error
        records = self.manager.db.find(limit=self.max_candidates + 1, **self._filters(query))
        if len(records) > self.max_candidates:
            records = records[: self.max_candidates]
            self.note = f"first {self.max_candidates} companies (choose ATECO categories to narrow)"
        lookup = self._place_lookup()
        result = []
        province = query.province if query.area == AREA_PROVINCE else ""
        for record in records:
            if cancel is not None and cancel.is_set():
                break
            company = record.to_candidate(province)
            unplaced = not record.get("region") and company.lat is None
            if (company.lat is None or company.lon is None) and lookup is not None and company.fields.get("town"):
                found = lookup(company.fields["town"], company.fields.get("province", ""))
                if found:
                    company.lat, company.lon = found[0], found[1]
                    company.fields["position"] = "town"
                    unplaced = False
                    if not company.fields.get("province") and len(found) > 2 and found[2]:
                        company.fields["province"] = found[2]
            if unplaced and lookup is not None and query.area == AREA_REGION:
                continue   # a list row with only a town that is not in this region

            if query.area != AREA_REGION and query.area != AREA_PROVINCE and company.lat is None:
                continue   # radius search: a company without any position cannot be placed
            result.append(company)
        return result

    def count(self, query, gateway, cancel) -> int | None:
        try:
            return min(self.manager.db.count(**self._filters(query)), self.max_candidates)
        except Exception:  # noqa: BLE001
            return None


__all__ = ["MAX_CANDIDATES", "RegionalRegisterSource"]
