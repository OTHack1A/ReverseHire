"""Building the Assessment: relevance, fit, confidence, explanations.

``outcome`` is a ``web.analysis.SiteOutcome`` or None; it is read by attribute
(``status``, ``postings``, ``company_text``, ``pages_read``, ``careers_url``,
``postings_source``) without importing the ``web`` package.

Confidence (0..1)::

    no site:    CONFIDENCE_WITHOUT_SITE (+ RICH_TAGS_CONFIDENCE if the company
                has many OSM tags)
    with site:  BASE + CONF_TEXT_WEIGHT * min(1, len(text) / FULL_TEXT)
                     + CONF_PAGES_WEIGHT * min(1, pages_read / FULL_PAGES)
                     + CONF_POSTINGS_WEIGHT * reliability(postings_source)
                capped at MAX_CONFIDENCE_UNREACHABLE if the site does not
                respond.

Besides relevance and fit, the Assessment carries ``desirability`` (the
candidate's preferences in ``Settings``), ``spontaneous`` (only without
postings), ``match`` (the sorting value) and, for the best posting,
``requirements``, ``blocked`` and ``blocked_reason``; formulas in
``scoring/preferences.py`` and ``scoring/requirements.py``.
"""

from __future__ import annotations

from ..models import HiringStatus, Assessment
from .fit import compute_fit
from .relevance import compute_relevance
from .explanation import (
    describe_check,
    preference_reasons_and_gaps,
    reasons_and_gaps,
    requirement_reasons_and_gaps,
    spontaneous_reason,
    _language,
)
from .preferences import compute_desirability, compute_match, compute_spontaneous

CONFIDENCE_WITHOUT_SITE = 0.15
RICH_TAGS_CONFIDENCE = 0.05
MIN_RICH_TAGS = 5
BASE_CONFIDENCE_WITH_SITE = 0.15
CONF_TEXT_WEIGHT = 0.35
CONF_PAGES_WEIGHT = 0.20
CONF_POSTINGS_WEIGHT = 0.30
FULL_TEXT = 3000  # characters of company_text that count as "full"
FULL_PAGES = 3
MAX_CONFIDENCE_UNREACHABLE = 0.2

# Reliability of the postings source (prefix -> 0..1).
SOURCE_RELIABILITY = {
    "ats:": 1.0,
    "jsonld": 0.9,
    "jobboard": 0.8,  # posting published on a job board (Indeed, LinkedIn)
    "heuristic": 0.5,
    "unsolicited": 0.4,
}
UNKNOWN_SOURCE_RELIABILITY = 0.3  # source "" or not recognized


def source_reliability(source: str) -> float:
    source = (source or "").lower()
    for prefix, value in SOURCE_RELIABILITY.items():
        if source.startswith(prefix):
            return value
    return UNKNOWN_SOURCE_RELIABILITY


def compute_confidence(company, outcome) -> float:
    if outcome is None:
        c = CONFIDENCE_WITHOUT_SITE
        from ..models import public_tags

        if len(public_tags(company.osm_tags)) >= MIN_RICH_TAGS:
            c += RICH_TAGS_CONFIDENCE
        return round(c, 3)
    text = getattr(outcome, "company_text", "") or ""
    pages = getattr(outcome, "pages_read", 0) or 0
    source = getattr(outcome, "postings_source", "") or ""
    c = (
        BASE_CONFIDENCE_WITH_SITE
        + CONF_TEXT_WEIGHT * min(1.0, len(text.strip()) / FULL_TEXT)
        + CONF_PAGES_WEIGHT * min(1.0, pages / FULL_PAGES)
        + CONF_POSTINGS_WEIGHT * source_reliability(source)
    )
    if getattr(outcome, "status", None) == HiringStatus.UNREACHABLE:
        c = min(c, MAX_CONFIDENCE_UNREACHABLE)
    return round(max(0.0, min(1.0, c)), 3)


def _max_travel_days(settings) -> int | None:
    value = getattr(settings, "max_travel_days_year", -1) if settings is not None else -1
    return None if value is None or value < 0 else int(value)


def assess(profile, company, outcome, embedder, ui_language: str = "it", *,
           settings=None, role_skills: list[str] | None = None) -> Assessment:
    """Full assessment of a company against the profile.

    ``settings`` (optional, keyword-only): ``config.Settings`` with the
    candidate's preferences and the search radius; None = defaults.
    ``role_skills`` (optional): ESCO essential skills of the candidate's
    occupation (``cv.esco.essential_skills``), used as "role coverage".
    """
    r_rel = compute_relevance(profile, company, outcome, embedder, role_skills=role_skills)
    postings = list(getattr(outcome, "postings", None) or []) if outcome is not None else []
    r_postings = compute_fit(profile, postings, embedder, role_skills=role_skills,
                             max_travel_days=_max_travel_days(settings))
    reasons, gaps = reasons_and_gaps(
        profile, company, outcome, r_rel, r_postings, ui_language
    )
    req_reasons, req_gaps = requirement_reasons_and_gaps(r_postings, ui_language)
    best = r_postings[0] if r_postings else None
    desirability = compute_desirability(
        company, best.posting if best else None, profile, settings, best.checks if best else None
    )
    pref_reasons, pref_gaps = preference_reasons_and_gaps(best.posting if best else None,
                                                          desirability, ui_language)
    spontaneous = None
    if best is None:
        spontaneous = compute_spontaneous(profile, company, outcome, r_rel)
        reason = spontaneous_reason(spontaneous, r_rel, ui_language)
        if reason is not None:
            if reason.source == "site":
                reason.source = (company.site or "site") if outcome is not None else "osm"
            reasons.append(reason)
    confidence = compute_confidence(company, outcome)
    fit = best.score if best else None
    blocked = bool(best and best.blocked)
    blocked_reason = None
    if blocked and best.blocked_by is not None:
        _, blocked_reason = describe_check(best.blocked_by, best.posting.title, _language(ui_language))
    match = compute_match(
        relevance=r_rel.score, fit=fit, desirability=desirability.score,
        spontaneous=None if spontaneous is None else spontaneous.score,
        confidence=confidence, blocked=blocked,
    )
    assessment = Assessment(
        relevance=r_rel.score,
        fit=fit,
        confidence=confidence,
        status=getattr(outcome, "status", HiringStatus.NOT_ASSESSED) if outcome is not None
        else HiringStatus.NOT_ASSESSED,
        reasons=reasons + req_reasons + pref_reasons,
        postings=[r.posting for r in r_postings],
        careers_url=getattr(outcome, "careers_url", None) if outcome is not None else None,
        gaps=req_gaps + gaps + pref_gaps,
        desirability=desirability.score,
        spontaneous=None if spontaneous is None else spontaneous.score,
        match=match,
        blocked=blocked,
        blocked_reason=blocked_reason,
        requirements=list(best.checks) if best else [],
    )
    if ui_language == "en":
        # Website sentences, posting titles and CV lines quoted in Italian or Ukrainian:
        # translated now (worker thread) for the display layer; the Assessment keeps
        # the original wording (evidence stays verifiable).
        from .. import display_text

        display_text.prewarm(display_text.assessment_texts(assessment), ui_language="en")
    return assessment
