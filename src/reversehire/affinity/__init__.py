"""Affinity between the user's CV and what a company is looking for, with a verdict:
is this a company to send the CV to (APPLY / SPONTANEOUS), to watch, not a fit, or
unknown for lack of data. See ``engine`` (components), ``weights`` (numbers and the
verdict table), ``demand`` (what the company seeks), ``calibration`` (personal
adjustment from feedback) and ``batch`` (tables with cache)."""

from .batch import AffinityCache, affinity_for_companies, cv_fingerprint, results_from_json, results_to_json
from .calibration import CalibrationReport, Feedback, calibrate, rejudge
from .demand import CompanyDemand, build_demand, demand_from_card, demand_from_result
from .engine import affinity, cv_context, finalize
from .model import AffinityResult
from .preferences import AffinityPreferences, Direction, default_directions
from .texts import FALLBACK, Note, text
from .weights import (APPLY, NOT_A_FIT, SPONTANEOUS, UNKNOWN, VERDICTS, WATCH, Thresholds, Weights,
                      default_weights, sort_key)

__all__ = [
    "AffinityCache", "AffinityPreferences", "AffinityResult", "CalibrationReport", "CompanyDemand", "Direction",
    "FALLBACK", "Feedback", "Note", "Thresholds", "Weights", "affinity", "affinity_for_companies", "build_demand",
    "calibrate", "cv_context", "cv_fingerprint", "default_directions", "default_weights", "demand_from_card",
    "demand_from_result", "finalize", "rejudge", "results_from_json", "results_to_json", "sort_key", "text",
    "APPLY", "SPONTANEOUS", "WATCH", "NOT_A_FIT", "UNKNOWN", "VERDICTS",
]
