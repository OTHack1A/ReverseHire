"""The affinity between a CV and what a company is looking for, with a verdict.

``affinity(cv_profile, demand, preferences, *, embedder=None)`` -> ``AffinityResult``.

Components (weights, thresholds and the verdict table in ``weights.py``):

(a) skills: every DemandItem gets the CV evidence of ``recruiter_sim.match.term_evidence``
    (levels 4 dated job / 3 elsewhere in the CV / 2 adjacent / 0, taxonomy synonyms of
    the skill dictionary; "semantic" mode with the injected embedder), credit
    ``match.CREDIT[level]`` and recency as in ``match_posting``:
        explicit = max over postings of sum(w c r) / sum(w)   (context items only as
                   positive evidence, as in match_posting)
        overlap  = min(1, sum(w c r) / min(sum(w), SATURATION_WEIGHT)) over stated and
                   inferred items (a company names many things; a few strong matches suffice)
        skills   = 100 (EXPLICIT_SHARE explicit + (1-EXPLICIT_SHARE) overlap), or the one present
(b) role: the user's roles (CV roles 1.0, past job titles x recency) vs the company's
    roles (posting titles 1.0, inferred x 0.8 / 0.7): lexical token_set_ratio over the
    IT/EN title variants, a shared role keyword of a direction (>= ROLE_KEYWORD_FLOOR), and
    the embedder when given.
(c) sector: closeness (knowledge.sector_closeness) of the company's sectors (register
    ATECO, OSM, site text) to the CV's sectors (months in jobs of that sector, saturating at
    SECTOR_FULL_MONTHS, x recency; or role/skill keywords at SECTOR_PROFILE_SHARE), and the
    company ATECO inside the profile's categories of interest (discovery.ateco).
(d) seniority: years asked vs CV years (under: X/Y; over: beyond max(2Y, Y+5) years the
    score drops to OVER_FLOOR) and level words (junior/mid/senior table).
(e) constraints: ``recruiter_sim.knockout.infer_knockouts`` on the chosen posting (the
    user's stored answers first, then the CV), plus the optional distance limit; a FAIL
    or a requirements gate caps the affinity at ``Thresholds.hard_cap`` and is named.
(f) practical: contract, remote, salary of the chosen posting vs the preferences;
    distance is informational unless a limit is set.
(g) opportunity: hiring now, hiring for the user's role, invitation, revenue trend,
    size - a separate indicator, never part of the affinity.

Per direction: the skills and role components are restricted to the demand of that
direction (skill families / names / role keywords of ``preferences.Direction``), the
other components are shared; a direction the company has nothing to do with is "not
applicable" (no score, no penalty). The headline is the best applicable direction,
else the whole profile ("general").

Pure and deterministic for a given ``today``; no network; thread-safe (no shared state).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from rapidfuzz import fuzz

from ..cv import skills as cv_skills
from ..geo.sectors import SECTOR_THRESHOLD, SECTORS, sector_scores
from ..models import ProfileItem
from ..recruiter_sim import match as rmatch
from ..recruiter_sim.cvindex import SEMANTIC, TAXONOMY, CvIndex, fold
from ..recruiter_sim.model import FAIL, OK, UNKNOWN, WARN, KnockoutAnswers, Target
from ..scoring.fit import title_variants
from ..scoring.preferences import CONTRACT_SCORES, REMOTE_SCORES, yearly_salary
from ..scoring.relevance import calibrate_similarity, normalize_text, range_of
from . import knowledge as kn
from . import weights as W
from .demand import EXPLICIT, EXPLICIT_CONTEXT, EXPLICIT_MUST, INFERRED, INFERRED_TYPE, STATED, CompanyDemand, \
    DemandItem, DemandRole, build_demand, item_key
from .model import GENERAL, STATUS_NOT_ENOUGH, STATUS_OK, AffinityResult, Component, Link, Opportunity, Point, \
    PostingFit, Scope
from .preferences import AffinityPreferences, Direction
from .texts import Note

# --- constants ------------------------------------------------------------------------

EXPLICIT_SHARE = 0.7
SATURATION_WEIGHT = 4.0  # two technologies named on the site, fully matched, saturate the overlap
CONTEXT_MIN_LEVEL = 2
QUALITY_EXPLICIT, QUALITY_STATED, QUALITY_INFERRED, QUALITY_TYPE_ONLY = 1.0, 0.8, 0.5, 0.25
ROLE_KEYWORD_FLOOR = 0.7
MIN_TITLE_FUZZY = 50
OLD_ROLE_FACTOR = 0.9  # past job titles: x recency, at most this
SECTOR_FULL_MONTHS = 24
SECTOR_PROFILE_SHARE = 0.6
SECTOR_QUALITY = {"ateco": 1.0, "osm": 1.0, "text": 0.6}
YEARS_TOLERANCE = 0.5
OVER_FLOOR = 0.5
OVER_SPAN = 10.0  # years beyond the over-qualification start where the floor is reached
LEVEL_FIT = {
    "junior": {"junior": 1.0, "mid": 0.8, "senior": 0.5},
    "mid": {"junior": 0.5, "mid": 1.0, "senior": 0.8},
    "senior": {"junior": 0.2, "mid": 0.6, "senior": 1.0},
}
KNOCKOUT_VALUE = {OK: 1.0, WARN: 0.6, UNKNOWN: 0.8, FAIL: 0.0}
SALARY_FLOOR = 0.1
MIN_DIRECTION_SHARE = 0.15
DIRECTION_POSTING_SHARE = 0.3
MAX_EVIDENCE_POINTS = 12
MAX_STRENGTHS, MAX_GAPS, MAX_EXTRAS, MAX_TALKING = 5, 5, 5, 3
OPP_POINTS = {"hiring": 40, "for_you": 30, "invitation": 10, "careers": 5, "growing": 15, "stable": 5,
              "shrinking": -10, "size": 5}
OPP_HIGH, OPP_MEDIUM = 60, 30
SIZE_SIGNAL_EMPLOYEES = 50


# --- CV context (computed once per CV) ---------------------------------------------------


@dataclass
class CvRole:
    title: str
    line: str
    factor: float


@dataclass
class CvContext:
    profile: object
    today: date
    index: CvIndex
    mode: str
    embedder: object | None
    embeddings: object | None
    family_lines: dict
    roles: list[CvRole]
    sectors: dict[str, tuple[float, str]]  # key -> (strength 0..1, evidence line)
    ateco: list  # discovery.ateco.AtecoCode
    years: float | None
    seniority: str | None
    _terms: dict = field(default_factory=dict)
    _vectors: dict = field(default_factory=dict)

    def evidence(self, term: str):
        cached = self._terms.get(term)
        if cached is None:
            cached = rmatch.term_evidence(term, self.index, self.mode, self.embeddings, self.family_lines)
            self._terms[term] = cached
        return cached

    def vector(self, text: str):
        if self.embedder is None:
            return None
        if text not in self._vectors:
            try:
                import numpy as np

                self._vectors[text] = np.asarray(self.embedder.embed([text], kind="query"))[0]
            except Exception:  # noqa: BLE001 - the semantic part is optional
                self._vectors[text] = None
        return self._vectors[text]


def _month_index(value: str, today: date) -> int | None:
    value = (value or "").strip().lower()
    if value in ("present", "current", "now", "oggi", "in corso"):
        return today.year * 12 + today.month - 1
    m = re.match(r"(\d{4})(?:-(\d{1,2}))?", value)
    if not m:
        return None
    return int(m.group(1)) * 12 + (int(m.group(2) or 12) - 1)


def _first_line(item) -> str:
    for e in getattr(item, "evidence", None) or []:
        if getattr(e, "source", "cv") == "cv" and (e.text or "").strip():
            return " ".join(e.text.split())[:220]
    return getattr(item, "value", "") or getattr(item, "title", "") or ""


class _Pseudo:
    def __init__(self, text: str):
        self.roles = [ProfileItem(text)]
        self.skills: list = []
        self.degrees: list = []


def cv_context(cv_profile, embedder=None, today: date | None = None) -> CvContext:
    today = today or date.today()
    index = CvIndex.build(getattr(cv_profile, "text", "") or "", today)
    mode = SEMANTIC if embedder is not None else TAXONOMY
    embeddings = rmatch.Embeddings(index, embedder) if embedder is not None else None
    family_lines = rmatch.family_lines(index, mode)
    now = today.year * 12 + today.month - 1
    roles: list[CvRole] = []
    seen: set[str] = set()
    for item in getattr(cv_profile, "roles", None) or []:
        for title in (item.variants() if hasattr(item, "variants") else [item.value]):
            if normalize_text(title) and normalize_text(title) not in seen:
                seen.add(normalize_text(title))
                roles.append(CvRole(title, _first_line(item), 1.0))
    sectors: dict[str, tuple[float, str]] = {}
    for exp in getattr(cv_profile, "experiences", None) or []:
        end = _month_index(getattr(exp, "end", ""), today)
        recency = rmatch.recency_factor(max(0, now - end)) if end is not None else rmatch.LISTED_ONLY_RECENCY
        line = _first_line(exp)
        for title in [exp.title] + ([exp.original_title] if getattr(exp, "original_title", None) else []):
            if normalize_text(title) and normalize_text(title) not in seen:
                seen.add(normalize_text(title))
                roles.append(CvRole(title, line, min(OLD_ROLE_FACTOR, recency) if end != now else 1.0))
        text = " ".join([exp.title or "", *(e.text for e in exp.evidence or [])])
        months = getattr(exp, "months", None) or 12
        for key, score in sector_scores(_Pseudo(text)).items():
            if score < 0.99:
                continue
            strength = min(1.0, months / SECTOR_FULL_MONTHS) * recency
            current = sectors.get(key)
            if current is None or strength > current[0]:
                sectors[key] = (round(strength, 3), line)
    profile_scores = sector_scores(cv_profile) if getattr(cv_profile, "roles", None) is not None else {}
    top = max(profile_scores.values(), default=0.0)
    first_role = next((r.line for r in roles), "")
    for key, score in profile_scores.items():
        if score >= SECTOR_THRESHOLD and top > 0:
            strength = SECTOR_PROFILE_SHARE * score / top
            if strength > sectors.get(key, (0.0, ""))[0]:
                sectors[key] = (round(strength, 3), first_role)
    from ..discovery.ateco import ateco_for_profile

    try:
        ateco = ateco_for_profile(cv_profile)
    except Exception:  # noqa: BLE001 - the ATECO table is optional data
        ateco = []
    return CvContext(cv_profile, today, index, mode, embedder, embeddings, family_lines, roles, sectors, ateco,
                     getattr(cv_profile, "years_experience", None), getattr(cv_profile, "seniority", None))


# --- directions -------------------------------------------------------------------------


@dataclass
class _DirectionIndex:
    direction: Direction
    keys: set[str]
    families: set[str]
    role_patterns: list[re.Pattern]

    @classmethod
    def build(cls, d: Direction) -> "_DirectionIndex":
        keys = {item_key(s)[0] for s in d.skills}
        patterns = [re.compile(r"(?<![a-z0-9])" + re.escape(normalize_text(r)) + r"s?(?![a-z0-9])")
                    for r in d.roles if normalize_text(r)]
        return cls(d, keys, set(d.families), patterns)

    def has_item(self, item: DemandItem) -> bool:
        return (item.family and item.family in self.families) or item.key in self.keys

    def role_keyword(self, title: str) -> str | None:
        norm = normalize_text(title)
        for p in self.role_patterns:
            m = p.search(norm)
            if m:
                return m.group(0)
        return None


# --- components ---------------------------------------------------------------------------


@dataclass
class _PostingEval:
    index: int
    match: object
    knockouts: object
    failed: list[Note]
    fit: PostingFit


def _credit(ctx: CvContext, item: DemandItem):
    ev = ctx.evidence(item.term)
    credit = rmatch.CREDIT.get(ev.level, 0.0)
    recency = ev.recency if ev.level else 1.0
    return ev, credit, recency


def _origin_note(origin: str) -> str:
    return f"affinity.origin.{origin}"


def _skill_point(item: DemandItem, ev, credit: float, recency: float) -> Point:
    quote = item.evidence[0] if item.evidence else None
    link = Link(cv_line=ev.line, cv_term=ev.cv_term, company_text=quote.text if quote else None,
                company_source=quote.source if quote else None, company_field=quote.field if quote else None,
                origin=item.origin)
    params = {"skill": item.term, "origin": _origin_note(item.origin), "cv_line": ev.line or ""}
    key = "affinity.ev.skill_matched" if ev.level >= 3 else "affinity.ev.skill_partial" if ev.level == 2 \
        else "affinity.ev.skill_missing"
    return Point(Note(key, params), link, item.term, item.weight, round(100 * credit * recency, 1))


def _skills(ctx: CvContext, items: list[DemandItem], postings: list[int]) -> Component:
    explicit_best, explicit_posting = None, None
    points: list[Point] = []
    for p in postings:
        earned = total = 0.0
        for item in items:
            if item.posting_index != p or item.origin not in EXPLICIT:
                continue
            ev, credit, recency = _credit(ctx, item)
            if item.origin == EXPLICIT_CONTEXT and ev.level < CONTEXT_MIN_LEVEL:
                continue
            earned += item.weight * credit * recency
            total += item.weight
        if total > 0 and (explicit_best is None or earned / total > explicit_best):
            explicit_best, explicit_posting = earned / total, p
    other = [i for i in items if i.origin not in EXPLICIT]
    overlap = None
    stated = False
    if other:
        earned = sum(i.weight * c * r for i in other for _, c, r in [_credit(ctx, i)])
        available = sum(i.weight for i in other)
        overlap = min(1.0, earned / min(available, SATURATION_WEIGHT)) if available > 0 else None
        stated = any(i.origin == STATED for i in other)
    for item in items:
        if item.origin in EXPLICIT and item.posting_index != explicit_posting:
            continue
        ev, credit, recency = _credit(ctx, item)
        if item.origin == EXPLICIT_CONTEXT and ev.level < CONTEXT_MIN_LEVEL:
            continue
        points.append(_skill_point(item, ev, credit, recency))
    points.sort(key=lambda pt: (-(pt.weight * (pt.score or 0)), -pt.weight, pt.term.casefold()))
    if explicit_best is None and overlap is None:
        return Component("skills", None, 0.0, [], {})
    if explicit_best is not None and overlap is not None:
        value = EXPLICIT_SHARE * explicit_best + (1 - EXPLICIT_SHARE) * overlap
    else:
        value = explicit_best if explicit_best is not None else overlap
    if explicit_best is not None:
        quality = QUALITY_EXPLICIT
    elif stated:
        quality = QUALITY_STATED
    elif any(i.origin != INFERRED_TYPE for i in other):
        quality = QUALITY_INFERRED
    else:
        quality = QUALITY_TYPE_ONLY  # only "a company of this type usually needs..."
    detail = {"explicit": None if explicit_best is None else round(explicit_best, 4),
              "overlap": None if overlap is None else round(overlap, 4), "posting_index": explicit_posting}
    # keep the most informative points: matched first, then missing must-haves
    kept = points[:MAX_EVIDENCE_POINTS]
    for pt in points[MAX_EVIDENCE_POINTS:]:
        if pt.link.origin == EXPLICIT_MUST and (pt.score or 0) == 0:
            kept.append(pt)
    return Component("skills", round(100 * value, 1), quality, kept, detail)


def _role_similarity(ctx: CvContext, user: CvRole, company: DemandRole, keyword_sets: list[_DirectionIndex]) -> float:
    forms = title_variants(company.title)
    user_norm = normalize_text(user.title)
    ratio = max((fuzz.token_set_ratio(f, user_norm) for f in forms), default=0)
    value = max(0.0, (ratio - MIN_TITLE_FUZZY) / (100 - MIN_TITLE_FUZZY))
    for d in keyword_sets:
        kw_c, kw_u = d.role_keyword(company.title), d.role_keyword(user.title)
        if kw_c and kw_u:
            value = max(value, ROLE_KEYWORD_FLOOR)
            break
    if ctx.embedder is not None:
        a, b = ctx.vector(user.title), ctx.vector(company.title)
        if a is not None and b is not None:
            interval = tuple(getattr(ctx.embedder, "title_range", range_of(ctx.embedder)))
            value = max(value, calibrate_similarity(float(a @ b), interval) / 100.0)
    return min(1.0, value)


def _role(ctx: CvContext, roles: list[DemandRole], keyword_sets: list[_DirectionIndex]) -> Component:
    if not roles:
        return Component("role", None, 0.0)
    pairs = []
    for company in roles:
        for user in ctx.roles:
            sim = _role_similarity(ctx, user, company, keyword_sets)
            pairs.append((sim * company.weight * user.factor, company, user))
    pairs.sort(key=lambda x: (-x[0], x[1].title, x[2].title))
    if any(r.origin == "posting" for r in roles):
        quality = QUALITY_EXPLICIT
    elif any(r.origin != INFERRED_TYPE for r in roles):
        quality = QUALITY_INFERRED
    else:
        quality = QUALITY_TYPE_ONLY
    if not pairs:  # the CV names no role at all
        return Component("role", 0.0, quality, [Point(Note("affinity.ev.role_none"))], {"best": None})
    points = []
    used: set[str] = set()
    for value, company, user in pairs:
        if company.title in used or value <= 0:
            continue
        used.add(company.title)
        quote = company.evidence
        points.append(Point(Note("affinity.ev.role", {"cv_role": user.title, "company_role": company.title,
                                                     "origin": _origin_note(company.origin) if company.origin != "posting"
                                                     else "affinity.origin.explicit_must",
                                                     "score": round(100 * value)}),
                            Link(cv_line=user.line, cv_term=user.title, company_text=quote.text if quote else company.title,
                                 company_source=quote.source if quote else None,
                                 company_field=quote.field if quote else None, origin=company.origin),
                            company.title, company.weight, round(100 * value, 1)))
        if len(points) >= 3:
            break
    best = pairs[0]
    if best[0] <= 0:
        points = [Point(Note("affinity.ev.role_none"))]
    return Component("role", round(100 * best[0], 1), quality, points,
                     {"best_company_role": best[1].title, "best_cv_role": best[2].title,
                      "best_origin": best[1].origin})


def _label_only(company_text: str, point: Point) -> bool:
    """True when the "company text" of a sector point is only a sector name, not a quote."""
    text = fold(company_text or "")
    labels = {fold(str(point.note.params.get(k, "") or "")) for k in ("company_sector", "cv_sector")}
    labels |= {fold(s.name_en) for s in SECTORS.values()} | {fold(getattr(s, "name_it", "") or "") for s in
                                                             SECTORS.values()}
    return not text or text in labels or len(text.split()) <= 3


def _sector(ctx: CvContext, demand: CompanyDemand) -> Component:
    if not demand.sectors and not demand.ateco:
        return Component("sector", None, 0.0)
    best, points = 0.0, []
    quality = max([SECTOR_QUALITY[c.via] for c in demand.sectors] + ([1.0] if demand.ateco else []))
    candidates = []
    for claim in demand.sectors:
        for key, (strength, line) in ctx.sectors.items():
            value = kn.sector_closeness(claim.key, key) * strength
            if value > 0:
                candidates.append((value, claim, key, line))
    candidates.sort(key=lambda x: (-x[0], x[1].key, x[2]))
    for value, claim, key, line in candidates[:2]:
        points.append(Point(Note("affinity.ev.sector", {
            "cv_sector": SECTORS[key].name_en if key in SECTORS else key, "cv_evidence": line,
            "company_sector": SECTORS[claim.key].name_en if claim.key in SECTORS else claim.key,
            "via": claim.via, "score": round(100 * value)}),
            Link(cv_line=line, company_text=claim.evidence.text, company_source=claim.evidence.source,
                 company_field=claim.evidence.field, origin=f"sector:{claim.via}"), claim.key, 1.0,
            round(100 * value, 1)))
    if candidates:
        best = candidates[0][0]
    if demand.ateco:
        from ..discovery.ateco import matches

        for code in ctx.ateco:
            if matches(demand.ateco, [code.code]) and code.weight > best:
                best = code.weight
                points.insert(0, Point(Note("affinity.ev.sector_ateco", {"code": demand.ateco, "why": code.why}),
                                       Link(cv_line=None, company_text=f"ATECO {demand.ateco}",
                                            company_source=demand.ateco_source, company_field="ateco",
                                            origin="sector:ateco"), code.code, 1.0, round(100 * code.weight, 1)))
                break
    return Component("sector", round(100 * best, 1), quality, points, {})


def _seniority(ctx: CvContext, demand: CompanyDemand, posting: int | None) -> Component:
    asked = [a for a in demand.asked if a.posting_index == posting and a.kind in ("years", "level")]
    if posting is None or not asked:
        return Component("seniority", None, 0.0)
    parts, points = [], []
    years = [a for a in asked if a.kind == "years" and a.data.get("years")]
    if years and ctx.years is not None:
        a = max(years, key=lambda x: x.data["years"])
        y, x = float(a.data["years"]), float(ctx.years)
        if x + YEARS_TOLERANCE >= y:
            start = max(2 * y, y + 5)
            value = 1.0 if x <= start else max(OVER_FLOOR, 1.0 - (1 - OVER_FLOOR) * (x - start) / OVER_SPAN)
            key = "affinity.ev.seniority_over" if x > start else "affinity.ev.seniority_years"
        else:
            value, key = max(0.0, x / y), "affinity.ev.seniority_years"
        parts.append(value)
        points.append(Point(Note(key, {"asked": a.data["years"], "cv": round(x, 1)}),
                            Link(company_text=a.line, company_source=demand.postings[posting].url or demand.site,
                                 company_field="posting", origin="asked:years"), "years", 1.0, round(100 * value, 1)))
    levels = [a for a in asked if a.kind == "level"]
    if levels and ctx.seniority in LEVEL_FIT:
        a = levels[0]
        value = LEVEL_FIT[a.data["level"]][ctx.seniority]
        parts.append(value)
        points.append(Point(Note("affinity.ev.seniority_level", {"asked": a.data["level"], "cv": ctx.seniority}),
                            Link(company_text=a.line, company_source=demand.postings[posting].url or demand.site,
                                 company_field="posting_title", origin="asked:level"), "level", 1.0,
                            round(100 * value, 1)))
    if not parts:
        return Component("seniority", None, 0.0)
    return Component("seniority", round(100 * min(parts), 1), QUALITY_EXPLICIT, points, {})


def _answers(prefs: AffinityPreferences) -> KnockoutAnswers:
    raw = {k: v for k, v in (prefs.knockout_answers or {}).items() if k != "max_commute_km"}
    answers = KnockoutAnswers.from_dict(raw)
    if prefs.max_travel_days is not None and answers.max_travel_days is None:
        answers.max_travel_days = prefs.max_travel_days
    return answers


ENTRY_LEVEL_MAX_YEARS = 5   # an internship / apprenticeship never "fits" a CV with this much experience
_ENTRY_LEVEL = re.compile(r"\b(?:intern|internship|stage|stagista|stagiaire|tirocin\w*|apprendist\w*|"
                          r"apprentice\w*|trainee|graduate programme|graduate program|neolaureat\w*)\b", re.I)


def _entry_level(title: str) -> bool:
    """An internship, apprenticeship or graduate programme ("Merchant Risk Intern")."""
    return bool(_ENTRY_LEVEL.search(title or ""))


def _title_score(ctx: CvContext, title: str) -> float:
    """0..100: how well a posting known only by its title fits the CV's roles."""
    company = DemandRole(title=title, origin="posting", weight=1.0)
    best = max((_role_similarity(ctx, user, company, []) * min(1.0, user.factor) for user in ctx.roles), default=0.0)
    return round(100.0 * best, 1)


def _evaluate_postings(ctx: CvContext, demand: CompanyDemand, prefs: AffinityPreferences,
                       thresholds: W.Thresholds) -> list[_PostingEval]:
    from ..recruiter_sim.knockout import infer_knockouts

    answers = _answers(prefs)
    result = []
    for i, posting in enumerate(demand.postings):
        m = rmatch.match_posting(ctx.profile, posting, mode=ctx.mode, embedder=ctx.embedder, today=ctx.today,
                                 index=ctx.index)
        target = Target.from_posting(posting)
        ko = infer_knockouts(target, m, answers, ctx.profile)
        failed = [Note("affinity.reason.hard_fail", {"requirement": q.posting_line, "topic": q.topic})
                  for q in ko.questions if q.status == FAIL and q.topic != "must_skill"]
        if m.capped and m.cap_reason:
            failed.insert(0, Note("affinity.reason.capped", {"cap": m.cap, "requirement": m.cap_reason}))
        score = float(m.score)
        if not m.total and posting.title:
            # Only the title could be read (a careers page listing "Back-end developer"):
            # no requirement to match, so the title is compared with the CV's roles
            # instead of scoring 0 % (which turned a matching opening into "spontaneous").
            score = _title_score(ctx, posting.title)
        if _entry_level(posting.title) and (ctx.years or 0) >= ENTRY_LEVEL_MAX_YEARS:
            failed.append(Note("affinity.reason.entry_level", {"title": posting.title}))
        fits = score >= thresholds.posting_fit and not failed and not m.capped
        fit = PostingFit(i, posting.title, posting.url, score, fits, bool(m.capped), failed, m.common,
                         m.total)
        result.append(_PostingEval(i, m, ko, failed, fit))
    return result


def _constraints(ev: _PostingEval | None, demand: CompanyDemand, prefs: AffinityPreferences
                 ) -> tuple[Component, bool, Note | None]:
    points, values = [], []
    hard_fail, cap_reason = False, None
    if ev is not None:
        for q in ev.knockouts.questions:
            if q.topic == "must_skill":
                continue
            values.append(KNOCKOUT_VALUE[q.status])
            points.append(Point(Note("affinity.ev.constraint", {"topic": f"affinity.topic.{q.topic}",
                                                               "status": f"affinity.status.{q.status}",
                                                               "posting_line": q.posting_line}),
                                Link(cv_line=q.cv_line or (q.answer and f"answer: {q.answer}") or None,
                                     company_text=q.posting_line,
                                     company_source=demand.postings[ev.index].url or demand.site,
                                     company_field="posting", origin=f"constraint:{q.topic}"), q.topic, 1.0,
                                100 * KNOCKOUT_VALUE[q.status]))
        if ev.failed:
            hard_fail, cap_reason = True, ev.failed[0]
    if prefs.max_distance_km is not None and demand.distance_km is not None:
        far = demand.distance_km > prefs.max_distance_km
        values.append(0.0 if far else 1.0)
        points.append(Point(Note("affinity.ev.distance_limit", {"distance": round(demand.distance_km),
                                                                "limit": round(prefs.max_distance_km)}),
                            Link(company_text=", ".join(demand.workplaces) or None, company_source="card:distance_km",
                                 company_field="distance_km", origin="constraint:distance"), "distance", 1.0,
                            0.0 if far else 100.0))
        if far:
            hard_fail = True
            cap_reason = cap_reason or Note("affinity.reason.too_far", {"distance": round(demand.distance_km),
                                                                       "limit": round(prefs.max_distance_km)})
    if not values:
        return Component("constraints", None, 0.0), False, None
    return (Component("constraints", round(100 * sum(values) / len(values), 1), QUALITY_EXPLICIT, points, {}),
            hard_fail, cap_reason)


def _practical(posting, demand: CompanyDemand, prefs: AffinityPreferences) -> Component:
    parts, points = [], []
    if posting is not None:
        url = posting.url or demand.site
        if prefs.prefer_permanent and posting.contract:
            value = CONTRACT_SCORES.get(posting.contract, 0.5)
            parts.append(value)
            points.append(Point(Note("affinity.ev.contract", {"contract": posting.contract}),
                                Link(company_text=posting.contract, company_source=url, company_field="contract",
                                     origin="practical"), "contract", 1.0, round(100 * value, 1)))
        if prefs.prefer_remote and posting.remote:
            value = REMOTE_SCORES.get(posting.remote, 0.5)
            parts.append(value)
            points.append(Point(Note("affinity.ev.remote", {"remote": posting.remote}),
                                Link(company_text=posting.remote, company_source=url, company_field="remote",
                                     origin="practical"), "remote", 1.0, round(100 * value, 1)))
        yearly = yearly_salary(posting)
        if prefs.min_salary_year > 0 and yearly:
            value = 1.0 if yearly >= prefs.min_salary_year else max(
                SALARY_FLOOR, 1.0 - 2.0 * (1.0 - yearly / prefs.min_salary_year))
            parts.append(value)
            points.append(Point(Note("affinity.ev.salary", {"salary": posting.salary or round(yearly)}),
                                Link(company_text=posting.salary or str(round(yearly)), company_source=url,
                                     company_field="salary", origin="practical"), "salary", 1.0, round(100 * value, 1)))
    if demand.distance_km is not None and prefs.max_distance_km is None:
        points.append(Point(Note("affinity.ev.distance", {"distance": round(demand.distance_km)}),
                            Link(company_text=", ".join(demand.workplaces) or None, company_source="card:distance_km",
                                 company_field="distance_km", origin="practical:info"), "distance", 0.0, None))
    if not parts:
        return Component("practical", None, 0.0, points, {})
    return Component("practical", round(100 * sum(parts) / len(parts), 1), QUALITY_EXPLICIT, points, {})


def _opportunity(demand: CompanyDemand, evals: list[_PostingEval], roles_best: float | None) -> Opportunity:
    signals, score = [], 0.0
    hiring_now = bool(demand.postings) or demand.hiring_status == "hiring"
    if demand.postings:
        score += OPP_POINTS["hiring"]
        signals.append(Point(Note("affinity.opp.hiring", {"count": len(demand.postings)}),
                             Link(company_text=demand.postings[0].title,
                                  company_source=demand.careers_url or demand.postings[0].url or demand.site,
                                  company_field="postings", origin="opportunity")))
    elif demand.hiring_status == "hiring":
        score += OPP_POINTS["hiring"] / 2
        signals.append(Point(Note("affinity.opp.hiring_status"),
                             Link(company_source=demand.careers_url or demand.site, company_field="hiring",
                                  origin="opportunity")))
    fitting = [e for e in evals if e.fit.fits]
    for_you = bool(fitting)
    if fitting:
        best = max(fitting, key=lambda e: e.fit.score)
        score += OPP_POINTS["for_you"]
        signals.append(Point(Note("affinity.opp.for_you", {"title": best.fit.title}),
                             Link(company_text=best.fit.title, company_source=best.fit.url or demand.site,
                                  company_field="posting_title", origin="opportunity")))
    if demand.invitation:
        score += OPP_POINTS["invitation"]
        signals.append(Point(Note("affinity.opp.invitation"), Link(company_source=demand.careers_url or demand.site,
                                                                   company_field="hiring", origin="opportunity")))
    elif demand.careers_url:
        score += OPP_POINTS["careers"]
        signals.append(Point(Note("affinity.opp.careers"), Link(company_source=demand.careers_url,
                                                                company_field="hiring", origin="opportunity")))
    if demand.revenue_trend:
        score += OPP_POINTS[demand.revenue_trend]
        first, last = (demand.revenue_years + [None, None])[:2]
        signals.append(Point(Note(f"affinity.opp.revenue_{demand.revenue_trend}",
                                  {"change": round(100 * (demand.revenue_change or 0), 1), "first": first, "last": last}),
                             Link(company_source=demand.revenue_source or "card:financials", company_field="financials",
                                  origin="opportunity")))
    if demand.employees and demand.employees >= SIZE_SIGNAL_EMPLOYEES:
        score += OPP_POINTS["size"]
        signals.append(Point(Note("affinity.opp.size", {"employees": int(demand.employees)}),
                             Link(company_source=demand.employees_source or "card", company_field="employees",
                                  origin="opportunity")))
    score = max(0.0, min(100.0, score))
    level = "high" if score >= OPP_HIGH else "medium" if score >= OPP_MEDIUM else "low"
    return Opportunity(round(score, 1), level, hiring_now, for_you, signals)


# --- scopes ---------------------------------------------------------------------------------


def _scope_postings(demand: CompanyDemand, d: _DirectionIndex | None) -> list[int]:
    if d is None:
        return list(range(len(demand.postings)))
    chosen = []
    for i, posting in enumerate(demand.postings):
        if d.role_keyword(posting.title):
            chosen.append(i)
            continue
        own = [it for it in demand.items if it.posting_index == i and it.origin in EXPLICIT]
        total = sum(it.weight for it in own)
        if total and sum(it.weight for it in own if d.has_item(it)) / total >= DIRECTION_POSTING_SHARE:
            chosen.append(i)
    return chosen


def _plausible(skills: Component, role: Component, demand: CompanyDemand, thresholds: W.Thresholds
               ) -> tuple[bool, str]:
    overlap = (skills.detail or {}).get("overlap")
    if overlap is not None and overlap >= thresholds.plausible_overlap:
        matched = [p for p in skills.evidence if p.link.origin not in EXPLICIT and (p.score or 0) >= 50]
        if matched:
            pt = matched[0]
            why = f"{pt.term} ({pt.link.company_text or pt.link.origin})"
            return True, why
    if role.score is not None and role.score >= thresholds.plausible_role:
        return True, str((role.detail or {}).get("best_company_role") or "")
    return False, ""


def _build_scope(ctx, demand, prefs, thresholds, evals, d: _DirectionIndex | None, all_dirs) -> Scope:
    if d is None:
        items, roles, postings = list(demand.items), list(demand.roles), list(range(len(demand.postings)))
        share, applicable = 1.0, True
        sid, name = GENERAL, "affinity.direction.general"
    else:
        items = [i for i in demand.items if d.has_item(i)]
        total = sum(i.weight for i in demand.items)
        share = sum(i.weight for i in items) / total if total else 0.0
        postings = _scope_postings(demand, d)
        roles = [r for r in demand.roles if d.role_keyword(r.title)
                 or (r.posting_index is not None and r.posting_index in postings)]
        applicable = bool(postings) or share >= MIN_DIRECTION_SHARE or (share > 0 and bool(roles))
        sid, name = d.direction.id, d.direction.name
        # the items of the scope's postings are all kept (a posting is judged as a whole)
        items = items + [i for i in demand.items if i.posting_index in postings and i not in items]
    scope = Scope(sid, name, applicable, demand_share=round(share, 3))
    if not applicable:
        return scope
    skills = _skills(ctx, items, postings)
    role = _role(ctx, roles, all_dirs)
    sector = _sector(ctx, demand)
    candidates = [e for e in evals if e.index in postings]
    ok = [e for e in candidates if not e.failed]
    chosen = max(ok or candidates, key=lambda e: (e.fit.score, -e.index), default=None)
    seniority = _seniority(ctx, demand, chosen.index if chosen else None)
    constraints, hard_fail, cap_reason = _constraints(chosen, demand, prefs)
    practical = _practical(demand.postings[chosen.index] if chosen else None, demand, prefs)
    scope.components = {c.key: c for c in (skills, role, sector, seniority, constraints, practical)}
    scope.best_posting = chosen.fit if chosen else None
    scope.hard_fail, scope.cap_reason = hard_fail, cap_reason
    scope.plausible_need, scope.plausible_why = _plausible(skills, role, demand, thresholds)
    return scope


# --- finalisation (pure on the stored result: also used by calibration) ---------------------


def _score_scope(scope: Scope, weights: W.Weights) -> None:
    t = weights.thresholds
    if not scope.applicable:
        scope.affinity, scope.raw_affinity, scope.coverage, scope.confidence = None, None, 0.0, "low"
        return
    scores = {k: (c.score, c.quality) for k, c in scope.components.items()}
    raw, coverage = W.combine(scores, weights)
    scope.raw_affinity = None if raw is None else round(raw, 1)
    scope.coverage = round(coverage, 3)
    described = scope.components.get("skills") is not None and \
        (scope.components["skills"].detail or {}).get("explicit") is not None
    scope.confidence = W.confidence_label(coverage, described, t)
    if raw is None or not W.enough_data(coverage, scores, t):
        scope.affinity = None
        return
    value = min(raw, t.hard_cap) if scope.hard_fail else raw
    scope.affinity = round(max(0.0, min(100.0, value)), 1)


def _strengths_gaps(result: AffinityResult, scope: Scope) -> None:
    skills = scope.components.get("skills")
    points = skills.evidence if skills else []
    strengths, gaps = [], []
    for pt in sorted(points, key=lambda p: (-(p.weight * (p.score or 0)), p.term.casefold())):
        if pt.note.key == "affinity.ev.skill_matched" and len(strengths) < MAX_STRENGTHS:
            strengths.append(Point(Note("affinity.strength", {"skill": pt.term, "origin": _origin_note(pt.link.origin),
                                                              "cv_line": pt.link.cv_line or ""}),
                                   pt.link, pt.term, pt.weight, pt.score))
    musts = [p for p in points if p.note.key == "affinity.ev.skill_missing" and p.link.origin == EXPLICIT_MUST]
    stated = [p for p in points if p.note.key == "affinity.ev.skill_missing" and p.link.origin == STATED]
    for pt in sorted(musts, key=lambda p: (-p.weight, p.term.casefold())):
        gaps.append(Point(Note("affinity.gap.state_it", {"skill": pt.term, "posting_line": pt.link.company_text or ""}),
                          pt.link, pt.term, pt.weight, 0.0))
    for pt in sorted(stated, key=lambda p: p.term.casefold()):
        gaps.append(Point(Note("affinity.gap.stated", {"skill": pt.term}), pt.link, pt.term, pt.weight, 0.0))
    result.strengths, result.gaps = strengths, gaps[:MAX_GAPS]
    talking: list[Point] = []
    used_texts: set[str] = set()
    for pt in sorted(strengths, key=lambda p: (p.link.origin in INFERRED, -(p.weight * (p.score or 0)))):
        if not (pt.link.cv_line and pt.link.company_text) or pt.link.company_text in used_texts:
            continue
        used_texts.add(pt.link.company_text)
        talking.append(Point(Note("affinity.talk.skill", {"skill": pt.term, "cv_line": pt.link.cv_line,
                                                          "company_text": pt.link.company_text}), pt.link, pt.term,
                             pt.weight, pt.score))
        if len(talking) >= MAX_TALKING - 1:
            break
    role = scope.components.get("role")
    if role and role.score and role.score >= 50 and role.evidence and role.evidence[0].link.cv_line and \
            len(talking) < MAX_TALKING:
        ev = role.evidence[0]
        if ev.link.origin == "posting":
            talking.append(Point(Note("affinity.talk.role", {"cv_role": ev.link.cv_term,
                                                             "company_role": ev.term}), ev.link, ev.term, 1.0, ev.score))
    sector = scope.components.get("sector")
    if sector and sector.score and sector.score >= 50 and len(talking) < MAX_TALKING:
        # only a REAL company quote: a sector label alone ("Electronics and automation", a
        # guess from the search) is not something the company says (a chrome-plating shop
        # was told "your background ... is their field")
        ev = next((p for p in sector.evidence if p.link.cv_line and p.link.company_text
                   and not _label_only(p.link.company_text, p)), None)
        if ev is not None:
            talking.append(Point(Note("affinity.talk.sector", {"sector": ev.note.params.get("company_sector", ev.term),
                                                               "cv_evidence": ev.link.cv_line,
                                                               "company_text": ev.link.company_text}),
                                 ev.link, ev.term, 1.0, ev.score))
    for pt in strengths[len(talking):]:
        if len(talking) >= MAX_TALKING:
            break
        if pt.link.cv_line and pt.link.company_text and pt.link.company_text not in used_texts:
            used_texts.add(pt.link.company_text)
            talking.append(Point(Note("affinity.talk.skill", {"skill": pt.term, "cv_line": pt.link.cv_line,
                                                              "company_text": pt.link.company_text}),
                                 pt.link, pt.term, pt.weight, pt.score))
    result.talking_points = talking[:MAX_TALKING]


def finalize(result: AffinityResult, weights: W.Weights) -> AffinityResult:
    """Scores every scope, picks the headline, decides the verdict and its reasons.

    Works on the stored components only, so a calibrated ``Weights`` can re-judge past
    results without the CV or the company data."""
    t = weights.thresholds
    for scope in result.scopes:
        _score_scope(scope, weights)
    general = next(s for s in result.scopes if s.id == GENERAL)
    directions = [s for s in result.scopes if s.id != GENERAL]
    applicable = [s for s in directions if s.applicable and s.affinity is not None]
    result.outside_directions = bool(directions) and not any(s.applicable for s in directions)
    def headline_value(s) -> float:
        # "postings first": a direction with an open posting that fits gets the same bonus
        # the ordering gives an APPLY (otherwise a 76 % "spontaneous" direction hid it)
        fits = bool(s.best_posting and s.best_posting.fits and s.affinity >= t.apply_min and not s.hard_fail)
        return s.affinity + (W.VERDICT_BONUS[W.APPLY] if fits else 0.0)

    headline = max(applicable, key=lambda s: (headline_value(s), -directions.index(s))) if applicable else general
    result.direction, result.direction_name = headline.id, headline.name
    result.affinity, result.confidence = headline.affinity, headline.confidence
    result.status = STATUS_OK if headline.affinity is not None else STATUS_NOT_ENOUGH
    result.best_posting = headline.best_posting or general.best_posting
    posting_fits = bool(headline.best_posting and headline.best_posting.fits)
    verdict, codes = W.decide(affinity=headline.affinity, confidence=headline.confidence, hard_fail=headline.hard_fail,
                              posting_fits=posting_fits, plausible_need=headline.plausible_need,
                              type_excluded=result.type_excluded, outside_directions=result.outside_directions,
                              directions_only=result.directions_only, thresholds=t)
    result.verdict = verdict
    reasons: list[Note] = []
    for code in codes:
        if code == "posting_fits":
            bp = headline.best_posting
            reasons.append(Note("affinity.reason.posting_fits", {"title": bp.title, "score": round(bp.score)}))
        elif code == "plausible_need":
            reasons.append(Note("affinity.reason.plausible_need", {"why": headline.plausible_why}))
        elif code == "hard_fail":
            reasons.append(headline.cap_reason or Note("affinity.reason.hard_fail", {"requirement": ""}))
        elif code == "low_affinity":
            reasons.append(Note("affinity.reason.low_affinity", {"score": round(headline.affinity or 0),
                                                                "threshold": round(t.not_fit)}))
        elif code == "type_excluded":
            reasons.append(Note("affinity.reason.type_excluded", {"type": f"affinity.type.{result.primary_type}"}))
        elif code == "not_enough_data":
            reasons.append(Note("affinity.reason.not_enough_data", {"coverage": round(100 * headline.coverage)}))
        elif code == "low_confidence":
            reasons.append(Note("affinity.reason.low_confidence",
                                {"confidence": f"affinity.confidence.{headline.confidence}"}))
        elif code == "no_plausible_need":
            reasons.append(Note("affinity.reason.no_plausible_need"))
        elif code == "outside_directions":
            reasons.append(Note("affinity.reason.outside_directions"))
    if verdict in (W.WATCH, W.SPONTANEOUS, W.NOT_A_FIT) and headline.best_posting and not headline.best_posting.fits \
            and not headline.hard_fail:
        bp = headline.best_posting
        reasons.append(Note("affinity.reason.posting_not_fit", {"title": bp.title, "score": round(bp.score)}))
    if result.outside_directions and "outside_directions" not in codes and verdict != W.UNKNOWN:
        reasons.append(Note("affinity.reason.outside_directions"))
    if headline.affinity is not None:
        reasons.append(Note("affinity.reason.affinity", {"score": round(headline.affinity),
                                                         "confidence": f"affinity.confidence.{headline.confidence}"}))
    for other in sorted(applicable, key=lambda s: -(s.affinity or 0)):
        if other is not headline:
            reasons.append(Note("affinity.direction.other", {"direction": other.name, "score": round(other.affinity)}))
    result.reasons = reasons
    _strengths_gaps(result, headline)
    return result


# --- public ---------------------------------------------------------------------------------


def _hints(demand: CompanyDemand) -> list[Note]:
    hints = []
    if not demand.site:
        hints.append(Note("affinity.hint.site"))
    if not demand.has_site_text:
        hints.append(Note("affinity.hint.build_card"))
    if not demand.postings:
        hints.append(Note("affinity.hint.paste_posting"))
    elif not demand.described_postings:
        hints.append(Note("affinity.hint.posting_text"))
    if not demand.ateco:
        hints.append(Note("affinity.hint.ateco"))
    return hints


def _extras(ctx: CvContext, demand: CompanyDemand) -> list[Point]:
    keys = {i.key for i in demand.items}
    extras = []
    for item in getattr(ctx.profile, "skills", None) or []:
        names = item.variants() if hasattr(item, "variants") else [item.value]
        if not names or any(item_key(n)[0] in keys for n in names):
            continue
        line = _first_line(item)
        core = bool(ctx.index.block_of(line)) if line in ctx.index.lines else False
        extras.append((not core, names[0].casefold(),
                       Point(Note("affinity.extra", {"skill": names[0]}), Link(cv_line=line, cv_term=names[0]),
                             names[0], 2.0 if core else 1.0)))
    extras.sort(key=lambda x: (x[0], x[1]))
    return [p for _, _, p in extras[:MAX_EXTRAS]]


def affinity(cv_profile, demand, preferences: AffinityPreferences | None = None, *, embedder=None,
             weights: W.Weights | None = None, today: date | None = None,
             context: CvContext | None = None) -> AffinityResult:
    """Affinity of a CV with a company (``demand``: a CompanyDemand, a card, a card row,
    or a (Company, Assessment, outcome) tuple).

    ``context``: a ``cv_context(...)`` computed once for many companies (batch).
    """
    prefs = preferences or AffinityPreferences()
    weights = weights or W.default_weights()
    demand = build_demand(demand)
    ctx = context or cv_context(cv_profile, embedder, today)
    evals = _evaluate_postings(ctx, demand, prefs, weights.thresholds)
    all_dirs = [_DirectionIndex.build(d) for d in prefs.directions]
    enabled = [_DirectionIndex.build(d) for d in prefs.enabled_directions()]
    scopes = [_build_scope(ctx, demand, prefs, weights.thresholds, evals, None, all_dirs)]
    for d in enabled:
        scopes.append(_build_scope(ctx, demand, prefs, weights.thresholds, evals, d, all_dirs))
    primary = demand.primary_type
    type_weight = prefs.type_weight(primary)
    role_best = scopes[0].components.get("role").score if scopes[0].components.get("role") else None
    result = AffinityResult(
        company_id=demand.company_id, company_name=demand.name, status=STATUS_OK, affinity=None,
        confidence="low", direction=GENERAL, direction_name="", verdict=W.UNKNOWN, scopes=scopes,
        hints=_hints(demand), extras=_extras(ctx, demand), opportunity=_opportunity(demand, evals, role_best),
        company_types=[t.to_dict() for t in demand.types], primary_type=primary, type_weight=type_weight,
        type_excluded=primary is not None and type_weight <= 0.0, directions_only=prefs.directions_only,
        distance_km=demand.distance_km,
        fingerprints={"data": demand.fingerprint(), "weights": weights.version, "preferences": prefs.fingerprint()},
    )
    finalize(result, weights)
    if result.affinity is not None:
        result.hints = [h for h in result.hints if h.key in ("affinity.hint.paste_posting", "affinity.hint.ateco")
                        and result.confidence == "low"]
    return result
