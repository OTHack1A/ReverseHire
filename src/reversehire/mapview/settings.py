"""Map settings: tile source, offline mode, identification and cache location.

OpenStreetMap tile usage policy (https://operations.osmfoundation.org/policies/tiles/),
as implemented here:
  * a stable, application-specific User-Agent (profile.setHttpUserAgent). No Referer: a
    page loaded from a local file has none and Chromium cancels a Referer header that the
    document did not produce (the policy only asks browsers for it; apps are exempt);
  * the attribution "(c) OpenStreetMap contributors" is always visible on the map;
  * normal HTTP caching through the WebEngine disk cache (never "no-cache" headers);
  * only tiles of the current view are requested: no prefetching, no offline download.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("reversehire.mapview")

OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_ATTRIBUTION_TEXT = "© OpenStreetMap contributors"
OSM_ATTRIBUTION_URL = "https://www.openstreetmap.org/copyright"
MAX_ZOOM = 19
CACHE_MAX_MB = 200
_LOOPBACK = ("127.0.0.1", "localhost", "::1")
_PLACEHOLDERS = ("{z}", "{x}", "{y}")


def default_user_agent() -> str:
    """The application's HTTP User-Agent from the configuration module."""
    try:
        from ..config import DEFAULT_USER_AGENT
        return DEFAULT_USER_AGENT
    except Exception:  # noqa: BLE001 - stand-alone use of the package
        return "ReverseHire (desktop app)"


def _split(template: str):
    return urlsplit(template.replace("{", "%7B").replace("}", "%7D"))


def validate_tile_template(template) -> tuple[str | None, str]:
    """(normalised template, "") when usable, else (None, reason).

    Usable: contains {z} {x} {y}; https, or http to a loopback host (a local tile
    server), or file:// (a local tile folder). Plain http to other hosts is refused.
    """
    text = str(template or "").strip()
    if not text:
        return None, "empty tile URL"
    if any(p not in text for p in _PLACEHOLDERS):
        return None, "tile URL needs {z}, {x} and {y}"
    try:
        parts = _split(text)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
    except ValueError:
        return None, "malformed tile URL"
    if scheme == "https" and host:
        return text, ""
    if scheme == "http" and host in _LOOPBACK:
        return text, ""
    if scheme == "file":
        return text, ""
    return None, f"tile URL scheme '{scheme or '?'}' is not allowed (https, loopback http or file)"


def tile_origin(template: str) -> tuple[str, str] | None:
    """("https", "host:port") for network templates, ("file", "<prefix>") for local ones."""
    text, _ = validate_tile_template(template)
    if not text:
        return None
    parts = _split(text)
    scheme = parts.scheme.lower()
    if scheme == "file":
        return "file", text.split("{", 1)[0]
    host = (parts.hostname or "").lower()
    default_port = 443 if scheme == "https" else 80
    return scheme, f"{host}:{parts.port or default_port}"


@dataclass(frozen=True)
class MapSettings:
    """Everything the integrator can configure.

    tile_url      "" = standard OpenStreetMap; otherwise a template with {z}/{x}/{y}
    offline       True: no tiles at all, markers on a blank themed background
    cache_dir     where the WebEngine HTTP cache lives (the app data folder); None keeps
                  the cache in memory only
    """

    tile_url: str = ""
    offline: bool = False
    attribution_text: str = OSM_ATTRIBUTION_TEXT
    attribution_url: str = OSM_ATTRIBUTION_URL
    user_agent: str = ""
    cache_dir: Path | None = None
    cache_max_mb: int = CACHE_MAX_MB
    max_zoom: int = MAX_ZOOM

    @classmethod
    def from_app_settings(cls, settings, data_folder, **overrides) -> "MapSettings":
        """Build from the app's Settings object (missing attributes use the defaults).

        Reads ``map_tile_url`` (str), ``map_offline`` (bool) and ``user_agent`` (str);
        the cache goes to ``<data_folder>/cache/map``.
        """
        return cls(
            tile_url=str(getattr(settings, "map_tile_url", "") or ""),
            offline=bool(getattr(settings, "map_offline", False)),
            user_agent=str(getattr(settings, "user_agent", "") or ""),
            cache_dir=Path(data_folder) / "cache" / "map" if data_folder else None,
            **overrides,
        )

    def resolved(self) -> "MapSettings":
        """A copy with every empty or invalid field replaced by a safe default (logged)."""
        tile_url = self.tile_url.strip()
        custom = bool(tile_url)
        if custom:
            checked, reason = validate_tile_template(tile_url)
            if checked is None:
                log.warning("map: custom tile URL ignored (%s); using OpenStreetMap", reason)
                tile_url, custom = "", False
        if not custom:
            tile_url = OSM_TILE_URL
        attribution_text = self.attribution_text.strip()
        attribution_url = self.attribution_url.strip()
        if not custom:
            # The OSM attribution is not optional on the OSM tile servers.
            attribution_text, attribution_url = OSM_ATTRIBUTION_TEXT, OSM_ATTRIBUTION_URL
        if attribution_url and not attribution_url.lower().startswith("https://"):
            attribution_url = ""
        return replace(
            self,
            tile_url=tile_url,
            attribution_text=attribution_text,
            attribution_url=attribution_url,
            user_agent=self.user_agent.strip() or default_user_agent(),
            cache_max_mb=max(20, min(int(self.cache_max_mb or CACHE_MAX_MB), 2000)),
            max_zoom=max(3, min(int(self.max_zoom or MAX_ZOOM), 22)),
        )

    def page_payload(self) -> dict:
        """What the page needs (the resolved settings; no paths, no User-Agent)."""
        r = self.resolved()
        return {
            "tile_url": "" if r.offline else r.tile_url,
            "offline": bool(r.offline),
            "attribution_text": r.attribution_text,
            "attribution_url": r.attribution_url,
            "max_zoom": r.max_zoom,
        }
