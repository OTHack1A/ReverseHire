"""Desirability ("would I like it"), spontaneous application and the overall ``match``.

All scores are 0..100; unknown values are neutral (``UNKNOWN``), never a penalty.

Desirability of a posting (or of a company without postings), weighted mean of::

    distance  1 - DISTANCE_DROP * min(1, distance / search radius)    (no distance: neutral)
    contract  CONTRACT_SCORES[posting.contract]       (weight 0 when prefer_permanent is off)
    remote    REMOTE_SCORES[posting.remote]           (weight 0 when prefer_remote is off)
    salary    published with numbers vs min_salary_year (yearly; month x MONTHS_PER_YEAR,
              hour x HOURS_PER_YEAR); published without a minimum = SALARY_PUBLISHED
    travel    posting travel vs the candidate's willingness and accepted days
              (requirements.check_travel level -> TRAVEL_LEVEL_SCORES)

Spontaneous application (company relevant but without postings), weighted mean of::

    relevance / 100, sector consistency, CV roles named on the site,
    careers page / "send your CV" invitation, size hints (legal form, employees, plants)

Match (sorting value, "best company first")::

    with postings:    raw = 0.50 fit + 0.25 desirability + 0.25 relevance
                      (x BLOCKED_MATCH_FACTOR when the best posting is blocked)
    without postings: raw = SPONTANEOUS_DISCOUNT * (0.55 spontaneous + 0.30 relevance
                      + 0.15 desirability)
    match = raw * (CONFIDENCE_FLOOR + (1 - CONFIDENCE_FLOOR) * confidence)
"""

from __future__ import annotations

import re

from ..models import item_variants
from dataclasses import dataclass, field

from .relevance import TextIndex
from .requirements import accepted_travel_days, check_travel

# --- desirability -----------------------------------------------------------------

UNKNOWN = 0.5
DESIRABILITY_WEIGHTS = {"distance": 0.30, "contract": 0.25, "remote": 0.15, "salary": 0.15,
                        "travel": 0.15}
DISTANCE_DROP = 0.8  # at the edge of the search radius the distance part is 0.2
DEFAULT_RADIUS_KM = 15.0
CONTRACT_SCORES = {
    "permanent": 1.0, "permanent_possible": 0.8, "apprenticeship": 0.45, "fixed_term": 0.4,
    "freelance": 0.3, "temporary_agency": 0.3, "internship": 0.2,
}
REMOTE_SCORES = {"remote": 1.0, "hybrid": 0.85, "onsite": 0.35}
SALARY_PUBLISHED = 0.8  # salary shown, no minimum set (or no numbers)
SALARY_BELOW_FLOOR = 0.1
MONTHS_PER_YEAR = 13  # Italian monthly pay usually counts 13 instalments ("tredicesima")
HOURS_PER_YEAR = 1760  # 220 working days x 8 hours
TRAVEL_NONE = 1.0
TRAVEL_LEVEL_SCORES = {4: 0.9, 3: 0.75, 2: 0.5, 1: 0.3, 0: 0.05}
TRAVEL_UNKNOWN_WILLINGNESS = {"occasional": 0.6, "required": 0.45}

# --- spontaneous application -------------------------------------------------------

SPONTANEOUS_WEIGHTS = {"relevance": 0.45, "sector": 0.15, "roles": 0.15, "careers": 0.15,
                       "size": 0.10}
ROLE_FOUND = 1.0
SKILLS_FOR_ROLE_CREDIT = 3  # CV skills on the site, saturating, worth up to SKILLS_ROLE_SHARE
SKILLS_ROLE_SHARE = 0.6
CAREERS_INVITATION = 1.0  # "send your CV" / open application signal
CAREERS_PAGE = 0.7
CAREERS_MAYBE = 0.5
SIZE_LARGE, SIZE_MEDIUM, SIZE_SMALL, SIZE_UNKNOWN = 1.0, 0.6, 0.3, 0.2
_EMPLOYEES = re.compile(
    r"\b(\d{1,3}(?:[.,]\d{3})*|\d+)\s*\+?\s*(?:dipendenti|collaboratori|addetti|persone|"
    r"employees|people|staff|professionisti|professionals)\b"
)
_LARGE = re.compile(
    r"\bs\.?\s?p\.?\s?a\b|\bgruppo\b|\bgroup\b|multinazional|multinational|stabiliment|"
    r"\bplants?\b|\bfilial[ei]\b|\bbranches\b|sedi in|offices in|headquarter"
)
_MEDIUM = re.compile(r"\bs\.?\s?r\.?\s?l\b|\bltd\b|\bgmbh\b|il nostro team|our team|\bteam di\b")

# --- match ------------------------------------------------------------------------

MATCH_WEIGHTS_HIRING = {"fit": 0.50, "desirability": 0.25, "relevance": 0.25}
MATCH_WEIGHTS_SPONTANEOUS = {"spontaneous": 0.55, "relevance": 0.30, "desirability": 0.15}
SPONTANEOUS_DISCOUNT = 0.8  # an open application is worth less than a matching posting
BLOCKED_MATCH_FACTOR = 0.6
CONFIDENCE_FLOOR = 0.6


@dataclass
class Desirability:
    score: float
    parts: dict[str, float] = field(default_factory=dict)
    # (key, values) pairs for explanation.py: what the candidate likes or not
    pluses: list[tuple[str, dict]] = field(default_factory=list)
    minuses: list[tuple[str, dict]] = field(default_factory=list)


@dataclass
class Spontaneous:
    score: float
    parts: dict[str, float] = field(default_factory=dict)
    found_roles: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)  # keys: relevant, roles, careers, invitation, size


def _setting(settings, name: str, default):
    value = getattr(settings, name, default) if settings is not None else default
    return default if value is None else value


def yearly_salary(posting) -> float | None:
    """Highest published amount on a yearly basis, or None."""
    amount = getattr(posting, "salary_max", None) or getattr(posting, "salary_min", None)
    if not amount or amount <= 0:
        return None
    period = getattr(posting, "salary_period", None)
    if period is None:
        period = "year" if amount >= 5000 else ("month" if amount >= 300 else "hour")
    return float(amount) * {"year": 1, "month": MONTHS_PER_YEAR, "hour": HOURS_PER_YEAR}.get(period, 1)


def _salary_part(posting, minimum: int, d: Desirability) -> float:
    published = getattr(posting, "salary", None) or getattr(posting, "salary_min", None) \
        or getattr(posting, "salary_max", None)
    if not published:
        return UNKNOWN
    shown = getattr(posting, "salary", None) or _amount_text(posting)
    yearly = yearly_salary(posting)
    if minimum > 0 and yearly is not None:
        if yearly >= minimum:
            d.pluses.append(("pref_salary_ok", {"salary": shown, "minimum": minimum}))
            return 1.0
        d.minuses.append(("pref_salary_low", {"salary": shown, "minimum": minimum}))
        ratio = yearly / minimum
        return max(SALARY_BELOW_FLOOR, 1.0 - 2.0 * (1.0 - ratio))
    d.pluses.append(("pref_salary", {"salary": shown}))
    return SALARY_PUBLISHED


def _amount_text(posting) -> str:
    low, high = getattr(posting, "salary_min", None), getattr(posting, "salary_max", None)
    values = "-".join(f"{v:,.0f}".replace(",", ".") for v in (low, high) if v)
    currency = getattr(posting, "salary_currency", None) or ""
    period = getattr(posting, "salary_period", None) or ""
    return " ".join(p for p in (values, currency, f"/{period}" if period else "") if p)


def _travel_part(posting, checks, profile, max_days: int | None, d: Desirability) -> float:
    travel = [c for c in checks or [] if c.kind == "travel"]
    frequency = getattr(posting, "travel", None) if posting is not None else None
    if frequency == "none" and not travel:
        return TRAVEL_NONE
    if travel:
        level = travel[0].level
        frequency = frequency or ("required" if travel[0].priority == "must" else "occasional")
    elif frequency in ("required", "occasional"):
        level, _ = check_travel(frequency, None, getattr(profile, "mobility", None), max_days)
    else:
        return UNKNOWN
    if level is None:
        return TRAVEL_UNKNOWN_WILLINGNESS.get(frequency, UNKNOWN)
    if level <= 1:
        d.minuses.append(("pref_travel_over", {"days": accepted_travel_days(
            getattr(profile, "mobility", None), max_days)}))
    return TRAVEL_LEVEL_SCORES[level]


def compute_desirability(company, posting, profile, settings=None, checks=None) -> Desirability:
    """Desirability 0..100 of a posting at a company (posting None: only the distance)."""
    d = Desirability(score=0.0)
    weights = dict(DESIRABILITY_WEIGHTS)
    if not _setting(settings, "prefer_permanent", True):
        weights["contract"] = 0.0
    if not _setting(settings, "prefer_remote", True):
        weights["remote"] = 0.0
    radius = float(_setting(settings, "radius_km", DEFAULT_RADIUS_KM) or DEFAULT_RADIUS_KM)
    max_days = _setting(settings, "max_travel_days_year", -1)
    max_days = None if max_days < 0 else int(max_days)

    distance = getattr(company, "distance_km", None)
    if distance is None:
        parts = {"distance": UNKNOWN}
    else:
        parts = {"distance": 1.0 - DISTANCE_DROP * min(1.0, max(0.0, distance) / radius)}
        if distance <= radius / 3:
            d.pluses.append(("pref_distance", {"km": distance, "radius": radius}))
    contract = getattr(posting, "contract", None) if posting is not None else None
    parts["contract"] = CONTRACT_SCORES.get(contract, UNKNOWN)
    if weights["contract"] > 0 and contract:
        if contract == "permanent":
            d.pluses.append(("pref_permanent", {}))
        elif contract == "permanent_possible":
            d.pluses.append(("pref_permanent_possible", {}))
        else:
            d.minuses.append(("pref_not_permanent", {"contract": contract}))
    remote = getattr(posting, "remote", None) if posting is not None else None
    parts["remote"] = REMOTE_SCORES.get(remote, UNKNOWN)
    if weights["remote"] > 0 and remote in ("remote", "hybrid"):
        d.pluses.append(("pref_" + remote, {}))
    parts["salary"] = UNKNOWN if posting is None else _salary_part(
        posting, int(_setting(settings, "min_salary_year", 0)), d)
    parts["travel"] = UNKNOWN if posting is None else _travel_part(posting, checks, profile, max_days, d)

    total = sum(weights.values())
    value = sum(weights[k] * parts[k] for k in weights) / total if total else UNKNOWN
    d.score = round(100.0 * value, 1)
    d.parts = {k: round(v, 3) for k, v in parts.items()}
    return d


# --- spontaneous application -------------------------------------------------------


def size_hint(text: str) -> float:
    """0..1 from the legal form, employee counts and words such as "gruppo", "stabilimento"."""
    low = (text or "").lower()
    counts = []
    for m in _EMPLOYEES.finditer(low):
        try:
            counts.append(int(re.sub(r"[.,]", "", m.group(1))))
        except ValueError:
            pass
    if counts:
        n = max(counts)
        return SIZE_LARGE if n >= 50 else (SIZE_MEDIUM if n >= 10 else SIZE_SMALL)
    if _LARGE.search(low):
        return SIZE_LARGE
    if _MEDIUM.search(low):
        return SIZE_MEDIUM
    return SIZE_UNKNOWN


def compute_spontaneous(profile, company, outcome, r_rel) -> Spontaneous:
    """How much an open application is worth (company without postings)."""
    text = (getattr(outcome, "company_text", "") or "") if outcome is not None else ""
    index = TextIndex(f"{company.name or ''} {text}")
    # A role counts when its English value or its CV wording is on the site.
    found_roles = [r.value for r in (profile.roles or [])[:5]
                   if (r.value or "").strip() and any(index.contains(v) for v in item_variants(r))]
    if found_roles:
        role_part = ROLE_FOUND
    else:
        skills = [s.value for s in (profile.skills or [])[:15] if (s.value or "").strip()]
        n = sum(1 for s in skills if index.contains(s)) if text else 0
        role_part = SKILLS_ROLE_SHARE * min(1.0, n / SKILLS_FOR_ROLE_CREDIT)

    careers = 0.0
    signals: list[str] = []
    if outcome is not None:
        if (getattr(outcome, "postings_source", "") or "").startswith("unsolicited"):
            careers = CAREERS_INVITATION
            signals.append("invitation")
        elif getattr(outcome, "careers_url", None):
            careers = CAREERS_PAGE
            signals.append("careers")
        elif str(getattr(getattr(outcome, "status", None), "value", "")) == "maybe":
            careers = CAREERS_MAYBE
    size = size_hint(f"{company.name or ''} {text}")
    parts = {
        "relevance": (r_rel.score or 0.0) / 100.0,
        "sector": r_rel.sector,
        "roles": role_part,
        "careers": careers,
        "size": size,
    }
    valid = {k: v for k, v in parts.items() if v is not None}
    total = sum(SPONTANEOUS_WEIGHTS[k] for k in valid)
    value = sum(SPONTANEOUS_WEIGHTS[k] * v for k, v in valid.items()) / total if total else 0.0
    if (r_rel.score or 0) >= 50:
        signals.insert(0, "relevant")
    if found_roles:
        signals.append("roles")
    if size >= SIZE_LARGE:
        signals.append("size")
    return Spontaneous(score=round(100.0 * value, 1), parts={k: round(v, 3) for k, v in valid.items()},
                       found_roles=found_roles, signals=signals)


# --- match ------------------------------------------------------------------------


def compute_match(*, relevance: float | None, fit: float | None, desirability: float | None,
                  spontaneous: float | None, confidence: float, blocked: bool = False) -> float:
    """Single ordering value 0..100 (see the module docstring)."""
    rel = relevance or 0.0
    des = UNKNOWN * 100 if desirability is None else desirability
    if fit is not None:
        w = MATCH_WEIGHTS_HIRING
        raw = w["fit"] * fit + w["desirability"] * des + w["relevance"] * rel
        if blocked:
            raw *= BLOCKED_MATCH_FACTOR
    else:
        w = MATCH_WEIGHTS_SPONTANEOUS
        spont = rel if spontaneous is None else spontaneous
        raw = SPONTANEOUS_DISCOUNT * (w["spontaneous"] * spont + w["relevance"] * rel
                                      + w["desirability"] * des)
    factor = CONFIDENCE_FLOOR + (1.0 - CONFIDENCE_FLOOR) * max(0.0, min(1.0, confidence or 0.0))
    return round(max(0.0, min(100.0, raw * factor)), 1)
