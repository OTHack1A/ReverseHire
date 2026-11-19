"""Small cache of the job search: sitemaps (one download per agency per day, then
conditional requests), posting pages (until validThrough), robots.txt (a day), ATS
boards (hours). In memory, and on disk when a folder is given (JSON files), so a second
search does not fetch again."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from ._util import log

DAY_S = 24 * 3600


class JobSearchCache:
    def __init__(self, directory: Path | str | None = None, *, clock=time.time, max_memory_items: int = 5000):
        self.directory = Path(directory) if directory else None
        self._clock = clock
        self._lock = threading.Lock()
        self._memory: dict[tuple[str, str], dict] = {}
        self._max = max_memory_items

    def _path(self, namespace: str, key: str) -> Path | None:
        if self.directory is None:
            return None
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.directory / namespace / f"{digest}.json"

    def get(self, namespace: str, key: str, max_age_s: float | None = None) -> dict | None:
        """The stored value, or None when missing or older than `max_age_s` (None = no limit;
        a value may also carry its own "expires" time)."""
        with self._lock:
            entry = self._memory.get((namespace, key))
        if entry is None:
            path = self._path(namespace, key)
            if path is not None and path.exists():
                try:
                    entry = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    entry = None
                if entry is not None:
                    with self._lock:
                        self._memory[(namespace, key)] = entry
        if not isinstance(entry, dict):
            return None
        now = self._clock()
        if max_age_s is not None and now - float(entry.get("stored", 0)) > max_age_s:
            return None
        expires = entry.get("expires")
        if expires is not None and now > float(expires):
            return None
        return entry.get("value")

    def age(self, namespace: str, key: str) -> float | None:
        with self._lock:
            entry = self._memory.get((namespace, key))
        if entry is None and self.get(namespace, key) is None:
            return None
        with self._lock:
            entry = self._memory.get((namespace, key))
        return None if entry is None else self._clock() - float(entry.get("stored", 0))

    def put(self, namespace: str, key: str, value: dict, *, expires: float | None = None) -> None:
        entry = {"stored": self._clock(), "value": value}
        if expires is not None:
            entry["expires"] = expires
        with self._lock:
            if len(self._memory) >= self._max:
                self._memory.pop(next(iter(self._memory)))
            self._memory[(namespace, key)] = entry
        path = self._path(namespace, key)
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
                tmp.replace(path)
            except OSError as error:
                log.info("Job search cache not written: %s", type(error).__name__)

    def clear(self) -> None:
        with self._lock:
            self._memory.clear()


_DEFAULT = JobSearchCache()


def default_cache() -> JobSearchCache:
    """Process-wide in-memory cache (the integrator should pass one with a folder)."""
    return _DEFAULT
