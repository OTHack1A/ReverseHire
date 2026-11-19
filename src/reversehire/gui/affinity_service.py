"""The affinity of the loaded CV with every company shown in the app, computed off the GUI
thread, cached in memory and in the store (schema 5), recomputed when the CV, the company
data or the affinity settings change.

Owned by the main window as ``window.affinity_service`` (built in ``window_affinity``).
Public API for the other features (all calls on the GUI thread):

- ``result_for_card(card) -> AffinityResult | None``: the cached result of a saved card
  (``companies.CompanyCard`` or its row dict), never computed here; None while it is
  missing or stale (the card changed since).
- ``result_for(item)``: the same for any item: a card, a ``(Company, Assessment)`` result
  row, a ``models.Company`` (map, regional register) or an ``affinity.CompanyDemand``.
- ``request_cards(cards, on_done=None)`` / ``request(items, on_done=None)``: computes the
  missing results in batches on workers (``on_done()`` on the GUI thread when the items of
  this request are done). Already cached or queued items are not computed twice.
- ``results_for(items, request=True) -> list[AffinityResult | None]``: cached results in
  input order; the missing ones are requested in one go (what a table calls on redraw).
- ``compute_for_candidates(items, on_done, *, direction=None)``: results for things that
  are NOT saved cards; ``on_done(results)`` gets a list aligned with ``items`` (None for an
  item whose data cannot be read). ``direction``: the id of one career direction
  (``AffinityPreferences.directions``) to judge the companies for that direction only.
- ``sort_key(result)``: ascending key "postings first, then spontaneous" (the affinity
  ``weights.sort_key``); None (not computed) sorts last.
- ``fingerprint()``: CV fingerprint + settings fingerprint ("" without a CV).
- ``record_feedback(card_or_key, result, agree, user_verdict=None)``,
  ``feedback_for(card_or_key) -> dict | None``, ``feedback_count() -> int``.
- ``listeners``: callables ``listener()`` called whenever results (or judgements) change,
  e.g. to redraw a table. ``on_progress(done, total)``, ``on_finished()`` and
  ``on_error(error)`` are set by the window (bottom bar, notice).
- ``key_of(item)``: the store key of an item ("card:<card key>" for a saved card,
  "result:<card key>" for a (Company, Assessment) row, "company:<card key>" for a bare
  models.Company, "demand:<id>" for a CompanyDemand).

Without a CV nothing is computed (``has_cv()`` False); the tables show empty cells.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import sqlite3
import threading
from collections import OrderedDict
from datetime import date
from typing import Callable

from .. import affinity as aff
from ..affinity import weights as W
from ..affinity.model import AffinityResult
from .common import run_job

log = logging.getLogger(__name__)

BATCH_SIZE = 20          # items per worker: the first results of a list appear quickly
BATCH_TIMEOUT_S = 180.0  # deadline of one batch (the window stops waiting, cancel is set)
MAX_MEMORY = 6000        # results kept in memory
CANDIDATES_TIMEOUT_S = 300.0


def sort_key(result) -> tuple:
    """Ascending key: APPLY and SPONTANEOUS first (by value), then WATCH, NOT_A_FIT,
    UNKNOWN; no result at all last."""
    if result is None:
        return (9,)
    return W.sort_key(result)


def _get(obj, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def is_card(item) -> bool:
    """A saved company card (CompanyCard or its row dict)."""
    if isinstance(item, dict):
        return "key" in item and ("products" in item or "hiring" in item)
    return hasattr(item, "key") and hasattr(item, "products") and hasattr(item, "hiring")


def key_of(item) -> str:
    """Store key of an item (see the module docstring)."""
    if isinstance(item, str):
        return item
    if is_card(item):
        return "card:" + str(_get(item, "key", "") or _get(item, "id", ""))
    if isinstance(item, aff.CompanyDemand):
        return "demand:" + item.company_id
    prefix = "result:" if isinstance(item, tuple) else "company:"
    company = item[0] if isinstance(item, tuple) else item
    from ..companies import card_key

    try:
        return prefix + card_key(company)
    except Exception:  # noqa: BLE001 - an odd object still gets a key
        return prefix + str(_get(company, "name", "") or id(company))


def token_of(item) -> str:
    """A cheap identity of the item's data (no demand built): a changed card or result
    gets a new token, so its cached result is not used."""
    if is_card(item):
        return f"{_get(item, 'id', '')}:{_get(item, 'updated', '')}"
    if isinstance(item, aff.CompanyDemand):
        return item.fingerprint()
    company = item[0] if isinstance(item, tuple) else item
    assessment = item[1] if isinstance(item, tuple) and len(item) > 1 else None
    postings = list(_get(assessment, "postings", None) or [])
    status = _get(assessment, "status", None)
    parts = [str(_get(company, "name", "")), str(_get(company, "site", "") or ""),
             str(_get(company, "distance_km", "")), str(sorted((_get(company, "ids", None) or {}).items())),
             str(getattr(status, "value", status) or ""), str(len(postings)),
             str(sum(len(str(_get(p, "description", "") or "")) for p in postings)),
             str(len(_get(assessment, "reasons", None) or []))]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def preferences_of(settings) -> aff.AffinityPreferences:
    base = aff.AffinityPreferences.from_dict(getattr(settings, "affinity_preferences", None) or {})
    return aff.AffinityPreferences.from_settings(settings, base)


def weights_of(settings) -> aff.Weights:
    return aff.Weights.from_dict(getattr(settings, "affinity_weights", None) or {})


def only_direction(prefs: aff.AffinityPreferences, direction: str | None) -> aff.AffinityPreferences:
    """A copy of the preferences with only `direction` enabled (unchanged when unknown)."""
    if not direction or not any(d.id == direction for d in prefs.directions):
        return prefs
    copy_ = aff.AffinityPreferences.from_dict(prefs.to_dict())
    for d in copy_.directions:
        d.enabled = d.id == direction
    return copy_


def compute_items(profile, items: list[tuple[str, object, str]], *, prefs, weights, cv_fp: str, settings_fp: str,
                  store=None, embedder=None, today: date | None = None, context=None, cancel=None,
                  save: bool = True) -> tuple[dict, object]:
    """Worker body: {key: (token, result or None)} for `items` [(key, item, token)], and the
    CV context (reused by the next batch). A stored result is used when its company data
    fingerprint is unchanged; a new one is saved."""
    out: dict[str, tuple[str, AffinityResult | None]] = {}
    for key, item, token in items:
        if cancel is not None and cancel.is_set():
            break
        try:
            demand = aff.build_demand(item)
            data_fp = demand.fingerprint()
            result = None
            if store is not None:
                row = store.affinity_read(key, cv_fp, settings_fp)
                if row is not None and row.get("data_fp") == data_fp:
                    try:
                        result = AffinityResult.from_json(row["result"])
                    except (ValueError, KeyError, TypeError):
                        result = None
            if result is None:
                if context is None:
                    context = aff.cv_context(profile, embedder, today)
                result = aff.affinity(profile, demand, prefs, embedder=embedder, weights=weights, today=today,
                                      context=context)
                result.fingerprints["cv"] = cv_fp
                if cancel is not None and cancel.is_set():
                    break  # the window is closing: its store may already be closed
                if store is not None and save:
                    store.affinity_save(key, cv_fp, settings_fp, data_fp, result.to_json(), verdict=result.verdict,
                                        affinity=result.affinity)
            out[key] = (token, result)
        except sqlite3.ProgrammingError:
            # the store was closed meanwhile (window closed): nothing to save, no traceback
            log.info("Affinity batch stopped: the store was closed")
            break
        except Exception:  # noqa: BLE001 - one unreadable company must not stop the others
            log.warning("Affinity not computed for one company", exc_info=True)
            out[key] = (token, None)
    return out, context


class _Request:
    def __init__(self, keys: list[str], on_done: Callable | None):
        self.keys = keys
        self.on_done = on_done


class AffinityService:
    """See the module docstring."""

    def __init__(self, run_in_background: Callable, store, *, settings_provider: Callable[[], object],
                 embedder_provider: Callable[[], object] | None = None, semantic: Callable[[], bool] | None = None,
                 today: date | None = None):
        self._run = run_in_background
        self._store = store
        self._settings_provider = settings_provider
        self._embedder_provider = embedder_provider
        self._semantic = semantic
        self._today = today
        self.listeners: list[Callable[[], None]] = []
        self.on_progress: Callable[[int, int], None] | None = None
        self.on_finished: Callable[[], None] | None = None
        self.on_error: Callable[[BaseException], None] | None = None
        self._profile = None
        self._cv_fp = ""
        self._settings_fp = ""
        self._mode = "taxonomy"
        self.prefs = aff.AffinityPreferences()
        self.weights = aff.Weights()
        self._generation = 0
        self._memory: OrderedDict[str, tuple[str, AffinityResult]] = OrderedDict()
        self._failed: dict[str, str] = {}  # key -> token whose computation failed
        self._queue: OrderedDict[str, tuple[object, str]] = OrderedDict()
        self._requests: list[_Request] = []
        self._running = False
        self._batch_keys: set[str] = set()
        self._cancel: threading.Event | None = None
        self._context = None  # (generation, CvContext) reused between batches
        self._done = self._total = 0
        self._feedback: dict[str, dict] | None = None
        self._closed = False
        self.reload_settings()

    # --- state -----------------------------------------------------------------------------

    @property
    def profile(self):
        return self._profile

    def has_cv(self) -> bool:
        return self._profile is not None

    def today(self) -> date:
        return self._today or date.today()

    def fingerprint(self) -> str:
        return f"{self._cv_fp}:{self._settings_fp}" if self._cv_fp else ""

    @property
    def running(self) -> bool:
        return self._running

    def _settings(self):
        try:
            return self._settings_provider()
        except Exception:  # noqa: BLE001
            from .. import config

            return config.Settings()

    def _semantic_mode(self) -> str:
        if self._semantic is None or self._embedder_provider is None:
            return "taxonomy"
        try:
            return "semantic" if self._semantic() else "taxonomy"
        except Exception:  # noqa: BLE001
            return "taxonomy"

    def reload_settings(self) -> bool:
        """Reads the preferences and weights from the settings; True when they changed
        (then everything is recomputed)."""
        settings = self._settings()
        prefs, weights = preferences_of(settings), weights_of(settings)
        mode = self._semantic_mode()
        # the algorithm version too: results stored by older rules are recomputed
        from ..affinity.model import ALGORITHM_VERSION

        fp = hashlib.sha256(f"{weights.version}|{prefs.fingerprint()}|{mode}|{ALGORITHM_VERSION}".encode()
                            ).hexdigest()[:16]
        if fp == self._settings_fp:
            return False
        self.prefs, self.weights, self._mode, self._settings_fp = prefs, weights, mode, fp
        self._reset()
        return True

    def settings_changed(self) -> bool:
        """Call after the settings were saved: recomputes when the affinity settings changed."""
        if not self.reload_settings():
            return False
        log.info("Affinity settings changed: results are recomputed")
        self._notify()
        return True

    def set_profile(self, profile) -> None:
        """A new CV (or None): every result is recomputed."""
        self._profile = profile
        self._cv_fp = aff.cv_fingerprint(profile, self.today()) if profile is not None else ""
        self._mode = self._semantic_mode()
        self._reset()
        self._notify()
        if profile is not None and self._store is not None:
            store, cv_fp, settings_fp = self._store, self._cv_fp, self._settings_fp
            # results of older CVs / settings are dropped from the store (in a worker)
            run_job(self._run, lambda: store.affinity_prune(cv_fp, settings_fp), lambda _n: None, lambda _e: None,
                    name="affinity store cleanup", timeout=60)

    def _reset(self) -> None:
        self._generation += 1
        self._memory.clear()
        self._failed.clear()
        self._queue.clear()
        self._requests = []
        self._context = None
        self._done = self._total = 0
        if self._cancel is not None:
            self._cancel.set()
        if not self._running and self.on_finished is not None:
            self._call(self.on_finished)

    def close(self) -> None:
        self._closed = True
        if self._cancel is not None:
            self._cancel.set()
        self._queue.clear()

    # --- lookups ---------------------------------------------------------------------------

    def result_for(self, item) -> AffinityResult | None:
        if self._profile is None or item is None:
            return None
        entry = self._memory.get(key_of(item))
        if entry is None or entry[0] != token_of(item):
            return None
        return entry[1]

    def result_for_card(self, card) -> AffinityResult | None:
        return self.result_for(card)

    def is_pending(self, item) -> bool:
        """True while the item's result is queued or being computed."""
        if self._profile is None:
            return False
        key = key_of(item)
        return key in self._queue or (self._running and key in self._batch_keys)

    def is_failed(self, item) -> bool:
        return self._failed.get(key_of(item)) == token_of(item)

    def results_for(self, items, request: bool = True) -> list[AffinityResult | None]:
        items = list(items or [])
        results = [self.result_for(i) for i in items]
        if request and self._profile is not None:
            missing = [i for i, r in zip(items, results) if r is None]
            if missing and self.request(missing):
                # a synchronous runner (or a cached store row) may have answered already
                results = [r if r is not None else self.result_for(i) for i, r in zip(items, results)]
        return results

    # --- computation -----------------------------------------------------------------------

    def request_cards(self, cards, on_done: Callable[[], None] | None = None) -> bool:
        return self.request(cards, on_done)

    def request(self, items, on_done: Callable[[], None] | None = None) -> bool:
        """Queues the items without a current result; False when nothing has to be computed
        (``on_done`` is then called at once)."""
        if self._profile is None or self._closed:
            if on_done is not None:
                self._call(on_done)
            return False
        keys = []
        added = 0
        for item in items or []:
            if item is None:
                continue
            key, token = key_of(item), token_of(item)
            entry = self._memory.get(key)
            if entry is not None and entry[0] == token:
                continue
            if self._failed.get(key) == token:
                continue
            keys.append(key)
            if key not in self._queue or self._queue[key][1] != token:
                if key not in self._queue:
                    added += 1
                self._queue[key] = (item, token)
        if not keys:
            if on_done is not None:
                self._call(on_done)
            return False
        if on_done is not None:
            self._requests.append(_Request(keys, on_done))
        self._total += added
        self._kick()
        return True

    def _kick(self) -> None:
        if self._running or not self._queue or self._profile is None or self._closed:
            return
        batch = []
        while self._queue and len(batch) < BATCH_SIZE:
            key, (item, token) = self._queue.popitem(last=False)
            batch.append((key, item, token))
        self._batch_keys = {k for k, _i, _t in batch}
        self._running = True
        generation, profile = self._generation, self._profile
        prefs, weights = self.prefs, self.weights
        cv_fp, settings_fp, store = self._cv_fp, self._settings_fp, self._store
        context = self._context[1] if self._context and self._context[0] == generation else None
        semantic, provider, today = self._mode == "semantic", self._embedder_provider, self.today()
        cancel = threading.Event()
        self._cancel = cancel

        def work():
            embedder = None
            if semantic and provider is not None and context is None:
                try:
                    embedder = provider()
                except Exception:  # noqa: BLE001 - the semantic part is optional
                    embedder = None
            return compute_items(profile, batch, prefs=prefs, weights=weights, cv_fp=cv_fp,
                                 settings_fp=settings_fp, store=store, embedder=embedder, today=today,
                                 context=context, cancel=cancel)

        def done(value) -> None:
            results, ctx = value
            self._batch_done(generation, batch, results, ctx)

        def failed(error: BaseException) -> None:
            log.warning("Affinity batch failed: %s", type(error).__name__)
            self._batch_done(generation, batch, {k: (t, None) for k, _i, t in batch}, None)
            if generation == self._generation and self.on_error is not None:
                self._call(self.on_error, error)

        run_job(self._run, work, done, failed, name=f"affinity ({len(batch)} companies)",
                timeout=BATCH_TIMEOUT_S, cancel=cancel)

    def _batch_done(self, generation: int, batch, results: dict, ctx) -> None:
        self._running = False
        self._batch_keys = set()
        if self._closed:
            return
        if generation == self._generation:
            if ctx is not None:
                self._context = (generation, ctx)
            for key, item, token in batch:
                token_result = results.get(key)
                if token_result is None:  # cancelled before it was reached: queued again
                    if key not in self._queue:
                        self._queue[key] = (item, token)
                    continue
                result = token_result[1]
                if result is None:
                    self._failed[key] = token
                else:
                    self._memory[key] = (token, result)
                    self._memory.move_to_end(key)
                self._done += 1
            while len(self._memory) > MAX_MEMORY:
                self._memory.popitem(last=False)
            if self.on_progress is not None and self._total:
                self._call(self.on_progress, min(self._done, self._total), self._total)
            self._finish_requests()
            self._notify()
        self._kick()
        if not self._running and not self._queue:
            self._done = self._total = 0
            if self.on_finished is not None:
                self._call(self.on_finished)

    def _finish_requests(self) -> None:
        waiting = []
        for request in self._requests:
            if any(k in self._queue or k in self._batch_keys for k in request.keys):
                waiting.append(request)
            else:
                self._call(request.on_done)
        self._requests = waiting

    def compute_for_candidates(self, items, on_done: Callable[[list], None], *, direction: str | None = None) -> bool:
        """Results for companies that are not saved cards (see the module docstring)."""
        items = list(items or [])
        if self._profile is None or self._closed:
            self._call(on_done, [None] * len(items))
            return False
        if not direction:
            def ready() -> None:
                self._call(on_done, [self.result_for(i) for i in items])

            self.request(items, ready)
            return True
        prefs = only_direction(self.prefs, direction)
        generation, profile, weights, today = self._generation, self._profile, self.weights, self.today()
        cv_fp = self._cv_fp
        settings_fp = hashlib.sha256(f"{self._settings_fp}|{direction}".encode()).hexdigest()[:16]
        batch = [(key_of(i), i, token_of(i)) for i in items]
        store = self._store

        def work():
            results, _ctx = compute_items(profile, batch, prefs=prefs, weights=weights, cv_fp=cv_fp,
                                          settings_fp=settings_fp, store=store, today=today, save=False)
            return [results.get(k, ("", None))[1] for k, _i, _t in batch]

        def done(results) -> None:
            if generation == self._generation:
                self._call(on_done, results)

        def failed(error: BaseException) -> None:
            log.warning("Affinity for one direction failed: %s", type(error).__name__)
            self._call(on_done, [None] * len(items))

        run_job(self._run, work, done, failed, name=f"affinity for one direction ({len(items)} companies)",
                timeout=CANDIDATES_TIMEOUT_S)
        return True

    @staticmethod
    def sort_key(result) -> tuple:
        return sort_key(result)

    # --- feedback --------------------------------------------------------------------------

    def _feedback_map(self) -> dict[str, dict]:
        if self._feedback is None:
            self._feedback = {}
            if self._store is not None:
                try:
                    for row in self._store.feedback_rows():
                        self._feedback[row["company_key"]] = row
                except Exception:  # noqa: BLE001 - the judgements are optional
                    log.warning("Affinity judgements not read", exc_info=True)
        return self._feedback

    def feedback_for(self, card_or_key) -> dict | None:
        return self._feedback_map().get(key_of(card_or_key))

    def feedback_count(self) -> int:
        return len(self._feedback_map())

    def record_feedback(self, card_or_key, result: AffinityResult | None, agree: bool,
                        user_verdict: str | None = None) -> dict:
        """Saves one judgement of the verdict shown (latest per company wins)."""
        import time

        key = key_of(card_or_key)
        verdict = result.verdict if result is not None else ""
        row = {"company_key": key, "agrees": bool(agree), "user_verdict": user_verdict, "verdict": verdict,
               "cv_fp": self._cv_fp, "result": result.to_json() if result is not None else "{}",
               "created": time.time()}
        self._feedback_map()[key] = row
        log.info("Affinity judgement recorded: %s with the verdict %s", "agrees" if agree else "disagrees",
                 verdict or "-")
        store = self._store
        if store is not None:
            def save():
                return store.feedback_add(key, bool(agree), verdict=verdict, user_verdict=user_verdict,
                                          cv_fp=row["cv_fp"], result_json=row["result"], when=row["created"])

            def failed(error: BaseException) -> None:
                log.warning("Affinity judgement not saved: %s", type(error).__name__)
                if self.on_error is not None:
                    self._call(self.on_error, error)

            run_job(self._run, save, lambda _id: None, failed, name="affinity judgement", timeout=30)
        self._notify()
        return row

    def calibration_inputs(self) -> tuple[list, list]:
        """(feedback, results) of the stored judgements, for ``affinity.calibrate``
        (company_id = the store key). Reads the store: call it in a worker."""
        rows = self._store.feedback_rows() if self._store is not None else list(self._feedback_map().values())
        return calibration_inputs(rows)

    # --- helpers ---------------------------------------------------------------------------

    def _notify(self) -> None:
        for listener in list(self.listeners):
            self._call(listener)

    @staticmethod
    def _call(function, *args) -> None:
        try:
            function(*args)
        except Exception:  # noqa: BLE001 - a listener must never break the service
            log.warning("Affinity listener failed", exc_info=True)


def calibration_inputs(rows: list[dict]) -> tuple[list, list]:
    """(Feedback list, AffinityResult list) from feedback rows (latest per company)."""
    latest: dict[str, dict] = {}
    for row in sorted(rows, key=lambda r: (r.get("created") or 0, r.get("id") or 0)):
        latest[row["company_key"]] = row
    feedback, results = [], []
    for key, row in sorted(latest.items()):
        try:
            result = AffinityResult.from_json(row.get("result") or "{}")
        except (ValueError, KeyError, TypeError):
            continue
        result = copy.copy(result)
        result.company_id = key
        verdict = row.get("user_verdict")
        feedback.append(aff.Feedback(key, bool(row.get("agrees")), verdict if verdict in W.VERDICTS else None,
                                     float(row.get("created") or 0.0)))
        results.append(result)
    return feedback, results
