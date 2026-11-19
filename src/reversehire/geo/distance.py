"""Distance between two points on the Earth's surface."""

from math import asin, atan2, cos, degrees, isfinite, radians, sin, sqrt

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in km on the Earth sphere (haversine formula)."""
    f1, f2 = radians(lat1), radians(lat2)
    df = f2 - f1
    dl = radians(lon2) - radians(lon1)
    a = sin(df / 2) ** 2 + cos(f1) * cos(f2) * sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(min(1.0, sqrt(a)))


def point_from_bearing(lat: float, lon: float, azimuth_deg: float, distance_km: float) -> tuple[float, float]:
    """Destination point after travelling `distance_km` along a great circle.

    Spherical Earth with the same radius as `haversine_km`. The azimuth is in
    degrees, 0 = north, clockwise (90 = east). Returns (lat, lon) in degrees with
    the longitude normalised to [-180, 180].

    Raises ValueError for non-finite inputs, latitude outside [-90, 90] or a
    negative distance. Any finite input longitude is accepted.
    """
    for name, value in (("lat", lat), ("lon", lon), ("azimuth_deg", azimuth_deg),
                         ("distance_km", distance_km)):
        if not isfinite(value):
            raise ValueError(f"{name} must be a finite number, got {value!r}")
    if not -90.0 <= lat <= 90.0:
        raise ValueError(f"latitude must be within [-90, 90], got {lat!r}")
    if distance_km < 0:
        raise ValueError(f"distance must not be negative, got {distance_km!r}")

    f1, l1 = radians(lat), radians(lon)
    theta = radians(azimuth_deg)
    delta = distance_km / EARTH_RADIUS_KM  # angular distance
    sin_f2 = sin(f1) * cos(delta) + cos(f1) * sin(delta) * cos(theta)
    f2 = asin(max(-1.0, min(1.0, sin_f2)))
    l2 = l1 + atan2(sin(theta) * sin(delta) * cos(f1), cos(delta) - sin(f1) * sin_f2)
    lon2 = (degrees(l2) + 540.0) % 360.0 - 180.0
    if lon2 == -180.0 and degrees(l2) > 0:
        lon2 = 180.0  # keep the eastern antimeridian as +180
    return degrees(f2), lon2
