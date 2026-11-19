"""Places and company discovery: geocoding, sectors and Overpass search."""

from .distance import haversine_km
from .geocoding import Geocoder, Place, parse_coordinates
from .overpass import OverpassClient, OverpassError
from .sectors import SECTORS, Sector, preliminary_relevance, sectors_for_profile

__all__ = [
    "Geocoder",
    "OverpassClient",
    "OverpassError",
    "Place",
    "SECTORS",
    "Sector",
    "haversine_km",
    "parse_coordinates",
    "preliminary_relevance",
    "sectors_for_profile",
]
