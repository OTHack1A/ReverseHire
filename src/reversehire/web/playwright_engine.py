"""Optional engine: headless browser with Playwright, for sites that depend on JavaScript.

Playwright is not a required dependency: it is imported only when needed and,
if it is missing (or the browsers are), the engine is simply unavailable.
"""

import logging
import threading

from .local_engine import Politeness, FetchResponse

log = logging.getLogger(__name__)

# Resources that are useless for reading text: not downloaded.
_BLOCKED_RESOURCES = {"image", "media", "font"}


class PlaywrightEngine:
    """Fetching through headless Chromium (optional `playwright` package)."""

    name = "playwright"

    def __init__(self, user_agent: str, politeness: Politeness, max_bytes: int, timeout_s: float = 30.0):
        self._user_agent = user_agent
        self._politeness = politeness
        self._max_bytes = max_bytes
        self._timeout_ms = int(timeout_s * 1000)
        self._available: bool | None = None
        # The Playwright sync API must not be used by several threads at once.
        self._lock = threading.Lock()

    def available(self) -> bool:
        if self._available is None:
            try:
                import playwright.sync_api  # noqa: F401  (lazy import)

                self._available = True
            except Exception:
                self._available = False
        return self._available

    def _filter_requests(self, route) -> None:
        """Blocks heavy resources and requests to local/private hosts."""
        request = route.request
        if request.resource_type in _BLOCKED_RESOURCES:
            route.abort()
            return
        if not self._politeness.is_allowed(request.url, robots=False):
            route.abort()
            return
        route.continue_()

    def download(self, url: str) -> FetchResponse | None:
        if not self.available():
            return None
        with self._lock:
            self._politeness.wait_turn(url)
            try:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    try:
                        context = browser.new_context(user_agent=self._user_agent)
                        page = context.new_page()
                        page.route("**/*", self._filter_requests)
                        response = page.goto(url, timeout=self._timeout_ms, wait_until="load")
                        try:
                            page.wait_for_load_state("networkidle", timeout=5000)
                        except Exception:
                            pass  # some pages never stop making requests
                        status = response.status if response is not None else 200
                        final_url = page.url or url
                        html = page.content()
                    finally:
                        browser.close()
            except Exception as error:
                # Typical: browser not installed ("playwright install chromium").
                log.info("Playwright did not read the page (%s)", type(error).__name__)
                if "Executable doesn't exist" in str(error):
                    self._available = False
                return None
        if not 200 <= status < 300:
            log.info("HTTP status %s with Playwright: %s", status, url)
            return None
        if not self._politeness.is_allowed(final_url, robots=True):
            return None
        if len(html.encode("utf-8", errors="ignore")) > self._max_bytes:
            log.info("Page over the size limit with Playwright: %s", url)
            return None
        return FetchResponse(final_url, status, html)
