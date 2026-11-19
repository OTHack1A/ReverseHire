"""The ONE polite HTTP gateway used by every discovery source.

Owner's rule: go slowly, otherwise the public services block the connection. So:

- a minimum pause between two requests to the same host (`settings.request_pause_s`,
  default 2 s), shared by the whole process (all gateways, all threads);
- at most MAX_PARALLEL_HOSTS hosts contacted at the same time;
- an identifying User-Agent with a contact URL (Wikimedia and Overpass require it):
  "ReverseHire/<version> (+<REPOSITORY_URL>) httpx/<version>";
- HTTP 429 (or 503 with Retry-After): one back-off (Retry-After, capped), then the host
  is given up for the rest of this gateway's life (one search) -> `SourceBlocked`;
- every successful text/JSON answer can be cached in the Store with a TTL;
- every wait and every download is cancellable (`cancel` event);
- downloads are resumable (HTTP Range on a ".part" file) and throttled
  (default 3 MB/s) so the user's connection stays usable;
- counters of requests and bytes per host (`stats()`), for diagnostics and reports.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import Counter
from dataclasses import dataclass
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import httpx

from .. import REPOSITORY_URL, __version__

CONTACT_URL = REPOSITORY_URL
DEFAULT_PAUSE_S = 2.0
MAX_PARALLEL_HOSTS = 2
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 40.0
MAX_BACKOFF_S = 30.0          # longest Retry-After honoured before giving up the host
DEFAULT_BACKOFF_S = 30.0      # Overpass policy: "pause for 30 seconds" after a 429
DOWNLOAD_RATE_BPS = 3 * 1024 * 1024
CHUNK_BYTES = 256 * 1024
CACHE_NAMESPACE = "discovery_http"


def user_agent() -> str:
    return f"ReverseHire/{__version__} (+{CONTACT_URL}) httpx/{httpx.__version__}"


class GatewayError(Exception):
    """Network failure, timeout or unexpected HTTP status (the source is skipped)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class SourceBlocked(GatewayError):
    """The host asked us to slow down (429) twice, or for too long: given up for this run."""


class Cancelled(Exception):
    """The user cancelled while a request or a wait was in progress."""


@dataclass
class Reply:
    status: int
    text: str
    headers: dict
    from_cache: bool = False

    def json(self):
        return json.loads(self.text)


# Process-wide pacing: one timestamp per host, whatever gateway sends the request.
_host_lock = threading.Lock()
_host_locks: dict[str, threading.Lock] = {}
_host_last: dict[str, float] = {}
_parallel = threading.BoundedSemaphore(MAX_PARALLEL_HOSTS)


def _lock_for(host: str) -> threading.Lock:
    with _host_lock:
        lock = _host_locks.get(host)
        if lock is None:
            lock = _host_locks[host] = threading.Lock()
        return lock


def reset_pacing() -> None:
    """Forget the per-host timestamps (tests)."""
    with _host_lock:
        _host_last.clear()


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        return max(0.0, when.timestamp() - time.time())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


class PoliteGateway:
    """All outgoing requests of the discovery layer.

    `client`, `clock` and `sleep` are injectable so the tests never touch the network
    or really wait. `sleep(seconds, cancel)` must return early when `cancel` is set.
    """

    def __init__(self, settings=None, store=None, client: httpx.Client | None = None, *,
                 pause_s: float | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable | None = None, max_backoff_s: float = MAX_BACKOFF_S):
        self.settings = settings
        self.store = store
        self.pause_s = float(pause_s if pause_s is not None
                             else getattr(settings, "request_pause_s", DEFAULT_PAUSE_S) or DEFAULT_PAUSE_S)
        self.pause_s = max(0.0, self.pause_s)
        self._client = client
        self._owns_client = client is None
        self._clock = clock
        self._sleep = sleep or self._default_sleep
        self.max_backoff_s = max_backoff_s
        self.blocked: set[str] = set()
        self._stats_lock = threading.Lock()
        self._requests: Counter = Counter()
        self._bytes: Counter = Counter()
        self._cache_hits: Counter = Counter()

    # --- helpers

    @staticmethod
    def _default_sleep(seconds: float, cancel=None) -> None:
        if seconds <= 0:
            return
        if cancel is not None:
            cancel.wait(seconds)
        else:
            time.sleep(seconds)

    def _wait(self, seconds: float, cancel) -> None:
        self._sleep(seconds, cancel)
        if cancel is not None and cancel.is_set():
            raise Cancelled()

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(follow_redirects=True)
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def headers(self, extra: dict | None = None) -> dict:
        headers = {"User-Agent": user_agent()}
        headers.update(extra or {})
        return headers

    def stats(self) -> dict:
        """{host: {"requests": n, "bytes": n, "cache_hits": n}}."""
        with self._stats_lock:
            hosts = set(self._requests) | set(self._cache_hits)
            return {h: {"requests": self._requests[h], "bytes": self._bytes[h],
                        "cache_hits": self._cache_hits[h]} for h in sorted(hosts)}

    def _count(self, host: str, size: int) -> None:
        with self._stats_lock:
            self._requests[host] += 1
            self._bytes[host] += size

    def _take_turn(self, host: str, cancel) -> None:
        """Wait until `pause_s` has passed since the last request to `host` (caller holds
        the host lock), then record the new request time."""
        last = _host_last.get(host)
        if last is not None:
            remaining = last + self.pause_s - self._clock()
            if remaining > 0:
                self._wait(remaining, cancel)
        _host_last[host] = self._clock()

    @staticmethod
    def cache_key(method: str, url: str, params=None, data=None) -> str:
        raw = json.dumps([method.upper(), url, params or {}, data or {}], sort_keys=True,
                         ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # --- requests

    def request(self, method: str, url: str, *, params: dict | None = None, data=None,
                content: str | bytes | None = None,
                headers: dict | None = None, timeout: float | httpx.Timeout | None = None,
                cache_ttl_s: float | None = None, cache_namespace: str = CACHE_NAMESPACE,
                cancel=None, retry_backoff: bool = True) -> Reply:
        """One polite request. Returns a Reply for any status (the caller decides);
        raises SourceBlocked after a repeated 429, GatewayError on network errors,
        Cancelled when `cancel` is set. Successful (200) answers are cached when
        `cache_ttl_s` is given (and read back from the cache before any request).

        `data` is a form (dict); a raw body (a JSON document as text, bytes) goes in
        `content`. A string or bytes passed as `data` is moved to `content` (httpx
        deprecates raw bodies in `data`)."""
        if isinstance(data, (str, bytes)):
            if content is None:
                content = data
            data = None
        host = host_of(url)
        if not host:
            raise GatewayError(f"invalid URL: {url!r}")
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        key = None
        if cache_ttl_s is not None and self.store is not None:
            body_key = data if content is None else (
                content.decode("utf-8", "replace") if isinstance(content, bytes) else content)
            key = self.cache_key(method, url, params, body_key)
            try:
                cached = self.store.cache_read(cache_namespace, key, cache_ttl_s)
            except Exception:
                cached = None
            if isinstance(cached, dict) and "text" in cached:
                with self._stats_lock:
                    self._cache_hits[host] += 1
                return Reply(int(cached.get("status", 200)), cached["text"], {}, True)
        if host in self.blocked:
            raise SourceBlocked(f"{host} asked to slow down earlier in this search", 429)
        if timeout is None:
            timeout = httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
        attempts = 2 if retry_backoff else 1
        reply: Reply | None = None
        for attempt in range(attempts):
            with _parallel:
                with _lock_for(host):
                    self._take_turn(host, cancel)
                    try:
                        response = self.client.request(method, url, params=params, data=data,
                                                       content=content, headers=self.headers(headers),
                                                       timeout=timeout)
                    except httpx.HTTPError as error:
                        self._count(host, 0)
                        raise GatewayError(f"{host}: {type(error).__name__}") from error
                    body = response.content
                    self._count(host, len(body))
                    _host_last[host] = self._clock()  # the pause counts from the end of the answer
            reply = Reply(response.status_code, response.text, dict(response.headers))
            slow_down = response.status_code == 429 or (
                response.status_code == 503 and response.headers.get("retry-after"))
            if not slow_down:
                break
            wait = _retry_after(response.headers.get("retry-after"))
            if wait is None:
                wait = DEFAULT_BACKOFF_S
            if attempt + 1 >= attempts or wait > self.max_backoff_s:
                self.blocked.add(host)
                raise SourceBlocked(f"{host} answered HTTP {response.status_code} (slow down)",
                                    response.status_code)
            self._wait(wait, cancel)
        assert reply is not None
        if reply.status == 200 and key is not None and self.store is not None:
            try:
                self.store.cache_write(cache_namespace, key, {"status": 200, "text": reply.text})
            except Exception:
                pass
        return reply

    def get(self, url: str, **kwargs) -> Reply:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> Reply:
        return self.request("POST", url, **kwargs)

    # --- downloads

    def download(self, url: str, target: Path, *, cancel=None,
                 progress: Callable[[int, int | None], None] | None = None,
                 max_rate_bps: float | None = DOWNLOAD_RATE_BPS,
                 if_modified_since: float | None = None) -> str:
        """Download `url` to `target` (via "<target>.part", resumed with Range when it
        exists). Returns "downloaded" or "not_modified" (304 with `if_modified_since`,
        a file time). Throttled to `max_rate_bps` (None = unthrottled)."""
        host = host_of(url)
        if host in self.blocked:
            raise SourceBlocked(f"{host} asked to slow down earlier", 429)
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        have = part.stat().st_size if part.exists() else 0
        headers = {}
        if have:
            headers["Range"] = f"bytes={have}-"
        if if_modified_since and not have:
            headers["If-Modified-Since"] = formatdate(if_modified_since, usegmt=True)
        timeout = httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
        with _parallel:
            with _lock_for(host):
                self._take_turn(host, cancel)
                try:
                    with self.client.stream("GET", url, headers=self.headers(headers),
                                            timeout=timeout) as response:
                        self._count(host, 0)
                        status = response.status_code
                        if status == 304:
                            return "not_modified"
                        if status == 429:
                            self.blocked.add(host)
                            raise SourceBlocked(f"{host} answered HTTP 429", 429)
                        if status == 416 and have:
                            # Range not satisfiable: the .part is already complete.
                            part.replace(target)
                            return "downloaded"
                        if status not in (200, 206):
                            raise GatewayError(f"{host} answered HTTP {status}", status)
                        if status == 200 and have:
                            have = 0  # the server ignored Range: start again
                        length = response.headers.get("content-length")
                        total = (int(length) + have) if length and length.isdigit() else None
                        mode = "ab" if have else "wb"
                        done = have
                        started = self._clock()
                        sent = 0
                        with part.open(mode) as handle:
                            for chunk in response.iter_bytes(CHUNK_BYTES):
                                if cancel is not None and cancel.is_set():
                                    raise Cancelled()
                                handle.write(chunk)
                                done += len(chunk)
                                sent += len(chunk)
                                with self._stats_lock:
                                    self._bytes[host] += len(chunk)
                                if progress is not None:
                                    progress(done, total)
                                if max_rate_bps:
                                    ahead = sent / max_rate_bps - (self._clock() - started)
                                    if ahead > 0:
                                        self._wait(ahead, cancel)
                except httpx.HTTPError as error:
                    raise GatewayError(f"{host}: {type(error).__name__}") from error
                finally:
                    _host_last[host] = self._clock()
        if total is not None and done < total:
            raise GatewayError(f"{host}: download incomplete ({done} of {total} bytes)")
        part.replace(target)
        return "downloaded"
