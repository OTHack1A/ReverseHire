"""Personal calibration from the user's judgements of past verdicts.

``Feedback(company_id, agrees, user_verdict=None)``: the user agrees with a verdict or
not, optionally saying which verdict would have been right.

``calibrate(feedback, results, weights=None) -> CalibrationReport``. Method (simple,
bounded, explainable):

1. Only judgements whose result had a number (affinity not None) count; the latest
   judgement per company wins. Nothing changes before ``MIN_JUDGEMENTS`` of them, nor
   when fewer than ``MIN_DISAGREEMENT_SHARE`` disagree (the defaults work for this user).
2. Direction of each disagreement: +1 when the user is more positive than the verdict
   (``weights.POSITIVITY``: APPLY 3 > SPONTANEOUS 2 > WATCH 1 > NOT_A_FIT 0), -1 when
   more negative; without a ``user_verdict`` it is -1 for a positive verdict (APPLY,
   SPONTANEOUS), +1 for NOT_A_FIT, and the judgement is skipped for WATCH/UNKNOWN.
3. Component weights: g_k = mean over disagreements of s * (score_k - affinity) / 100
   (a component that was above the affinity when the user wanted more gains weight).
   step_k = clamp(LEARNING_RATE * g_k, +-MAX_STEP); w_k in [MIN_WEIGHT, MAX_WEIGHT]
   (no component may dominate), then renormalised with the cap kept.
4. Thresholds (not_fit, apply_min, spontaneous_min): moved by THRESHOLD_STEP against the
   mean direction when |mean| >= THRESHOLD_TRIGGER, never farther than THRESHOLD_BOUND
   from the defaults.
5. The report gives before/after weights and how many past verdicts would change
   (``engine.finalize`` re-judges the stored components with the new weights).

Deterministic: judgements are processed in company-id order.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field

from . import weights as W
from .model import AffinityResult
from .texts import Note

MIN_JUDGEMENTS = 8
MIN_DISAGREEMENT_SHARE = 0.2
LEARNING_RATE = 0.5
MAX_STEP = 0.05
THRESHOLD_STEP = 2.0
THRESHOLD_TRIGGER = 0.3


@dataclass
class Feedback:
    company_id: str
    agrees: bool
    user_verdict: str | None = None
    when: float = 0.0  # timestamp; the latest judgement per company wins

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Feedback":
        verdict = d.get("user_verdict")
        return cls(str(d["company_id"]), bool(d["agrees"]), verdict if verdict in W.VERDICTS else None,
                   float(d.get("when", 0.0) or 0.0))


@dataclass
class CalibrationReport:
    before: W.Weights
    after: W.Weights
    used: int  # judgements used
    disagreements: int
    applied: bool
    changed: list[tuple[str, str, str]] = field(default_factory=list)  # (company_id, old, new)
    note: Note | None = None

    @property
    def changed_count(self) -> int:
        return len(self.changed)

    def to_dict(self) -> dict:
        return {"before": self.before.to_dict(), "after": self.after.to_dict(), "used": self.used,
                "disagreements": self.disagreements, "applied": self.applied,
                "changed": [list(c) for c in self.changed], "changed_count": self.changed_count,
                "note": self.note.to_dict() if self.note else None}


def _direction(fb: Feedback, verdict: str) -> int:
    if fb.agrees:
        return 0
    if fb.user_verdict in W.POSITIVITY and verdict in W.POSITIVITY:
        diff = W.POSITIVITY[fb.user_verdict] - W.POSITIVITY[verdict]
        return (diff > 0) - (diff < 0)
    if verdict in (W.APPLY, W.SPONTANEOUS):
        return -1
    if verdict == W.NOT_A_FIT:
        return 1
    return 0


def _cap_normalize(values: dict[str, float]) -> dict[str, float]:
    """Sum 1 with every weight in [MIN_WEIGHT, MAX_WEIGHT] (iterative clipping)."""
    fixed: dict[str, float] = {}
    result = dict(values)
    for _ in range(len(values) + 1):
        free = {k: max(v, 1e-9) for k, v in values.items() if k not in fixed}
        if not free:
            result = dict(fixed)
            break
        rest = 1.0 - sum(fixed.values())
        total = sum(free.values())
        scaled = {k: v / total * rest for k, v in free.items()}
        violated = {k: (W.MAX_WEIGHT if v > W.MAX_WEIGHT else W.MIN_WEIGHT) for k, v in scaled.items()
                    if v > W.MAX_WEIGHT + 1e-12 or v < W.MIN_WEIGHT - 1e-12}
        result = {**fixed, **scaled}
        if not violated:
            break
        fixed.update(violated)
    return {k: round(result[k], 6) for k in values}


def rejudge(results: list[AffinityResult], weights: W.Weights) -> list[AffinityResult]:
    """Copies of the results finalised again with ``weights``."""
    from .engine import finalize

    return [finalize(copy.deepcopy(r), weights) for r in results]


def calibrate(feedback: list[Feedback], results: list[AffinityResult],
              weights: W.Weights | None = None) -> CalibrationReport:
    before = weights or W.default_weights()
    by_id = {r.company_id: r for r in results}
    latest: dict[str, Feedback] = {}
    for fb in sorted(feedback, key=lambda f: (f.company_id, f.when)):
        latest[fb.company_id] = fb
    pairs = [(latest[k], by_id[k]) for k in sorted(latest) if k in by_id and by_id[k].affinity is not None]
    used = len(pairs)
    signed = [(s, r) for fb, r in pairs for s in [_direction(fb, r.verdict)] if s != 0]
    disagreements = len(signed)
    if used < MIN_JUDGEMENTS:
        return CalibrationReport(before, before, used, disagreements, False,
                                 note=Note("affinity.calibration.too_few", {"count": used, "needed": MIN_JUDGEMENTS}))
    if disagreements < MIN_DISAGREEMENT_SHARE * used:
        return CalibrationReport(before, before, used, disagreements, False, note=Note("affinity.calibration.agrees"))
    current = before.normalized()
    gradient = {k: 0.0 for k in W.COMPONENTS}
    for s, result in signed:
        for key, comp in result.components.items():
            if comp.score is not None and key in gradient:
                gradient[key] += s * (comp.score - result.affinity) / 100.0
    new = {}
    for key in W.COMPONENTS:
        step = max(-MAX_STEP, min(MAX_STEP, LEARNING_RATE * gradient[key] / disagreements))
        new[key] = current[key] + step
    components = _cap_normalize(new)
    thresholds = W.Thresholds.from_dict(before.thresholds.to_dict())
    mean = sum(s for s, _ in signed) / disagreements
    if abs(mean) >= THRESHOLD_TRIGGER:
        delta = -THRESHOLD_STEP if mean > 0 else THRESHOLD_STEP
        for name in W.CALIBRATED_THRESHOLDS:
            base = getattr(W.DEFAULT_THRESHOLDS, name)
            value = getattr(thresholds, name) + delta
            setattr(thresholds, name, max(base - W.THRESHOLD_BOUND, min(base + W.THRESHOLD_BOUND, value)))
    after = W.Weights(components, thresholds, before.judgements + used)
    old = {r.company_id: r.verdict for r in results}
    changed = [(r.company_id, old[r.company_id], r.verdict) for r in rejudge(results, after)
               if r.verdict != old[r.company_id]]
    changed.sort()
    return CalibrationReport(before, after, used, disagreements, True, changed,
                             Note("affinity.calibration.updated", {"count": used, "changed": len(changed)}))
