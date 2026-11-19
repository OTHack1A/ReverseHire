"""Tile-less QPainter map, used when Qt WebEngine is unavailable or the page fails.

A plain scatter of the points in Web-Mercator coordinates (so ``set_center`` zoom
levels mean the same as in the web page), the search-radius circle, a scale bar and
a short notice. Drag to pan, wheel to zoom, click a marker to activate it.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from .model import (MapPoint, circle_bounds, circle_polygon, points_bounds, valid_coordinates,
                    valid_radius_km)
from .strings import STRING_DEFAULTS, format_km
from .theme import MapTheme

TILE = 256.0
MIN_ZOOM = 1.0
MAX_ZOOM = 19.0
HIT_RADIUS_PX = 11.0
_METERS_PER_PIXEL_Z0 = 156543.03392804097  # equator, 256-px tiles


def world_size(zoom: float) -> float:
    return TILE * (2.0 ** zoom)


def project(lat: float, lon: float, zoom: float) -> tuple[float, float]:
    """Web-Mercator pixel coordinates (origin top-left of the world) at a zoom level."""
    size = world_size(zoom)
    lat = max(-85.05112878, min(85.05112878, lat))
    x = (lon + 180.0) / 360.0 * size
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * size
    return x, y


def unproject(x: float, y: float, zoom: float) -> tuple[float, float]:
    size = world_size(zoom)
    lon = x / size * 360.0 - 180.0
    n = math.pi - 2.0 * math.pi * y / size
    lat = math.degrees(math.atan(math.sinh(n)))
    return lat, lon


def meters_per_pixel(lat: float, zoom: float) -> float:
    return _METERS_PER_PIXEL_Z0 * math.cos(math.radians(lat)) / (2.0 ** zoom)


def nice_scale_meters(max_meters: float) -> float:
    """Largest 1/2/5 x 10^n that does not exceed ``max_meters``."""
    if max_meters <= 0:
        return 1.0
    exponent = math.floor(math.log10(max_meters))
    for factor in (5, 2, 1):
        value = factor * 10 ** exponent
        if value <= max_meters:
            return float(value)
    return float(10 ** exponent)


class FallbackMap(QWidget):
    """The plain map. Same data API as the web page, painted with QPainter."""

    pointActivated = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(200, 150)
        self.setMouseTracking(True)
        self._points: list[MapPoint] = []
        self._theme = MapTheme.dark_theme()
        self._strings = dict(STRING_DEFAULTS)
        self._center = (42.0, 12.5)
        self._zoom = 5.0
        self._radius: tuple[float, float, float] | None = None
        self._selected: str | None = None
        self._notice_key = "mapview.fallback"
        self._drag_from: QPointF | None = None
        self._drag_moved = False
        self._hover: str | None = None

    # --- data ---------------------------------------------------------------------
    def set_theme(self, theme: MapTheme) -> None:
        self._theme = theme
        self.update()

    def set_strings(self, strings: dict) -> None:
        self._strings = {**STRING_DEFAULTS, **strings}
        self.update()

    def set_notice_key(self, key: str) -> None:
        self._notice_key = key
        self.update()

    def set_points(self, points: list[MapPoint]) -> None:
        self._points = list(points)
        if self._selected not in {p.id for p in self._points}:
            self._selected = None
        self.update()

    def set_view(self, lat: float, lon: float, zoom: float) -> None:
        if not valid_coordinates(lat, lon):
            return
        self._center = (float(lat), float(lon))
        self._zoom = max(MIN_ZOOM, min(MAX_ZOOM, float(zoom)))
        self.update()

    def set_radius(self, lat: float, lon: float, km) -> None:
        value = valid_radius_km(km)
        if value is None or not valid_coordinates(lat, lon):
            self._radius = None
        else:
            self._radius = (float(lat), float(lon), value)
        self.update()

    def select(self, point_id: str | None) -> None:
        match = next((p for p in self._points if p.id == point_id), None)
        self._selected = match.id if match else None
        if match is not None:
            self._center = (match.lat, match.lon)
            self._zoom = max(self._zoom, 12.0)
        self.update()

    def fit(self, include_radius: bool = True, margin_px: int = 48) -> None:
        bounds = list(points_bounds(self._points) or ())
        if include_radius and self._radius is not None:
            south, west, north, east = circle_bounds(*self._radius)
            if bounds:
                bounds = [min(bounds[0], south), min(bounds[1], west),
                          max(bounds[2], north), max(bounds[3], east)]
            else:
                bounds = [south, west, north, east]
        if not bounds:
            return
        south, west, north, east = bounds
        width = max(1.0, self.width() - 2 * margin_px)
        height = max(1.0, self.height() - 2 * margin_px)
        zoom = MAX_ZOOM - 2
        while zoom > MIN_ZOOM:
            x1, y1 = project(north, west, zoom)
            x2, y2 = project(south, east, zoom)
            if abs(x2 - x1) <= width and abs(y2 - y1) <= height:
                break
            zoom -= 0.25
        self._zoom = max(MIN_ZOOM, zoom)
        self._center = ((south + north) / 2.0, (west + east) / 2.0)
        self.update()

    # --- geometry ----------------------------------------------------------------
    def to_screen(self, lat: float, lon: float) -> QPointF:
        cx, cy = project(self._center[0], self._center[1], self._zoom)
        x, y = project(lat, lon, self._zoom)
        return QPointF(self.width() / 2.0 + (x - cx), self.height() / 2.0 + (y - cy))

    def point_at(self, pos: QPointF) -> str | None:
        best, best_d = None, HIT_RADIUS_PX ** 2
        for point in self._points:
            screen = self.to_screen(point.lat, point.lon)
            d = (screen.x() - pos.x()) ** 2 + (screen.y() - pos.y()) ** 2
            if d <= best_d:
                best, best_d = point.id, d
        return best

    def scale_bar(self, max_pixels: float = 140.0) -> tuple[float, str]:
        """(pixel length, label) of a round-number scale at the map centre."""
        mpp = meters_per_pixel(self._center[0], self._zoom)
        meters = nice_scale_meters(mpp * max_pixels)
        if meters >= 1000:
            label = self._strings["mapview.scale_km"].replace("{n}", format_km(meters / 1000.0))
        else:
            label = self._strings["mapview.scale_m"].replace("{n}", format_km(meters))
        return meters / mpp, label

    # --- events ------------------------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.position()
            self._drag_moved = False

    def mouseMoveEvent(self, event):
        if self._drag_from is not None:
            delta = event.position() - self._drag_from
            if abs(delta.x()) + abs(delta.y()) > 3:
                self._drag_moved = True
            if self._drag_moved:
                cx, cy = project(self._center[0], self._center[1], self._zoom)
                self._center = unproject(cx - delta.x(), cy - delta.y(), self._zoom)
                self._drag_from = event.position()
                self.update()
        else:
            hover = self.point_at(event.position())
            if hover != self._hover:
                self._hover = hover
                self.setCursor(Qt.CursorShape.PointingHandCursor if hover else Qt.CursorShape.ArrowCursor)
                self.setToolTip(self._tooltip_for(hover))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._drag_from is not None:
            if not self._drag_moved:
                hit = self.point_at(event.position())
                if hit is not None:
                    self._selected = hit
                    self.update()
                    self.pointActivated.emit(hit)
            self._drag_from = None

    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120.0
        if steps:
            self._zoom = max(MIN_ZOOM, min(MAX_ZOOM, self._zoom + 0.5 * steps))
            self.update()

    def _tooltip_for(self, point_id: str | None) -> str:
        point = next((p for p in self._points if p.id == point_id), None)
        if point is None:
            return ""
        return (point.label + ("\n" + point.tooltip if point.tooltip else "")).strip()

    # --- painting ----------------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            self._paint(painter)
        finally:
            painter.end()

    def _paint(self, painter: QPainter) -> None:
        theme = self._theme
        painter.fillRect(self.rect(), QColor(theme.background))
        self._paint_grid(painter)
        self._paint_radius(painter)
        for point in self._points:
            if point.id != self._selected:
                self._paint_point(painter, point)
        selected = next((p for p in self._points if p.id == self._selected), None)
        if selected is not None:
            self._paint_point(painter, selected, selected=True)
        self._paint_scale(painter)
        self._paint_notice(painter)

    def _paint_grid(self, painter: QPainter) -> None:
        pen = QPen(QColor(self._theme.grid))
        pen.setWidth(1)
        painter.setPen(pen)
        step = 64
        offset_x = int(self.width() / 2) % step
        offset_y = int(self.height() / 2) % step
        for x in range(offset_x, self.width(), step):
            painter.drawLine(x, 0, x, self.height())
        for y in range(offset_y, self.height(), step):
            painter.drawLine(0, y, self.width(), y)

    def _paint_radius(self, painter: QPainter) -> None:
        if self._radius is None:
            return
        lat, lon, km = self._radius
        ring = QPolygonF([self.to_screen(la, lo) for la, lo in circle_polygon(lat, lon, km)])
        accent = QColor(self._theme.accent)
        fill = QColor(accent)
        fill.setAlpha(36)
        pen = QPen(accent)
        pen.setWidthF(2.0)
        painter.setPen(pen)
        painter.setBrush(QBrush(fill))
        painter.drawPolygon(ring)

    def _paint_point(self, painter: QPainter, point: MapPoint, selected: bool = False) -> None:
        pos = self.to_screen(point.lat, point.lon)
        if pos.x() < -20 or pos.y() < -20 or pos.x() > self.width() + 20 or pos.y() > self.height() + 20:
            return
        color = QColor(self._theme.point_color(point.kind, point.status))
        outline = QColor(self._theme.outline)
        kind = point.kind
        if kind == "home":
            radius, pen_color, width, fill = 9.0, QColor(self._theme.home_ring), 3.0, color
        elif kind == "posting":
            radius, pen_color, width, fill = 5.5, outline, 1.5, color
        elif kind == "hypothesis":
            fill = QColor(color)
            fill.setAlpha(70)
            radius, pen_color, width = 7.0, color, 2.0
        else:
            radius, pen_color, width, fill = 7.5, outline, 2.0, color
        pen = QPen(pen_color)
        pen.setWidthF(width)
        if kind == "hypothesis":
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(QBrush(fill))
        painter.drawEllipse(pos, radius, radius)
        if selected:
            ring = QPen(QColor(self._theme.accent))
            ring.setWidthF(3.0)
            painter.setPen(ring)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(pos, radius + 6.0, radius + 6.0)
            if point.label:
                painter.setPen(QColor(self._theme.foreground))
                painter.drawText(QPointF(pos.x() + radius + 9.0, pos.y() + 4.0), point.label)

    def _paint_scale(self, painter: QPainter) -> None:
        length, label = self.scale_bar()
        x0, y0 = 14.0, self.height() - 16.0
        pen = QPen(QColor(self._theme.foreground))
        pen.setWidthF(2.0)
        painter.setPen(pen)
        painter.drawLine(QPointF(x0, y0), QPointF(x0 + length, y0))
        painter.drawLine(QPointF(x0, y0 - 4), QPointF(x0, y0 + 4))
        painter.drawLine(QPointF(x0 + length, y0 - 4), QPointF(x0 + length, y0 + 4))
        painter.drawText(QPointF(x0, y0 - 8.0), label)

    def _paint_notice(self, painter: QPainter) -> None:
        text = self._strings.get(self._notice_key) or self._strings["mapview.fallback"]
        font = QFont(painter.font())
        font.setItalic(True)
        painter.setFont(font)
        painter.setPen(QColor(self._theme.foreground))
        rect = QRectF(10, 8, self.width() - 20, 22)
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
