"""Scoring of a posting against the CV, with an injectable scorer.

The sibling package `recruiter_sim` provides `match_posting(cv_profile, posting)`; it is
NOT imported here. The integrator passes it as `scorer`, e.g.

    from reversehire.recruiter_sim import match_posting
    search_jobs(..., scorer=lambda cv, p: match_posting(cv, p.to_model_posting()))

A scorer may return a number 0..100, a (score, why) pair, a dict with "score" and
optionally "why" / "blocked" / "reason", or an object with those attributes. Without
a scorer, `skills_overlap_scorer` is used: the share of the ad's skills found in the CV.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import lexicon
from ._util import contains_phrase, fold, log
from .models import Posting
from .texts import text

MAX_LISTED = 6
CV_SKILLS_CONSIDERED = 20


@dataclass
class ScoreResult:
    score: float | None
    why: list[str] = field(default_factory=list)
    blocked: bool = False
    reason: str | None = None
    details: dict = field(default_factory=dict)


def _cv_skill_forms(cv_profile) -> list[tuple[str, set[str]]]:
    """(display name, folded forms) of the CV skills."""
    result = []
    for item in list(getattr(cv_profile, "skills", None) or [])[:200]:
        if isinstance(item, str):
            values = [item]
        else:
            values = [getattr(item, "value", "") or "", getattr(item, "original", "") or ""]
        values = [v for v in values if v]
        if not values:
            continue
        forms = set()
        for value in values:
            forms.update(lexicon.skill_forms(value))
            for canonical in lexicon.extract_skills(value, limit=3):
                forms.update(lexicon.skill_forms(canonical))
        result.append((values[0], {f for f in forms if f}))
    return result


def skills_overlap_scorer(cv_profile, posting: Posting) -> ScoreResult:
    """Built-in fallback: skills in common between the ad and the CV."""
    cv = _cv_skill_forms(cv_profile)
    if not cv:
        return ScoreResult(None, [text("jobsearch.why.no_cv")])
    if posting.skills:
        matched = []
        for skill in posting.skills:
            forms = set(lexicon.skill_forms(skill))
            if any(forms & cv_forms for _, cv_forms in cv):
                matched.append(skill)
        share = len(matched) / len(posting.skills)
        why = [text("jobsearch.why.skills", n=len(matched), m=len(posting.skills),
                    list=", ".join(matched[:MAX_LISTED]) or "-")] if matched else [text("jobsearch.why.no_skills")]
        return ScoreResult(round(100 * share, 1), why,
                           details={"matched": matched, "requested": list(posting.skills), "method": "skills_overlap"})
    folded = fold(posting.title + "\n" + posting.description)
    top = cv[:CV_SKILLS_CONSIDERED]
    found = [name for name, forms in top if any(contains_phrase(folded, f) for f in forms)]
    share = len(found) / len(top) if top else 0.0
    score = round(min(60.0, 100 * share), 1)
    why = [text("jobsearch.why.skills_cv", n=len(found), list=", ".join(found[:MAX_LISTED]))] if found else \
        [text("jobsearch.why.no_skills")]
    return ScoreResult(score, why, details={"matched": found, "method": "cv_skills_in_text"})


def coerce(value) -> ScoreResult:
    """Any scorer output -> ScoreResult (unknown shapes -> score None)."""
    if isinstance(value, ScoreResult):
        return value
    if value is None:
        return ScoreResult(None)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return ScoreResult(float(value))
    if isinstance(value, tuple) and value:
        score = value[0] if isinstance(value[0], (int, float)) else None
        why = value[1] if len(value) > 1 else []
        return ScoreResult(float(score) if score is not None else None,
                           [str(w) for w in (why if isinstance(why, (list, tuple)) else [why]) if w])
    if isinstance(value, dict):
        get = value.get
    else:
        def get(name, default=None):
            return getattr(value, name, default)
    score = get("score")
    if score is None:
        score = get("total")
    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    why = get("why") or get("reasons") or []
    if isinstance(why, str):
        why = [why]
    blocked = bool(get("blocked", False))
    reason = get("reason") or get("blocked_reason")
    details = value if isinstance(value, dict) else {}
    return ScoreResult(score, [str(w) for w in why if w], blocked, str(reason) if reason else None,
                       {k: v for k, v in details.items() if k not in ("why",)} if details else {})


def score_posting(cv_profile, posting: Posting, scorer=None) -> ScoreResult:
    """Run the scorer (or the fallback); never raises."""
    if cv_profile is None:
        return ScoreResult(None, [text("jobsearch.why.no_cv")])
    if scorer is not None:
        try:
            result = coerce(scorer(cv_profile, posting))
            if result.score is not None:
                result.score = max(0.0, min(100.0, result.score))
                return result
        except Exception as error:
            log.warning("Injected scorer failed (%s): fallback used", type(error).__name__)
    return skills_overlap_scorer(cv_profile, posting)
