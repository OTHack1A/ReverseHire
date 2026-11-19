"""Data models of the application (dataclasses and enums)."""

from dataclasses import asdict, dataclass, field
from enum import Enum


class HiringStatus(str, Enum):
    """Outcome of the search for hiring signals for a company."""

    HIRING = "hiring"
    MAYBE = "maybe"
    NO_SIGNAL = "no_signal"
    UNREACHABLE = "unreachable"
    NOT_ASSESSED = "not_assessed"


def _simplify(value):
    """Recursively converts enums to strings, for JSON serialization."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _simplify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_simplify(v) for v in value]
    return value


def _to_dict(item) -> dict:
    return _simplify(asdict(item))


@dataclass
class Evidence:
    """Source sentence of a piece of data or of a score."""

    text: str
    source: str  # "cv", page URL, etc.

    def to_dict(self) -> dict:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Evidence":
        return cls(text=d["text"], source=d["source"])


@dataclass
class ProfileItem:
    """A profile item (role, skill, ...) with its evidence.

    ``value`` is shown to the user (in English when the CV was translated);
    ``original`` keeps the CV wording when it differs from ``value`` (None
    otherwise). Matching uses both (see ``item_variants``).
    """

    value: str
    evidence: list[Evidence] = field(default_factory=list)
    esco_code: str | None = None
    original: str | None = None

    def to_dict(self) -> dict:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ProfileItem":
        return cls(
            value=d["value"],
            evidence=[Evidence.from_dict(e) for e in d.get("evidence", [])],
            esco_code=d.get("esco_code"),
            original=d.get("original") or None,
        )

    def variants(self) -> list[str]:
        return item_variants(self)


def item_variants(item) -> list[str]:
    """The texts of a profile item to match against postings and company pages:
    the displayed value and, when different, the CV wording (``original``).

    Duck-typed: works for ``ProfileItem``, ``WorkExperience`` (title and
    ``original_title``) and plain strings."""
    if isinstance(item, str):
        values = [item]
    else:
        values = [getattr(item, "value", None) or getattr(item, "title", None) or "",
                  getattr(item, "original", None) or getattr(item, "original_title", None) or ""]
    result: list[str] = []
    for v in values:
        v = (v or "").strip()
        if v and v.casefold() not in (r.casefold() for r in result):
            result.append(v)
    return result


def _evidence_list(values) -> list[Evidence]:
    return [Evidence.from_dict(e) for e in values or []]


@dataclass
class WorkExperience:
    """One entry of the work history (dates as "YYYY-MM" or "YYYY"; end may be "present")."""

    title: str
    employer: str = ""
    start: str = ""
    end: str = ""
    months: int | None = None
    evidence: list[Evidence] = field(default_factory=list)
    original_title: str | None = None  # CV wording when the title was translated

    def to_dict(self) -> dict:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "WorkExperience":
        return cls(
            title=d.get("title", ""),
            employer=d.get("employer", ""),
            start=d.get("start", ""),
            end=d.get("end", ""),
            months=d.get("months"),
            evidence=_evidence_list(d.get("evidence")),
            original_title=d.get("original_title") or None,
        )


@dataclass
class EmploymentGap:
    """Period without work between two consecutive experiences ("YYYY-MM")."""

    start: str
    end: str
    months: int

    def to_dict(self) -> dict:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "EmploymentGap":
        return cls(start=d.get("start", ""), end=d.get("end", ""), months=int(d.get("months", 0)))


@dataclass
class Mobility:
    """Willingness to travel for work (business trips) and to relocate, licence and car.

    travel: "declared" | "likely" | "excluded" | "unknown";
    relocation: "declared" | "excluded" | "unknown" (moving home, separate from travel);
    travel_days_per_year: days away per year when the CV states days or a percentage
    (percentages are converted on 220 working days).
    """

    travel: str = "unknown"
    travel_days_per_year: int | None = None
    travel_evidence: list[Evidence] = field(default_factory=list)
    relocation: str = "unknown"
    relocation_evidence: list[Evidence] = field(default_factory=list)
    driving_licence: list[str] = field(default_factory=list)  # e.g. ["B"]; empty = not stated
    own_car: bool | None = None
    licence_evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Mobility":
        d = d or {}
        licence = d.get("driving_licence") or []
        if isinstance(licence, str):
            licence = [licence]
        return cls(
            travel=d.get("travel") or "unknown",
            travel_days_per_year=d.get("travel_days_per_year"),
            travel_evidence=_evidence_list(d.get("travel_evidence")),
            relocation=d.get("relocation") or "unknown",
            relocation_evidence=_evidence_list(d.get("relocation_evidence")),
            driving_licence=list(licence),
            own_car=d.get("own_car"),
            licence_evidence=_evidence_list(d.get("licence_evidence")),
        )


@dataclass
class CVProfile:
    """Candidate profile inferred from the CV."""

    cv_path: str
    language: str
    roles: list[ProfileItem] = field(default_factory=list)
    skills: list[ProfileItem] = field(default_factory=list)
    spoken_languages: list[ProfileItem] = field(default_factory=list)
    degrees: list[ProfileItem] = field(default_factory=list)
    sectors: list[str] = field(default_factory=list)
    years_experience: float | None = None
    text: str = ""
    # Added later: absent keys in stored profiles get these defaults.
    experiences: list[WorkExperience] = field(default_factory=list)  # most recent first
    gaps: list[EmploymentGap] = field(default_factory=list)  # most recent first
    mobility: Mobility = field(default_factory=Mobility)
    home: str | None = None  # town/city (postcode, province, country) usable for geocoding
    seniority: str | None = None  # "junior" | "mid" | "senior"
    seniority_evidence: list[Evidence] = field(default_factory=list)
    # The fields are shown in English; for a CV in another language:
    translated: bool = False  # True when values were translated from `language`
    translation: str = ""  # "tables" (built-in tables only) | "local" (also the local MT model) | ""
    untranslated: list[str] = field(default_factory=list)  # values left in the CV language

    def to_dict(self) -> dict:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CVProfile":
        def items(key: str) -> list[ProfileItem]:
            return [ProfileItem.from_dict(v) for v in d.get(key, [])]

        return cls(
            cv_path=d["cv_path"],
            language=d["language"],
            roles=items("roles"),
            skills=items("skills"),
            spoken_languages=items("spoken_languages"),
            degrees=items("degrees"),
            sectors=list(d.get("sectors", [])),
            years_experience=d.get("years_experience"),
            text=d.get("text", ""),
            experiences=[WorkExperience.from_dict(e) for e in d.get("experiences") or []],
            gaps=[EmploymentGap.from_dict(g) for g in d.get("gaps") or []],
            mobility=Mobility.from_dict(d.get("mobility")),
            home=d.get("home") or None,
            seniority=d.get("seniority") or None,
            seniority_evidence=_evidence_list(d.get("seniority_evidence")),
            translated=bool(d.get("translated", False)),
            translation=d.get("translation") or "",
            untranslated=list(d.get("untranslated") or []),
        )


# Keys of Company.ids (legal identifiers attached by discovery, an import or the card).
COMPANY_ID_KEYS = ("vat", "tax_code", "rea", "lei", "wikidata", "pec", "ateco")


@dataclass
class Company:
    """Candidate company, from OpenStreetMap, a discovery source, an import or entered by hand.

    ``ids`` holds legal identifiers when known (keys in COMPANY_ID_KEYS: "vat" = partita
    IVA, "tax_code" = codice fiscale, "rea" = "BO-123456", "lei", "wikidata" = "Q…", "pec",
    "ateco"). ``sources`` is the per-field provenance: dicts with the keys "field", "value",
    "source", "url" and "date" (ISO date), e.g. {"field": "vat", "value": "…",
    "source": "wikidata", "url": "https://www.wikidata.org/wiki/Q…", "date": "2026-11-17"}.
    """

    name: str
    lat: float | None
    lon: float | None
    address: str = ""
    site: str | None = None
    osm_tags: dict[str, str] = field(default_factory=dict)
    distance_km: float | None = None
    origin: str = "osm"  # "osm", "manual", "jobboard", "import" (or a discovery source)
    ids: dict[str, str] = field(default_factory=dict)
    sources: list[dict] = field(default_factory=list)
    # Extra data from discovery or an import (town, province, phone, email, industry,
    # employees, "why_selected"...); never used as scoring text.
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def discovery_fields(self) -> dict:
        """Old name of `fields` (discovery sources used to attach it as an attribute)."""
        return self.fields

    @discovery_fields.setter
    def discovery_fields(self, value: dict) -> None:
        self.fields = dict(value or {})

    def to_dict(self) -> dict:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Company":
        ids = d.get("ids") or {}
        sources = d.get("sources") or []
        extra = d.get("fields") or {}
        return cls(
            name=d["name"],
            lat=d.get("lat"),
            lon=d.get("lon"),
            address=d.get("address", ""),
            site=d.get("site"),
            osm_tags=dict(d.get("osm_tags", {})),
            distance_km=d.get("distance_km"),
            origin=d.get("origin", "osm"),
            ids={str(k): str(v) for k, v in ids.items() if v not in (None, "")}
            if isinstance(ids, dict) else {},
            sources=[dict(s) for s in sources if isinstance(s, dict)] if isinstance(sources, list) else [],
            fields={str(k): v for k, v in extra.items() if v not in (None, "")}
            if isinstance(extra, dict) else {},
        )


# OSM tags that say nothing about the company's activity (identifiers, provenance).
NON_DESCRIPTIVE_TAGS = ("osm_id",)


def public_tags(tags: dict | None) -> dict[str, str]:
    """The descriptive OSM tags: keys starting with "_" (internal data) and identifiers
    such as "osm_id" removed. Scoring text and tag counts use only these."""
    return {str(k): v for k, v in (tags or {}).items()
            if not str(k).startswith("_") and k not in NON_DESCRIPTIVE_TAGS}


@dataclass
class Posting:
    """Job posting found on a company's site."""

    title: str
    url: str
    description: str = ""
    location: str = ""
    source: str = ""
    # Attributes detected from structured data or text (None = not stated).
    # contract: "permanent" | "permanent_possible" | "fixed_term" | "internship" |
    #           "apprenticeship" | "freelance" | "temporary_agency"
    contract: str | None = None
    remote: str | None = None  # "remote" | "hybrid" | "onsite"
    salary: str | None = None  # as published, lightly normalised; never estimated
    salary_min: float | None = None
    salary_max: float | None = None
    salary_period: str | None = None  # "year" | "month" | "hour"
    salary_currency: str | None = None  # ISO code, e.g. "EUR"
    travel: str | None = None  # "required" | "occasional" | "none"
    travel_note: str | None = None  # sentence about travel

    def to_dict(self) -> dict:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Posting":
        return cls(
            title=d["title"],
            url=d["url"],
            description=d.get("description", ""),
            location=d.get("location", ""),
            source=d.get("source", ""),
            contract=d.get("contract"),
            remote=d.get("remote"),
            salary=d.get("salary"),
            salary_min=d.get("salary_min"),
            salary_max=d.get("salary_max"),
            salary_period=d.get("salary_period"),
            salary_currency=d.get("salary_currency"),
            travel=d.get("travel"),
            travel_note=d.get("travel_note"),
        )


@dataclass
class RequirementCheck:
    """One requirement of a posting checked against the CV (see scoring/requirements.py).

    priority: "must" | "nice" | "context";
    kind: "years" | "language" | "degree" | "licence" | "car" | "travel" | "skill" | "duty";
    level: anchored 0..4 (4 = explicit strong evidence, 0 = explicit contradiction),
    None = no evidence either way ("uncertain", never a failure);
    status: "met" | "partial" | "uncertain" | "not_met" | "contradicted";
    evidence: the CV line quoted for the level (None when nothing was found);
    blocking: True only for a must-have explicitly contradicted by the CV.
    """

    text: str
    priority: str = "context"
    kind: str = "skill"
    level: int | None = None
    status: str = "uncertain"
    evidence: str | None = None
    blocking: bool = False

    def to_dict(self) -> dict:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RequirementCheck":
        return cls(
            text=d.get("text", ""),
            priority=d.get("priority") or "context",
            kind=d.get("kind") or "skill",
            level=d.get("level"),
            status=d.get("status") or "uncertain",
            evidence=d.get("evidence"),
            blocking=bool(d.get("blocking", False)),
        )


@dataclass
class Assessment:
    """Result of assessing a company against the profile.

    Scores 0..100: ``relevance`` (company vs profile), ``fit`` (best posting vs CV,
    "would they take me"), ``desirability`` (candidate preferences, "would I like
    it"), ``spontaneous`` (worth an open application; only without postings) and
    ``match``, the single value used to sort companies (see scoring/preferences.py).
    ``requirements`` and ``blocked`` refer to ``postings[0]`` (the best fit).
    """

    relevance: float | None = None
    fit: float | None = None
    confidence: float = 0.0
    status: HiringStatus = HiringStatus.NOT_ASSESSED
    reasons: list[Evidence] = field(default_factory=list)
    postings: list[Posting] = field(default_factory=list)
    careers_url: str | None = None
    gaps: list[str] = field(default_factory=list)
    # Added later: absent keys in stored assessments get these defaults.
    desirability: float | None = None
    spontaneous: float | None = None
    match: float | None = None
    blocked: bool = False
    blocked_reason: str | None = None
    requirements: list[RequirementCheck] = field(default_factory=list)

    def to_dict(self) -> dict:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Assessment":
        return cls(
            relevance=d.get("relevance"),
            fit=d.get("fit"),
            confidence=d.get("confidence", 0.0),
            status=HiringStatus(d.get("status", HiringStatus.NOT_ASSESSED.value)),
            reasons=[Evidence.from_dict(e) for e in d.get("reasons", [])],
            postings=[Posting.from_dict(a) for a in d.get("postings", [])],
            careers_url=d.get("careers_url"),
            gaps=list(d.get("gaps", [])),
            desirability=d.get("desirability"),
            spontaneous=d.get("spontaneous"),
            match=d.get("match"),
            blocked=bool(d.get("blocked", False)),
            blocked_reason=d.get("blocked_reason"),
            requirements=[RequirementCheck.from_dict(r) for r in d.get("requirements") or []],
        )
