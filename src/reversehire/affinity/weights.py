"""Component weights, thresholds, the combination, the verdict and the ordering key.

All numbers of the decision live here.

Components (each 0..100 with a data ``quality`` 0..1, ``None`` = no data):

    skills       0.35  what they ask/use vs what the CV shows (core of "affinity")
    role         0.20  the user's roles vs the roles they hire or plausibly need
    sector       0.15  experience in their sector (or an adjacent one)
    seniority    0.10  years / level asked vs the CV (over- and under-qualification)
    constraints  0.10  hard requirements and screening answers (a failure also caps)
    practical    0.10  contract, remote, salary when published (distance only with a limit)

Justification: skills and role are what a recruiter checks first (study §2.7 puts
80 % of the posting score on requirement coverage); the sector is the next filter a
company applies to an open application; seniority, constraints and practical fit
matter but are often unknown, so they weigh less and are dropped (weights
renormalised) when there is no data. Opportunity (hiring now, growth) is NOT a
component: it is reported separately and only used by the verdict.

    affinity = sum_k w_k s_k / sum_k w_k            over components with data
    coverage = sum_k w_k q_k / sum_expected w_k     (q = data quality: explicit posting
                                                     requirements 1.0, site text 0.8,
                                                     inferred from products/type 0.5;
                                                     expected = skills, role, sector, plus
                                                     the posting-only components that have
                                                     data, so a company without postings is
                                                     not judged on what only a posting says)
    affinity = None ("not enough data") when coverage < MIN_COVERAGE or neither
               skills nor role has data
    a failed hard constraint caps the affinity at HARD_CAP
    confidence = high (coverage >= 0.75 and a described posting), medium (>= 0.55), low

Verdict (first rule that applies):

    UNKNOWN      affinity is None
    NOT_A_FIT    company type excluded, a hard constraint failed, outside every
                 direction with ``directions_only``, or affinity < NOT_FIT
    APPLY        an open posting fits (posting score >= POSTING_FIT, no gate, no failed
                 screening question) and affinity >= APPLY_MIN
    SPONTANEOUS  affinity >= SPONTANEOUS_MIN, the company plausibly needs the profile
                 and confidence is not low
    WATCH        otherwise

Ordering ("postings first, then spontaneous"): within the "positive" tier the value
is ``affinity + VERDICT_BONUS[verdict]`` times the company-type weight, so an APPLY at
60 % ranks above a SPONTANEOUS at 74 % but below one at 76 %; NOT_A_FIT and UNKNOWN
always come last. See ``sort_key``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

COMPONENTS = ("skills", "role", "sector", "seniority", "constraints", "practical")
DEFAULT_COMPONENT_WEIGHTS = {"skills": 0.35, "role": 0.20, "sector": 0.15, "seniority": 0.10,
                             "constraints": 0.10, "practical": 0.10}
MIN_WEIGHT, MAX_WEIGHT = 0.03, 0.45  # calibration bounds of one component
# Components every company can have data for; seniority, constraints and practical fit
# exist only with a posting and count in the coverage only when they have data.
CORE_COMPONENTS = ("skills", "role", "sector")

APPLY, SPONTANEOUS, WATCH, NOT_A_FIT, UNKNOWN = "APPLY", "SPONTANEOUS", "WATCH", "NOT_A_FIT", "UNKNOWN"
VERDICTS = (APPLY, SPONTANEOUS, WATCH, NOT_A_FIT, UNKNOWN)
POSITIVITY = {APPLY: 3, SPONTANEOUS: 2, WATCH: 1, NOT_A_FIT: 0}  # for calibration
VERDICT_TIER = {APPLY: 0, SPONTANEOUS: 0, WATCH: 1, NOT_A_FIT: 2, UNKNOWN: 3}
VERDICT_BONUS = {APPLY: 15.0, SPONTANEOUS: 0.0, WATCH: 0.0, NOT_A_FIT: 0.0, UNKNOWN: 0.0}


@dataclass
class Thresholds:
    min_coverage: float = 0.45
    not_fit: float = 40.0
    apply_min: float = 50.0
    spontaneous_min: float = 55.0
    posting_fit: float = 55.0  # recruiter_sim.match score of a posting that "fits"
    hard_cap: float = 25.0
    confidence_high: float = 0.75
    confidence_medium: float = 0.55
    plausible_overlap: float = 0.40  # stated/inferred skill overlap that shows a plausible need
    plausible_role: float = 50.0  # role score that shows a plausible need

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Thresholds":
        base = cls()
        for k, v in (d or {}).items():
            if hasattr(base, k) and isinstance(v, (int, float)) and not isinstance(v, bool):
                setattr(base, k, float(v))
        return base


DEFAULT_THRESHOLDS = Thresholds()
THRESHOLD_BOUND = 10.0  # calibration never moves a verdict threshold farther than this
CALIBRATED_THRESHOLDS = ("not_fit", "apply_min", "spontaneous_min")


@dataclass
class Weights:
    components: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_COMPONENT_WEIGHTS))
    thresholds: Thresholds = field(default_factory=Thresholds)
    judgements: int = 0  # feedback items used to calibrate (0 = defaults)

    def normalized(self) -> dict[str, float]:
        total = sum(max(0.0, self.components.get(k, 0.0)) for k in COMPONENTS)
        if total <= 0:
            return dict(DEFAULT_COMPONENT_WEIGHTS)
        return {k: max(0.0, self.components.get(k, 0.0)) / total for k in COMPONENTS}

    def to_dict(self) -> dict:
        return {"components": {k: round(self.components.get(k, 0.0), 6) for k in COMPONENTS},
                "thresholds": self.thresholds.to_dict(), "judgements": self.judgements}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Weights":
        d = d or {}
        comps = dict(DEFAULT_COMPONENT_WEIGHTS)
        for k, v in (d.get("components") or {}).items():
            if k in comps and isinstance(v, (int, float)):
                comps[k] = float(v)
        return cls(comps, Thresholds.from_dict(d.get("thresholds")), int(d.get("judgements", 0) or 0))

    @property
    def version(self) -> str:
        data = json.dumps(self.to_dict(), sort_keys=True)
        return hashlib.sha256(data.encode("utf-8")).hexdigest()[:12]


def default_weights() -> Weights:
    return Weights()


def combine(scores: dict[str, tuple[float | None, float]], weights: Weights) -> tuple[float | None, float]:
    """(affinity 0..100 or None before the threshold test, coverage 0..1).

    ``scores``: component -> (score or None, data quality 0..1).
    """
    w = weights.normalized()
    total = used = covered = 0.0
    expected = sum(w[k] for k in CORE_COMPONENTS)
    for key in COMPONENTS:
        score, quality = scores.get(key, (None, 0.0))
        if score is None:
            continue
        total += w[key] * score
        used += w[key]
        covered += w[key] * max(0.0, min(1.0, quality))
        if key not in CORE_COMPONENTS:
            expected += w[key]
    if used <= 0:
        return None, 0.0
    return total / used, covered / expected if expected > 0 else 0.0


def confidence_label(coverage: float, has_described_posting: bool, thresholds: Thresholds) -> str:
    if coverage >= thresholds.confidence_high and has_described_posting:
        return "high"
    if coverage >= thresholds.confidence_medium:
        return "medium"
    return "low"


def enough_data(coverage: float, scores: dict[str, tuple[float | None, float]], thresholds: Thresholds) -> bool:
    core = any(scores.get(k, (None, 0))[0] is not None for k in ("skills", "role"))
    return core and coverage + 1e-9 >= thresholds.min_coverage


def decide(*, affinity: float | None, confidence: str, hard_fail: bool, posting_fits: bool,
           plausible_need: bool, type_excluded: bool, outside_directions: bool, directions_only: bool,
           thresholds: Thresholds) -> tuple[str, list[str]]:
    """(verdict, reason codes) - the codes are explained by the engine with evidence."""
    if affinity is None:
        return UNKNOWN, ["not_enough_data"]
    if type_excluded:
        return NOT_A_FIT, ["type_excluded"]
    if hard_fail:
        return NOT_A_FIT, ["hard_fail"]
    if directions_only and outside_directions:
        return NOT_A_FIT, ["outside_directions"]
    if affinity < thresholds.not_fit:
        return NOT_A_FIT, ["low_affinity"]
    if posting_fits and affinity >= thresholds.apply_min:
        return APPLY, ["posting_fits"]
    if affinity >= thresholds.spontaneous_min and plausible_need and confidence != "low":
        return SPONTANEOUS, ["plausible_need"]
    reasons = []
    if confidence == "low":
        reasons.append("low_confidence")
    if not plausible_need:
        reasons.append("no_plausible_need")
    return WATCH, reasons or ["affinity"]


def order_value(verdict: str, affinity: float | None, type_weight: float = 1.0) -> float:
    if affinity is None:
        return 0.0
    return (affinity + VERDICT_BONUS.get(verdict, 0.0)) * max(0.0, min(1.0, type_weight))


def sort_key(result) -> tuple:
    """Ascending sort key of an AffinityResult: positive verdicts first, then by value."""
    return (VERDICT_TIER.get(result.verdict, 3), -order_value(result.verdict, result.affinity, result.type_weight),
            {"high": 0, "medium": 1, "low": 2}.get(result.confidence, 3), result.company_name.casefold(),
            result.company_id)
