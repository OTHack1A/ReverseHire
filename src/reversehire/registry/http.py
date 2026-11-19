"""Small, polite HTTP client for the free registry APIs (VIES, GLEIF).

Network manners (owner's rule: "slow, otherwise my browser gets blocked"):
- at most one request every MIN_INTERVAL_S seconds per host (or the larger
  `settings.request_pause_s`), shared by every client of the process;
- the app's identifying User-Agent (`settings.user_agent`);
- answers cached in the Store (namespace "registry", REGISTRY_TTL_S = 30 days);
- on HTTP 429 it waits (Retry-After, capped) once and retries; a second 429 pauses
  that host for HOST_PAUSE_S and raises RegistryError("rate_limited");
- every wait is cancellable through `cancel` (threading.Event).
Only http(s) URLs of the hosts in ALLOWED_HOSTS are requested.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlencode, urlsplit

import httpx

log = logging.getLogger(__name__)

CACHE_NAMESPACE = "registry"
REGISTRY_TTL_S = 30 * 24 * 3600
MIN_INTERVAL_S = 2.0
MAX_RETRY_AFTER_S = 60.0
HOST_PAUSE_S = 300.0
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
ALLOWED_HOSTS = ("ec.europa.eu", "api.gleif.org")


class RegistryError(Exception):
    """A registry answer could not be obtained; `kind` says why."""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind  # "cancelled", "rate_limited", "unavailable", "bad_answer", "not_allowed"


@dataclass
class HostState:
    """Last request time and pause per host (shared by the clients that use it)."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    last: dict[str, float] = field(default_factory=dict)
    paused_until: dict[str, float] = field(default_factory=dict)


_SHARED_STATE = HostState()


@dataclass
class Answer:
    status: int
    data: object
    url: str
    cached: bool = False


class RegistryClient:
    """GET/POST JSON with per-host pacing, Store cache and 429 back-off."""

    def __init__(self, settings, store=None, client: httpx.Client | None = None, *,
                 wait: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None,
                 cancel: threading.Event | None = None, state: HostState | None = None):
        self.settings = settings
        self.store = store
        self.cancel = cancel
        self._owns = client is None
        self._client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
        self._clock = clock or time.monotonic
        self._wait = wait
        # Tests with their own clock get their own state; the app shares one per process.
        self._state = state or (HostState() if clock is not None else _SHARED_STATE)
        self.requests = 0  # real requests sent by this client

    @property
    def interval(self) -> float:
        pause = getattr(self.settings, "request_pause_s", MIN_INTERVAL_S)
        try:
            pause = float(pause)
        except (TypeError, ValueError):
            pause = MIN_INTERVAL_S
        return max(MIN_INTERVAL_S, pause)

    def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self._wait is not None:
            self._wait(seconds)
        elif self.cancel is not None:
            self.cancel.wait(seconds)
        else:
            time.sleep(seconds)
        self._check_cancel()

    def _check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise RegistryError("cancelled")

    def _turn(self, host: str) -> None:
        """Waits for this host's turn (one request every `interval` seconds)."""
        with self._state.lock:
            now = self._clock()
            paused = self._state.paused_until.get(host, 0.0)
            if paused > now:
                raise RegistryError("rate_limited", f"{host} asked to slow down; paused")
            last = self._state.last.get(host)
            start = now if last is None else max(now, last + self.interval)
            self._state.last[host] = start
        self._sleep(start - now)

    def _cache_read(self, key: str):
        if self.store is None:
            return None
        try:
            return self.store.cache_read(CACHE_NAMESPACE, key, REGISTRY_TTL_S)
        except Exception:
            return None

    def _cache_write(self, key: str, value) -> None:
        if self.store is None:
            return
        try:
            self.store.cache_write(CACHE_NAMESPACE, key, value)
        except Exception as error:
            log.info("Registry cache not writable (%s)", type(error).__name__)

    def get_json(self, url: str, params: dict | None = None, *, headers: dict | None = None,
                 json_body: dict | None = None, cache: Callable[[int, object], bool] | None = None
                 ) -> Answer:
        """JSON answer of `url` (GET, or POST of `json_body`), from the cache when fresh.

        `cache(status, data)` decides whether an answer is stored (default: status 200).
        Raises RegistryError for network failures, 5xx, non-JSON bodies, 429 twice, cancel.
        """
        full = url + ("?" + urlencode(params) if params else "")
        parts = urlsplit(full)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or host not in ALLOWED_HOSTS:
            raise RegistryError("not_allowed", full)
        body = None if json_body is None else json.dumps(json_body, sort_keys=True, separators=(",", ":"))
        key = full if body is None else f"{full}|post:{body}"
        cached = self._cache_read(key)
        if isinstance(cached, dict) and "status" in cached:
            return Answer(int(cached["status"]), cached.get("data"), full, cached=True)
        self._check_cancel()
        request_headers = {"Accept": "application/json", **(headers or {}),
                           "User-Agent": getattr(self.settings, "user_agent", "ReverseHire")}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        for attempt in (1, 2):
            self._turn(host)
            try:
                self.requests += 1
                response = self._client.request("GET" if body is None else "POST", full,
                                                headers=request_headers,
                                                content=None if body is None else body.encode("utf-8"))
            except httpx.HTTPError as error:
                raise RegistryError("unavailable", f"{type(error).__name__}") from error
            if response.status_code == 429:
                if attempt == 2:
                    with self._state.lock:
                        self._state.paused_until[host] = self._clock() + HOST_PAUSE_S
                    raise RegistryError("rate_limited", f"{host} answered 429 twice")
                self._sleep(_retry_after(response.headers.get("retry-after"), self.interval))
                continue
            if response.status_code >= 500:
                raise RegistryError("unavailable", f"HTTP {response.status_code}")
            if len(response.content) > MAX_BYTES:
                raise RegistryError("bad_answer", "answer too large")
            try:
                data = response.json() if response.content else None
            except ValueError as error:
                raise RegistryError("bad_answer", "not JSON") from error
            store_it = cache(response.status_code, data) if cache else response.status_code == 200
            if store_it:
                self._cache_write(key, {"status": response.status_code, "data": data})
            return Answer(response.status_code, data, full)
        raise RegistryError("unavailable")  # not reached

    def close(self) -> None:
        if self._owns:
            self._client.close()

    def __enter__(self) -> "RegistryClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()


def _retry_after(value: str | None, default: float) -> float:
    try:
        seconds = float(value) if value is not None else default
    except ValueError:
        seconds = default
    return max(default, min(seconds, MAX_RETRY_AFTER_S))
