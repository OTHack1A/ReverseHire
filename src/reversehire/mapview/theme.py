"""Colours of the map, derived from the application palette."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def safe_color(value, default: str) -> str:
    """"#rgb"/"#rrggbb" (a QColor is accepted through its ``name()``); anything else: default."""
    if hasattr(value, "name") and callable(value.name):
        try:
            value = value.name()
        except Exception:  # noqa: BLE001 - a foreign object: use the default
            return default
    text = str(value or "").strip()
    return text.lower() if _HEX.match(text) else default


_DARK = {
    "background": "#1e2229", "foreground": "#e6e9ee", "accent": "#4da3ff",
    "outline": "#10141a", "home_ring": "#ffffff", "grid": "#2b313b",
}
_LIGHT = {
    "background": "#eef1f5", "foreground": "#1d232b", "accent": "#1769d1",
    "outline": "#ffffff", "home_ring": "#1d232b", "grid": "#d5dbe3",
}
DEFAULT_KIND_COLORS = {
    "company": "#3b82f6", "posting": "#f59e0b", "home": "#10b981", "hypothesis": "#a78bfa",
}
DEFAULT_STATUS_COLORS = {
    "new": "#22c55e", "saved": "#38bdf8", "applied": "#a855f7", "interview": "#f97316",
    "offer": "#eab308", "rejected": "#6b7280", "hot": "#ef4444",
}


@dataclass
class MapTheme:
    """Colours the page uses. ``dark`` also switches the tile filter (dark tiles)."""

    dark: bool = True
    background: str = _DARK["background"]
    foreground: str = _DARK["foreground"]
    accent: str = _DARK["accent"]
    outline: str = _DARK["outline"]
    home_ring: str = _DARK["home_ring"]
    grid: str = _DARK["grid"]
    kind_colors: dict = field(default_factory=lambda: dict(DEFAULT_KIND_COLORS))
    status_colors: dict = field(default_factory=lambda: dict(DEFAULT_STATUS_COLORS))

    @classmethod
    def dark_theme(cls) -> "MapTheme":
        return cls(dark=True)

    @classmethod
    def light_theme(cls) -> "MapTheme":
        return cls(dark=False, **_LIGHT)

    @classmethod
    def from_palette(cls, palette, *, dark: bool | None = None,
                     status_colors: dict | None = None, kind_colors: dict | None = None) -> "MapTheme":
        """Theme following a QPalette: window/text/highlight colours; dark when the window is dark."""
        window = palette.window().color()
        if dark is None:
            dark = window.lightness() < 128
        base = _DARK if dark else _LIGHT
        theme = cls(
            dark=bool(dark),
            background=safe_color(window, base["background"]),
            foreground=safe_color(palette.windowText().color(), base["foreground"]),
            accent=safe_color(palette.highlight().color(), base["accent"]),
            outline=base["outline"], home_ring=base["home_ring"], grid=base["grid"],
        )
        if status_colors:
            theme.status_colors.update(status_colors)
        if kind_colors:
            theme.kind_colors.update(kind_colors)
        return theme

    def to_payload(self) -> dict:
        base = _DARK if self.dark else _LIGHT
        fallback = DEFAULT_KIND_COLORS["company"]
        return {
            "dark": bool(self.dark),
            "background": safe_color(self.background, base["background"]),
            "foreground": safe_color(self.foreground, base["foreground"]),
            "accent": safe_color(self.accent, base["accent"]),
            "outline": safe_color(self.outline, base["outline"]),
            "home_ring": safe_color(self.home_ring, base["home_ring"]),
            "grid": safe_color(self.grid, base["grid"]),
            "kind_colors": {str(k)[:40]: safe_color(v, fallback)
                            for k, v in (self.kind_colors or {}).items()},
            "status_colors": {str(k)[:40]: safe_color(v, fallback)
                              for k, v in (self.status_colors or {}).items()},
        }

    def point_color(self, kind: str, status: str) -> str:
        by_status = (self.status_colors or {}).get(status) if status else None
        color = by_status or (self.kind_colors or {}).get(kind) or self.accent
        return safe_color(color, safe_color(self.accent, DEFAULT_KIND_COLORS["company"]))
