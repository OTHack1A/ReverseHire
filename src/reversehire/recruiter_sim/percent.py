"""Every simulated score as a percentage 0-100 %, whatever the system shows the recruiter.

The engine computes ONE simulated score per system on 0..100 (engine.build_score);
each system then presents it on its own scale (`SystemProfile.score.kind`). The GUI
always shows the percentage first and keeps the system's own presentation as a
secondary note ("this system shows grades A–D to the recruiter: grade B").

Mapping between each scale and the percentage (used both ways: `to_percent` reads a
value given on the system's scale, `ScoreBreakdown.value` is already the percentage):

- "percent":        the value itself, clamped to 0..100.
- "rating_points":  0..5 points (stars) -> points x 20.
- "grade_AD":       A / B / C / D -> the middle of the band the engine uses to give
                    the grade (A >= 75, B >= 55, C >= 35, D below):
                    A = 87.5, B = 65, C = 45, D = 17.5.
- "high_med_low":   High / Medium / Low -> the middle of the engine's bands
                    (High >= 70, Medium >= 40): High = 85, Medium = 55, Low = 20.
- "criteria_met":   met of total must-have requirements -> met / total x 100
                    (no requirement to check -> None: nothing to measure).
- "none":           the system shows no score; the recruiter sees the requirements
                    met: same as "criteria_met" when reading a value, and the
                    simulated score is still shown as a percentage, with the note.
- a 0-10 scale (`to_percent("ten", x)`) -> x x 10.
"""

from __future__ import annotations

GRADE_PERCENT = {"A": 87.5, "B": 65.0, "C": 45.0, "D": 17.5}
LEVEL_PERCENT = {"high": 85.0, "medium": 55.0, "low": 20.0}
SCALE_KINDS = ("percent", "rating_points", "grade_AD", "high_med_low", "criteria_met", "none")


def clamp(value) -> int | None:
    """An integer percentage 0..100; None stays None."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return int(round(max(0.0, min(100.0, number))))


def to_percent(kind: str, raw, total: int | None = None) -> int | None:
    """A value shown on a system's scale -> 0..100 % (see the module docstring)."""
    if raw is None:
        return None
    if kind == "percent":
        return clamp(raw)
    if kind == "rating_points":
        try:
            return clamp(float(raw) * 20)
        except (TypeError, ValueError):
            return None
    if kind == "ten":
        try:
            return clamp(float(raw) * 10)
        except (TypeError, ValueError):
            return None
    if kind == "grade_AD":
        return clamp(GRADE_PERCENT.get(str(raw).strip().upper()[:1]))
    if kind == "high_med_low":
        return clamp(LEVEL_PERCENT.get(str(raw).strip().lower()))
    if kind in ("criteria_met", "none"):
        if not total:
            return None
        try:
            return clamp(100.0 * float(raw) / float(total))
        except (TypeError, ValueError):
            return None
    return clamp(raw)


def score_percent(score) -> int:
    """The simulated score of a ScoreBreakdown as 0..100 % (never None)."""
    return clamp(getattr(score, "value", 0)) or 0


def scale_kind(score) -> str:
    """The scale the system shows the recruiter ("percent" when unknown)."""
    kind = getattr(score, "kind", None)
    if kind in SCALE_KINDS:
        return kind
    display = getattr(score, "display", None)
    key = getattr(display, "key", "") or ""
    for candidate in SCALE_KINDS:
        if key.endswith("." + candidate):
            return candidate
    if key.endswith((".criteria_met", ".no_criteria")):
        return "criteria_met"
    return "percent"


def shows_other_scale(score) -> bool:
    """True when the recruiter does not see a percentage (a note is then shown)."""
    return scale_kind(score) != "percent"
