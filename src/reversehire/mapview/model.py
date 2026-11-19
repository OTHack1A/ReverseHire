"""Plain-Python data model of the map: points, text sanitising, radius geometry.

Nothing here imports Qt, so it is cheap to test and safe to use from any thread.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass

log = logging.getLogger("reversehire.mapview")

KINDS = ("company", "posting", "home", "hypothesis")
DEFAULT_KIND = "company"

EARTH_RADIUS_KM = 6371.0088

MAX_ID_LEN = 200
MAX_LABEL_LEN = 200
MAX_TOOLTIP_LEN = 1000
MAX_STATUS_LEN = 40

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f  ]")


def clean_text(value, limit: int, *, keep_newlines: bool = False) -> str:
    """Plain text safe to hand to the page: control characters removed, length capped.

    The page inserts every string with ``textContent`` (never as HTML), so markup in a
    company name is shown literally and can never run. Nothing is HTML-escaped here on
    purpose: escaping *and* ``textContent`` would show "&amp;" to the user.
    """
    text = "" if value is None else str(value)
    if keep_newlines:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    else:
        text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = _CONTROL_CHARS.sub("", text)
    if len(text) > limit:
        text = text[: max(0, limit - 1)] + "…"
    return text.strip()


@dataclass(frozen=True)
class MapPoint:
    """One marker. ``id`` is returned by ``MapView.pointActivated``.

    kind     company | posting | home | hypothesis (anything else is shown as company)
    status   key looked up in the theme's status colours (empty: the colour of the kind)
    tooltip  plain text shown under the label when hovering (not HTML; escaped by design)
    """

    id: str
    lat: float
    lon: float
    label: str = ""
    kind: str = DEFAULT_KIND
    status: str = ""
    tooltip: str = ""

    def is_valid(self) -> bool:
        return valid_coordinates(self.lat, self.lon) and bool(clean_text(self.id, MAX_ID_LEN))

    def to_payload(self) -> dict:
        """The JSON-serialisable form sent to the page (sanitised)."""
        kind = self.kind if self.kind in KINDS else DEFAULT_KIND
        return {
            "id": clean_text(self.id, MAX_ID_LEN),
            "lat": round(float(self.lat), 6),
            "lon": round(float(self.lon), 6),
            "label": clean_text(self.label, MAX_LABEL_LEN),
            "kind": kind,
            "status": clean_text(self.status, MAX_STATUS_LEN),
            "tooltip": clean_text(self.tooltip, MAX_TOOLTIP_LEN, keep_newlines=True),
        }


def valid_coordinates(lat, lon) -> bool:
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return (math.isfinite(lat_f) and math.isfinite(lon_f)
            and -90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0)


def normalise_points(points) -> list[MapPoint]:
    """Valid points only, one per id (the last one wins, first position kept); logs the drops."""
    by_id: dict[str, MapPoint] = {}
    dropped = 0
    for point in points or ():
        if not isinstance(point, MapPoint) or not point.is_valid():
            dropped += 1
            continue
        by_id[clean_text(point.id, MAX_ID_LEN)] = point
    if dropped:
        log.warning("map: %d point(s) ignored (invalid coordinates or id)", dropped)
    return list(by_id.values())


def points_payload(points) -> list[dict]:
    return [point.to_payload() for point in normalise_points(points)]


# --- geometry -----------------------------------------------------------------------

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_phi = p2 - p1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def destination_point(lat: float, lon: float, bearing_deg: float, km: float) -> tuple[float, float]:
    """Point reached from (lat, lon) travelling ``km`` along ``bearing_deg`` on a sphere."""
    delta = km / EARTH_RADIUS_KM
    theta = math.radians(bearing_deg)
    phi1, lam1 = math.radians(lat), math.radians(lon)
    sin_phi2 = math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta)
    phi2 = math.asin(max(-1.0, min(1.0, sin_phi2)))
    lam2 = lam1 + math.atan2(math.sin(theta) * math.sin(delta) * math.cos(phi1),
                             math.cos(delta) - math.sin(phi1) * sin_phi2)
    lon2 = (math.degrees(lam2) + 540.0) % 360.0 - 180.0
    return math.degrees(phi2), lon2


def circle_polygon(lat: float, lon: float, km: float, steps: int = 72) -> list[tuple[float, float]]:
    """Closed ring of (lat, lon) vertices of the circle of radius ``km`` around a point."""
    steps = max(8, int(steps))
    ring = [destination_point(lat, lon, 360.0 * i / steps, km) for i in range(steps)]
    ring.append(ring[0])
    return ring


def circle_bounds(lat: float, lon: float, km: float) -> tuple[float, float, float, float]:
    """(south, west, north, east) of the circle; the longitude span widens with latitude."""
    north = destination_point(lat, lon, 0.0, km)[0]
    south = destination_point(lat, lon, 180.0, km)[0]
    east = destination_point(lat, lon, 90.0, km)[1]
    west = destination_point(lat, lon, 270.0, km)[1]
    return south, west, north, east


def points_bounds(points) -> tuple[float, float, float, float] | None:
    """(south, west, north, east) of the points, or None when there are none."""
    pts = list(points)
    if not pts:
        return None
    lats = [p.lat for p in pts]
    lons = [p.lon for p in pts]
    return min(lats), min(lons), max(lats), max(lons)


def valid_radius_km(km) -> float | None:
    """The radius as a finite positive float, or None (no circle)."""
    try:
        value = float(km)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return min(value, 5000.0)
