"""Integrated map: companies, job postings, the home point and the search radius.

    from reversehire.mapview import MapView, MapPoint, MapSettings, MapTheme

Leaflet runs in a QWebEngineView loaded from the bundled files (no network except the
tile requests). If Qt WebEngine cannot be imported or the page fails, the widget shows
a plain QPainter map instead and logs why (logger "reversehire.mapview").

Importing this package never imports Qt WebEngine.
"""

from .model import KINDS, MapPoint
from .settings import (OSM_ATTRIBUTION_TEXT, OSM_ATTRIBUTION_URL, OSM_TILE_URL, MapSettings,
                       validate_tile_template)
from .strings import STRING_DEFAULTS
from .theme import MapTheme
from .widget import BACKEND_FALLBACK, BACKEND_WEB, MapView

__all__ = [
    "BACKEND_FALLBACK", "BACKEND_WEB", "KINDS", "MapPoint", "MapSettings", "MapTheme", "MapView",
    "OSM_ATTRIBUTION_TEXT", "OSM_ATTRIBUTION_URL", "OSM_TILE_URL", "STRING_DEFAULTS",
    "validate_tile_template",
]
