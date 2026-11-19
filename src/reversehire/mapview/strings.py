"""The few user-visible strings of the map and the translation hook.

The widget takes ``tr(key, default) -> str`` (for example a wrapper around the app's
i18n). Without it the English defaults below are used. ``{km}`` is replaced after
translation.
"""

from __future__ import annotations

from collections.abc import Callable

STRING_DEFAULTS: dict[str, str] = {
    "mapview.zoom_in": "Zoom in",
    "mapview.zoom_out": "Zoom out",
    "mapview.radius": "Search radius: {km} km",
    "mapview.offline": "Map backgrounds switched off: only the points are drawn",
    "mapview.fallback": "Simple map (no tiles)",
    "mapview.offline_message": "The map background is switched off (setting \"Show the map background\"): only the points are drawn.",
    "mapview.show_online": "Show the online map",
    "mapview.work_offline": "Work offline (no map backgrounds)",
    "mapview.tiles_failed": "The map backgrounds could not be loaded: {reason}",
    "mapview.web_failed": "The full map could not be started: {reason}",
    "mapview.retry": "Retry",
    "mapview.reason_network": "no connection to the map server ({detail})",
    "mapview.reason_status": "the map server answered with error {status}",
    "mapview.reason_unknown": "the map server did not send any background",
    "mapview.scale_km": "{n} km",
    "mapview.scale_m": "{n} m",
}

Translator = Callable[[str, str], str]


def default_tr(key: str, default: str) -> str:
    return default


def make_strings(tr: Translator | None) -> dict[str, str]:
    """All strings translated (a failing or empty translation falls back to the default)."""
    translate = tr or default_tr
    result: dict[str, str] = {}
    for key, default in STRING_DEFAULTS.items():
        try:
            text = translate(key, default)
        except Exception:  # noqa: BLE001 - a broken hook must not break the map
            text = default
        result[key] = text if isinstance(text, str) and text else default
    return result


def format_km(km: float) -> str:
    """12 -> "12", 7.5 -> "7.5", 0.25 -> "0.25"."""
    return f"{km:.2f}".rstrip("0").rstrip(".")
