"""Posting <-> CV match score ("Match with my CV") wherever postings are listed.

The score is `recruiter_sim.match_posting(cv_profile, posting)`: more skills in common
with the ad give a higher score. It is computed in a worker (a CV index is built once
per CV) and cached per posting, so the tables never wait for it: they show "…" until
the value arrives and are redrawn by the `on_ready` callback.

The company-level scores of the results (match, desirability, relevance, fit) are a
different thing and are left as they are.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from ..i18n import t
from .common import run_job

log = logging.getLogger(__name__)

PENDING = "…"
MAX_LISTED = 8
MATCH_TIMEOUT_S = 120.0


def posting_identity(posting) -> str:
    """Cache key of a posting (card dict, models.Posting or jobsearch Posting)."""
    get = posting.get if isinstance(posting, dict) else (lambda name, default="": getattr(posting, name, default))
    return "|".join(str(get(name, "") or "") for name in ("url", "title", "location"))


def _as_target(posting):
    """What match_posting reads: a posting object, or a Target built from a card dict."""
    if not isinstance(posting, dict):
        return posting
    from ..recruiter_sim import Target

    return Target(title=str(posting.get("title") or ""), description=str(posting.get("description") or ""),
                  location=str(posting.get("location") or ""), kind="posting")


def badge_percent(result) -> int | None:
    """The match as an integer 0..100 (None without a result)."""
    if result is None:
        return None
    try:
        return int(round(max(0.0, min(100.0, float(result.score)))))
    except (TypeError, ValueError):
        return None


def badge_text(result) -> str:
    """"72 %" (the match, always 0-100 %), "–" without a result."""
    value = badge_percent(result)
    return "–" if value is None else f"{value} %"


def summary_text(result) -> str:
    """"3 skills in common out of 5" (the owner's definition: more in common = higher)."""
    if result is None:
        return ""
    return t("match.common", n=result.common, m=result.total)


def tooltip_text(result) -> str:
    """Skills in common and missing, for the tooltip of the badge."""
    if result is None:
        return t("match.no_cv")
    lines = [t("match.badge_tooltip", score=badge_percent(result)), summary_text(result)]
    common = [i.text for i in result.in_common][:MAX_LISTED]
    partial = [i.text for i in result.partial][:MAX_LISTED]
    missing = [i.text for i in (result.missing_must + result.missing_nice + result.missing_other)][:MAX_LISTED]
    lines.append(t("match.in_common", items=", ".join(common) if common else "–"))
    if partial:
        lines.append(t("match.partial", items=", ".join(partial)))
    lines.append(t("match.missing", items=", ".join(missing) if missing else "–"))
    if result.capped and result.cap_reason:
        lines.append(t("match.capped", cap=result.cap, reason=result.cap_reason))
    lines.append(t("match.source"))
    return "\n".join(lines)


class MatchService:
    """Match results of postings against the current CV, computed off the GUI thread."""

    def __init__(self, run_in_background: Callable):
        self._run = run_in_background
        self._profile = None
        self._index = None
        self._generation = 0
        self._cache: dict[str, object] = {}
        self._pending: set[str] = set()
        self._lock = threading.Lock()

    @property
    def profile(self):
        return self._profile

    def set_profile(self, profile) -> None:
        """A new CV: the cache is emptied (the scores belong to the previous CV)."""
        with self._lock:
            self._generation += 1
            self._profile = profile
            self._index = None
            self._cache.clear()
            self._pending.clear()

    def has_cv(self) -> bool:
        return self._profile is not None

    def cached(self, posting):
        return self._cache.get(posting_identity(posting))

    def is_known(self, posting) -> bool:
        """True once the match was computed (its result may be None: not computable)."""
        return posting_identity(posting) in self._cache

    def compute_now(self, posting):
        """Computes (or returns the cached) match on the calling thread: for workers only."""
        key = posting_identity(posting)
        if key in self._cache:
            return self._cache[key]
        profile, generation = self._profile, self._generation
        if profile is None:
            return None
        result = self._compute(profile, posting)
        with self._lock:
            if generation == self._generation:
                self._cache[key] = result
        return result

    def _compute(self, profile, posting):
        from ..recruiter_sim import match_posting
        from ..recruiter_sim.cvindex import CvIndex

        if self._index is None:
            self._index = CvIndex.build(getattr(profile, "text", "") or "")
        try:
            result = match_posting(profile, _as_target(posting), index=self._index)
        except Exception:  # noqa: BLE001 - one odd posting must not stop the others
            log.warning("Match with the CV not computed for one posting", exc_info=True)
            return None
        # An ad with no requirement at all (a careers-page link with a title only) has
        # nothing to compare: "not computable", never a score of 0.
        return result if result.items else None

    def request(self, postings, on_ready: Callable[[], None]) -> bool:
        """Computes the missing matches in a worker; `on_ready()` runs on the GUI thread
        when they are in the cache. False when nothing had to be computed."""
        if self._profile is None:
            return False
        todo = []
        for posting in postings or []:
            key = posting_identity(posting)
            if key not in self._cache and key not in self._pending:
                todo.append((key, posting))
                self._pending.add(key)
        if not todo:
            return False
        profile, generation = self._profile, self._generation

        def work():
            results = {}
            for key, posting in todo:
                results[key] = self._compute(profile, posting)
            return results

        def done(results) -> None:
            with self._lock:
                if generation != self._generation:
                    return
                self._cache.update(results)
                self._pending.difference_update(results)
            on_ready()

        def failed(_error) -> None:
            with self._lock:
                self._pending.difference_update(key for key, _p in todo)

        run_job(self._run, work, done, failed, name=f"match with the CV ({len(todo)} postings)",
                timeout=MATCH_TIMEOUT_S)
        return True

    def scorer(self):
        """A `search_jobs(scorer=...)` callable for the job search worker (None without CV)."""
        if self._profile is None:
            return None

        def score(_cv_profile, posting):
            result = self.compute_now(posting)
            if result is None:
                return None
            why = [summary_text(result)]
            return {"score": float(result.score), "why": why, "blocked": bool(result.capped),
                    "reason": result.cap_reason or None, "matched": [i.text for i in result.in_common],
                    "missing": [i.text for i in result.missing_must + result.missing_nice]}

        return score
