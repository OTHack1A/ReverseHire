"""User-settable preferences of the affinity engine (JSON round trip).

- ``directions``: career directions (``Direction``), shipped in ``data/directions.json``
  (industrial automation, software development, cybersecurity and industrial networks,
  technical coordination), each switchable on/off; the user can add their own. The
  affinity is computed per enabled direction and the best applicable one is reported
  ("fits you as: industrial automation, 78 %"); a company is never penalised for
  directions it has nothing to do with.
- ``company_types``: weight 0..1 per company type (``knowledge.COMPANY_TYPES``); 0
  excludes the type (verdict NOT_A_FIT, named), a value below 1 lowers the company in
  the ordering only (the affinity number itself is about fit, not taste).
- ``max_distance_km``: None (default) = work anywhere, distance is informational only;
  a number = a hard limit that caps the affinity when the company is farther.
- ``prefer_permanent``, ``prefer_remote``, ``min_salary_year``: practical fit, scored
  only when a posting publishes the attribute.
- ``knockout_answers``: the user's stored screening answers
  (``config.Settings.knockout_answers``; ``recruiter_sim.KnockoutAnswers`` fields).
  ``max_commute_km`` there is ignored: the distance limit is ``max_distance_km``.
- ``directions_only``: when True a company outside every enabled direction is NOT_A_FIT.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

from .knowledge import COMPANY_TYPES, default_directions_data


@dataclass
class Direction:
    id: str
    name: str
    roles: list[str] = field(default_factory=list)  # role keywords (normalised match)
    families: list[str] = field(default_factory=list)  # skill families of cv/data/skills.json
    skills: list[str] = field(default_factory=list)  # extra skill names
    enabled: bool = True
    custom: bool = False  # added by the user

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Direction":
        return cls(id=str(d["id"]), name=str(d.get("name") or d["id"]),
                   roles=[str(x) for x in d.get("roles") or []],
                   families=[str(x) for x in d.get("families") or []],
                   skills=[str(x) for x in d.get("skills") or []],
                   enabled=bool(d.get("enabled", True)), custom=bool(d.get("custom", False)))


def default_directions() -> list[Direction]:
    return [Direction.from_dict(d) for d in default_directions_data()]


@dataclass
class AffinityPreferences:
    directions: list[Direction] = field(default_factory=default_directions)
    company_types: dict[str, float] = field(default_factory=lambda: {t: 1.0 for t in COMPANY_TYPES})
    max_distance_km: float | None = None
    prefer_permanent: bool = True
    prefer_remote: bool = True
    min_salary_year: float = 0.0
    max_travel_days: int | None = None
    knockout_answers: dict = field(default_factory=dict)
    directions_only: bool = False

    def enabled_directions(self) -> list[Direction]:
        return [d for d in self.directions if d.enabled]

    def type_weight(self, type_key: str | None) -> float:
        if not type_key:
            return 1.0
        value = self.company_types.get(type_key, 1.0)
        return max(0.0, min(1.0, float(value)))

    def to_dict(self) -> dict:
        return {
            "directions": [d.to_dict() for d in self.directions],
            "company_types": dict(self.company_types),
            "max_distance_km": self.max_distance_km,
            "prefer_permanent": self.prefer_permanent,
            "prefer_remote": self.prefer_remote,
            "min_salary_year": self.min_salary_year,
            "max_travel_days": self.max_travel_days,
            "knockout_answers": dict(self.knockout_answers),
            "directions_only": self.directions_only,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "AffinityPreferences":
        d = d or {}
        prefs = cls()
        if isinstance(d.get("directions"), list):
            prefs.directions = [Direction.from_dict(x) for x in d["directions"] if isinstance(x, dict) and x.get("id")]
        if isinstance(d.get("company_types"), dict):
            types = {t: 1.0 for t in COMPANY_TYPES}
            for k, v in d["company_types"].items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    types[str(k)] = max(0.0, min(1.0, float(v)))
            prefs.company_types = types
        distance = d.get("max_distance_km")
        prefs.max_distance_km = float(distance) if isinstance(distance, (int, float)) and distance > 0 else None
        prefs.prefer_permanent = bool(d.get("prefer_permanent", True))
        prefs.prefer_remote = bool(d.get("prefer_remote", True))
        salary = d.get("min_salary_year") or 0
        prefs.min_salary_year = float(salary) if isinstance(salary, (int, float)) and salary > 0 else 0.0
        travel = d.get("max_travel_days")
        prefs.max_travel_days = int(travel) if isinstance(travel, int) and travel >= 0 else None
        prefs.knockout_answers = dict(d.get("knockout_answers") or {})
        prefs.directions_only = bool(d.get("directions_only", False))
        return prefs

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "AffinityPreferences":
        try:
            return cls.from_dict(json.loads(text) if text else {})
        except ValueError:
            return cls()

    def fingerprint(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()[:16]

    @classmethod
    def from_settings(cls, settings, base: "AffinityPreferences | None" = None) -> "AffinityPreferences":
        """Preferences with the app Settings' practical fields (read by attribute)."""
        prefs = cls.from_dict(base.to_dict()) if base is not None else cls()
        prefs.prefer_permanent = bool(getattr(settings, "prefer_permanent", True))
        prefs.prefer_remote = bool(getattr(settings, "prefer_remote", True))
        prefs.min_salary_year = float(getattr(settings, "min_salary_year", 0) or 0)
        travel = getattr(settings, "max_travel_days_year", -1)
        prefs.max_travel_days = None if travel is None or travel < 0 else int(travel)
        prefs.knockout_answers = dict(getattr(settings, "knockout_answers", None) or {})
        return prefs
