"""From an address or coordinates to a geographic point (OpenStreetMap Nominatim).

Nominatim usage policy is respected: at most 1 request per second (across
threads), an identifying User-Agent, and cached results.
"""

import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable

import httpx

from ..store import Store
from ..config import Settings

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
CACHE_NAMESPACE = "nominatim"
MIN_INTERVAL_S = 1.0
TIMEOUT_S = 15.0

_NUMBER_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
_SEPARATORS = set(" ,;/\t")


@dataclass
class Place:
    lat: float
    lon: float
    name: str


def parse_coordinates(text: str) -> tuple[float, float] | None:
    """Read a "lat, lon" pair from a text; None if they are not valid coordinates.

    Accepts comma, semicolon or space separators and, when unambiguous, the
    Italian decimal comma ("44,4949; 11,3426" or "44,4949 11,3426").
    """
    if not isinstance(text, str):
        return None
    t = text.strip()
    if not t:
        return None
    matches = list(_NUMBER_RE.finditer(t))
    if len(matches) != 2:
        return None
    # Only separators are allowed outside the two numbers.
    outside = t[: matches[0].start()] + t[matches[0].end() : matches[1].start()] + t[matches[1].end() :]
    if any(c not in _SEPARATORS for c in outside):
        return None
    try:
        lat = float(matches[0].group().replace(",", "."))
        lon = float(matches[1].group().replace(",", "."))
    except ValueError:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


def _cache_key(text: str) -> str:
    """Normalised text: lowercase, no accents, collapsed spaces."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    unaccented = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(unaccented.split())


class RateLimiter:
    """Guarantees a minimum interval between requests, even across threads.

    The clock and the wait are injectable, so tests do not really wait.
    """

    def __init__(
        self,
        interval_s: float = MIN_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        wait: Callable[[float], None] = time.sleep,
    ):
        self.interval_s = interval_s
        self._clock = clock
        self._wait = wait
        self._lock = threading.Lock()
        self._last: float | None = None

    def wait_turn(self) -> None:
        """Block until a new request is allowed and record the moment."""
        with self._lock:
            if self._last is not None:
                remaining = self._last + self.interval_s - self._clock()
                if remaining > 0:
                    self._wait(remaining)
            self._last = self._clock()


# Limiter shared by all Geocoders in the process (one IP, one limit).
_DEFAULT_LIMITER = RateLimiter()


class Geocoder:
    """Convert a text into a `Place`: direct coordinates or Nominatim."""

    def __init__(
        self,
        settings: Settings,
        store: Store | None = None,
        client: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
    ):
        self._settings = settings
        self._store = store
        self._client = client
        self._limiter = limiter or _DEFAULT_LIMITER

    def geocode(self, text: str) -> Place | None:
        """Place for an address or a coordinate pair; None if not found
        or if the network is unavailable."""
        if not isinstance(text, str) or not text.strip():
            return None
        coords = parse_coordinates(text)
        if coords is not None:
            return Place(lat=coords[0], lon=coords[1], name=text.strip())

        key = _cache_key(text)
        if self._store is not None:
            in_cache = self._store.cache_read(CACHE_NAMESPACE, key, None)
            if isinstance(in_cache, dict):
                try:
                    return Place(
                        lat=float(in_cache["lat"]),
                        lon=float(in_cache["lon"]),
                        name=str(in_cache.get("name", text.strip())),
                    )
                except (KeyError, TypeError, ValueError):
                    pass  # malformed cache entry: the request is made again

        place = self._query(text.strip())
        if place is not None and self._store is not None:
            self._store.cache_write(
                CACHE_NAMESPACE,
                key,
                {"lat": place.lat, "lon": place.lon, "name": place.name},
            )
        return place

    def _query(self, text: str) -> Place | None:
        self._limiter.wait_turn()
        parameters = {"q": text, "format": "jsonv2", "limit": "1"}
        headers = {"User-Agent": self._settings.user_agent}
        try:
            if self._client is not None:
                response = self._client.get(
                    NOMINATIM_URL, params=parameters, headers=headers, timeout=TIMEOUT_S
                )
            else:
                with httpx.Client(follow_redirects=True) as client:
                    response = client.get(
                        NOMINATIM_URL, params=parameters, headers=headers, timeout=TIMEOUT_S
                    )
            if response.status_code != 200:
                return None
            data = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            return None
        try:
            first = data[0]
            return Place(
                lat=float(first["lat"]),
                lon=float(first["lon"]),
                name=str(first.get("display_name") or text),
            )
        except (KeyError, TypeError, ValueError):
            return None
