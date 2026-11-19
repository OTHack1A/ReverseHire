"""MapView: the reusable map widget (Leaflet in Qt WebEngine, plain painter fallback)."""

from __future__ import annotations

import json
import logging
import os
import threading
from urllib.parse import urlsplit

from PySide6.QtCore import QEvent, QObject, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QStackedLayout, QToolButton, QWidget

from .bridge import MapBridge
from .fallback import FallbackMap
from .model import (MapPoint, normalise_points, points_payload, valid_coordinates,
                    valid_radius_km)
from .settings import MapSettings
from .strings import Translator, format_km, make_strings
from .theme import MapTheme

log = logging.getLogger("reversehire.mapview")

READY_TIMEOUT_MS = 25_000
PROBE_TIMEOUT_S = 8.0
FORCE_FALLBACK_ENV = "REVERSEHIRE_MAP_FALLBACK"

BACKEND_WEB = "web"
BACKEND_FALLBACK = "fallback"


def probe_tiles(tile_url: str, user_agent: str) -> str:
    """Why tiles fail: fetch the world tile once (z0) and describe the problem.

    Returns "network:<detail>", "status:<code>" or "" (the server answers normally).
    Runs on a worker thread (network)."""
    import httpx

    url = tile_url.replace("{z}", "0").replace("{x}", "0").replace("{y}", "0").replace("{s}", "a")
    if not url.lower().startswith(("http://", "https://")):
        return ""
    try:
        response = httpx.get(url, headers={"User-Agent": user_agent}, timeout=PROBE_TIMEOUT_S,
                             follow_redirects=True)
    except Exception as exc:  # noqa: BLE001 - every network problem is a reason to show
        return f"network:{type(exc).__name__}"
    if response.status_code >= 400:
        return f"status:{response.status_code}"
    return ""


class MapBanner(QFrame):
    """The message on top of the map (offline mode, backgrounds failed) with one button."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("mapBanner")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("#mapBanner { background: rgba(20, 24, 30, 0.90); border-radius: 6px; }"
                           "#mapBanner QLabel { color: #f0f3f7; background: transparent; }")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 6, 6)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.button = QPushButton(self)
        self.button.setObjectName("mapBannerButton")
        self.button.setProperty("role", "accent")
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.button)
        self._action: QAction | None = None
        self.button.clicked.connect(self._clicked)
        self.hide()

    def _clicked(self) -> None:
        if self._action is not None and self._action.isEnabled():
            self._action.trigger()

    def show_message(self, text: str, action: QAction | None) -> None:
        self.label.setText(text)
        self._action = action
        self.button.setVisible(action is not None)
        if action is not None:
            self.button.setText(action.text())
            try:  # the button and its menu entry share the action (gui/menus.py rule)
                from ..gui.menus import MENU_ACTION
                self.button.setProperty(MENU_ACTION, action)
            except Exception:  # noqa: BLE001 - stand-alone use of the package
                pass
        self.show()
        self.raise_()


class _Resizer(QObject):
    def __init__(self, callback, parent):
        super().__init__(parent)
        self._callback = callback

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._callback()
        return False


class MapView(QWidget):
    """Companies, postings, the home point and the search radius on a map.

    Signals
      pointActivated(str)          a marker was clicked (the MapPoint id)
      externalUrlRequested(str)    a link on the page was clicked (opened in the system
                                   browser unless ``open_external_links`` is False)
      backendChanged(str)          "web" or "fallback" (the page failed: plain map shown)
    """

    pointActivated = Signal(str)
    externalUrlRequested = Signal(str)
    backendChanged = Signal(str)
    offlineRequested = Signal(bool)   # the user switched map backgrounds off (True) / on (False)
    tilesFailed = Signal(str)         # reason code why no background could be loaded
    _probed = Signal(str)

    def __init__(self, parent=None, *, settings: MapSettings | None = None,
                 theme: MapTheme | None = None, tr: Translator | None = None,
                 force_fallback: bool = False, open_external_links: bool = True,
                 actions: dict | None = None):
        super().__init__(parent)
        self.setMinimumSize(240, 180)
        self._settings = settings or MapSettings()
        self._theme = theme or MapTheme.dark_theme()
        self._tr = tr
        self._strings = make_strings(tr)
        self._points: list[MapPoint] = []
        self._view: tuple[float, float, int] | None = None
        self._radius: tuple[float, float, float] | None = None
        self._selected: str | None = None
        # The last layout request ("view" or "fit"), replayed when the page becomes ready:
        # a map built while hidden must still open on all its points, not on the center.
        self._intent: dict | None = None
        self._open_external = open_external_links
        self._page_ready = False
        self._backend = BACKEND_FALLBACK
        self._web = None
        self._bridge: MapBridge | None = None
        self._fallback_reason = ""
        self._forced_fallback = bool(force_fallback or os.environ.get(FORCE_FALLBACK_ENV) == "1")
        self._tile_failure = ""            # "" or the reason code of the last failure
        self._logged_causes: set[str] = set()
        self._probing = False
        self._banner_state = ""
        self.tile_probe = probe_tiles      # replaced in the tests
        self._probed.connect(self._on_probed)

        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._fallback = FallbackMap(self)
        self._fallback.pointActivated.connect(self._on_point_activated)
        self._stack.addWidget(self._fallback)
        self._fallback.set_theme(self._theme)
        self._fallback.set_strings(self._strings)

        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(lambda: self._use_fallback("the page did not become ready in time"))

        self._build_controls(actions)
        if self._forced_fallback:
            self._use_fallback("fallback requested", log_level=logging.INFO)
        else:
            self._start_web()
        self._update_banner()

    # --- offline / failure controls -------------------------------------------------
    def _build_controls(self, actions: dict | None) -> None:
        """The banner and the corner toggle. `actions` = {"online", "offline", "retry"}:
        QActions shared with the window's menu; without them the map makes its own,
        which only emit offlineRequested / retry this map."""
        actions = dict(actions or {})
        self._own_actions = not actions
        if "online" not in actions:
            actions["online"] = QAction(self, triggered=lambda: self.offlineRequested.emit(False))
        if "offline" not in actions:
            toggle = QAction(self)
            toggle.setCheckable(True)
            toggle.toggled.connect(lambda on: self.offlineRequested.emit(bool(on)))
            actions["offline"] = toggle
        if "retry" not in actions:
            actions["retry"] = QAction(self, triggered=lambda: self.retry())
        self.online_action = actions["online"]
        self.offline_action = actions["offline"]
        self.retry_action = actions["retry"]
        self.banner = MapBanner(self)
        self.offline_button = QToolButton(self)
        self.offline_button.setObjectName("mapOfflineToggle")
        self.offline_button.setDefaultAction(self.offline_action)
        self.offline_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.offline_button.setStyleSheet(
            "#mapOfflineToggle { background: rgba(20, 24, 30, 0.80); color: #f0f3f7; border: none;"
            " border-radius: 4px; padding: 2px 6px; }"
            " #mapOfflineToggle:checked { background: rgba(170, 95, 20, 0.92); }")
        try:
            from ..gui.menus import MENU_ACTION
            self.offline_button.setProperty(MENU_ACTION, self.offline_action)
        except Exception:  # noqa: BLE001
            pass
        self.offline_button.show()
        self.installEventFilter(_Resizer(self._place_controls, self))
        self._retranslate_controls()

    def _retranslate_controls(self) -> None:
        strings = self._strings
        if self._own_actions:
            self.online_action.setText(strings["mapview.show_online"])
            self.retry_action.setText(strings["mapview.retry"])
            self.offline_action.setText(strings["mapview.work_offline"])
        self.offline_button.setToolTip(strings["mapview.work_offline"])
        self._sync_offline_action()
        self._update_banner()

    def _sync_offline_action(self) -> None:
        action = self.offline_action
        if action.isChecked() != bool(self._settings.offline):
            blocked = action.blockSignals(True)
            action.setChecked(bool(self._settings.offline))
            action.blockSignals(blocked)

    def _place_controls(self) -> None:
        try:
            margin = 8
            width = max(160, min(self.width() - 2 * margin, 560))
            self.banner.setFixedWidth(width)
            self.banner.setFixedHeight(self.banner.layout().heightForWidth(width)
                                       if self.banner.layout().hasHeightForWidth()
                                       else self.banner.sizeHint().height())
            # a centred panel on the map (a strip at the top was easy to miss), never above
            # the corner toggle
            top = max(margin + self.offline_button.sizeHint().height() + 4,
                      (self.height() - self.banner.height()) // 2)
            self.banner.move((self.width() - width) // 2, top)
            self.offline_button.adjustSize()
            self.offline_button.move(self.width() - self.offline_button.width() - margin, margin)
            self.banner.raise_()
            self.offline_button.raise_()
        except RuntimeError:
            pass

    def banner_state(self) -> str:
        """"offline", "tiles_failed", "web_failed" or "" (no message shown)."""
        return self._banner_state

    def banner_text(self) -> str:
        return self.banner.label.text() if self._banner_state else ""

    def _update_banner(self) -> None:
        if not hasattr(self, "banner"):
            return
        strings = self._strings
        if self._settings.offline:
            state, text, action = "offline", strings["mapview.offline_message"], self.online_action
        elif self._backend == BACKEND_FALLBACK and not self._forced_fallback:
            state = "web_failed"
            text = strings["mapview.web_failed"].replace("{reason}", self._fallback_reason or "?")
            action = self.retry_action
        elif self._tile_failure:
            state = "tiles_failed"
            text = strings["mapview.tiles_failed"].replace("{reason}", self.failure_text(self._tile_failure))
            action = self.retry_action
        else:
            state, text, action = "", "", None
        self._banner_state = state
        if state:
            self.banner.show_message(text, action)
        else:
            self.banner.hide()
        self._place_controls()

    def failure_text(self, code: str) -> str:
        strings = self._strings
        kind, _, detail = (code or "").partition(":")
        if kind == "network":
            return strings["mapview.reason_network"].replace("{detail}", detail or "?")
        if kind == "status":
            return strings["mapview.reason_status"].replace("{status}", detail or "?")
        return strings["mapview.reason_unknown"]

    def _report_failure(self, code: str) -> None:
        self._tile_failure = code or "unknown"
        if self._tile_failure not in self._logged_causes:  # once per cause, not per tile
            self._logged_causes.add(self._tile_failure)
            log.warning("map: no background tile could be loaded (%s)", self._tile_failure)
        self._update_banner()
        self.tilesFailed.emit(self._tile_failure)

    def _start_probe(self) -> None:
        if self._probing:
            return
        self._probing = True
        resolved = self._settings.resolved()
        probe, signal = self.tile_probe, self._probed

        def work() -> None:
            try:
                reason = probe(resolved.tile_url, resolved.user_agent)
            except Exception as exc:  # noqa: BLE001
                reason = f"network:{type(exc).__name__}"
            try:
                signal.emit(reason or "unknown")
            except RuntimeError:  # the view was deleted meanwhile
                pass

        threading.Thread(target=work, daemon=True, name="map tile probe").start()

    def _on_probed(self, reason: str) -> None:
        self._probing = False
        if self._settings.offline:
            return
        self._report_failure(reason)

    def retry(self) -> None:
        """Retry: a new web page when the page failed, else the backgrounds again."""
        log.info("map: retry requested (%s)", self._banner_state or "nothing failed")
        self._tile_failure = ""
        if self._backend == BACKEND_FALLBACK and not self._forced_fallback:
            self._start_web()
        else:
            self._send({"cmd": "settings", "settings": self._settings.page_payload()})
        self._update_banner()

    # --- public API -----------------------------------------------------------
    @property
    def backend(self) -> str:
        """"web" while the Leaflet page is in use, "fallback" for the plain map."""
        return self._backend

    @property
    def fallback_reason(self) -> str:
        return self._fallback_reason

    @property
    def page_ready(self) -> bool:
        return self._backend == BACKEND_WEB and self._page_ready

    @property
    def points(self) -> list[MapPoint]:
        return list(self._points)

    @property
    def selected_id(self) -> str | None:
        return self._selected

    @property
    def settings(self) -> MapSettings:
        return self._settings

    def set_center(self, lat: float, lon: float, zoom: float = 11) -> None:
        if not valid_coordinates(lat, lon):
            log.warning("map: set_center ignored (invalid coordinates)")
            return
        zoom_i = max(2, min(19, int(round(float(zoom)))))
        self._view = (float(lat), float(lon), zoom_i)
        self._fallback.set_view(lat, lon, zoom_i)
        self._intent = {"cmd": "view", "lat": float(lat), "lon": float(lon), "zoom": zoom_i}
        self._send(self._intent)

    def set_radius(self, lat: float, lon: float, km: float) -> None:
        """Draw the search circle (``km`` <= 0 or invalid removes it)."""
        value = valid_radius_km(km)
        if value is None or not valid_coordinates(lat, lon):
            self.clear_radius()
            return
        self._radius = (float(lat), float(lon), value)
        self._fallback.set_radius(lat, lon, value)
        self._send({"cmd": "radius", "lat": float(lat), "lon": float(lon), "km": value})

    def clear_radius(self) -> None:
        self._radius = None
        self._fallback.set_radius(0.0, 0.0, 0.0)
        self._send({"cmd": "radius", "lat": 0.0, "lon": 0.0, "km": 0.0})

    def set_points(self, points) -> None:
        """Replace all markers. Invalid points and duplicate ids are dropped (logged)."""
        self._points = normalise_points(points)
        if self._selected not in {p.id for p in self._points}:
            self._selected = None
        self._fallback.set_points(self._points)
        self._send({"cmd": "points", "points": points_payload(self._points)})
        if self._selected:
            self._send({"cmd": "select", "id": self._selected})

    def select(self, point_id: str | None) -> None:
        """Highlight a marker, bring it into view and open its tooltip (None clears)."""
        if point_id is not None and point_id not in {p.id for p in self._points}:
            log.debug("map: select ignored (unknown id)")
            point_id = None
        self._selected = point_id
        self._fallback.select(point_id)
        self._send({"cmd": "select", "id": point_id})

    def fit_to_points(self, include_radius: bool = True) -> None:
        """Zoom and pan so all points (and the radius circle) are visible."""
        self._fallback.fit(include_radius)
        self._intent = {"cmd": "fit", "include_radius": bool(include_radius)}
        self._send(self._intent)

    def set_theme(self, theme: MapTheme) -> None:
        self._theme = theme
        self._fallback.set_theme(theme)
        self._send({"cmd": "theme", "theme": theme.to_payload()})
        if self._web is not None:
            self._web.set_background(theme.background)

    def set_settings(self, settings: MapSettings) -> None:
        """Change tile source / offline mode. A changed cache folder needs a restart."""
        was_offline = self._settings.offline
        self._settings = settings
        self._tile_failure = ""
        if was_offline != settings.offline:
            log.info("map: backgrounds %s", "switched off (offline)" if settings.offline else "switched on")
            self._fallback.set_notice_key("mapview.offline" if settings.offline else "mapview.fallback")
        self._sync_offline_action()
        self._update_banner()
        if self._backend == BACKEND_WEB:
            try:
                from .webbackend import get_profile
                get_profile(settings)
            except Exception as exc:  # noqa: BLE001
                self._use_fallback(f"profile update failed: {exc}")
                return
        self._send({"cmd": "settings", "settings": settings.page_payload()})

    def set_translator(self, tr: Translator | None) -> None:
        """Install the ``tr(key, default)`` hook (also call it after a language change)."""
        self._tr = tr
        self._strings = make_strings(tr)
        self._fallback.set_strings(self._strings)
        self._send({"cmd": "strings", "strings": self._strings})
        if hasattr(self, "banner"):
            self._retranslate_controls()

    def retranslate(self) -> None:
        self.set_translator(self._tr)

    # --- internals --------------------------------------------------------------
    def _send(self, message: dict) -> None:
        """Queue-free send: the page gets the full state again when it announces ready."""
        if self._backend != BACKEND_WEB or not self._page_ready or self._bridge is None:
            return
        try:
            self._bridge.command.emit(json.dumps(message, ensure_ascii=True, allow_nan=False))
        except (TypeError, ValueError) as exc:
            log.warning("map: message not sent (%s)", exc)

    def _start_web(self) -> None:
        try:
            from .webbackend import WebMap
            web = WebMap(self._settings, self)
        except Exception as exc:  # noqa: BLE001 - missing module, DLL blocked, no GPU...
            self._use_fallback(f"Qt WebEngine unavailable: {type(exc).__name__}: {exc}")
            return
        self._web = web
        web.set_background(self._theme.background)
        self._bridge = web.bridge
        self._bridge.pageReady.connect(self._on_page_ready)
        self._bridge.markerClicked.connect(self._on_point_activated)
        self._bridge.tileStats.connect(self._on_tile_stats)
        self._bridge.pageMessage.connect(self._on_page_message)
        web.guarded_page.externalUrlRequested.connect(self._on_external_url)
        web.loadFinished.connect(self._on_load_finished)
        web.page().renderProcessTerminated.connect(self._on_render_terminated)
        self._stack.addWidget(web)
        self._backend = BACKEND_WEB
        # Shown from the start (blank themed page) so it gets its real size before the
        # page is ready; the plain map takes over only if the page fails.
        self._stack.setCurrentWidget(web)
        self._timeout.start(READY_TIMEOUT_MS)
        web.load_index()

    def _use_fallback(self, reason: str, log_level: int = logging.WARNING) -> None:
        self._timeout.stop()
        self._fallback_reason = reason
        was_web = self._backend == BACKEND_WEB
        log.log(log_level, "map: using the plain fallback map (%s)", reason)
        self._page_ready = False
        self._backend = BACKEND_FALLBACK
        if self._web is not None:
            web, self._web = self._web, None
            self._bridge = None
            self._stack.removeWidget(web)
            web.setParent(None)
            web.deleteLater()
        self._stack.setCurrentWidget(self._fallback)
        self._fallback.set_notice_key("mapview.offline" if self._settings.offline else "mapview.fallback")
        if self._view:
            self._fallback.set_view(*self._view)
        if was_web:
            self.backendChanged.emit(BACKEND_FALLBACK)
        self._update_banner()

    def _on_page_ready(self) -> None:
        self._timeout.stop()
        self._page_ready = True
        # Replay the whole state: anything set before the page was ready is applied now.
        self._send({"cmd": "strings", "strings": self._strings})
        self._send({"cmd": "theme", "theme": self._theme.to_payload()})
        self._send({"cmd": "settings", "settings": self._settings.page_payload()})
        if self._view:
            self._send({"cmd": "view", "lat": self._view[0], "lon": self._view[1], "zoom": self._view[2]})
        if self._radius:
            self._send({"cmd": "radius", "lat": self._radius[0], "lon": self._radius[1],
                        "km": self._radius[2]})
        if self._points:
            self._send({"cmd": "points", "points": points_payload(self._points)})
        if self._intent is not None and self._intent.get("cmd") == "fit":
            self._send(self._intent)  # after the points: they are what the fit frames
        if self._selected:
            self._send({"cmd": "select", "id": self._selected})
        self.backendChanged.emit(BACKEND_WEB)
        self._update_banner()
        log.info("map: web page ready (%d points)", len(self._points))

    def _on_load_finished(self, ok: bool) -> None:
        if not ok:
            self._use_fallback("the map page failed to load")

    def _on_render_terminated(self, status, exit_code) -> None:
        self._use_fallback(f"web render process ended (status {int(status.value)}, code {exit_code})")

    def _on_point_activated(self, point_id: str) -> None:
        if point_id in {p.id for p in self._points}:
            self._selected = point_id
            self.pointActivated.emit(point_id)

    def _on_tile_stats(self, loaded: int, errors: int) -> None:
        if self._settings.offline:
            return
        if errors and loaded == 0:
            self._start_probe()  # why? one request (network error or HTTP status), then the banner
            return
        if errors:
            log.info("map: %d tile(s) loaded, %d failed", loaded, errors)
        if loaded and self._tile_failure:
            self._tile_failure = ""
            self._update_banner()

    def _on_page_message(self, level: str, text: str) -> None:
        log.log(logging.WARNING if level == "error" else logging.DEBUG, "map page: %s", text)

    def _on_external_url(self, url: str) -> None:
        scheme = urlsplit(url).scheme.lower()
        if scheme not in ("http", "https"):
            return
        self.externalUrlRequested.emit(url)
        if self._open_external:
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl(url))

    def closeEvent(self, event) -> None:  # noqa: N802
        self._timeout.stop()
        super().closeEvent(event)

    def radius_text(self) -> str:
        """The translated "Search radius: N km" text for the current radius ('' if none)."""
        if self._radius is None:
            return ""
        return self._strings["mapview.radius"].replace("{km}", format_km(self._radius[2]))
