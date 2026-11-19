"""Fetching of web pages with politeness rules shared by all engines.

- http/https only, never local or private hosts (URLs come from external data);
- robots.txt respected per domain (in-memory cache; unreachable = allowed);
- minimum delay between requests to the same domain, valid across threads;
- size limit, HTML/text content only, declared User-Agent;
- page cache in the Store (namespace "pages", 7 days).

Engine choice (D8/D9): starts from `settings.crawl_engine` ("local" by
default); if the page has almost no text and seems to depend on JavaScript,
Playwright (if installed) and Firecrawl (if there is a key) are tried.
"""

import ipaddress
import json
import logging
import re
import socket
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from ..store import Store
from ..config import CRAWL_ENGINES, Settings
from .firecrawl_engine import FirecrawlEngine
from .local_engine import LocalEngine, FetchResponse
from .playwright_engine import PlaywrightEngine
from .text import compact, extract_text, host_base, clean_url

log = logging.getLogger(__name__)

CACHE_NAMESPACE = "pages"
PAGES_TTL_S = 7 * 24 * 3600
API_TTL_S = 24 * 3600
ROBOTS_TTL_S = 24 * 3600          # robots.txt kept for a day
PAGE_MISS_TTL_S = 24 * 3600       # an address that answered 404/410 is not asked again for a day
PROBE_MISS_TTL_S = 14 * 24 * 3600  # a guessed ATS board that does not exist: asked again after 2 weeks
MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_ROBOTS_BYTES = 512 * 1024
MAX_API_BYTES = 10 * 1024 * 1024
MAX_CRAWL_DELAY_S = 30.0
EMPTY_TEXT_THRESHOLD = 200
TIMEOUT = httpx.Timeout(15.0, connect=10.0)

_LOCAL_HOSTNAMES = ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback")
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".localdomain")
_JS_SIGNS = re.compile(
    r"""id=["'](?:root|app|__next|__nuxt|___gatsby)["']|__NEXT_DATA__|window\.__NUXT__|"""
    r"""ng-app|ng-version|data-reactroot|enable javascript|abilita(?:re)? javascript|"""
    r"""javascript (?:is )?(?:required|disabled)|javascript non (?:è )?abilitato""",
    re.I,
)


@dataclass
class Page:
    url: str
    final_url: str
    http_status: int
    html: str
    text: str
    engine: str


def looks_dynamic(html: str, text: str) -> bool:
    """Almost empty text and HTML that looks built by JavaScript."""
    if len(compact(text)) >= EMPTY_TEXT_THRESHOLD:
        return False
    if not html.strip():
        return True
    return bool(_JS_SIGNS.search(html)) or html.lower().count("<script") >= 3


def _ip_forbidden(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def host_forbidden(host: str) -> bool:
    """True for local names, private/reserved IPs and ambiguous numeric forms."""
    host = (host or "").strip().strip("[]").rstrip(".").lower()
    if not host:
        return True
    if host in _LOCAL_HOSTNAMES or host.endswith(_LOCAL_SUFFIXES):
        return True
    try:
        return _ip_forbidden(ipaddress.ip_address(host.split("%")[0]))
    except ValueError:
        pass
    # A valid name has an alphabetic top-level domain: forms such as
    # "2130706433" or "0x7f.1" would be interpreted as IPs by the system.
    last = host.rsplit(".", 1)[-1]
    if re.fullmatch(r"[0-9]+|0x[0-9a-f]+", last) or "." not in host:
        return True
    return False


def _resolve_dns(host: str) -> list[str]:
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except (OSError, UnicodeError):
        return []


class Fetcher:
    """Fetches pages with the chosen engine and the shared politeness rules."""

    def __init__(
        self,
        settings: Settings,
        store: Store | None = None,
        client: httpx.Client | None = None,
        *,
        wait: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        resolve_dns: Callable[[str], list[str]] | None = None,
        engines: dict | None = None,
    ):
        """`wait`, `clock`, `resolve_dns` and `engines` are for the tests.

        Without an explicit `client` hosts are also resolved via DNS and
        discarded if they point to private addresses; with a test client the
        DNS check is only active when `resolve_dns` is passed.
        """
        self.settings = settings
        self.store = store
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=TIMEOUT,
            follow_redirects=False,
            headers={"User-Agent": settings.user_agent},
        )
        if resolve_dns is None and client is None:
            resolve_dns = _resolve_dns
        self._resolve = resolve_dns
        self._wait = wait or time.sleep
        self._clock = clock or time.monotonic

        self._lock = threading.Lock()
        self._domain_locks: dict[str, threading.Lock] = {}
        self._robots_locks: dict[str, threading.Lock] = {}
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._crawl_delay: dict[str, float] = {}
        self._dns: dict[str, bool] = {}
        self.requests_per_domain: Counter[str] = Counter()
        # Domains that answered HTTP 429 ("too many requests"): never asked again by
        # this Fetcher (one search, one card build).
        self.blocked_domains: dict[str, str] = {}

        ua = settings.user_agent
        self._local_engine = LocalEngine(self._client, ua, self, MAX_PAGE_BYTES)
        self.engines = {
            "local": self._local_engine,
            "playwright": PlaywrightEngine(ua, self, MAX_PAGE_BYTES),
            "firecrawl": FirecrawlEngine(self._client, settings.firecrawl_key, ua, self, MAX_PAGE_BYTES),
        }
        if engines:
            self.engines.update(engines)

    # --- politeness rules (also used by the engines) ---------------------

    def _lock_for(self, collection: dict[str, threading.Lock], key: str) -> threading.Lock:
        with self._lock:
            if key not in collection:
                collection[key] = threading.Lock()
            return collection[key]

    def safe_url(self, url: str) -> bool:
        """http/https scheme and public host (also after DNS resolution, if active)."""
        try:
            parts = urlsplit(url)
            host = parts.hostname
            _ = parts.port  # invalid port -> ValueError
        except ValueError:
            return False
        if parts.scheme not in ("http", "https") or not host:
            return False
        if parts.username or parts.password:
            return False
        if host_forbidden(host):
            return False
        if self._resolve is None:
            return True
        with self._lock:
            known = self._dns.get(host)
        if known is None:
            addresses = self._resolve(host)
            known = bool(addresses)
            for address in addresses:
                try:
                    if _ip_forbidden(ipaddress.ip_address(address.split("%")[0])):
                        known = False
                except ValueError:
                    known = False
            with self._lock:
                self._dns[host] = known
        return known

    def block(self, url: str, reason: str = "HTTP 429") -> None:
        """Stop asking a domain (it answered "too many requests")."""
        domain = host_base(url) or url
        with self._lock:
            new = domain not in self.blocked_domains
            self.blocked_domains[domain] = reason
        if new:
            log.warning("%s answered %s: no more requests to it in this session", domain, reason)

    def note_missing(self, url: str) -> None:
        """The engine got 404/410 for `url`: remembered (Store) for PAGE_MISS_TTL_S."""
        self._to_cache("miss:" + clean_url(url), True)

    def _known_missing(self, url: str) -> bool:
        if self.store is None:
            return False
        try:
            return bool(self.store.cache_read(CACHE_NAMESPACE, "miss:" + url, PAGE_MISS_TTL_S))
        except Exception:
            return False

    def is_blocked(self, url: str) -> bool:
        domain = host_base(url) or url
        with self._lock:
            return domain in self.blocked_domains

    def is_allowed(self, url: str, robots: bool = True) -> bool:
        if not self.safe_url(url):
            log.info("Address not allowed (scheme or host): %s", url)
            return False
        if self.is_blocked(url):
            log.info("Domain stopped after HTTP 429: %s", url)
            return False
        if robots and not self._robots_allows(url):
            log.info("Disallowed by robots.txt: %s", url)
            return False
        return True

    def wait_turn(self, url: str, interval: float | None = None) -> None:
        """Respects the minimum delay since the last request to the same domain."""
        domain = host_base(url) or url
        minimum = max(self.settings.request_delay_s, self._crawl_delay.get(domain, 0.0))
        if interval is not None:
            minimum = max(minimum, interval)
        with self._lock_for(self._domain_locks, domain):
            last = self._last_request.get(domain)
            if last is not None:
                wait = last + minimum - self._clock()
                if wait > 0:
                    self._wait(wait)
            self._last_request[domain] = self._clock()
            self.requests_per_domain[domain] += 1

    def _robots_allows(self, url: str) -> bool:
        parts = urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc.lower()}"
        with self._lock_for(self._robots_locks, root):
            if root not in self._robots:
                self._robots[root] = self._download_robots(root)
            rules = self._robots[root]
        if rules is None:
            return True
        try:
            return rules.can_fetch(self.settings.user_agent, url)
        except Exception:
            return True

    def sitemaps(self, url: str) -> list[str]:
        """Sitemap URLs declared in the robots.txt of the URL's site (already read
        when the site was fetched; read now otherwise); empty if none."""
        parts = urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc.lower()}"
        if not self.safe_url(root + "/"):
            return []
        with self._lock_for(self._robots_locks, root):
            if root not in self._robots:
                self._robots[root] = self._download_robots(root)
            rules = self._robots[root]
        try:
            found = rules.site_maps() if rules is not None else None
        except Exception:
            found = None
        return [u for u in (found or []) if isinstance(u, str) and u.startswith(("http://", "https://"))]

    def _download_robots(self, root: str) -> RobotFileParser | None:
        """robots.txt rules, or None (everything allowed) if absent or unreachable.

        The file is kept in the Store for ROBOTS_TTL_S, so a new search (or a card
        build) does not ask every site for it again."""
        key = "robots:" + root
        text = None
        cached = None
        if self.store is not None:
            try:
                cached = self.store.cache_read(CACHE_NAMESPACE, key, ROBOTS_TTL_S)
            except Exception:
                cached = None
        if isinstance(cached, dict):
            text = cached.get("text")
        else:
            response = self._local_engine.download_raw(
                root + "/robots.txt",
                accept=None,
                headers={"Accept": "text/plain,*/*;q=0.5"},
                max_bytes=MAX_ROBOTS_BYTES,
                robots=False,
                success_only=False,
            )
            if response is not None and 200 <= response.http_status < 300:
                text = response.html
            if response is not None:  # unreachable sites are asked again next time
                self._to_cache(key, {"text": text})
        if text is None:
            return None
        rules = RobotFileParser()
        try:
            rules.parse(text.splitlines())
        except Exception:
            return None
        try:
            delay = rules.crawl_delay(self.settings.user_agent)
        except Exception:
            delay = None
        if delay:
            try:
                value = min(float(delay), MAX_CRAWL_DELAY_S)
                self._crawl_delay[host_base(root)] = value
            except (TypeError, ValueError):
                pass
        return rules

    # --- fetching -----------------------------------------------------------

    def _engine_order(self) -> list[str]:
        preferred = self.settings.crawl_engine
        if preferred not in CRAWL_ENGINES:
            preferred = "local"
        return [preferred] + [m for m in CRAWL_ENGINES if m != preferred]

    def _make_page(self, url: str, response: FetchResponse, engine: str) -> Page:
        text = extract_text(response.html, response.final_url)
        if not text and response.text:
            text = compact(response.text)
        return Page(url, response.final_url, response.http_status, response.html, text, engine)

    def _from_cache(self, url: str, force_engine: str | None) -> Page | None:
        if self.store is None:
            return None
        try:
            data = self.store.cache_read(CACHE_NAMESPACE, url, PAGES_TTL_S)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        if force_engine and data.get("engine") != force_engine:
            return None
        try:
            return Page(**data)
        except TypeError:
            return None

    def _to_cache(self, key: str, value) -> None:
        if self.store is None:
            return
        try:
            self.store.cache_write(CACHE_NAMESPACE, key, value)
        except Exception as error:
            log.info("Cache not writable (%s)", type(error).__name__)

    def fetch_page(self, url: str, force_engine: str | None = None) -> Page | None:
        """Page read (or from the cache), or None if forbidden or unreadable."""
        url = clean_url(url)
        if force_engine is not None and force_engine not in self.engines:
            return None
        if not self.safe_url(url):
            log.info("Address not allowed (scheme or host): %s", url)
            return None
        cached = self._from_cache(url, force_engine)
        if cached is not None:
            return cached
        if self._known_missing(url):
            return None  # answered 404 recently (a guessed "/careers"...): not asked again
        if not self.is_allowed(url):
            return None

        order = [force_engine] if force_engine else self._engine_order()
        best: Page | None = None
        for name in order:
            engine = self.engines[name]
            if not engine.available():
                continue
            try:
                response = engine.download(url)
            except Exception as error:  # an engine must never make the flow fail
                log.info("Engine %s: unexpected error (%s)", name, type(error).__name__)
                response = None
            if response is None:
                if name == "local":
                    break  # unreachable or blocking site: no external engines
                continue
            page = self._make_page(url, response, name)
            if not looks_dynamic(page.html, page.text):
                best = page
                break
            if best is None or len(page.text) > len(best.text):
                best = page
            log.info("Page with little text and dependent on JavaScript (%s): %s", name, url)
        if best is not None:
            self._to_cache(url, asdict(best))
        return best

    def download_data(self, url: str, max_age_s: float = API_TTL_S, *,
                      json_body: dict | None = None) -> str | None:
        """Body of a public API (ATS JSON/XML): no robots.txt, same other rules.

        With `json_body` the request is a POST of that JSON (Workday); the cache
        key then includes the body.
        """
        url = clean_url(url)
        if not self.safe_url(url):
            return None
        payload = None if json_body is None else json.dumps(json_body, sort_keys=True, separators=(",", ":"))
        key = "api:" + url + ("" if payload is None else "|post:" + payload)
        if self.store is not None:
            try:
                data = self.store.cache_read(CACHE_NAMESPACE, key, max_age_s)
            except Exception:
                data = None
            if isinstance(data, str):
                return data
        headers = {"Accept": "application/json, application/xml;q=0.9, */*;q=0.5"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        response = self._local_engine.download_raw(
            url,
            accept=lambda kind: not kind or "json" in kind or "xml" in kind or kind.startswith("text/"),
            headers=headers,
            max_bytes=MAX_API_BYTES,
            robots=False,
            method="GET" if payload is None else "POST",
            content=None if payload is None else payload.encode("utf-8"),
        )
        if response is None:
            return None
        self._to_cache(key, response.html)
        return response.html

    def download_probe(self, url: str, max_age_s: float = PROBE_MISS_TTL_S) -> str | None:
        """`download_data` for a GUESSED address (ATS board named after the domain): a
        miss (404...) is remembered for `max_age_s`, so the same guess is not sent
        again at every search or card build."""
        key = "probe-miss:" + clean_url(url)
        if self.store is not None:
            try:
                if self.store.cache_read(CACHE_NAMESPACE, key, max_age_s):
                    return None
            except Exception:
                pass
        body = self.download_data(url)
        if body is None:
            self._to_cache(key, True)
        return body

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *_) -> None:
        self.close()
