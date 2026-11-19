"""The simulator engine: one pipeline, parameterised by a `SystemProfile`.

`simulate(cv, target, profile, ...)` runs the checks of study §1.7 in order and
builds check 11, the simulated ATS score (study §1.5, explainable):

    score = weighted mean of the assessed components (renormalised):
      parse 0.20        parse quality (0.6) + record round trip (0.4)
      findability 0.25  share of the recruiter searches that find the CV
      match 0.35        the §2.7 match score (0.25 coverage + 0.10 recency of the
                        study; recency applies only when the profile says so)
      timeline 0.10     experience summary penalties
      credentials 0.10  languages with level, degree, certifications asked
    knock-out: a failed question caps the score at `knockout.cap_on_fail`
    (20 by default, "a knock-out rejects before any score"); mode "scored"
    instead mixes the screening answers in with `screening_weight`.

Pure functions, no network, no GUI. Long steps (file layout, record
re-extraction) are meant for a worker thread: `progress(step, done, total)` is
called before each step and `cancel()` is checked between steps (raising
`SimulationCancelled`).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import date

from .cvindex import LITERAL, SEMANTIC, TAXONOMY, CvIndex
from .findability import build_queries, findability_findings, run_queries
from .knockout import infer_knockouts, knockout_findings
from .layout import analyse_layout
from .match import CREDIT, MatchResult, _Embeddings, match_posting, score_items, term_evidence
from .model import (COMPONENT_CREDENTIALS, COMPONENT_FINDABILITY, COMPONENT_MATCH, COMPONENT_PARSE,
                    COMPONENT_TIMELINE, COMPONENTS, CHECK_SCORE, FAIL, OK, WARN, CvDocument, Edit, Finding,
                    KnockoutAnswers, ScoreBreakdown, SimulationCancelled, SimulationResult, Target, msg,
                    sort_findings)
from .other_checks import ai_findings, credential_findings, format_findings, semantic_findings
from .parse_view import PARSE_SHARE, RECORD_SHARE, build_parsed_view, parse_findings, parse_quality
from .profiles import SystemProfile, load_profiles
from .record import build_record, record_findings
from .timeline import skill_recency_findings, timeline_findings

DEFAULT_WEIGHTS = {COMPONENT_PARSE: 0.20, COMPONENT_FINDABILITY: 0.25, COMPONENT_MATCH: 0.35,
                   COMPONENT_TIMELINE: 0.10, COMPONENT_CREDENTIALS: 0.10}
METHOD_MODES = {"keyword_share": LITERAL, "taxonomy_match": TAXONOMY, "semantic": SEMANTIC}
BAND_TOP, BAND_MIDDLE = 70, 45
LABEL_HIGH, LABEL_MEDIUM = 70, 45
MAX_TOP_FIXES = 10
MIN_POINTS = 0.05
STEPS = ("layout", "parse", "record", "match", "knockout", "findability", "timeline", "credentials",
         "format", "score")

Progress = Callable[[str, int, int], None]
Cancel = Callable[[], bool]


def _notify(progress: Progress | None, step: str) -> None:
    if progress is None:
        return
    try:
        progress(step, STEPS.index(step), len(STEPS))
    except Exception:  # noqa: BLE001 - a broken callback never stops the simulation
        pass


def _check_cancel(cancel: Cancel | None) -> None:
    if cancel is not None and cancel():
        raise SimulationCancelled()


def _as_target(target) -> Target:
    if isinstance(target, Target):
        return target
    if isinstance(target, str):
        return Target.from_role(target)
    return Target.from_posting(target)


# --- score ----------------------------------------------------------------------------------


def weights_for(profile: SystemProfile) -> dict[str, float]:
    weights = dict(DEFAULT_WEIGHTS)
    if profile.score.weights:
        weights.update({k: float(v) for k, v in profile.score.weights.items()})
    return weights


def combine(components: dict[str, float | None], weights: dict[str, float]) -> float:
    """Weighted mean over the assessed components (None = not assessed)."""
    total = sum(weights.get(c, 0.0) for c, v in components.items() if v is not None)
    if not total:
        return 0.0
    return sum(weights.get(c, 0.0) * v for c, v in components.items() if v is not None) / total


def _grade(value: float) -> str:
    return "A" if value >= 75 else "B" if value >= 55 else "C" if value >= 35 else "D"


def display_for(kind: str, value: int, match: MatchResult | None):
    musts = match.musts if match is not None else []
    met = sum(1 for i in musts if i.status == "matched")
    if kind == "percent":
        return msg("rsim.score.display.percent", value=value)
    if kind == "grade_AD":
        return msg("rsim.score.display.grade_AD", grade=_grade(value))
    if kind == "high_med_low":
        level = "high" if value >= LABEL_HIGH else "medium" if value >= LABEL_MEDIUM else "low"
        return msg("rsim.score.display.high_med_low", label=msg(f"rsim.score.label.{level}").default)
    if kind in ("criteria_met", "none") and not musts:
        # a target without must-have requirements (a role typed without skills): "0 of 0
        # criteria met" beside "near the top of the list" read as a contradiction
        return msg("rsim.score.display.no_criteria")
    if kind == "criteria_met":
        return msg("rsim.score.display.criteria_met", met=met, total=len(musts))
    if kind == "rating_points":
        return msg("rsim.score.display.rating_points", points=round(value / 20, 1))
    return msg("rsim.score.display.none", met=met, total=len(musts))


def build_score(components: dict[str, float | None], profile: SystemProfile, knockout_outcome,
                match: MatchResult | None) -> ScoreBreakdown:
    weights = weights_for(profile)
    raw = combine(components, weights)
    explanation = []
    mode = profile.knockout.mode
    capped, cap = False, None
    value = raw
    failed = knockout_outcome.failed if knockout_outcome is not None else []
    if mode == "scored" and profile.knockout.screening_weight:
        share = profile.knockout.screening_weight
        value = (1 - share) * raw + share * knockout_outcome.passed_share
        explanation.append(msg("rsim.score.screening", share=round(share * 100)))
    elif failed and mode != "none" and profile.knockout.cap_on_fail is not None:
        cap = profile.knockout.cap_on_fail
        if value > cap:
            value, capped = float(cap), True
        reason = msg(f"rsim.knockout.topic.{failed[0].topic}").default
        explanation.insert(0, msg("rsim.score.capped", cap=cap, reason=reason))
    final = int(round(max(0.0, min(100.0, value))))
    if failed and mode == "auto_disqualify":
        band = "rejected"
    elif failed and mode in ("manual_review", "none"):
        band = "at_risk"
    else:
        band = "top" if final >= BAND_TOP else "middle" if final >= BAND_MIDDLE else "bottom"
    display = display_for(profile.score.kind, final, match)
    explanation.insert(0, msg("rsim.score.value", score=final, display=display))  # translated with the rest
    total = sum(weights.get(c, 0.0) for c, v in components.items() if v is not None) or 1.0
    for c in COMPONENTS:
        v = components.get(c)
        if v is not None:
            explanation.append(msg("rsim.score.component",
                                   component=msg(f"rsim.score.component.{c}").default, value=round(v),
                                   weight=round(100 * weights.get(c, 0.0) / total)))
    breakdown = ScoreBreakdown(final, dict(components), weights, capped, cap, display, band, explanation,
                               kind=profile.score.kind)
    breakdown.raw = raw  # uncapped, before screening answers
    return breakdown


def edit_points(edit: Edit, score: ScoreBreakdown) -> float:
    """Points of simulated score (uncapped) the edit adds."""
    before = combine(score.components, score.weights)
    after_components = dict(score.components)
    for c, gain in edit.gains.items():
        if after_components.get(c) is not None and gain:
            after_components[c] = min(100.0, after_components[c] + gain)
    return round(combine(after_components, score.weights) - before, 2)


def collect_edits(findings: list[Finding], score: ScoreBreakdown) -> list[Edit]:
    """All candidate edits of the findings, merged by message and ordered by points
    (supported edits before conditional ones on a tie)."""
    merged: dict[tuple, Edit] = {}
    for f in findings:
        for e in f.edits:
            key = (e.message.key, tuple(sorted((k, str(v)) for k, v in e.message.params.items())))
            if key in merged:
                old = merged[key]
                for c, g in e.gains.items():
                    old.gains[c] = max(old.gains.get(c, 0.0), g)
                old.evidence.extend(x for x in e.evidence if x not in old.evidence)
                old.conditional = old.conditional and e.conditional
            else:
                merged[key] = copy.deepcopy(e)
    edits = list(merged.values())
    for e in edits:
        e.points = edit_points(e, score)
    edits.sort(key=lambda e: (-e.points, e.conditional, e.code))
    return edits


# --- simulation -------------------------------------------------------------------------------


def _apply_recency(match: MatchResult, use_recency: bool) -> None:
    if use_recency:
        return
    for item in match.items:
        item.recency = 1.0
    score, ad_fit, cv_use = score_items(match.items, match.cv_skills)
    match.ad_fit, match.cv_use = ad_fit, cv_use
    match.score = round(min(score, match.cap) if match.cap is not None else score, 1)


def _match_gain_function(match: MatchResult, index: CvIndex, use_recency: bool):
    base, _, _ = score_items(match.items, match.cv_skills)

    def gain(term: str, dated: bool = False) -> float:
        target = next((i for i in match.items if i.text.lower() == term.lower()), None)
        if target is None:
            return 0.0
        changed = copy.copy(target)
        ev = term_evidence(term, index, TAXONOMY)
        if not dated and ev.level >= 3:
            changed.level, changed.recency = ev.level, (ev.recency if use_recency else 1.0)
        else:
            changed.level, changed.recency = 4, 1.0
        changed.credit = CREDIT[changed.level]
        items = [changed if i is target else i for i in match.items]
        after, _, _ = score_items(items, match.cv_skills)
        return round(max(0.0, after - base), 2)

    return gain


def simulate(cv: CvDocument, target, profile: SystemProfile, *, answers: KnockoutAnswers | None = None,
             embedder=None, ai_estimate=None, today: date | None = None, progress: Progress | None = None,
             cancel: Cancel | None = None) -> SimulationResult:
    """Run the CV through one simulated screening system.

    `target`: a `Target`, a `models.Posting` or a role title (str). `answers`: the
    user's saved knock-out answers. `embedder`: the app's embedder (optional; the
    semantic profiles fall back to lexical matching with synonyms without it).
    `ai_estimate`: the caller's `estimate_ai_probability` result (optional).
    """
    target = _as_target(target)
    today = today or date.today()
    degraded: list[str] = []
    findings: list[Finding] = []

    _notify(progress, "layout")
    layout = cv.layout if cv.layout is not None else analyse_layout(cv.file_path)
    _check_cancel(cancel)

    _notify(progress, "parse")
    view = build_parsed_view(cv, profile.parser, layout)
    parse = parse_findings(cv, view, profile)
    findings.extend(parse)
    index = CvIndex.build(view.text, today)
    _check_cancel(cancel)

    _notify(progress, "record")
    record, parsed = build_record(view, index, cv.text, profile.record_fields, today, cv.profile)
    record_list, record_share = record_findings(record)
    findings.extend(record_list)
    _check_cancel(cancel)

    _notify(progress, "match")
    match_profile = parsed if parsed is not None else cv.profile
    if parsed is not None:
        # The parsed profile has no years when the parser lost the dates; keep the app's
        # mobility and languages only when the parser could read them (it is the parsed view).
        match_profile.mobility = getattr(cv.profile, "mobility", match_profile.mobility)
    mode = METHOD_MODES[profile.score.method]
    if mode == SEMANTIC and embedder is None:
        degraded.append("semantic->lexical")
    match = match_posting(match_profile, target, mode=mode, embedder=embedder, today=today, index=index)
    _apply_recency(match, profile.score.recency)
    match_gain = _match_gain_function(match, index, profile.score.recency)
    findings.extend(semantic_findings(match, mode == SEMANTIC and embedder is not None, match_gain, index))
    _check_cancel(cancel)

    _notify(progress, "knockout")
    outcome = infer_knockouts(target, match, answers, cv.profile)
    findings.extend(knockout_findings(outcome, profile))
    _check_cancel(cancel)

    _notify(progress, "findability")
    search_mode = profile.search.keyword_mode
    embeddings = _Embeddings(index, embedder) if (search_mode == SEMANTIC and embedder is not None) else None
    if search_mode == SEMANTIC and embedder is None and "semantic->lexical" not in degraded:
        degraded.append("semantic->lexical")
    queries = build_queries(target, match, getattr(cv.profile, "language", "it") or "it")
    run_queries(queries, index, search_mode, embeddings)
    found_list, findability = findability_findings(queries, profile, match,
                                                   match_gain if mode == LITERAL else None)
    findings.extend(found_list)
    _check_cancel(cancel)

    _notify(progress, "timeline")
    timeline_list, timeline, _summary = timeline_findings(index, profile)
    findings.extend(timeline_list)
    skill_names = [s.value for s in (getattr(match_profile, "skills", None) or [])]
    findings.extend(skill_recency_findings(skill_names, index, match, match_gain))
    _check_cancel(cancel)

    _notify(progress, "credentials")
    credential_list, credentials = credential_findings(match_profile, index, target)
    findings.extend(credential_list)

    _notify(progress, "format")
    findings.extend(format_findings(cv.file_path, profile, layout.pages))
    findings.extend(ai_findings(ai_estimate))
    _check_cancel(cancel)

    _notify(progress, "score")
    quality = parse_quality(parse)
    parse_component = quality if record_share is None else PARSE_SHARE * quality + RECORD_SHARE * record_share
    raw_match = 100.0 * (0.8 * match.ad_fit + 0.2 * match.cv_use) if match.items else None
    components = {COMPONENT_PARSE: parse_component, COMPONENT_FINDABILITY: findability,
                  COMPONENT_MATCH: raw_match, COMPONENT_TIMELINE: timeline, COMPONENT_CREDENTIALS: credentials}
    score = build_score(components, profile, outcome, match)
    status = FAIL if score.band in ("rejected", "at_risk") or score.value < BAND_MIDDLE else (
        WARN if score.value < BAND_TOP else OK)
    findings.append(Finding(CHECK_SCORE, status, msg("rsim.score.do"), score.explanation[0], [],
                            None, "score.value", [], {"score": score.to_dict()}))
    if degraded:
        findings.append(Finding(CHECK_SCORE, OK, msg("rsim.score.do"), msg("rsim.degraded.semantic"), [], None,
                                "score.degraded"))
    findings = sort_findings(findings)
    edits = collect_edits(findings, score)
    top = [e for e in edits if e.points >= MIN_POINTS][:MAX_TOP_FIXES]
    return SimulationResult(profile.key, profile.name, profile.approximation, findings, record, score, top,
                            match, degraded)


def simulate_all(cv: CvDocument, target, profiles: list[SystemProfile] | None = None, **kwargs
                 ) -> dict[str, SimulationResult]:
    """Run several profiles on the same CV, reading the file layout once.

    `progress` (in kwargs) is called with "<profile key>:<step>"."""
    profiles = profiles or list(load_profiles().values())
    if cv.layout is None:
        cv = CvDocument(cv.profile, cv.read, cv.path, analyse_layout(cv.file_path))
    progress = kwargs.pop("progress", None)
    results: dict[str, SimulationResult] = {}
    for p in profiles:
        def step(name, done, total, _key=p.key):
            if progress is not None:
                progress(f"{_key}:{name}", done, total)
        results[p.key] = simulate(cv, target, p, progress=step, **kwargs)
    return results


# --- what-if ----------------------------------------------------------------------------------


def what_if(result: SimulationResult, n: int = 5) -> list[Edit]:
    """The n concrete CV edits that would raise the simulated score most.

    Only changes the CV evidence supports (write a synonym next to the word you
    used, add months to dates, name a listed skill in the job, move contacts...);
    skills the CV does not show come only as conditional edits ("if you have X,
    state it", `Edit.conditional`). Points are computed on the uncapped score."""
    edits = collect_edits(result.findings, result.score)
    return [e for e in edits if e.points >= MIN_POINTS][:n]


def score_after(result: SimulationResult, edits: list[Edit]) -> int:
    """The simulated score after applying all the edits together (cap kept unless
    the edits fix the knock-out, which wording cannot)."""
    components = dict(result.score.components)
    for e in edits:
        for c, gain in e.gains.items():
            if components.get(c) is not None and gain:
                components[c] = min(100.0, components[c] + gain)
    value = combine(components, result.score.weights)
    if result.score.capped and result.score.cap is not None:
        value = min(value, result.score.cap)
    return int(round(value))
