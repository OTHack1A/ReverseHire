"""Default fetch engine: direct HTTP requests with httpx.

Redirects are followed by hand, one at a time, to check every hop (private
hosts, robots.txt, per-domain delay). The response is read as a stream to
stop as soon as it exceeds the size limit.
"""

import logging
import re
from dataclasses import dataclass
from typing import Callable, Protocol
from urllib.parse import urljoin

import httpx

log = logging.getLogger(__name__)

MAX_REDIRECT = 5
PAGE_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
    "Accept-Language": "it,en;q=0.8",
}

_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.I)


@dataclass
class FetchResponse:
    """Raw result of an engine."""

    final_url: str
    http_status: int
    html: str
    text: str = ""  # some engines (Firecrawl) already provide a text


class Politeness(Protocol):
    """Rules shared by all engines, provided by the Fetcher."""

    def is_allowed(self, url: str, robots: bool = True) -> bool: ...

    def wait_turn(self, url: str, interval: float | None = None) -> None: ...


class SizeError(Exception):
    """The response exceeds the size limit."""


def content_type(response: httpx.Response) -> str:
    return (response.headers.get("content-type") or "").split(";")[0].strip().lower()


def decode(content: bytes, response: httpx.Response) -> str:
    """Decodes the body: header charset, then <meta>, then UTF-8/cp1252."""
    candidates: list[str] = []
    header = response.headers.get("content-type") or ""
    found = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9_.:-]+)", header, re.I)
    if found:
        candidates.append(found.group(1))
    meta = _META_CHARSET.search(content[:4096])
    if meta:
        candidates.append(meta.group(1).decode("ascii", "ignore"))
    candidates.append("utf-8")
    for encoding in candidates:
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("cp1252", errors="replace")


def read_limited(response: httpx.Response, max_bytes: int) -> bytes:
    """Reads the body as a stream; SizeError beyond `max_bytes`."""
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise SizeError()
    parts: list[bytes] = []
    total = 0
    for block in response.iter_bytes():
        total += len(block)
        if total > max_bytes:
            raise SizeError()
        parts.append(block)
    return b"".join(parts)


class LocalEngine:
    """Fetching with httpx, controlled redirects and a size limit."""

    name = "local"

    def __init__(
        self,
        client: httpx.Client,
        user_agent: str,
        politeness: Politeness,
        max_bytes: int,
        max_redirect: int = MAX_REDIRECT,
    ):
        self._client = client
        self._user_agent = user_agent
        self._politeness = politeness
        self._max_bytes = max_bytes
        self._max_redirect = max_redirect

    def available(self) -> bool:
        return True

    def download(self, url: str) -> FetchResponse | None:
        """HTML/text page; None for errors, non-2xx statuses or non-text content."""
        return self.download_raw(
            url,
            accept=lambda kind: not kind or kind in PAGE_CONTENT_TYPES,
            headers=PAGE_HEADERS,
        )

    def download_raw(
        self,
        url: str,
        accept: Callable[[str], bool] | None = None,
        headers: dict[str, str] | None = None,
        max_bytes: int | None = None,
        robots: bool = True,
        success_only: bool = True,
        method: str = "GET",
        content: bytes | None = None,
    ) -> FetchResponse | None:
        """Generic request (GET, or POST with `content`) with controlled redirects.

        `accept(kind)` decides on content types; `robots=False` skips the
        robots.txt check on redirects (for robots.txt itself and the APIs).
        A POST is never redirected (a redirect answer gives None).
        """
        limit = max_bytes or self._max_bytes
        current = url
        final_headers = {**(headers or {}), "User-Agent": self._user_agent}
        blocked = getattr(self._politeness, "is_blocked", None)
        for step in range(self._max_redirect + 1):
            if step > 0 and not self._politeness.is_allowed(current, robots=robots):
                log.info("Redirect to a disallowed address: discarded")
                return None
            if blocked is not None and blocked(current):
                return None  # the host asked us to slow down (HTTP 429): left alone
            self._politeness.wait_turn(current)
            try:
                request = self._client.build_request(method, current, headers=final_headers,
                                                     content=content)
                response = self._client.send(request, stream=True, follow_redirects=False)
            except (httpx.HTTPError, ValueError) as error:
                log.info("Request failed (%s): %s", type(error).__name__, current)
                return None
            try:
                if response.is_redirect:
                    destination = response.headers.get("location")
                    if not destination or method != "GET":
                        return None
                    current = urljoin(current, destination)
                    continue
                if response.status_code == 429:
                    block = getattr(self._politeness, "block", None)
                    if block is not None:
                        block(current, "HTTP 429")
                    return None
                if success_only and response.status_code in (404, 410):
                    note = getattr(self._politeness, "note_missing", None)
                    if note is not None:
                        note(url)
                if success_only and not 200 <= response.status_code < 300:
                    log.info("HTTP status %s: %s", response.status_code, current)
                    return None
                kind = content_type(response)
                if accept is not None and not accept(kind):
                    log.info("Content of type %r ignored: %s", kind, current)
                    return None
                content = read_limited(response, limit)
                return FetchResponse(current, response.status_code, decode(content, response))
            except SizeError:
                log.info("Response over %d bytes: %s", limit, current)
                return None
            except httpx.HTTPError as error:
                log.info("Read failed (%s): %s", type(error).__name__, current)
                return None
            finally:
                response.close()
        log.info("Too many redirects: %s", url)
        return None
