"""A map that is created only when the user opens a view that shows it.

`LazyMap` is a plain container: it keeps the points, the home point, the radius and
the selection given to it, and builds the `mapview.MapView` (Qt WebEngine, or its
painter fallback) the first time `ensure()` is called, i.e. when its view is shown.
Nothing of Qt WebEngine is imported before that.
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from .. import config
from ..i18n import t

log = logging.getLogger(__name__)

DEFAULT_ZOOM = 11
PLACEHOLDER_PAINT_MS = 80     # lets the event loop paint "Loading the map…" before the slow step
WARM_UP_DELAY_MS = 20_000     # after start-up: Qt WebEngine is started at a quiet moment
WARM_UP_QUIET_S = 3.0         # ...when nothing was typed or clicked for this long
WARM_UP_TRIES = 6


def web_maps_enabled() -> bool:
    """False when no web map can be shown (forced plain map, or no screen: tests)."""
    import os

    if os.environ.get("REVERSEHIRE_MAP_FALLBACK") == "1":
        return False
    return os.environ.get("QT_QPA_PLATFORM", "").lower() not in ("offscreen", "minimal")


def web_engine_started() -> bool:
    """True once the map's WebEngine profile exists (the slow first start is over)."""
    import sys

    backend = sys.modules.get("reversehire.mapview.webbackend")
    return bool(getattr(backend, "_profiles", None))


def warm_up(settings) -> float | None:
    """Starts Qt WebEngine (the map profile) now, on the GUI thread; the seconds it took,
    or None when nothing was done (already started, maps disabled, WebEngine missing)."""
    import time

    if web_engine_started() or not web_maps_enabled():
        return None
    started = time.monotonic()
    try:
        from ..mapview.webbackend import get_profile

        get_profile(map_settings(settings))
    except Exception:  # noqa: BLE001 - WebEngine missing or blocked: the map will use its fallback
        log.info("Map warm-up: Qt WebEngine not available", exc_info=True)
        return None
    seconds = time.monotonic() - started
    log.info("Map warm-up: Qt WebEngine started in %.1f s", seconds)
    return seconds


def map_translator(key: str, default: str) -> str:
    """The map's translation hook: the locale text without filling the {placeholders}."""
    text = t(key)
    return default if text == key else text


def map_settings(settings):
    from ..mapview import MapSettings

    try:
        folder = config.data_folder()
    except Exception:  # noqa: BLE001 - no data folder: memory cache only
        folder = None
    return MapSettings.from_app_settings(settings, folder)


def map_theme(palette, dark: bool):
    from ..mapview import MapTheme

    theme = MapTheme.dark_theme() if dark else MapTheme.light_theme()
    theme.background = palette.background
    theme.foreground = palette.text
    theme.accent = palette.accent
    return theme


class LazyMap(QWidget):
    pointActivated = Signal(str)

    def __init__(self, settings_provider: Callable[[], object], palette_provider: Callable[[], tuple],
                 parent: QWidget | None = None, *, force_fallback: bool = False):
        super().__init__(parent)
        self._settings_provider = settings_provider
        self._palette_provider = palette_provider  # () -> (Palette, dark: bool)
        self._force_fallback = force_fallback
        self.view = None
        self._points: list = []
        self._center: tuple[float, float, float] | None = None
        self._radius: tuple[float, float, float] | None = None
        self._selected: str | None = None
        # The window's shared QActions {"online", "offline", "retry"} (menu entries of the
        # map's banner button and corner toggle) and the handler of the offline switch.
        self.actions: dict | None = None
        self.on_offline: Callable[[bool], None] | None = None
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._placeholder: QWidget | None = None
        self.setMinimumSize(200, 160)

    @property
    def created(self) -> bool:
        return self.view is not None

    @property
    def loading(self) -> bool:
        """The "Loading the map…" placeholder is shown, the map is being built."""
        return self._placeholder is not None

    def ensure_soon(self) -> None:
        """What a click calls: when Qt WebEngine has not started yet in this session (its
        first start blocks the GUI thread for seconds and must run there), a "Loading the
        map…" placeholder is painted first and the map is built in a deferred step; the
        data given meanwhile is applied when it is ready."""
        if self.view is not None or self._placeholder is not None:
            return
        if self._force_fallback or web_engine_started() or not web_maps_enabled():
            self.ensure()
            return
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QLabel, QProgressBar

        holder = QWidget(self)
        holder.setObjectName("mapLoading")
        layout = QVBoxLayout(holder)
        layout.addStretch(1)
        label = QLabel(t("mapview.loading"), holder)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setProperty("role", "title")
        bar = QProgressBar(holder)
        bar.setRange(0, 0)          # busy
        bar.setMaximumWidth(320)
        bar.setTextVisible(False)
        layout.addWidget(label)
        layout.addWidget(bar, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        self._layout.addWidget(holder)
        self._placeholder = holder
        log.info("Map: first start of the web map, placeholder shown")
        QTimer.singleShot(PLACEHOLDER_PAINT_MS, self._build_deferred)

    def _build_deferred(self) -> None:
        holder, self._placeholder = self._placeholder, None
        if holder is not None:
            holder.hide()
            self._layout.removeWidget(holder)
            holder.deleteLater()
        self.ensure()

    def ensure(self):
        """Builds the map now (first time the view is shown); returns the MapView."""
        if self.view is not None:
            return self.view
        from ..mapview import MapView

        palette, dark = self._palette_provider()
        try:
            view = MapView(self, settings=map_settings(self._settings_provider()), theme=map_theme(palette, dark),
                           tr=map_translator, force_fallback=self._force_fallback, actions=self.actions)
        except Exception:  # noqa: BLE001 - the widget has its own fallback; this is the last guard
            log.warning("Map not created", exc_info=True)
            return None
        view.pointActivated.connect(self.pointActivated.emit)
        view.offlineRequested.connect(lambda offline: self.on_offline(offline) if self.on_offline else None)
        self._layout.addWidget(view)
        self.view = view
        log.info("Map opened (%s)", view.backend)
        self._apply()
        return view

    def _apply(self) -> None:
        view = self.view
        if view is None:
            return
        view.set_points(self._points)
        if self._radius is not None:
            view.set_radius(*self._radius)
        else:
            view.clear_radius()
        if self._center is not None:
            view.set_center(*self._center)
        if self._points or self._radius is not None:
            view.fit_to_points(include_radius=True)
        if self._selected:
            view.select(self._selected)

    def set_data(self, points, *, center: tuple[float, float] | None = None, radius_km: float | None = None) -> None:
        """All the markers, the home point (center) and the search radius around it."""
        self._points = list(points or [])
        self._center = (center[0], center[1], DEFAULT_ZOOM) if center else None
        self._radius = (center[0], center[1], float(radius_km)) if center and radius_km else None
        self._apply()

    def select(self, point_id: str | None) -> None:
        self._selected = point_id
        if self.view is not None:
            self.view.select(point_id)

    def apply_theme(self, palette, dark: bool) -> None:
        if self.view is not None:
            self.view.set_theme(map_theme(palette, dark))

    def apply_settings(self, settings) -> None:
        if self.view is not None:
            self.view.set_settings(map_settings(settings))

    def retry(self) -> None:
        if self.view is not None:
            self.view.retry()

    def retranslate(self) -> None:
        if self.view is not None:
            self.view.retranslate()

    def backend(self) -> str:
        return self.view.backend if self.view is not None else ""


def clear_map_cache() -> bool:
    """Empties the tile cache of the maps open in this session (WebEngine owns those files:
    it is asked to clear them). False when no map was opened: the caller then deletes the
    cache folder itself (see MainWindow.clear_cache). GUI thread only."""
    import sys

    backend = sys.modules.get("reversehire.mapview.webbackend")
    profiles = list(getattr(backend, "_profiles", {}).values()) if backend is not None else []
    for profile, _policy, _interceptor in profiles:
        try:
            profile.clearHttpCache()
        except Exception:  # noqa: BLE001 - a cache that cannot be cleared is not an error worth a box
            log.warning("Map tile cache not cleared", exc_info=True)
    return bool(profiles)

