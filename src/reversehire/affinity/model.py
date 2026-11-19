"""Result dataclasses of the affinity engine, with JSON (de)serialisation for the store."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .texts import Note

ALGORITHM_VERSION = "3"   # 2: title-only postings, internships, postings-first headline; 3: sector quotes
STATUS_OK, STATUS_NOT_ENOUGH = "ok", "not_enough_data"
GENERAL = "general"  # scope id of the whole profile (no direction)


def _notes(values) -> list[Note]:
    return [n for n in (Note.from_dict(v) for v in values or []) if n is not None]


@dataclass
class Link:
    """One piece of evidence: the CV side and the company side (each may be empty)."""

    cv_line: str | None = None
    cv_term: str | None = None
    company_text: str | None = None
    company_source: str | None = None  # URL, "card:<field>" or "osm"
    company_field: str | None = None
    origin: str | None = None  # demand origin (explicit_must, stated, inferred_product...)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Link":
        return cls(**(d or {}))


@dataclass
class Point:
    """A strength, gap, extra, talking point or component evidence line."""

    note: Note
    link: Link = field(default_factory=Link)
    term: str = ""
    weight: float = 0.0
    score: float | None = None  # 0..100 contribution when it applies

    def to_dict(self) -> dict:
        return {"note": self.note.to_dict(), "link": self.link.to_dict(), "term": self.term,
                "weight": self.weight, "score": self.score}

    @classmethod
    def from_dict(cls, d: dict) -> "Point":
        return cls(Note.from_dict(d["note"]), Link.from_dict(d.get("link")), d.get("term", ""),
                   float(d.get("weight", 0.0)), d.get("score"))


@dataclass
class Component:
    key: str
    score: float | None  # 0..100, None = no data
    quality: float = 0.0  # 0..1 data quality (explicit 1, stated 0.8, inferred 0.5)
    evidence: list[Point] = field(default_factory=list)
    detail: dict = field(default_factory=dict)  # numbers behind the score (documented per component)

    def to_dict(self) -> dict:
        return {"key": self.key, "score": self.score, "quality": self.quality,
                "evidence": [p.to_dict() for p in self.evidence], "detail": dict(self.detail)}

    @classmethod
    def from_dict(cls, d: dict) -> "Component":
        return cls(d["key"], d.get("score"), float(d.get("quality", 0.0)),
                   [Point.from_dict(p) for p in d.get("evidence", [])], dict(d.get("detail") or {}))


@dataclass
class PostingFit:
    index: int
    title: str
    url: str
    score: float  # recruiter_sim.match score 0..100 (gate applied)
    fits: bool
    capped: bool = False
    failed: list[Note] = field(default_factory=list)  # failed screening questions / gate
    common: int = 0
    total: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["failed"] = [n.to_dict() for n in self.failed]
        return d

    @classmethod
    def from_dict(cls, d: dict | None) -> "PostingFit | None":
        if not d:
            return None
        return cls(int(d["index"]), d.get("title", ""), d.get("url", ""), float(d.get("score", 0.0)),
                   bool(d.get("fits", False)), bool(d.get("capped", False)), _notes(d.get("failed")),
                   int(d.get("common", 0)), int(d.get("total", 0)))


@dataclass
class Scope:
    """The affinity for one direction (or for the whole profile: id GENERAL)."""

    id: str
    name: str
    applicable: bool
    affinity: float | None = None
    raw_affinity: float | None = None  # before the threshold and the cap
    coverage: float = 0.0
    confidence: str = "low"
    components: dict[str, Component] = field(default_factory=dict)
    best_posting: PostingFit | None = None
    plausible_need: bool = False
    plausible_why: str = ""
    hard_fail: bool = False
    cap_reason: Note | None = None
    demand_share: float = 0.0  # share of the company's demand weight in this direction

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "applicable": self.applicable, "affinity": self.affinity,
                "raw_affinity": self.raw_affinity, "coverage": self.coverage, "confidence": self.confidence,
                "components": {k: c.to_dict() for k, c in self.components.items()},
                "best_posting": self.best_posting.to_dict() if self.best_posting else None,
                "plausible_need": self.plausible_need, "plausible_why": self.plausible_why,
                "hard_fail": self.hard_fail, "cap_reason": self.cap_reason.to_dict() if self.cap_reason else None,
                "demand_share": self.demand_share}

    @classmethod
    def from_dict(cls, d: dict) -> "Scope":
        return cls(d["id"], d.get("name", ""), bool(d.get("applicable", False)), d.get("affinity"),
                   d.get("raw_affinity"), float(d.get("coverage", 0.0)), d.get("confidence", "low"),
                   {k: Component.from_dict(c) for k, c in (d.get("components") or {}).items()},
                   PostingFit.from_dict(d.get("best_posting")), bool(d.get("plausible_need", False)),
                   d.get("plausible_why", ""), bool(d.get("hard_fail", False)), Note.from_dict(d.get("cap_reason")),
                   float(d.get("demand_share", 0.0)))


@dataclass
class Opportunity:
    """Signals that matter for the decision but are NOT affinity (never mixed into it)."""

    score: float = 0.0  # 0..100
    level: str = "low"  # low | medium | high
    hiring_now: bool = False
    hiring_for_you: bool = False
    signals: list[Point] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"score": self.score, "level": self.level, "hiring_now": self.hiring_now,
                "hiring_for_you": self.hiring_for_you, "signals": [p.to_dict() for p in self.signals]}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Opportunity":
        d = d or {}
        return cls(float(d.get("score", 0.0)), d.get("level", "low"), bool(d.get("hiring_now", False)),
                   bool(d.get("hiring_for_you", False)), [Point.from_dict(p) for p in d.get("signals", [])])


@dataclass
class AffinityResult:
    company_id: str
    company_name: str
    status: str  # STATUS_OK | STATUS_NOT_ENOUGH
    affinity: float | None  # 0..100 of the headline scope, None = not enough data
    confidence: str
    direction: str  # headline scope id (a direction id or GENERAL)
    direction_name: str
    verdict: str
    reasons: list[Note] = field(default_factory=list)
    hints: list[Note] = field(default_factory=list)  # what to collect when data is thin
    scopes: list[Scope] = field(default_factory=list)
    strengths: list[Point] = field(default_factory=list)
    gaps: list[Point] = field(default_factory=list)
    extras: list[Point] = field(default_factory=list)
    talking_points: list[Point] = field(default_factory=list)
    best_posting: PostingFit | None = None
    opportunity: Opportunity = field(default_factory=Opportunity)
    company_types: list[dict] = field(default_factory=list)  # knowledge.CompanyType.to_dict()
    primary_type: str | None = None
    type_weight: float = 1.0
    type_excluded: bool = False
    outside_directions: bool = False
    directions_only: bool = False
    distance_km: float | None = None
    fingerprints: dict = field(default_factory=dict)  # cv, data, weights, preferences
    algorithm: str = ALGORITHM_VERSION

    @property
    def headline(self) -> Scope | None:
        return next((s for s in self.scopes if s.id == self.direction), None)

    @property
    def components(self) -> dict[str, Component]:
        scope = self.headline
        return scope.components if scope else {}

    def headline_note(self) -> Note:
        if self.affinity is None:
            return Note("affinity.headline.none")
        conf = f"affinity.confidence.{self.confidence}"
        if self.direction == GENERAL:
            return Note("affinity.headline.general", {"score": round(self.affinity), "confidence": conf})
        return Note("affinity.headline", {"direction": self.direction_name, "score": round(self.affinity),
                                          "confidence": conf})

    def to_dict(self) -> dict:
        return {
            "company_id": self.company_id, "company_name": self.company_name, "status": self.status,
            "affinity": self.affinity, "confidence": self.confidence, "direction": self.direction,
            "direction_name": self.direction_name, "verdict": self.verdict,
            "reasons": [n.to_dict() for n in self.reasons], "hints": [n.to_dict() for n in self.hints],
            "scopes": [s.to_dict() for s in self.scopes],
            "strengths": [p.to_dict() for p in self.strengths], "gaps": [p.to_dict() for p in self.gaps],
            "extras": [p.to_dict() for p in self.extras],
            "talking_points": [p.to_dict() for p in self.talking_points],
            "best_posting": self.best_posting.to_dict() if self.best_posting else None,
            "opportunity": self.opportunity.to_dict(), "company_types": list(self.company_types),
            "primary_type": self.primary_type, "type_weight": self.type_weight,
            "type_excluded": self.type_excluded, "outside_directions": self.outside_directions,
            "directions_only": self.directions_only, "distance_km": self.distance_km,
            "fingerprints": dict(self.fingerprints), "algorithm": self.algorithm,
            "headline": self.headline_note().to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AffinityResult":
        return cls(
            company_id=str(d["company_id"]), company_name=d.get("company_name", ""), status=d["status"],
            affinity=d.get("affinity"), confidence=d.get("confidence", "low"), direction=d.get("direction", GENERAL),
            direction_name=d.get("direction_name", ""), verdict=d["verdict"], reasons=_notes(d.get("reasons")),
            hints=_notes(d.get("hints")), scopes=[Scope.from_dict(s) for s in d.get("scopes", [])],
            strengths=[Point.from_dict(p) for p in d.get("strengths", [])],
            gaps=[Point.from_dict(p) for p in d.get("gaps", [])],
            extras=[Point.from_dict(p) for p in d.get("extras", [])],
            talking_points=[Point.from_dict(p) for p in d.get("talking_points", [])],
            best_posting=PostingFit.from_dict(d.get("best_posting")),
            opportunity=Opportunity.from_dict(d.get("opportunity")),
            company_types=list(d.get("company_types", [])), primary_type=d.get("primary_type"),
            type_weight=float(d.get("type_weight", 1.0)), type_excluded=bool(d.get("type_excluded", False)),
            outside_directions=bool(d.get("outside_directions", False)),
            directions_only=bool(d.get("directions_only", False)), distance_km=d.get("distance_km"),
            fingerprints=dict(d.get("fingerprints") or {}), algorithm=str(d.get("algorithm", ALGORITHM_VERSION)),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "AffinityResult":
        return cls.from_dict(json.loads(text))
