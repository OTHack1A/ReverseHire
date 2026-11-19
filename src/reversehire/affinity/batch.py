"""Batch affinity with progress, cancellation and a cache, plus JSON helpers for the store.

Cache key: (CV fingerprint, company data fingerprint, weights version, preferences
fingerprint, algorithm version). The CV fingerprint includes the month of ``today``
(skill recency is measured in months), so a table refreshes instantly while nothing
changed and recomputes only the companies whose card, the CV, the weights or the
preferences changed.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from datetime import date
from typing import Callable

from . import weights as W
from .demand import build_demand
from .engine import affinity, cv_context
from .model import ALGORITHM_VERSION, AffinityResult
from .preferences import AffinityPreferences

DEFAULT_CACHE_SIZE = 5000


def cv_fingerprint(cv_profile, today: date | None = None) -> str:
    today = today or date.today()
    data = cv_profile.to_dict() if hasattr(cv_profile, "to_dict") else repr(cv_profile)
    text = json.dumps({"cv": data, "month": f"{today.year}-{today.month:02d}"}, ensure_ascii=False, sort_keys=True,
                      default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class AffinityCache:
    """Thread-safe LRU of AffinityResult by key (in memory; persist with results_to_json)."""

    def __init__(self, size: int = DEFAULT_CACHE_SIZE):
        self.size = size
        self._data: OrderedDict[tuple, AffinityResult] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, key: tuple) -> AffinityResult | None:
        with self._lock:
            value = self._data.get(key)
            if value is None:
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return value

    def put(self, key: tuple, value: AffinityResult) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.size:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


def cache_key(cv_fp: str, data_fp: str, weights: W.Weights, prefs: AffinityPreferences) -> tuple:
    return (cv_fp, data_fp, weights.version, prefs.fingerprint(), ALGORITHM_VERSION)


def _cancelled(cancel) -> bool:
    if cancel is None:
        return False
    if hasattr(cancel, "is_set"):
        return cancel.is_set()
    return bool(cancel())


def affinity_for_companies(cv_profile, cards_or_results, preferences: AffinityPreferences | None = None, *,
                           weights: W.Weights | None = None, embedder=None, cache: AffinityCache | None = None,
                           progress: Callable[[int, int, str], None] | None = None, cancel=None,
                           today: date | None = None, sort: bool = False) -> list[AffinityResult]:
    """Affinity of the CV with each company (card, card row, CompanyDemand or a
    (Company, Assessment[, outcome]) tuple), in input order (``sort=True``: by
    ``weights.sort_key``). ``progress(done, total, company_name)`` after each company;
    ``cancel`` (threading.Event or callable) is checked between companies: the results
    computed so far are returned. A company whose data cannot be read is skipped."""
    prefs = preferences or AffinityPreferences()
    weights = weights or W.default_weights()
    today = today or date.today()
    items = list(cards_or_results or [])
    cv_fp = cv_fingerprint(cv_profile, today)
    context = None
    results: list[AffinityResult] = []
    for n, item in enumerate(items):
        if _cancelled(cancel):
            break
        demand = build_demand(item)
        key = cache_key(cv_fp, demand.fingerprint(), weights, prefs)
        result = cache.get(key) if cache is not None else None
        if result is None:
            if context is None:
                context = cv_context(cv_profile, embedder, today)
            result = affinity(cv_profile, demand, prefs, embedder=embedder, weights=weights, today=today,
                              context=context)
            result.fingerprints["cv"] = cv_fp
            if cache is not None:
                cache.put(key, result)
        results.append(result)
        if progress is not None:
            progress(n + 1, len(items), demand.name)
    if sort:
        results.sort(key=W.sort_key)
    return results


def results_to_json(results: list[AffinityResult]) -> str:
    return json.dumps([r.to_dict() for r in results], ensure_ascii=False, sort_keys=True)


def results_from_json(text: str) -> list[AffinityResult]:
    return [AffinityResult.from_dict(d) for d in json.loads(text or "[]")]
