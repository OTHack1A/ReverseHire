"""Qt WebEngine part of the map. Imported lazily: any failure falls back to the plain map.

Provides the guarded page (navigation locked to the bundled page), the request
interceptor (policy + tile identification), the cached profile and the view.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QColor
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile, QWebEngineSettings,
                                     QWebEngineUrlRequestInfo, QWebEngineUrlRequestInterceptor)
from PySide6.QtWebEngineWidgets import QWebEngineView

from .bridge import MapBridge
from .policy import RequestPolicy
from .settings import MapSettings

log = logging.getLogger("reversehire.mapview")

WEB_DIR = Path(__file__).resolve().parent / "web"
INDEX_FILE = WEB_DIR / "index.html"

_LEVELS = {
    QWebEnginePage.JavaScriptConsoleMessageLevel.InfoMessageLevel: logging.DEBUG,
    QWebEnginePage.JavaScriptConsoleMessageLevel.WarningMessageLevel: logging.WARNING,
    QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel: logging.WARNING,
}


class TileRequestInterceptor(QWebEngineUrlRequestInterceptor):
    """Blocks every request the policy does not allow.

    Runs on the network thread: pure Python only, no GUI access.
    """

    def __init__(self, policy: RequestPolicy, parent: QObject | None = None):
        super().__init__(parent)
        self.policy = policy
        self.blocked = 0

    def interceptRequest(self, info: QWebEngineUrlRequestInfo) -> None:
        try:
            decision = self.policy.decide(info.requestUrl().toString())
            if not decision.allow:
                self.blocked += 1
                info.block(True)
                return
            # No Referer is added: a page loaded from a local file has none, and Chromium
            # cancels requests whose Referer header differs from the document's referrer.
            # The tile servers are identified by the profile's User-Agent instead.
        except Exception:  # noqa: BLE001 - never let the interceptor break loading
            info.block(True)


class GuardedPage(QWebEnginePage):
    """Only the bundled index page may load; links to http(s) go to the system browser."""

    externalUrlRequested = Signal(str)
    consoleProblem = Signal(str)

    def __init__(self, profile: QWebEngineProfile, policy: RequestPolicy, parent=None):
        super().__init__(profile, parent)
        self._policy = policy
        self._index = os.path.normcase(os.path.realpath(str(INDEX_FILE)))
        s = self.settings()
        a = QWebEngineSettings.WebAttribute
        s.setAttribute(a.LocalContentCanAccessFileUrls, True)
        s.setAttribute(a.LocalContentCanAccessRemoteUrls, True)  # tile images only; see policy
        s.setAttribute(a.JavascriptCanOpenWindows, False)
        s.setAttribute(a.JavascriptCanAccessClipboard, False)
        s.setAttribute(a.PluginsEnabled, False)
        s.setAttribute(a.AllowRunningInsecureContent, False)
        s.setAttribute(a.ErrorPageEnabled, False)
        s.setAttribute(a.ScreenCaptureEnabled, False)
        s.setAttribute(a.WebGLEnabled, False)
        s.setAttribute(a.Accelerated2dCanvasEnabled, False)
        s.setAttribute(a.AutoLoadIconsForPage, False)
        s.setAttribute(a.FullScreenSupportEnabled, False)

    def _is_index(self, url: QUrl) -> bool:
        if not url.isLocalFile():
            return False
        return os.path.normcase(os.path.realpath(url.toLocalFile())) == self._index

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:
        if is_main_frame and self._is_index(url):
            return True
        scheme = url.scheme().lower()
        if (scheme in ("http", "https")
                and nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked):
            self.externalUrlRequested.emit(url.toString())
        else:
            log.info("map: navigation blocked (%s)", url.scheme() or "?")
        return False

    def createWindow(self, window_type):  # noqa: N802 - Qt override; popups are never allowed
        return None

    def javaScriptConsoleMessage(self, level, message, line, source_id):  # noqa: N802
        py_level = _LEVELS.get(level, logging.DEBUG)
        log.log(py_level, "map page: %s (line %s)", message, line)
        if py_level >= logging.WARNING:
            self.consoleProblem.emit(str(message)[:300])


_profiles: dict[str, tuple] = {}


def get_profile(settings: MapSettings) -> tuple[QWebEngineProfile, RequestPolicy, TileRequestInterceptor]:
    """The shared map profile (one per cache folder), updated with the current settings."""
    from PySide6.QtCore import QCoreApplication

    resolved = settings.resolved()
    key = str(resolved.cache_dir) if resolved.cache_dir else ""
    entry = _profiles.get(key)
    if entry is None:
        app = QCoreApplication.instance()
        if resolved.cache_dir:
            cache = Path(resolved.cache_dir)
            try:
                (cache / "http").mkdir(parents=True, exist_ok=True)
                (cache / "storage").mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warning("map: cache folder unusable (%s); using a memory cache", exc)
                cache = None
        else:
            cache = None
        if cache is not None:
            profile = QWebEngineProfile("reversehire_map", app)
            profile.setCachePath(str(cache / "http"))
            profile.setPersistentStoragePath(str(cache / "storage"))
            profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
        else:
            profile = QWebEngineProfile(app)  # off the record: memory cache only
            profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
        profile.setSpellCheckEnabled(False)
        policy = RequestPolicy(WEB_DIR)
        interceptor = TileRequestInterceptor(policy, profile)
        profile.setUrlRequestInterceptor(interceptor)
        entry = (profile, policy, interceptor)
        _profiles[key] = entry
    profile, policy, interceptor = entry
    profile.setHttpUserAgent(resolved.user_agent)
    profile.setHttpCacheMaximumSize(int(resolved.cache_max_mb) * 1024 * 1024)
    policy.set_tile_template(None if resolved.offline else resolved.tile_url)
    return entry


class WebMap(QWebEngineView):
    """The Leaflet page with its channel. Emits what the page reports."""

    def __init__(self, settings: MapSettings, parent=None):
        super().__init__(parent)
        profile, policy, _ = get_profile(settings)
        self.bridge = MapBridge(self)
        self.channel = QWebChannel(self)
        self.channel.registerObject("bridge", self.bridge)
        self._page = GuardedPage(profile, policy, self)
        self._page.setWebChannel(self.channel)
        self.setPage(self._page)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

    @property
    def guarded_page(self) -> GuardedPage:
        return self._page

    def set_background(self, color: str) -> None:
        self._page.setBackgroundColor(QColor(color))

    def load_index(self) -> None:
        self.load(QUrl.fromLocalFile(str(INDEX_FILE)))
