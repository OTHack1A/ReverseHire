"""Italian regions: OpenStreetMap extract URLs, Wikidata ids, provinces.

Extract URLs: download.openstreetmap.fr (directory listing checked on 2026-11-17, all
20 regional files present; Emilia-Romagna 205 MB, rebuilt daily). Licence of the data:
ODbL, "(c) OpenStreetMap contributors". Another country or area can be used through a
custom URL (`custom_region`).

Province lists: static for Emilia-Romagna; for any region the built extract index
also lists the provinces found in the data (admin_level 6 boundaries).
"""

from __future__ import annotations

from dataclasses import dataclass, field

OSM_FR_BASE = "https://download.openstreetmap.fr/extracts/europe/italy/"
CUSTOM_PREFIX = "custom:"


@dataclass(frozen=True)
class Province:
    code: str   # sigla, e.g. "BO"
    name: str


@dataclass(frozen=True)
class Region:
    key: str            # "emilia-romagna"
    name: str           # "Emilia-Romagna"
    slug: str           # file name part on download.openstreetmap.fr
    wikidata: str       # QID of the region
    iso: str            # ISO 3166-2 code
    provinces: tuple[Province, ...] = field(default_factory=tuple)

    @property
    def url(self) -> str:
        if self.slug.startswith(("http://", "https://")):
            return self.slug
        return f"{OSM_FR_BASE}{self.slug}-latest.osm.pbf"

    @property
    def file_name(self) -> str:
        return self.url.rsplit("/", 1)[-1] or f"{self.key}.osm.pbf"


_ER_PROVINCES = (
    Province("BO", "Bologna"), Province("FE", "Ferrara"), Province("FC", "Forlì-Cesena"),
    Province("MO", "Modena"), Province("PR", "Parma"), Province("PC", "Piacenza"),
    Province("RA", "Ravenna"), Province("RE", "Reggio Emilia"), Province("RN", "Rimini"),
)

REGIONS: dict[str, Region] = {r.key: r for r in (
    Region("abruzzo", "Abruzzo", "abruzzo", "Q1284", "IT-65"),
    Region("basilicata", "Basilicata", "basilicata", "Q1452", "IT-77"),
    Region("calabria", "Calabria", "calabria", "Q1458", "IT-78"),
    Region("campania", "Campania", "campania", "Q1438", "IT-72"),
    Region("emilia-romagna", "Emilia-Romagna", "emilia_romagna", "Q1263", "IT-45", _ER_PROVINCES),
    Region("friuli-venezia-giulia", "Friuli-Venezia Giulia", "friuli_venezia_giulia", "Q1250", "IT-36"),
    Region("lazio", "Lazio", "lazio", "Q1282", "IT-62"),
    Region("liguria", "Liguria", "liguria", "Q1256", "IT-42"),
    Region("lombardia", "Lombardia", "lombardia", "Q1210", "IT-25"),
    Region("marche", "Marche", "marche", "Q1279", "IT-57"),
    Region("molise", "Molise", "molise", "Q1443", "IT-67"),
    Region("piemonte", "Piemonte", "piemonte", "Q1216", "IT-21"),
    Region("puglia", "Puglia", "puglia", "Q1447", "IT-75"),
    Region("sardegna", "Sardegna", "sardegna", "Q1462", "IT-88"),
    Region("sicilia", "Sicilia", "sicilia", "Q1460", "IT-82"),
    Region("toscana", "Toscana", "toscana", "Q1273", "IT-52"),
    Region("trentino-alto-adige", "Trentino-Alto Adige", "trentino_alto_adige", "Q1237", "IT-32"),
    Region("umbria", "Umbria", "umbria", "Q1280", "IT-55"),
    Region("valle-d-aosta", "Valle d'Aosta", "valle_aosta", "Q1222", "IT-23"),
    Region("veneto", "Veneto", "veneto", "Q1243", "IT-34"),
)}

DEFAULT_REGION = "emilia-romagna"


def custom_region(url: str) -> Region:
    """A region made of a user-given .osm.pbf URL (other countries or areas)."""
    name = url.rsplit("/", 1)[-1].replace("-latest.osm.pbf", "").replace(".osm.pbf", "") or "custom"
    key = "custom-" + "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")
    return Region(key, name, url, "", "")


def region_for(value: str | None) -> Region | None:
    """The Region for a settings value: a key ("emilia-romagna"), a name, or
    "custom:<url>" / a bare http(s) URL. None when unknown."""
    text = (value or "").strip()
    if not text:
        return REGIONS[DEFAULT_REGION]
    if text.startswith(CUSTOM_PREFIX):
        text = text[len(CUSTOM_PREFIX):].strip()
    if text.startswith(("http://", "https://")):
        return custom_region(text)
    lowered = text.lower().replace("_", "-").replace(" ", "-").replace("'", "-")
    if lowered in REGIONS:
        return REGIONS[lowered]
    for region in REGIONS.values():
        if region.name.lower() == text.lower() or region.slug == text.lower():
            return region
    return None


def provinces_of(region: Region | None) -> list[Province]:
    """The provinces of a region: the static list, else the complete Italian table of
    geo/italy.py (the same codes the place filters and the normaliser use)."""
    if region is None:
        return []
    if region.provinces:
        return list(region.provinces)
    from .italy import provinces_of_region

    return [Province(p.code, p.name) for p in provinces_of_region(region.key)]


# Start point of a province / region search when the user gives no address: the
# province's chief town and the regional capital (approximate coordinates). The full
# national table, when present, lives in geo/italy.py and takes precedence.
_PROVINCE_CENTRES: dict[str, tuple[float, float, str]] = {
    "BO": (44.4939, 11.3428, "Bologna"), "FE": (44.8354, 11.6199, "Ferrara"),
    "FC": (44.2225, 12.0408, "Forlì"), "MO": (44.6471, 10.9252, "Modena"),
    "PR": (44.8015, 10.3279, "Parma"), "PC": (45.0526, 9.6930, "Piacenza"),
    "RA": (44.4175, 12.2035, "Ravenna"), "RE": (44.6983, 10.6312, "Reggio Emilia"),
    "RN": (44.0594, 12.5683, "Rimini"),
}
_REGION_CENTRES: dict[str, tuple[float, float, str]] = {
    "abruzzo": (42.3498, 13.3995, "L'Aquila"), "basilicata": (40.6401, 15.8051, "Potenza"),
    "calabria": (38.9098, 16.5877, "Catanzaro"), "campania": (40.8518, 14.2681, "Napoli"),
    "emilia-romagna": (44.4939, 11.3428, "Bologna"), "friuli-venezia-giulia": (45.6495, 13.7768, "Trieste"),
    "lazio": (41.9028, 12.4964, "Roma"), "liguria": (44.4056, 8.9463, "Genova"),
    "lombardia": (45.4642, 9.1900, "Milano"), "marche": (43.6158, 13.5189, "Ancona"),
    "molise": (41.5603, 14.6627, "Campobasso"), "piemonte": (45.0703, 7.6869, "Torino"),
    "puglia": (41.1171, 16.8719, "Bari"), "sardegna": (39.2238, 9.1217, "Cagliari"),
    "sicilia": (38.1157, 13.3615, "Palermo"), "toscana": (43.7696, 11.2558, "Firenze"),
    "trentino-alto-adige": (46.0748, 11.1217, "Trento"), "umbria": (43.1107, 12.3908, "Perugia"),
    "valle-d-aosta": (45.7372, 7.3201, "Aosta"), "veneto": (45.4408, 12.3155, "Venezia"),
}


def area_centre(area: str, province: str = "", region: str = "") -> tuple[float, float, str] | None:
    """(lat, lon, name) used as the start point of a province or region search without
    an address; None when unknown (or in radius mode)."""
    if area not in ("province", "region"):
        return None
    try:  # the national table, when available
        from . import italy  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - optional module
        italy = None
    code = (province or "").strip().upper()
    found = region_for(region) if region else None
    key = found.key if found is not None else (region or "").strip().lower()
    if italy is not None:
        for name, value in (("province_centre", code), ("region_centre", key)):
            if name == "province_centre" and (area != "province" or not code):
                continue
            getter = getattr(italy, name, None)
            if getter is None:
                continue
            try:
                centre = getter(value)
            except Exception:  # noqa: BLE001
                centre = None
            if centre:
                lat, lon = float(centre[0]), float(centre[1])
                label = centre[2] if len(centre) > 2 else value
                return lat, lon, str(label)
    if area == "province" and code in _PROVINCE_CENTRES:
        return _PROVINCE_CENTRES[code]
    return _REGION_CENTRES.get(key)
