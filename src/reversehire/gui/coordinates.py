"""Pure functions for the starting point: reading pasted coordinates and moving a
point by azimuth and distance. No Qt here, so everything is unit-tested directly.

`parse_coordinates` accepts what Google Maps copies and the usual hand-written
forms:

* "44.4937, 11.3430" and "44.4937 11.3430" (dot decimals)
* "44,4937; 11,3430" and "44,4937 11,3430" (comma decimals)
* "44°29'37.3\"N 11°20'34.8\"E" (degrees, minutes, seconds, hemisphere)
* "44.4937° N, 11.3430° E" and "N 44.4937 E 11.3430" (hemisphere letters)
* a Google Maps URL containing "@44.4937,11.3430,15z"
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

LAT_LIMIT = 90.0
LON_LIMIT = 180.0
AZIMUTH_MAX = 360.0
DISTANCE_MAX_KM = 1000.0

_NUMBER = r"[+-]?\d+(?:[.,]\d+)?"
_UNSIGNED = r"\d+(?:[.,]\d+)?"

# Typographic variants of the degree, minute and second marks.
_MARKS = {
    "º": "°", "˚": "°",
    "′": "'", "’": "'", "‘": "'", "´": "'", "`": "'",
    "″": '"', "”": '"', "“": '"',
}


def _component(suffix: str) -> str:
    return (
        rf"(?P<h1{suffix}>[NSEW])?\s*"
        rf"(?P<deg{suffix}>{_NUMBER})\s*(?P<degmark{suffix}>°)?\s*"
        rf"(?:(?P<min{suffix}>{_UNSIGNED})\s*'\s*)?"
        rf"(?:(?P<sec{suffix}>{_UNSIGNED})\s*\"\s*)?"
        # A trailing letter only when there was no leading one ("N 44 E 11").
        rf"(?(h1{suffix})|(?P<h2{suffix}>[NSEW])?)"
    )


_DMS_PAIR = re.compile(rf"^\s*{_component('a')}\s*[,;]?\s*{_component('b')}\s*$", re.IGNORECASE)
_URL_AT = re.compile(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")
_DOT_PAIR = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*(?:,|\s)\s*([+-]?\d+(?:\.\d+)?)\s*$")
_COMMA_SPACE_PAIR = re.compile(r"^\s*([+-]?\d+(?:,\d+)?)\s+([+-]?\d+(?:,\d+)?)\s*$")
_SEMICOLON_PAIR = re.compile(rf"^\s*({_NUMBER})\s*;\s*({_NUMBER})\s*$")


def parse_number(text: str | None) -> float | None:
    """A finite number from a text field (comma or dot decimals); None if invalid."""
    try:
        value = float((text or "").strip().replace(",", "."))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _valid(lat: float, lon: float) -> tuple[float, float] | None:
    if -LAT_LIMIT <= lat <= LAT_LIMIT and -LON_LIMIT <= lon <= LON_LIMIT:
        return lat, lon
    return None


def _dms_value(match: re.Match, suffix: str) -> tuple[float, str | None] | None:
    """Signed decimal degrees and hemisphere letter of one component."""
    h1, h2 = match.group(f"h1{suffix}"), match.group(f"h2{suffix}")
    if h1 and h2:
        return None
    hemisphere = (h1 or h2 or "").upper() or None
    degrees = parse_number(match.group(f"deg{suffix}"))
    minutes = parse_number(match.group(f"min{suffix}") or "0")
    seconds = parse_number(match.group(f"sec{suffix}") or "0")
    if degrees is None or minutes is None or seconds is None:
        return None
    if minutes >= 60 or seconds >= 60:
        return None
    has_parts = match.group(f"min{suffix}") is not None or match.group(f"sec{suffix}") is not None
    if has_parts and (degrees != int(degrees)):
        return None  # "44.5°30'" makes no sense
    negative = degrees < 0
    value = abs(degrees) + minutes / 60 + seconds / 3600
    if hemisphere in ("S", "W"):
        if negative:
            return None  # "-44 S" is contradictory
        negative = True
    return (-value if negative else value), hemisphere


def _parse_dms(text: str) -> tuple[float, float] | None:
    match = _DMS_PAIR.match(text)
    if match is None:
        return None
    marked = any(match.group(g) for g in ("degmarka", "degmarkb", "mina", "minb", "seca", "secb",
                                          "h1a", "h2a", "h1b", "h2b"))
    if not marked:
        return None  # plain numbers are handled by the decimal forms
    first, second = _dms_value(match, "a"), _dms_value(match, "b")
    if first is None or second is None:
        return None
    (v1, h1), (v2, h2) = first, second
    lat_letters, lon_letters = {"N", "S"}, {"E", "W"}
    if h1 in lon_letters and (h2 in lat_letters or h2 is None):
        v1, v2, h1, h2 = v2, v1, h2, h1  # longitude written first
    if (h1 is not None and h1 not in lat_letters) or (h2 is not None and h2 not in lon_letters):
        return None
    return _valid(v1, v2)


def parse_coordinates(text: str | None) -> tuple[float, float] | None:
    """(latitude, longitude) read from `text`, or None if it is not a valid pair."""
    if not isinstance(text, str):
        return None
    clean = text.strip()
    if not clean:
        return None
    for variant, mark in _MARKS.items():
        clean = clean.replace(variant, mark)
    clean = clean.replace("''", '"')

    at = _URL_AT.search(clean)
    if at is not None:
        return _valid(float(at.group(1)), float(at.group(2)))

    for pattern in (_DOT_PAIR, _COMMA_SPACE_PAIR, _SEMICOLON_PAIR):
        match = pattern.match(clean)
        if match is not None:
            lat, lon = parse_number(match.group(1)), parse_number(match.group(2))
            if lat is None or lon is None:
                return None
            return _valid(lat, lon)
    return _parse_dms(clean)


def destination_point(lat: float, lon: float, azimuth_deg: float, distance_km: float) -> tuple[float, float]:
    """Point reached from (lat, lon) moving `distance_km` towards `azimuth_deg` (0 = north),
    computed by the project's `geo.distanza.punto_da_azimut`."""
    from ..geo.distance import point_from_bearing

    end_lat, end_lon = point_from_bearing(lat, lon, azimuth_deg, distance_km)
    return end_lat, end_lon


@dataclass(frozen=True)
class StartPoint:
    lat: float
    lon: float
    origin_lat: float
    origin_lon: float
    azimuth: float | None = None
    distance_km: float | None = None

    @property
    def moved(self) -> bool:
        return self.azimuth is not None


def compute_start_point(lat_text: str, lon_text: str, azimuth_text: str = "",
                        distance_text: str = "") -> tuple[StartPoint | None, list[str]]:
    """Validates the dialog fields and computes the starting point.

    Returns (point, []) or (None, error keys). Error keys are i18n keys; the
    distance error takes the `max` placeholder (DISTANCE_MAX_KM).
    """
    errors: list[str] = []
    if not (lat_text or "").strip() and not (lon_text or "").strip():
        return None, ["start.empty"]
    lat = parse_number(lat_text)
    if lat is None or not -LAT_LIMIT <= lat <= LAT_LIMIT:
        errors.append("start.err_lat")
    lon = parse_number(lon_text)
    if lon is None or not -LON_LIMIT <= lon <= LON_LIMIT:
        errors.append("start.err_lon")

    has_azimuth = bool((azimuth_text or "").strip())
    has_distance = bool((distance_text or "").strip())
    azimuth = distance = None
    if has_azimuth != has_distance:
        errors.append("start.err_pair")
    elif has_azimuth:
        azimuth = parse_number(azimuth_text)
        if azimuth is None or not 0.0 <= azimuth <= AZIMUTH_MAX:
            errors.append("start.err_azimuth")
        distance = parse_number(distance_text)
        if distance is None or not 0.0 < distance <= DISTANCE_MAX_KM:
            errors.append("start.err_distance")
    if errors:
        return None, errors
    if azimuth is None:
        return StartPoint(lat, lon, lat, lon), []
    end_lat, end_lon = destination_point(lat, lon, azimuth % 360.0, distance)
    return StartPoint(end_lat, end_lon, lat, lon, azimuth, distance), []
