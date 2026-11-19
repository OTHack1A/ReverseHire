"""Source A: a LOCAL OpenStreetMap extract, filtered once into a small SQLite index.

The public Overpass servers are often overloaded; a regional extract downloaded once
answers radius, province and region queries instantly and offline.

1. download: the regional .osm.pbf (Emilia-Romagna ~205 MB, download.openstreetmap.fr,
   daily) into `<data folder>/osm/`, through the polite gateway: resumable, throttled
   (3 MB/s by default), cancellable, verified with the server's .md5 when available,
   reused for MAX_AGE_DAYS (then refreshed with If-Modified-Since);
2. index: ONE read of the file with the built-in reader (`pbf.py`, stdlib + numpy; the
   compiled `osmium` is not needed) keeps only company-like features (the tag set of
   `geo/sectors.py`, generic company tags included) with a name: nodes, ways (centroid
   of their nodes) and multipolygon relations (centroid of their outer ways), plus
   towns (place=*) and the province / municipality boundaries (admin_level 6 / 8),
   which are rasterised so that every feature gets its province and municipality;
3. queries: `OsmIndex.radius / province / region / count_*`, `find_place(town)`.

Data (c) OpenStreetMap contributors, ODbL. The index is a derived database: it stays
on the user's PC (never published).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from ..config import data_folder
from ..geo.distance import haversine_km
from ..geo.overpass import GENERIC_TAGS, USEFUL_TAGS, normalize_site
from ..geo.regions import Region, region_for
from ..geo.sectors import GENERIC_COMPANY_TAGS, SECTORS, normalize_text
from ..models import Company
from . import pbf
from .gateway import Cancelled, GatewayError, PoliteGateway

log = logging.getLogger(__name__)

FOLDER_NAME = "osm"
MAX_AGE_DAYS = 30
INDEX_VERSION = "4"  # 4: the "product" tag is stored (pre-crawl ranking)
GRID_RES_DEG = 0.005          # ~550 m x 400 m cells for the province/municipality raster
PLACE_KINDS = {"city", "town", "village", "suburb", "hamlet", "quarter"}
CONTACT_TAGS = ("website", "contact:website", "url", "phone", "contact:phone", "email",
                "contact:email", "operator", "brand", "wikidata", "start_date", "ref:vatin",
                "addr:street", "addr:housenumber", "addr:postcode", "addr:city", "addr:province")

ProgressFn = Callable[[str, int, int], None]  # (phase, done, total)


# --- the tag set ---------------------------------------------------------------------------


def _feature_pairs() -> tuple[set[tuple[str, str]], set[str]]:
    pairs: set[tuple[str, str]] = set()
    wild: set[str] = set()
    every = [p for s in SECTORS.values() for p in s.osm_tags] + list(GENERIC_COMPANY_TAGS) + list(GENERIC_TAGS)
    for key, value in every:
        if value == "*":
            wild.add(key)
        else:
            pairs.add((key, value))
    return pairs, wild


FEATURE_PAIRS, WILD_KEYS = _feature_pairs()
FEATURE_KEYS = {k for k, _ in FEATURE_PAIRS} | WILD_KEYS
_FEATURE_KEY_BYTES = {k.encode() for k in FEATURE_KEYS}
_NODE_KEY_BYTES = _FEATURE_KEY_BYTES | {b"place"}
_RELATION_KEY_BYTES = _FEATURE_KEY_BYTES | {b"boundary"}
_STORED_TAGS = tuple(dict.fromkeys(list(USEFUL_TAGS) + sorted(FEATURE_KEYS)))


def is_feature(tags: dict) -> bool:
    if not (tags.get("name") or "").strip():
        return False
    for key, value in tags.items():
        if key in WILD_KEYS or (key, value) in FEATURE_PAIRS:
            return True
    return False


# --- paths and status ------------------------------------------------------------------------


def osm_folder() -> Path:
    folder = data_folder() / FOLDER_NAME
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def pbf_path(region: Region) -> Path:
    return osm_folder() / region.file_name


def index_path(region: Region) -> Path:
    return osm_folder() / f"{region.key}.index.sqlite"


@dataclass
class ExtractStatus:
    region: str
    pbf_exists: bool = False
    pbf_bytes: int = 0
    pbf_age_days: float | None = None
    index_exists: bool = False
    index_age_days: float | None = None
    features: int = 0
    places: int = 0
    build_seconds: float | None = None

    @property
    def ready(self) -> bool:
        return self.index_exists and self.features > 0

    @property
    def stale(self) -> bool:
        return self.index_age_days is None or self.index_age_days > MAX_AGE_DAYS

    def to_dict(self) -> dict:
        return dict(self.__dict__, ready=self.ready, stale=self.stale)


def _age_days(path: Path) -> float | None:
    try:
        return max(0.0, (time.time() - path.stat().st_mtime) / 86400.0)
    except OSError:
        return None


def status(region: Region | str | None) -> ExtractStatus:
    region = region if isinstance(region, Region) else region_for(region)
    if region is None:
        return ExtractStatus(region="")
    result = ExtractStatus(region=region.key)
    source = pbf_path(region)
    if source.exists():
        result.pbf_exists = True
        result.pbf_bytes = source.stat().st_size
        result.pbf_age_days = _age_days(source)
    target = index_path(region)
    if target.exists():
        try:
            meta = OsmIndex(target).meta()
        except (sqlite3.Error, OSError):
            meta = {}
        if meta.get("index_version") == INDEX_VERSION:
            result.index_exists = True
            built = float(meta.get("built_at", 0) or 0)
            result.index_age_days = max(0.0, (time.time() - built) / 86400.0) if built else _age_days(target)
            result.features = int(meta.get("features", 0) or 0)
            result.places = int(meta.get("places", 0) or 0)
            result.build_seconds = float(meta.get("seconds", 0) or 0) or None
    return result


# --- download -------------------------------------------------------------------------------


def download(region: Region, gateway: PoliteGateway, *, cancel=None,
             progress: ProgressFn | None = None, force: bool = False,
             max_rate_bps: float | None = None) -> str:
    """Make sure the region's .osm.pbf is on disk: "reused" (younger than MAX_AGE_DAYS),
    "not_modified" (the server has nothing newer) or "downloaded"."""
    target = pbf_path(region)
    age = _age_days(target) if target.exists() else None
    if age is not None and age <= MAX_AGE_DAYS and not force:
        return "reused"

    def report(done: int, total: int | None) -> None:
        if progress is not None:
            progress("download", done, total or 0)

    kwargs = {} if max_rate_bps is None else {"max_rate_bps": max_rate_bps}
    outcome = gateway.download(region.url, target, cancel=cancel, progress=report,
                               if_modified_since=target.stat().st_mtime if target.exists() else None,
                               **kwargs)
    if outcome == "not_modified":
        os.utime(target, None)
        return outcome
    expected = _remote_md5(region, gateway, cancel)
    if expected:
        actual = _md5(target, cancel)
        if actual != expected:
            target.unlink(missing_ok=True)
            raise GatewayError(f"downloaded map data is damaged (md5 {actual} != {expected})")
    return outcome


def _remote_md5(region: Region, gateway: PoliteGateway, cancel) -> str:
    try:
        reply = gateway.get(region.url + ".md5", cancel=cancel)
    except GatewayError:
        return ""
    if reply.status != 200:
        return ""
    text = reply.text.strip().split()
    return text[0].lower() if text and len(text[0]) == 32 else ""


def _md5(path: Path, cancel=None) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            chunk = handle.read(4 * 1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


# --- index build ---------------------------------------------------------------------------------


@dataclass
class BuildReport:
    features: int = 0
    places: int = 0
    provinces: int = 0
    municipalities: int = 0
    seconds: float = 0.0
    pass_seconds: list[float] = field(default_factory=list)


def _province_code(tags: dict) -> str:
    for key in ("ref", "short_name"):
        value = (tags.get(key) or "").strip().upper()
        if len(value) == 2 and value.isalpha():
            return value
    iso = (tags.get("ISO3166-2") or "").strip().upper()
    if "-" in iso and len(iso.split("-")[-1]) == 2 and iso.split("-")[-1].isalpha():
        return iso.split("-")[-1]
    return ""


class _Grid:
    """Raster of polygons on a regular lat/lon grid (even-odd scanline fill)."""

    def __init__(self, south: float, west: float, north: float, east: float, res: float = GRID_RES_DEG):
        self.res = res
        self.south, self.west = south, west
        self.rows = max(1, int(math.ceil((north - south) / res)))
        self.cols = max(1, int(math.ceil((east - west) / res)))
        self.cells = np.full((self.rows, self.cols), -1, dtype=np.int32)

    def fill(self, edges: np.ndarray, value: int) -> None:
        """edges: (n, 4) array of lon1, lat1, lon2, lat2 (all the segments of the outer
        and inner rings; their order does not matter for an even-odd fill)."""
        if edges.size == 0:
            return
        x1, y1, x2, y2 = edges[:, 0], edges[:, 1], edges[:, 2], edges[:, 3]
        keep = y1 != y2
        x1, y1, x2, y2 = x1[keep], y1[keep], x2[keep], y2[keep]
        swap = y1 > y2
        x1, x2 = np.where(swap, x2, x1), np.where(swap, x1, x2)
        y1, y2 = np.where(swap, y2, y1), np.where(swap, y1, y2)
        # Rows whose centre y_r = south + (r + 0.5) res lies in [y1, y2).
        r0 = np.ceil((y1 - self.south) / self.res - 0.5).astype(np.int64)
        r1 = np.ceil((y2 - self.south) / self.res - 0.5).astype(np.int64) - 1
        counts = np.clip(r1 - r0 + 1, 0, None)
        if counts.sum() == 0:
            return
        edge_of = np.repeat(np.arange(x1.size), counts)
        first = np.repeat(r0, counts)
        offsets = np.arange(counts.sum()) - np.repeat(np.cumsum(counts) - counts, counts)
        rows = first + offsets
        yc = self.south + (rows + 0.5) * self.res
        e = edge_of
        xs = x1[e] + (yc - y1[e]) * (x2[e] - x1[e]) / (y2[e] - y1[e])
        order = np.lexsort((xs, rows))
        rows, xs = rows[order], xs[order]
        boundaries = np.flatnonzero(np.diff(rows)) + 1
        for chunk_rows, chunk_xs in zip(np.split(rows, boundaries), np.split(xs, boundaries)):
            row = int(chunk_rows[0])
            if row < 0 or row >= self.rows:
                continue
            for i in range(0, len(chunk_xs) - 1, 2):
                c0 = int(math.ceil((chunk_xs[i] - self.west) / self.res - 0.5))
                c1 = int(math.ceil((chunk_xs[i + 1] - self.west) / self.res - 0.5)) - 1
                c0, c1 = max(c0, 0), min(c1, self.cols - 1)
                if c1 >= c0:
                    self.cells[row, c0:c1 + 1] = value

    def lookup(self, lat: float, lon: float) -> int:
        r = int(math.floor((lat - self.south) / self.res))
        c = int(math.floor((lon - self.west) / self.res))
        if 0 <= r < self.rows and 0 <= c < self.cols:
            return int(self.cells[r, c])
        return -1


def build_index(source: Path, target: Path, *, cancel=None, progress: ProgressFn | None = None,
                region_key: str = "") -> BuildReport:
    """Read `source` (.osm.pbf) once (three passes over parts of it) and write the
    SQLite index `target` (built in a temporary file, then renamed)."""
    started = time.monotonic()
    report = BuildReport()
    reader = pbf.PbfFile(source)
    total = reader.size

    def tick(phase: str, offset: int) -> None:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if progress is not None:
            progress(phase, offset, total)

    dense_offsets: list[int] = []
    way_offsets: list[int] = []
    features: dict[str, dict] = {}       # osm id -> {"tags", "lat", "lon"} (nodes)
    feature_ways: dict[int, tuple[dict, np.ndarray]] = {}
    feature_rels: dict[int, tuple[dict, list[int]]] = {}
    admins: list[tuple[int, dict, list[int]]] = []
    places: list[tuple[str, str, float, float]] = []

    # Pass 1: tagged nodes, feature ways, relations (features and admin boundaries).
    for offset, data in reader.data_blobs():
        tick("index_pass1", offset)
        block = pbf.parse_block(data)
        kinds = block.kinds()
        if kinds & {1, 2}:
            dense_offsets.append(offset)
        if 3 in kinds:
            way_offsets.append(offset)
        for kind, payload in block.groups:
            if kind in (1, 2):
                key_ids = block.ids_of(_NODE_KEY_BYTES)
                if not key_ids:
                    continue
                nodes = (pbf.dense_tagged(block, payload, key_ids) if kind == 2
                         else pbf.plain_nodes(block, payload, key_ids))
                for node_id, lat, lon, tags in nodes:
                    if is_feature(tags):
                        features[f"n{node_id}"] = {"tags": tags, "lat": lat, "lon": lon}
                    elif tags.get("place") in PLACE_KINDS and tags.get("name"):
                        places.append((tags["name"], tags["place"], lat, lon))
            elif kind == 3:
                key_ids = block.ids_of(_FEATURE_KEY_BYTES)
                if not key_ids:
                    continue
                for way_id, tags, refs in pbf.ways(block, payload, key_ids):
                    if is_feature(tags) and refs is not None and refs.size:
                        feature_ways[way_id] = (tags, refs)
            elif kind == 4:
                key_ids = block.ids_of(_RELATION_KEY_BYTES)
                if not key_ids:
                    continue
                for rel_id, tags, members in pbf.relations(block, payload, key_ids):
                    if tags.get("boundary") == "administrative" and tags.get("admin_level") in ("6", "8"):
                        admins.append((rel_id, tags, [m[1] for m in members if m[0] == 1]))
                    elif is_feature(tags) and tags.get("type") in ("multipolygon", "site", None):
                        outer = [m[1] for m in members if m[0] == 1 and m[2] in ("outer", "")]
                        if outer:
                            feature_rels[rel_id] = (tags, outer)
    report.pass_seconds.append(round(time.monotonic() - started, 1))

    # Pass 2: node lists of the ways that relations need (outer rings, boundaries).
    wanted_ways = {w for _, (_, members) in feature_rels.items() for w in members}
    wanted_ways |= {w for _, _, members in admins for w in members}
    wanted_ways -= set(feature_ways)
    member_refs: dict[int, np.ndarray] = {}
    if wanted_ways:
        for offset, data in reader.data_blobs(way_offsets):
            tick("index_pass2", offset)
            block = pbf.parse_block(data)
            for kind, payload in block.groups:
                if kind == 3:
                    for way_id, _, refs in pbf.ways(block, payload, None, wanted_ids=wanted_ways):
                        if refs is not None:
                            member_refs[way_id] = refs
    for way_id in set(wanted_ways) & set(feature_ways):
        member_refs[way_id] = feature_ways[way_id][1]
    for way_id in {w for _, (_, m) in feature_rels.items() for w in m} & set(feature_ways):
        member_refs[way_id] = feature_ways[way_id][1]
    report.pass_seconds.append(round(time.monotonic() - started, 1))

    # Pass 3: coordinates of every node those ways reference.
    arrays = [refs for _, refs in feature_ways.values()] + list(member_refs.values())
    need = np.unique(np.concatenate(arrays)) if arrays else np.zeros(0, dtype=np.int64)
    need_lat = np.full(need.size, np.nan)
    need_lon = np.full(need.size, np.nan)
    if need.size:
        for offset, data in reader.data_blobs(dense_offsets):
            tick("index_pass3", offset)
            block = pbf.parse_block(data)
            for kind, payload in block.groups:
                if kind == 2:
                    ids, lat, lon, _ = pbf.dense_arrays(payload)
                    if ids.size == 0:
                        continue
                    position = np.searchsorted(need, ids)
                    position[position >= need.size] = 0
                    hit = need[position] == ids
                    if hit.any():
                        need_lat[position[hit]] = pbf.degrees(block, lat[hit], block.lat_offset)
                        need_lon[position[hit]] = pbf.degrees(block, lon[hit], block.lon_offset)
                elif kind == 1:
                    for node_id, la, lo in pbf.plain_node_coords(block, payload):
                        i = int(np.searchsorted(need, node_id))
                        if i < need.size and need[i] == node_id:
                            need_lat[i], need_lon[i] = la, lo
    report.pass_seconds.append(round(time.monotonic() - started, 1))

    def coords(refs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if refs is None or refs.size == 0 or need.size == 0:
            return np.zeros(0), np.zeros(0)
        position = np.searchsorted(need, refs)
        position[position >= need.size] = 0
        found = need[position] == refs
        la, lo = need_lat[position[found]], need_lon[position[found]]
        ok = ~np.isnan(la)
        return la[ok], lo[ok]

    for way_id, (tags, refs) in feature_ways.items():
        if refs.size > 2 and refs[0] == refs[-1]:
            refs = refs[:-1]
        la, lo = coords(refs)
        if la.size:
            features[f"w{way_id}"] = {"tags": tags, "lat": float(la.mean()), "lon": float(lo.mean())}
    for rel_id, (tags, outer) in feature_rels.items():
        parts = [coords(member_refs.get(w)) for w in outer]
        la = np.concatenate([p[0] for p in parts]) if parts else np.zeros(0)
        lo = np.concatenate([p[1] for p in parts]) if parts else np.zeros(0)
        if la.size:
            features[f"r{rel_id}"] = {"tags": tags, "lat": float(la.mean()), "lon": float(lo.mean())}

    # Province / municipality raster.
    province_grid = municipality_grid = None
    province_names: list[tuple[str, str]] = []
    municipality_names: list[str] = []
    edge_sets: list[tuple[dict, np.ndarray]] = []
    for _, tags, members in admins:
        segments = []
        for way_id in members:
            la, lo = coords(member_refs.get(way_id))
            if la.size >= 2:
                segments.append(np.column_stack([lo[:-1], la[:-1], lo[1:], la[1:]]))
        if segments:
            edge_sets.append((tags, np.concatenate(segments)))
    if edge_sets:
        every = np.concatenate([e for _, e in edge_sets])
        south, north = float(every[:, [1, 3]].min()) - 0.01, float(every[:, [1, 3]].max()) + 0.01
        west, east = float(every[:, [0, 2]].min()) - 0.01, float(every[:, [0, 2]].max()) + 0.01
        province_grid = _Grid(south, west, north, east)
        municipality_grid = _Grid(south, west, north, east)
        for tags, edges in edge_sets:
            if tags.get("admin_level") == "6":
                code = _province_code(tags)
                province_grid.fill(edges, len(province_names))
                province_names.append((code, tags.get("name", "")))
            else:
                municipality_grid.fill(edges, len(municipality_names))
                municipality_names.append(tags.get("name", ""))
    report.provinces = len(province_names)
    report.municipalities = len(municipality_names)

    def area_of(lat: float, lon: float) -> tuple[str, str]:
        province = municipality = ""
        if province_grid is not None:
            i = province_grid.lookup(lat, lon)
            if i >= 0:
                province = province_names[i][0]
            j = municipality_grid.lookup(lat, lon)
            if j >= 0:
                municipality = municipality_names[j]
        return province, municipality

    tick("index_write", total)
    _write_index(target, features, places, province_names, area_of, region_key, report, started, source)
    report.features = len(features)
    report.places = len(places)
    report.seconds = round(time.monotonic() - started, 1)
    log.info("OSM index built: %d features, %d places, %d provinces, %d municipalities in %.1f s",
             report.features, report.places, report.provinces, report.municipalities, report.seconds)
    return report


_SCHEMA = """
CREATE TABLE features (
    osm_id TEXT PRIMARY KEY, name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
    tags TEXT NOT NULL, website TEXT, phone TEXT, email TEXT, address TEXT, city TEXT,
    postcode TEXT, province TEXT, municipality TEXT, wikidata TEXT, operator TEXT, brand TEXT,
    start_date TEXT, vat TEXT
);
CREATE INDEX features_lat ON features(lat);
CREATE INDEX features_province ON features(province);
CREATE TABLE places (name TEXT, norm TEXT, kind TEXT, lat REAL, lon REAL, province TEXT, municipality TEXT);
CREATE INDEX places_norm ON places(norm);
CREATE TABLE provinces (code TEXT, name TEXT);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _write_index(target: Path, features: dict, places: list, provinces: list, area_of,
                 region_key: str, report: BuildReport, started: float, source: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".building")
    temporary.unlink(missing_ok=True)
    connection = sqlite3.connect(str(temporary))
    try:
        connection.executescript(_SCHEMA)
        rows = []
        for osm_id, item in features.items():
            tags = item["tags"]
            province, municipality = area_of(item["lat"], item["lon"])
            province = province or (tags.get("addr:province") or "").strip().upper()[:2]
            stored = {k: tags[k] for k in _STORED_TAGS if k in tags}
            website = next((tags[k] for k in ("website", "contact:website", "url") if tags.get(k)), None)
            street = " ".join(p for p in (tags.get("addr:street"), tags.get("addr:housenumber")) if p)
            town = " ".join(p for p in (tags.get("addr:postcode"), tags.get("addr:city")) if p)
            rows.append((osm_id, tags["name"].strip(), item["lat"], item["lon"],
                         json.dumps(stored, ensure_ascii=False), website,
                         tags.get("phone") or tags.get("contact:phone"),
                         tags.get("email") or tags.get("contact:email"),
                         ", ".join(p for p in (street, town) if p),
                         tags.get("addr:city") or municipality, tags.get("addr:postcode"),
                         province, municipality, tags.get("wikidata"), tags.get("operator"),
                         tags.get("brand"), tags.get("start_date"), tags.get("ref:vatin")))
        connection.executemany("INSERT OR REPLACE INTO features VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        place_rows = []
        for name, kind, lat, lon in places:
            province, municipality = area_of(lat, lon)
            place_rows.append((name, normalize_text(name), kind, lat, lon, province, municipality))
        connection.executemany("INSERT INTO places VALUES (?,?,?,?,?,?,?)", place_rows)
        connection.executemany("INSERT INTO provinces VALUES (?,?)",
                               sorted({(c, n) for c, n in provinces if c}))
        meta = {"index_version": INDEX_VERSION, "built_at": str(time.time()), "region": region_key,
                "features": str(len(rows)), "places": str(len(place_rows)),
                "seconds": f"{time.monotonic() - started:.1f}", "source_file": source.name,
                "source_bytes": str(source.stat().st_size), "source_mtime": str(source.stat().st_mtime),
                "pass_seconds": json.dumps(report.pass_seconds)}
        connection.executemany("INSERT INTO meta VALUES (?,?)", list(meta.items()))
        connection.commit()
    finally:
        connection.close()
    os.replace(temporary, target)


# --- queries -----------------------------------------------------------------------------------


class OsmIndex:
    """Read-only access to a built index."""

    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        if not self.path.exists():
            raise FileNotFoundError(str(self.path))

    @contextlib.contextmanager
    def _connect(self):
        """A short read-only connection, always closed (Windows keeps files locked otherwise)."""
        connection = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def meta(self) -> dict:
        with self._connect() as connection:
            return {r["key"]: r["value"] for r in connection.execute("SELECT key, value FROM meta")}

    def provinces(self) -> list[tuple[str, str]]:
        with self._connect() as connection:
            return [(r["code"], r["name"]) for r in connection.execute(
                "SELECT code, name FROM provinces ORDER BY code")]

    def _rows(self, where: str, args: tuple) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(f"SELECT * FROM features WHERE {where}", args).fetchall()

    @staticmethod
    def _box(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
        dlat = radius_km / 111.32
        dlon = radius_km / (111.32 * max(0.05, math.cos(math.radians(lat))))
        return lat - dlat, lat + dlat, lon - dlon, lon + dlon

    def radius(self, lat: float, lon: float, radius_km: float) -> list[Company]:
        south, north, west, east = self._box(lat, lon, radius_km)
        rows = self._rows("lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?", (south, north, west, east))
        result = []
        for row in rows:
            distance = haversine_km(lat, lon, row["lat"], row["lon"])
            if distance <= radius_km:
                result.append(row_to_company(row, distance))
        result.sort(key=lambda c: (c.distance_km, c.name))
        return result

    def count_radius(self, lat: float, lon: float, radius_km: float) -> int:
        return len(self.radius(lat, lon, radius_km))

    def province(self, code: str, lat: float | None = None, lon: float | None = None) -> list[Company]:
        rows = self._rows("province = ?", ((code or "").upper(),))
        return _with_distance(rows, lat, lon)

    def region(self, lat: float | None = None, lon: float | None = None) -> list[Company]:
        return _with_distance(self._rows("1 = 1", ()), lat, lon)

    def count_province(self, code: str) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM features WHERE province = ?",
                                          ((code or "").upper(),)).fetchone()[0])

    def count_region(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM features").fetchone()[0])

    def find_place(self, name: str, province: str = "") -> tuple[float, float, str] | None:
        """(lat, lon, province) of a town by name (accents/case ignored); the largest kind
        wins (city > town > village...), within `province` when given."""
        norm = normalize_text(name)
        if not norm:
            return None
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM places WHERE norm = ?", (norm,)).fetchall()
        if province:
            rows = [r for r in rows if (r["province"] or "") == province.upper()] or rows
        if not rows:
            return None
        order = {"city": 0, "town": 1, "village": 2, "suburb": 3, "quarter": 4, "hamlet": 5}
        row = min(rows, key=lambda r: order.get(r["kind"], 9))
        return float(row["lat"]), float(row["lon"]), row["province"] or ""

    def bbox_contains(self, lat: float, lon: float) -> bool:
        """True when the point lies inside the area covered by the index (rough box)."""
        with self._connect() as connection:
            row = connection.execute("SELECT MIN(lat), MAX(lat), MIN(lon), MAX(lon) FROM features").fetchone()
        if row is None or row[0] is None:
            return False
        return row[0] - 0.05 <= lat <= row[1] + 0.05 and row[2] - 0.05 <= lon <= row[3] + 0.05


def _with_distance(rows, lat, lon) -> list[Company]:
    result = []
    for row in rows:
        distance = haversine_km(lat, lon, row["lat"], row["lon"]) if lat is not None and lon is not None else None
        result.append(row_to_company(row, distance))
    result.sort(key=lambda c: (c.distance_km if c.distance_km is not None else math.inf, c.name))
    return result


def row_to_company(row, distance_km: float | None) -> Company:
    tags = json.loads(row["tags"] or "{}")
    useful = {k: v for k, v in tags.items() if k in USEFUL_TAGS}
    useful["osm_id"] = {"n": "node", "w": "way", "r": "relation"}.get(row["osm_id"][0], "node") + "/" + row["osm_id"][1:]
    company = Company(name=row["name"], lat=float(row["lat"]), lon=float(row["lon"]),
                      address=row["address"] or "", site=normalize_site(row["website"]),
                      osm_tags=useful,
                      distance_km=round(distance_km, 3) if distance_km is not None else None,
                      origin="osm")
    extra = {"province": row["province"] or "", "municipality": row["municipality"] or "",
             "phone": row["phone"] or "", "email": row["email"] or "", "wikidata": row["wikidata"] or "",
             "operator": row["operator"] or "", "brand": row["brand"] or "",
             "start_date": row["start_date"] or "", "vat": row["vat"] or "",
             "town": row["city"] or ""}
    company.discovery_fields = {k: v for k, v in extra.items() if v}  # read by merge.attach
    return company
