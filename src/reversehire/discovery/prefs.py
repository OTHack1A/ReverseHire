"""The discovery choices of the search panel (area mode, province, ATECO categories),
kept in the Store so that the pipeline uses them even when the caller does not pass
them explicitly, and so that they survive a restart."""

from __future__ import annotations

from dataclasses import dataclass, field

NAMESPACE = "discovery_prefs"
KEY = "current"


@dataclass
class DiscoveryPrefs:
    area: str = "radius"            # "radius" | "province" | "region"
    province: str = ""              # province code for area "province"
    ateco: list[str] = field(default_factory=list)          # chosen categories (codes)
    ateco_custom: bool = False      # True once the user changed the proposed list
    sources: list[str] | None = None   # sources ticked in the panel (None = settings)
    region: str = ""                # region chosen in the panel ("" = settings.osm_region)

    def to_dict(self) -> dict:
        return {"area": self.area, "province": self.province, "ateco": list(self.ateco),
                "ateco_custom": self.ateco_custom, "sources": self.sources, "region": self.region}

    @classmethod
    def from_dict(cls, d) -> "DiscoveryPrefs":
        if not isinstance(d, dict):
            return cls()
        area = d.get("area") if d.get("area") in ("radius", "province", "region") else "radius"
        return cls(area=area, province=str(d.get("province") or "").upper()[:2],
                   ateco=[str(c) for c in d.get("ateco") or [] if c],
                   ateco_custom=bool(d.get("ateco_custom", False)),
                   sources=[str(x) for x in d["sources"]] if isinstance(d.get("sources"), list) else None,
                   region=str(d.get("region") or ""))


def read(store) -> DiscoveryPrefs:
    if store is None:
        return DiscoveryPrefs()
    try:
        return DiscoveryPrefs.from_dict(store.cache_read(NAMESPACE, KEY))
    except Exception:
        return DiscoveryPrefs()


def write(store, prefs: DiscoveryPrefs) -> None:
    if store is not None:
        store.cache_write(NAMESPACE, KEY, prefs.to_dict())


def effective_sources(settings, store) -> str:
    """The panel's source choice when the user made one, else settings.discovery_sources."""
    chosen = read(store).sources
    if chosen is not None:
        return ",".join(chosen)
    return getattr(settings, "discovery_sources", "osm_extract,overpass,wikidata")


def effective_region(settings, store) -> str:
    return read(store).region or getattr(settings, "osm_region", "emilia-romagna")
