"""Match score posting <-> CV (fonti/RECRUITER_STUDY.md §2.7): "the more skills in
common, the higher the score".

On top of `scoring.requirements.extract_items` (units, must/nice/context, typed
requirements) each skill named in the posting becomes one item:

    w_i = 5 must | 3 nice | 1 context (+1 duty)
    c_i = credit(level): 4 -> 1.00, 3 -> 0.85, 2 -> 0.50, 1 -> 0.20, 0/None -> 0
          years: min(1, cv_years / required_years)
    r_i = recency of the CV evidence: 1 if last used <= 24 months ago, else
          max(0.6, exp(-(m - 24) / 96)); a skill only listed (no dated job) -> 0.8
    AdFit = sum(w c r) / sum(w)                      (share of the ad covered)
    CvUse = sum(s_k used_k) / sum(s_k) over the CV skills, s_k = 2 when the
            skill is used in a dated job (core), 1 otherwise; used_k = 1 when the
            posting names it, 0.5 when it names a skill of the same family
    Score = 100 (0.8 AdFit + 0.2 CvUse); a must-have of a hard type contradicted
            by the CV (requirements gate) caps it at GATE_CAP.

Skill evidence levels: 4 named inside a dated job, 3 named elsewhere in the CV
(skills list, summary), 2 adjacent (a skill of the same family, part of a
multi-word term, or the embedder), 0 not found. Context free-text sentences
count only as positive evidence (level >= 2), as in `requirements.check_items`.

Deterministic and explainable: every item quotes the posting line and the CV
line it was decided on.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from ..cv import skills as cv_skills
from ..models import item_variants
from ..scoring import requirements as req
from ..scoring.relevance import calibrate_similarity, normalize_text
from .cvindex import LITERAL, SEMANTIC, STEM, TAXONOMY, CvIndex, fold, forms_for, month_text
from .model import Message, Target, msg
from .topics import topic_of

CREDIT = {4: 1.00, 3: 0.85, 2: 0.50, 1: 0.20, 0: 0.0}
AD_WEIGHT, CV_WEIGHT = 0.8, 0.2
GATE_CAP = 20
RECENT_MONTHS = 24
RECENCY_DECAY_MONTHS = 96
RECENCY_FLOOR = 0.6
LISTED_ONLY_RECENCY = 0.8
CORE_WEIGHT, OTHER_WEIGHT = 2, 1
FAMILY_USE = 0.5
FREE_STRONG_SHARE = 0.6  # share of a sentence's content words in one CV line -> level 3
FREE_PARTIAL_SHARE = 0.34  # -> level 2
MAX_SKILL_WORDS = 6  # free items up to this many words count as "skills" in n/m
SEMANTIC_SKILL_CAP = 2  # the embedder alone never gives a named skill more than "partial"
CONFIDENCE_PARSED_WEIGHT = 0.6
CONFIDENCE_LOW, CONFIDENCE_HIGH = 0.5, 0.75
NO_HEADING_FACTOR = 0.6  # parse share when no must/nice structure could be read
MODES = (LITERAL, STEM, TAXONOMY, SEMANTIC)

MATCHED, PARTIAL, MISSING, UNCERTAIN, CONTRADICTED = "matched", "partial", "missing", "uncertain", "contradicted"


@dataclass
class MatchItem:
    """One requirement of the posting with its credit."""

    text: str  # skill name (posting wording) or the requirement line
    priority: str  # "must" | "nice" | "context"
    kind: str  # "skill" | "duty" | "years" | "language" | "degree" | "licence" | "car" | "travel"
    weight: float
    level: int | None
    credit: float
    recency: float
    status: str
    posting_line: str
    is_skill: bool = True
    cv_line: str | None = None
    cv_term: str | None = None  # CV wording that matched (may differ from `text`)
    literal: bool = False  # the posting wording itself is in the CV
    blocking: bool = False
    months: int | None = None
    last_used: str | None = None  # "YYYY-MM" | "present" | None
    entry_family: str = ""

    @property
    def earned(self) -> float:
        return self.weight * self.credit * self.recency

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class CvSkill:
    name: str
    core: bool
    used: float  # 0, 0.5 or 1
    line: str | None = None


@dataclass
class MatchResult:
    score: float
    ad_fit: float
    cv_use: float
    mode: str
    items: list[MatchItem] = field(default_factory=list)
    cv_skills: list[CvSkill] = field(default_factory=list)
    capped: bool = False
    cap: int | None = None
    cap_reason: str | None = None
    confidence: float = 0.0
    parsed_share: float = 0.0

    @property
    def skill_items(self) -> list[MatchItem]:
        return [i for i in self.items if i.is_skill]

    @property
    def in_common(self) -> list[MatchItem]:
        return [i for i in self.skill_items if i.status == MATCHED]

    @property
    def partial(self) -> list[MatchItem]:
        return [i for i in self.skill_items if i.status == PARTIAL]

    @property
    def uncertain(self) -> list[MatchItem]:
        return [i for i in self.skill_items if i.status == UNCERTAIN]

    def _missing(self, priority: str) -> list[MatchItem]:
        return [i for i in self.skill_items if i.status in (MISSING, CONTRADICTED) and i.priority == priority]

    @property
    def missing_must(self) -> list[MatchItem]:
        return self._missing("must")

    @property
    def missing_nice(self) -> list[MatchItem]:
        return self._missing("nice")

    @property
    def missing_other(self) -> list[MatchItem]:
        return self._missing("context")

    @property
    def extra_strengths(self) -> list[CvSkill]:
        return [s for s in self.cv_skills if s.used == 0]

    @property
    def common(self) -> int:
        return len(self.in_common)

    @property
    def total(self) -> int:
        return len(self.skill_items)

    @property
    def musts(self) -> list[MatchItem]:
        return [i for i in self.items if i.priority == "must"]

    @property
    def musts_met(self) -> int:
        return sum(1 for i in self.musts if i.status == MATCHED)

    @property
    def confidence_label(self) -> str:
        if self.confidence < CONFIDENCE_LOW:
            return "low"
        return "medium" if self.confidence < CONFIDENCE_HIGH else "high"

    def summary(self) -> list[Message]:
        result = [
            msg("rsim.match.summary", common=self.common, total=self.total, musts_met=self.musts_met,
                musts=len(self.musts)),
            msg("rsim.match.score", score=round(self.score), ad_fit=round(self.ad_fit * 100),
                cv_use=round(self.cv_use * 100)),
            msg("rsim.match.confidence", label=self.confidence_label, value=round(self.confidence * 100),
                parsed=round(self.parsed_share * 100)),
        ]
        if self.capped:
            result.insert(1, msg("rsim.match.capped", cap=self.cap, reason=self.cap_reason or ""))
        return result

    def to_dict(self) -> dict:
        return {
            "score": self.score, "ad_fit": self.ad_fit, "cv_use": self.cv_use, "mode": self.mode,
            "capped": self.capped, "cap": self.cap, "cap_reason": self.cap_reason,
            "confidence": self.confidence, "confidence_label": self.confidence_label,
            "parsed_share": self.parsed_share, "common": self.common, "total": self.total,
            "musts_met": self.musts_met, "musts": len(self.musts),
            "items": [i.to_dict() for i in self.items],
            "in_common": [i.text for i in self.in_common], "partial": [i.text for i in self.partial],
            "missing_must": [i.text for i in self.missing_must],
            "missing_nice": [i.text for i in self.missing_nice],
            "uncertain": [i.text for i in self.uncertain],
            "extra_strengths": [s.name for s in self.extra_strengths],
            "summary": [m.to_dict() for m in self.summary()],
        }


# --- evidence of one term --------------------------------------------------------------


def recency_factor(months_ago: int) -> float:
    if months_ago <= RECENT_MONTHS:
        return 1.0
    return max(RECENCY_FLOOR, math.exp(-(months_ago - RECENT_MONTHS) / RECENCY_DECAY_MONTHS))


@dataclass
class TermEvidence:
    level: int
    line: str | None = None
    cv_term: str | None = None
    literal: bool = False
    recency: float = 1.0
    months: int | None = None
    last_used: str | None = None
    dated: bool = False  # found inside a dated job


def _original_wording(line: str, form: str) -> str:
    """The CV's own wording of a folded form, cut from the line."""
    import re

    folded = fold(line)
    m = re.search(r"(?<![\w+#.])" + re.escape(form) + r"(?![\w+#])", folded)
    if m and len(folded) == len(line):
        return line[m.start():m.end()]
    return form


class _Embeddings:
    """CV line vectors, computed once per index (semantic mode only)."""

    def __init__(self, index: CvIndex, embedder):
        self.embedder = embedder
        self.lines = [x for x in index.lines if len(x.split()) >= 2][:200]
        self.vectors = None
        self.interval = tuple(getattr(embedder, "requirement_range", None)
                              or getattr(embedder, "similarity_range", (0.0, 1.0)))
        if embedder is not None and self.lines:
            try:
                self.vectors = np.asarray(embedder.embed(self.lines, kind="query"))
            except Exception:  # noqa: BLE001 - the semantic part is optional
                self.vectors = None

    @property
    def ready(self) -> bool:
        return self.vectors is not None and len(self.lines) > 0

    def best(self, text: str) -> tuple[float, str | None]:
        if not self.ready:
            return 0.0, None
        try:
            q = np.asarray(self.embedder.embed([text], kind="query"))[0]
        except Exception:  # noqa: BLE001
            return 0.0, None
        sims = self.vectors @ q
        j = int(np.argmax(sims))
        return calibrate_similarity(float(sims[j]), self.interval) / 100.0, self.lines[j]


def _level_from_similarity(value: float) -> int | None:
    for threshold, level in req.SKILL_LEVEL_THRESHOLDS:
        if value >= threshold:
            return level
    return None


def term_evidence(term: str, index: CvIndex, mode: str, embeddings: _Embeddings | None = None,
                  family_lines: dict[str, str] | None = None) -> TermEvidence:
    """Evidence level of a named skill in the CV under a keyword mode."""
    hits = index.find(term, mode)
    if hits:
        dated_blocks = []
        for h in hits:
            block = index.block_of(index.lines[h.line])
            if block is not None and block not in dated_blocks:
                dated_blocks.append(block)
        first = hits[0]
        literal = any(h.literal for h in hits)
        line = index.lines[first.line]
        cv_term = _original_wording(line, first.form)
        if dated_blocks:
            from .cvindex import merged_months

            latest = max(dated_blocks, key=lambda b: b.end)
            months_ago = max(0, index.today_index - latest.end)
            line = next((index.lines[h.line] for h in hits if index.block_of(index.lines[h.line]) is latest),
                        line)
            return TermEvidence(4, line, cv_term, literal, recency_factor(months_ago),
                                merged_months([(b.start, b.end) for b in dated_blocks]),
                                "present" if latest.in_progress else month_text(latest.end), True)
        return TermEvidence(3, line, cv_term, literal, LISTED_ONLY_RECENCY)
    if mode != LITERAL:
        entry = cv_skills.dictionary().lookup(term)
        if entry is not None and entry.family and family_lines and entry.family in family_lines:
            if mode in (TAXONOMY, SEMANTIC):
                return TermEvidence(2, family_lines[entry.family], recency=LISTED_ONLY_RECENCY)
        words = [w for w in normalize_text(term).split() if len(w) >= 3]
        if len(words) >= 2:
            wanted = req.stems(term)
            best, best_line = 0.0, None
            for line, line_stems in zip(index.lines, index.stems):
                share = len(wanted & line_stems) / max(1, len(wanted))
                if share > best:
                    best, best_line = share, line
            if best >= 0.5:
                return TermEvidence(2, best_line, recency=LISTED_ONLY_RECENCY)
    if mode == SEMANTIC and embeddings is not None and embeddings.ready:
        value, line = embeddings.best(term)
        level = _level_from_similarity(value)
        if level is not None:
            return TermEvidence(min(level, SEMANTIC_SKILL_CAP), line, recency=LISTED_ONLY_RECENCY)
    return TermEvidence(0)


def _sentence_evidence(text: str, index: CvIndex, mode: str, embeddings: _Embeddings | None) -> TermEvidence:
    """Evidence of a free requirement sentence: share of its content words in one CV line."""
    if mode == LITERAL:
        words = {w for w in normalize_text(text).split() if len(w) >= 4 and w not in req._STOP_WORDS}
        line_sets = [set(normalize_text(x).split()) for x in index.lines]
    else:
        words = {s for s in req.stems(text) if len(s) >= 4}
        line_sets = list(index.stems)
    best, best_line = 0.0, None
    if words:
        for line, tokens in zip(index.lines, line_sets):
            share = len(words & tokens) / len(words)
            if share > best:
                best, best_line = share, line
    level = 3 if best >= FREE_STRONG_SHARE else 2 if best >= FREE_PARTIAL_SHARE else 0
    if mode == SEMANTIC and embeddings is not None and embeddings.ready:
        value, line = embeddings.best(text)
        semantic = _level_from_similarity(value) or 0
        if semantic > level:
            level, best_line = semantic, line
    if level == 0:
        return TermEvidence(0)
    dated = index.block_of(best_line) if best_line else None
    recency = recency_factor(max(0, index.today_index - dated.end)) if dated else LISTED_ONLY_RECENCY
    return TermEvidence(level, best_line, recency=recency, dated=dated is not None)


# --- items -------------------------------------------------------------------------------

_PRIORITY_RANK = {"must": 0, "nice": 1, "context": 2}


def _skill_status(level: int | None) -> str:
    if level is None:
        return UNCERTAIN
    return MATCHED if level >= 3 else PARTIAL if level == 2 else MISSING


def _typed_status(level: int | None) -> str:
    if level is None:
        return UNCERTAIN
    return {4: MATCHED, 3: MATCHED, 2: PARTIAL, 1: MISSING}.get(level, CONTRADICTED)


def _weight(priority: str, kind: str) -> float:
    return req.PRIORITY_WEIGHTS[priority] + (req.DUTY_BONUS if kind == "duty" else 0)


def _dictionary_hits(text: str):
    d = cv_skills.dictionary()
    short = len(text.split()) <= 4 or cv_skills.list_like(text)
    seen, result = set(), []
    for hit in d.find(text, list_context=short):
        if hit.entry.index not in seen:
            seen.add(hit.entry.index)
            result.append(hit)
    return result


def _typed_item(item, profile) -> MatchItem:
    check = req.check_items([item], profile)[0]
    level = check.level
    if item.kind == "years":
        cv_years = getattr(profile, "years_experience", None)
        credit = min(1.0, cv_years / item.data["years"]) if cv_years is not None and item.data.get("years") else 0.0
    else:
        credit = CREDIT.get(level, 0.0) if level is not None else 0.0
    return MatchItem(text=item.text, priority=item.priority, kind=item.kind, weight=_weight(item.priority, item.kind),
                     level=level, credit=credit, recency=1.0, status=_typed_status(level),
                     posting_line=item.text, is_skill=False, cv_line=check.evidence, blocking=check.blocking)


def _target(posting) -> Target:
    if isinstance(posting, Target):
        return posting
    if isinstance(posting, str):
        return Target(title="", description=posting)
    return Target.from_posting(posting)


def _family_lines(index: CvIndex, mode: str) -> dict[str, str]:
    """Skill family -> a CV line naming a dictionary skill of that family."""
    families: dict[str, str] = {}
    if mode == LITERAL:
        return families
    d = cv_skills.dictionary()
    for line in index.lines:
        for hit in d.find(line, list_context=True):
            if hit.entry.family and hit.entry.family not in families:
                families[hit.entry.family] = line
    return families


# Public names of the two helpers above, used by the affinity engine (kept stable; the
# underscored names stay for the existing callers).
Embeddings = _Embeddings
family_lines = _family_lines


def build_items(target: Target, profile, index: CvIndex, mode: str,
                embeddings: _Embeddings | None = None) -> tuple[list[MatchItem], float]:
    """Scored items of the target and the share of the posting that could be read."""
    text = target.requirement_text()
    posting = target.posting
    units = [u for u in req._units(text) if req._heading_section(u) is None]
    raw = req.extract_items(text, posting)
    structured = any(i.priority in ("must", "nice") for i in raw)
    parsed = sum(1 for i in raw)
    parsed_share = min(1.0, parsed / len(units)) if units else (1.0 if raw else 0.0)
    if raw and not structured:
        parsed_share *= NO_HEADING_FACTOR
    family_lines = _family_lines(index, mode)
    items: list[MatchItem] = []
    by_entry: dict[int, MatchItem] = {}
    for item in raw:
        if item.kind == "offer":
            continue
        if item.kind in req.TYPED_KINDS:
            items.append(_typed_item(item, profile))
            continue
        hits = _dictionary_hits(item.text)
        if hits:
            for hit in hits:
                term = hit.surface.strip()
                priority = item.priority if item.kind == "skill" else "context"
                existing = by_entry.get(hit.entry.index)
                if existing is not None:
                    if _PRIORITY_RANK[priority] < _PRIORITY_RANK[existing.priority]:
                        existing.priority, existing.kind = priority, item.kind
                        existing.weight = _weight(priority, item.kind)
                        existing.posting_line = item.text
                        if existing not in items:
                            items.append(existing)
                    continue
                ev = term_evidence(term, index, mode, embeddings, family_lines)
                m = MatchItem(text=term, priority=priority, kind=item.kind, weight=_weight(priority, item.kind),
                              level=ev.level, credit=CREDIT[ev.level], recency=ev.recency if ev.level else 1.0,
                              status=_skill_status(ev.level), posting_line=item.text, is_skill=True,
                              cv_line=ev.line, cv_term=ev.cv_term, literal=ev.literal, months=ev.months,
                              last_used=ev.last_used, entry_family=hit.entry.family)
                by_entry[hit.entry.index] = m
                if priority != "context" or ev.level >= req.CONTEXT_MIN_LEVEL:
                    items.append(m)  # skills of duty/context sentences count only as evidence
            continue
        if topic_of(item.text):
            continue  # location, right to work, availability...: a knock-out question, not a skill
        ev = _sentence_evidence(item.text, index, mode, embeddings)
        if item.priority == "context" and ev.level < req.CONTEXT_MIN_LEVEL:
            continue  # plain context sentences count only as positive evidence
        words = len(normalize_text(item.text).split())
        is_skill = item.kind == "skill" and item.priority in ("must", "nice") and words <= MAX_SKILL_WORDS
        items.append(MatchItem(text=item.text, priority=item.priority, kind=item.kind,
                               weight=_weight(item.priority, item.kind), level=ev.level,
                               credit=CREDIT[ev.level], recency=ev.recency if ev.level else 1.0,
                               status=_skill_status(ev.level), posting_line=item.text, is_skill=is_skill,
                               cv_line=ev.line))
    return items, parsed_share


def cv_skill_list(profile, index: CvIndex, target_text: str, mode: str) -> list[CvSkill]:
    """The CV's skills with their weight (core/other) and whether the posting uses them."""
    posting_folded = fold(target_text)
    posting_families = {h.entry.family for h in cv_skills.dictionary().find(target_text, list_context=True)
                        if h.entry.family}
    result: list[CvSkill] = []
    seen: set[str] = set()
    from .cvindex import _term_regex

    for item in getattr(profile, "skills", None) or []:
        names = item_variants(item)
        if not names or fold(names[0]) in seen:
            continue
        seen.add(fold(names[0]))
        core = any(index.block_of(index.lines[h.line]) for n in names for h in index.find(n, mode))
        used = 0.0
        for n in names:
            forms = forms_for(n, mode) if mode != LITERAL else [fold(n)]
            if any(f and _term_regex(f).search(posting_folded) for f in forms):
                used = 1.0
                break
        if not used:
            entry = cv_skills.dictionary().lookup(names[0])
            if entry is None and len(names) > 1:
                entry = cv_skills.dictionary().lookup(names[1])
            if entry is not None and entry.family and entry.family in posting_families and mode != LITERAL:
                used = FAMILY_USE
        line = next((e.text for e in getattr(item, "evidence", []) or [] if getattr(e, "source", "cv") == "cv"),
                    None)
        result.append(CvSkill(names[-1] if len(names) > 1 else names[0], core, used, line))
    result.sort(key=lambda s: (not s.core, s.name.lower()))
    return result


def score_items(items: list[MatchItem], cv_skills_list: list[CvSkill]) -> tuple[float, float, float]:
    """(score 0..100, AdFit 0..1, CvUse 0..1) before the gate."""
    total = sum(i.weight for i in items)
    ad_fit = sum(i.earned for i in items) / total if total else 0.0
    weights = [(CORE_WEIGHT if s.core else OTHER_WEIGHT, s.used) for s in cv_skills_list]
    available = sum(w for w, _ in weights)
    cv_use = sum(w * u for w, u in weights) / available if available else ad_fit
    return 100.0 * (AD_WEIGHT * ad_fit + CV_WEIGHT * cv_use), ad_fit, cv_use


def match_posting(cv_profile, posting, *, mode: str = TAXONOMY, embedder=None, today: date | None = None,
                  cv_text: str | None = None, index: CvIndex | None = None) -> MatchResult:
    """Match score of a posting (models.Posting, Target or plain text) against a CV profile.

    `mode`: keyword mode of the skill matching (default "taxonomy": synonyms of
    the app's skill dictionary); "semantic" uses the embedder when given.
    `cv_text`/`index`: the CV text as a parser sees it (default: profile.text).
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    target = _target(posting)
    if index is None:
        index = CvIndex.build(cv_text if cv_text is not None else getattr(cv_profile, "text", "") or "", today)
    embeddings = _Embeddings(index, embedder) if (mode == SEMANTIC and embedder is not None) else None
    items, parsed_share = build_items(target, cv_profile, index, mode, embeddings)
    cv_list = cv_skill_list(cv_profile, index, target.requirement_text() + "\n" + target.title, mode)
    score, ad_fit, cv_use = score_items(items, cv_list)
    blocking = next((i for i in items if i.blocking), None)
    capped = blocking is not None and score > GATE_CAP
    if blocking is not None:
        score = min(score, GATE_CAP)
    total_weight = sum(i.weight for i in items)
    verifiable = sum(i.weight for i in items if i.level is not None) / total_weight if total_weight else 0.0
    confidence = (CONFIDENCE_PARSED_WEIGHT * parsed_share + (1 - CONFIDENCE_PARSED_WEIGHT) * verifiable
                  if items else 0.0)
    return MatchResult(score=round(score, 1), ad_fit=ad_fit, cv_use=cv_use, mode=mode, items=items,
                       cv_skills=cv_list, capped=capped, cap=GATE_CAP if blocking is not None else None,
                       cap_reason=blocking.text if blocking is not None else None,
                       confidence=round(confidence, 3), parsed_share=round(parsed_share, 3))
