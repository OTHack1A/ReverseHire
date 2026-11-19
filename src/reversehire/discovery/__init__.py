"""Company discovery from several independent sources, merged and deduplicated.

Public API:
    build_sources(settings, store, names=None, region=None) -> list[DiscoverySource]
    discover(query, sources, gateway, cancel, on_status, progress) -> DiscoveryResult
    DiscoveryQuery, SourceStatus, PoliteGateway
    ateco.ateco_for_profile(profile) / ateco.describe(code) / ateco.search(text)
    osm_extract.status(region) / download(...) / build_index(...)
    imports.preview(path) / records(...)          (stored with registers.lists.import_list)
    REGIONAL_REGISTER ("regional_register")       the register source; "imports" is its old name
    merge.info(company) -> sources, ids, provenance, conflicts

Every outgoing request goes through one `PoliteGateway` (gateway.py).
"""

from __future__ import annotations

from ..geo.regions import Region, region_for
from .gateway import Cancelled, GatewayError, PoliteGateway, SourceBlocked, user_agent
from .sources import (AREA_PROVINCE, AREA_RADIUS, AREA_REGION, AREAS, IMPORTS, KNOWN_SOURCES, ON_BY_DEFAULT,
                      OSM_EXTRACT, OVERPASS, OVERTURE, OWN, REGIONAL_REGISTER, REGISTER_OFF, WIKIDATA,
                      DiscoveryQuery, DiscoveryResult, DiscoverySource, SourceSkipped, SourceStatus, discover)

DEFAULT_SOURCES = "osm_extract,overpass,wikidata"
# Names of older versions -> current name ("imports": the user's lists, now in the register).
ALIASES = {IMPORTS: REGIONAL_REGISTER}


def source_names(settings) -> list[str]:
    """Enabled sources from `settings.discovery_sources` (or a "a,b" string; unknown names
    dropped, old names mapped by ALIASES), plus the always-on own list and the regional
    register unless the value switches it off ("-regional_register")."""
    raw = settings if isinstance(settings, (str, list, tuple)) else getattr(
        settings, "discovery_sources", DEFAULT_SOURCES)
    if isinstance(raw, (list, tuple)):
        raw = ",".join(raw)
    names = []
    items = [item.strip().lower() for item in str(raw or DEFAULT_SOURCES).split(",")]
    for item in items:
        name = ALIASES.get(item, item)
        if name in KNOWN_SOURCES and name not in names and name != IMPORTS:
            names.append(name)
    for name in ON_BY_DEFAULT:
        if name not in names and "-" + name not in items:
            names.append(name)
    for name in (OWN,):
        if name not in names:
            names.append(name)
    return names


def sources_value(chosen: list[str]) -> str:
    """The settings value for the sources ticked in the panel (the register's "off" is
    written explicitly, since it is on by default)."""
    names = [ALIASES.get(n, n) for n in chosen]
    names = [n for i, n in enumerate(names) if n in KNOWN_SOURCES and n not in names[:i] and n not in (OWN, IMPORTS)]
    off = [("-" + n) for n in ON_BY_DEFAULT if n not in names]
    return ",".join(names + off)


def build_sources(settings, store, names: list[str] | None = None, region: Region | None = None, *,
                  register_manager=None) -> list[DiscoverySource]:
    """Source objects in priority order (local first): osm_extract, regional_register,
    own, overpass, wikidata, overture. `register_manager` defaults to the shared manager
    of the data folder (registers.shared_manager)."""
    from . import osm_extract
    from .local_sources import OsmExtractSource, OwnListSource
    from .network_sources import OverpassSource, OvertureSource, WikidataSource
    from .prefs import effective_region, effective_sources

    if region is None:
        region = region_for(effective_region(settings, store))
    if names is None:
        wanted = source_names(effective_sources(settings, store))
    else:
        wanted = [ALIASES.get(n, n) for n in names]
    alone = [n for n in wanted if n not in (REGIONAL_REGISTER, OWN)] == [OVERPASS]

    def register_source():
        from ..registers import discovery_source, shared_manager

        manager = register_manager if register_manager is not None else shared_manager(
            settings if not isinstance(settings, (str, dict)) else None)
        return discovery_source(manager, region, store=store)

    def province_lookup(town: str) -> str:
        try:
            if region is None or not osm_extract.status(region).ready:
                return ""
            found = osm_extract.OsmIndex(osm_extract.index_path(region)).find_place(town)
            return found[2] if found else ""
        except Exception:
            return ""

    factories = {
        OSM_EXTRACT: lambda: OsmExtractSource(region),
        REGIONAL_REGISTER: register_source,
        OWN: lambda: OwnListSource(store),
        OVERPASS: lambda: OverpassSource(settings, region, alone=alone),
        WIKIDATA: lambda: WikidataSource(region, province_lookup),
        OVERTURE: lambda: OvertureSource(store),
    }
    order = [OSM_EXTRACT, REGIONAL_REGISTER, OWN, OVERPASS, WIKIDATA, OVERTURE]
    return [factories[name]() for name in order if name in wanted]


__all__ = [
    "ALIASES", "AREAS", "AREA_PROVINCE", "AREA_RADIUS", "AREA_REGION", "Cancelled", "DEFAULT_SOURCES",
    "DiscoveryQuery", "DiscoveryResult", "DiscoverySource", "GatewayError", "KNOWN_SOURCES",
    "PoliteGateway", "REGIONAL_REGISTER", "REGISTER_OFF", "Region", "SourceBlocked", "SourceSkipped",
    "SourceStatus", "build_sources", "discover", "region_for", "source_names", "sources_value", "user_agent",
]
