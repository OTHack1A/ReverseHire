"""Recruiter analysis: local simulators of the screening systems a CV goes through
(parse, candidate record, knock-out questions, keyword searches, match score,
timeline, skills, languages, format notes), plus the posting <-> CV match score.

Every simulator is the same engine run with a `SystemProfile` from
`data/profiles.json`, and every result carries the label "approximation built
from public documentation". See fonti/RECRUITER_STUDY.md Part 1 and §2.7.

Public API for the GUI:
    CvDocument, Target, KnockoutAnswers             inputs
    simulate(cv, target, profile, ...)              -> SimulationResult
    simulate_all(cv, target, profiles=None, ...)    -> {profile key: SimulationResult}
    what_if(result, n=5)                            -> [Edit] (best 5 CV edits)
    score_after(result, edits)                      -> int
    match_posting(cv_profile, posting, ...)         -> MatchResult
    load_profiles(), get_profile(key), profile_for_platform(key), profile_for_host(host)
    analyse_layout(path)                            -> LayoutFacts (reuse across profiles)
    texts.render(key, params), texts.all_keys()     English defaults of the i18n keys
"""

from . import percent, texts
from .engine import score_after, simulate, simulate_all, what_if
from .layout import LayoutFacts, analyse_layout
from .match import MatchItem, MatchResult, match_posting
from .model import (CHECK_IDS, COMPONENTS, CvDocument, Edit, EvidenceRef, Finding, KnockoutAnswers, Message,
                    RecordField, RecordPreview, ScoreBreakdown, SimulationCancelled, SimulationResult, Target)
from .profiles import (ProfileError, SystemProfile, baseline, get_profile, load_profiles, load_profiles_from,
                       profile_for_host, profile_for_platform, validate_document, validate_profile)

__all__ = [
    "CHECK_IDS", "COMPONENTS", "CvDocument", "Edit", "EvidenceRef", "Finding", "KnockoutAnswers", "LayoutFacts",
    "MatchItem", "MatchResult", "Message", "ProfileError", "RecordField", "RecordPreview", "ScoreBreakdown",
    "SimulationCancelled", "SimulationResult", "SystemProfile", "Target", "analyse_layout", "baseline",
    "get_profile", "load_profiles", "load_profiles_from", "match_posting", "profile_for_host",
    "profile_for_platform", "score_after", "simulate", "simulate_all", "texts", "validate_document",
    "validate_profile", "what_if",
]
