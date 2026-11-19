"""Optional engine: Firecrawl scrape API, used only if there is a key.

Reference (checked on https://docs.firecrawl.dev/api-reference/endpoint/scrape,
November 2026): `POST https://api.firecrawl.dev/v2/scrape`, header
`Authorization: Bearer <key>`, JSON body `{"url": ..., "formats": [...]}`.
Response: `{"success": true, "data": {"markdown", "rawHtml", "metadata":
{"statusCode", "url", "sourceURL", "creditsUsed"?, ...}}}`; errors with
`{"success": false, "error": ...}` and statuses 402 (credits exhausted),
429 (rate limit), etc.

Only company URLs are sent to Firecrawl. The key never appears in logs or
messages.
"""

import json
import logging
import threading

import httpx

from .local_engine import Politeness, SizeError, FetchResponse, read_limited

log = logging.getLogger(__name__)

URL_API = "https://api.firecrawl.dev/v2/scrape"
# Free plan: 10 scrapes per minute (docs.firecrawl.dev/rate-limits).
API_INTERVAL_S = 6.0
TIMEOUT_SCRAPE_MS = 45000


class FirecrawlEngine:
    """Fetching through the Firecrawl API (direct HTTP call with httpx)."""

    name = "firecrawl"

    def __init__(self, client: httpx.Client, key: str, user_agent: str, politeness: Politeness, max_bytes: int):
        self._client = client
        self._key = (key or "").strip()
        self._user_agent = user_agent
        self._politeness = politeness
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self.credits_used = 0  # approximate counter of credits used
        self.requests = 0

    def available(self) -> bool:
        return bool(self._key)

    def download(self, url: str) -> FetchResponse | None:
        if not self.available():
            return None
        # Delay towards the company site (which Firecrawl will visit) and towards the API.
        self._politeness.wait_turn(url)
        self._politeness.wait_turn(URL_API, interval=API_INTERVAL_S)
        body = {
            "url": url,
            "formats": ["rawHtml", "markdown"],
            "onlyMainContent": False,
            "timeout": TIMEOUT_SCRAPE_MS,
        }
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "User-Agent": self._user_agent,
        }
        try:
            request = self._client.build_request(
                "POST", URL_API, json=body, headers=headers,
                timeout=httpx.Timeout(TIMEOUT_SCRAPE_MS / 1000 + 15, connect=10.0),
            )
            response = self._client.send(request, stream=True)
        except (httpx.HTTPError, ValueError) as error:
            # No exception details: they might contain headers.
            log.info("Firecrawl unreachable (%s)", type(error).__name__)
            return None
        try:
            with self._lock:
                self.requests += 1
            if response.status_code != 200:
                log.info("Firecrawl responded with status %s", response.status_code)
                return None
            data = json.loads(read_limited(response, self._max_bytes * 3))
        except SizeError:
            log.info("Firecrawl response over the size limit")
            return None
        except (httpx.HTTPError, ValueError) as error:
            log.info("Firecrawl response unreadable (%s)", type(error).__name__)
            return None
        finally:
            response.close()
        return self._parse(data, url)

    def _parse(self, data, url: str) -> FetchResponse | None:
        if not isinstance(data, dict) or not data.get("success"):
            log.info("Firecrawl did not read the page")
            return None
        content = data.get("data") or {}
        if not isinstance(content, dict):
            return None
        metadata = content.get("metadata") or {}
        credit_cost = metadata.get("creditsUsed") if isinstance(metadata, dict) else None
        with self._lock:
            self.credits_used += credit_cost if isinstance(credit_cost, int) else 1
        status = metadata.get("statusCode", 200) if isinstance(metadata, dict) else 200
        if not isinstance(status, int):
            status = 200
        if not 200 <= status < 300:
            log.info("The site responded to Firecrawl with status %s", status)
            return None
        html = content.get("rawHtml") or content.get("html") or ""
        markdown = content.get("markdown") or ""
        if not html and not markdown:
            return None
        final_url = (metadata.get("url") or metadata.get("sourceURL") or url) if isinstance(metadata, dict) else url
        if not self._politeness.is_allowed(final_url, robots=True):
            return None
        # The markdown is a fallback if no text can be extracted from the HTML.
        return FetchResponse(final_url, status, html, text=markdown)
