"""Data model of the recruiter simulators: findings, evidence, targets, results.

Every text shown to the user is a `Message` (i18n key + parameters); its
English default comes from `texts.py`. Nothing here does I/O.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import texts

# Status of a finding.
OK, WARN, FAIL, UNKNOWN = "ok", "warn", "fail", "unknown"
STATUSES = (OK, WARN, FAIL, UNKNOWN)
_STATUS_ORDER = {FAIL: 0, WARN: 1, UNKNOWN: 2, OK: 3}

# Identifiers of the checks, in the order of the tab (study §1.7).
CHECK_PARSE = "parse"
CHECK_RECORD = "record"
CHECK_KNOCKOUT = "knockout"
CHECK_FINDABILITY = "findability"
CHECK_SEMANTIC = "semantic"
CHECK_TIMELINE = "timeline"
CHECK_SKILL_RECENCY = "skill_recency"
CHECK_CREDENTIALS = "credentials"
CHECK_FORMAT = "format"
CHECK_AI_TEXT = "ai_text"
CHECK_SCORE = "score"
CHECK_IDS = (CHECK_PARSE, CHECK_RECORD, CHECK_KNOCKOUT, CHECK_FINDABILITY, CHECK_SEMANTIC,
             CHECK_TIMELINE, CHECK_SKILL_RECENCY, CHECK_CREDENTIALS, CHECK_FORMAT, CHECK_AI_TEXT,
             CHECK_SCORE)

# Parts of the simulated score (study §1.5, "Simulated score").
COMPONENT_PARSE = "parse"
COMPONENT_FINDABILITY = "findability"
COMPONENT_MATCH = "match"
COMPONENT_TIMELINE = "timeline"
COMPONENT_CREDENTIALS = "credentials"
COMPONENTS = (COMPONENT_PARSE, COMPONENT_FINDABILITY, COMPONENT_MATCH, COMPONENT_TIMELINE,
              COMPONENT_CREDENTIALS)

# Where a piece of evidence comes from.
SOURCE_CV = "cv"
SOURCE_POSTING = "posting"
SOURCE_FILE = "file"
SOURCE_ANSWER = "answer"
SOURCE_PROFILE = "profile"  # the simulator profile (public documentation)


@dataclass(frozen=True)
class Message:
    """A text for the user: i18n key, parameters and the English default."""

    key: str
    params: dict = field(default_factory=dict)

    @property
    def default(self) -> str:
        return texts.render(self.key, self.params)

    def to_dict(self) -> dict:
        params = {k: v.to_dict() if isinstance(v, Message) else v for k, v in self.params.items()}
        return {"key": self.key, "params": params, "default": self.default}


def msg(key: str, **params) -> Message:
    return Message(key, params)


@dataclass(frozen=True)
class EvidenceRef:
    """A quoted line behind a finding. `line` is 1-based in its source, or None."""

    source: str  # SOURCE_*
    text: str
    line: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Finding:
    """One result of a check: what recruiters (and their software) do, what we found
    in this CV, the lines it comes from, and the fix.

    `code` names the sub-check ("parse.columns"); `edits` are the concrete changes
    to the CV that would fix it (see `Edit`); `details` holds structured data for
    the GUI (queries, record fields, knock-out questions...).
    """

    check_id: str
    status: str
    what_recruiters_do: Message
    what_we_found: Message
    evidence: list[EvidenceRef] = field(default_factory=list)
    fix: Message | None = None
    code: str = ""
    edits: list["Edit"] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "check_id": self.check_id,
            "status": self.status,
            "code": self.code,
            "what_recruiters_do": self.what_recruiters_do.to_dict(),
            "what_we_found": self.what_we_found.to_dict(),
            "evidence": [e.to_dict() for e in self.evidence],
            "fix": self.fix.to_dict() if self.fix else None,
            "edits": [e.to_dict() for e in self.edits],
            "details": self.details,
        }


def worst_status(statuses) -> str:
    """The most severe status (fail > warn > unknown > ok); OK for an empty list."""
    statuses = list(statuses)
    if not statuses:
        return OK
    return min(statuses, key=lambda s: _STATUS_ORDER.get(s, 2))


def sort_findings(findings: list[Finding]) -> list[Finding]:
    order = {c: i for i, c in enumerate(CHECK_IDS)}
    return sorted(findings, key=lambda f: (order.get(f.check_id, 99), _STATUS_ORDER.get(f.status, 2)))


# --- inputs ------------------------------------------------------------------------


@dataclass
class CvDocument:
    """The CV as the app has it: the profile, how the file was read, and the file.

    `read` is the `cv.reading.ReadResult` (optional: without it `profile.text` is
    used and the method is taken as "text"); `path` enables the layout checks.
    `layout` may be given precomputed (see `layout.analyse_layout`), so several
    simulations of the same file read it once.
    """

    profile: object  # models.CVProfile
    read: object | None = None  # cv.reading.ReadResult
    path: str | None = None
    layout: object | None = None  # layout.LayoutFacts

    @property
    def text(self) -> str:
        if self.read is not None and getattr(self.read, "text", None):
            return self.read.text
        return getattr(self.profile, "text", "") or ""

    @property
    def method(self) -> str:
        return getattr(self.read, "method", "text") if self.read is not None else "text"

    @property
    def file_path(self) -> str | None:
        return self.path or getattr(self.profile, "cv_path", None) or None


@dataclass
class Target:
    """What the CV is screened against: a posting or a role.

    `skills` (role targets) are treated as must-have requirements; `distance_km`
    (optional, computed by the caller with the app's geocoder) is the distance from
    the candidate's home to the posting location.
    """

    title: str
    description: str = ""
    location: str = ""
    skills: list[str] = field(default_factory=list)
    kind: str = "posting"  # "posting" | "role"
    posting: object | None = None  # models.Posting
    distance_km: float | None = None

    @classmethod
    def from_posting(cls, posting, distance_km: float | None = None) -> "Target":
        return cls(title=getattr(posting, "title", "") or "",
                   description=getattr(posting, "description", "") or "",
                   location=getattr(posting, "location", "") or "",
                   kind="posting", posting=posting, distance_km=distance_km)

    @classmethod
    def from_role(cls, title: str, skills=(), description: str = "", location: str = "",
                  distance_km: float | None = None) -> "Target":
        return cls(title=title or "", description=description or "", location=location or "",
                   skills=[s for s in skills if s and s.strip()], kind="role",
                   distance_km=distance_km)

    def requirement_text(self) -> str:
        """The text the requirement extraction reads (role skills become a list)."""
        parts = [self.description or ""]
        if self.skills:
            parts.append("Requirements:\n" + "\n".join(f"- {s}" for s in self.skills))
        return "\n".join(p for p in parts if p.strip())


@dataclass
class KnockoutAnswers:
    """The user's truthful answers to the usual screening questions (Settings).

    None everywhere means "not answered". Salary is gross per year in the posting's
    currency; notice is in weeks.
    """

    right_to_work: bool | None = None  # allowed to work in the country of the job
    needs_sponsorship: bool | None = None
    max_commute_km: float | None = None
    willing_to_relocate: bool | None = None
    notice_weeks: int | None = None
    min_salary_year: float | None = None
    driving_licence: list[str] | None = None
    own_car: bool | None = None
    max_travel_days: int | None = None
    shifts_ok: bool | None = None

    @classmethod
    def from_dict(cls, d: dict | None) -> "KnockoutAnswers":
        d = d or {}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)


# --- outputs -------------------------------------------------------------------------


@dataclass
class RecordField:
    """One field of the candidate record as the ATS would fill it."""

    name: str  # "name", "email", "phone", "town", "title", "employer", "dates", ...
    value: str = ""
    status: str = "missing"  # "found" | "garbled" | "missing" | "lost"
    evidence: EvidenceRef | None = None

    def to_dict(self) -> dict:
        return {"name": self.name, "value": self.value, "status": self.status,
                "evidence": self.evidence.to_dict() if self.evidence else None}


@dataclass
class RecordPreview:
    """"What the recruiter sees": the filled record and the start of the parsed text."""

    fields: list[RecordField] = field(default_factory=list)
    positions: list[dict] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    text_lines: list[str] = field(default_factory=list)

    def field(self, name: str) -> RecordField | None:
        return next((f for f in self.fields if f.name == name), None)

    def to_dict(self) -> dict:
        return {"fields": [f.to_dict() for f in self.fields], "positions": list(self.positions),
                "skills": list(self.skills), "text_lines": list(self.text_lines)}


@dataclass
class ScoreBreakdown:
    """The simulated ATS score (0..100) and how it was built."""

    value: int
    components: dict[str, float | None]  # COMPONENTS -> 0..100 or None (not assessed)
    weights: dict[str, float]
    capped: bool = False
    cap: int | None = None
    display: Message | None = None  # the score on the system's scale
    band: str = "middle"  # "top" | "middle" | "bottom" | "rejected" | "at_risk"
    explanation: list[Message] = field(default_factory=list)
    kind: str = "percent"  # the scale the system shows the recruiter (profiles.SCORE_KINDS)

    @property
    def percent(self) -> int:
        """The simulated score as 0..100 % (see percent.py), whatever the system shows."""
        return int(round(max(0.0, min(100.0, float(self.value)))))

    def to_dict(self) -> dict:
        return {"value": self.value, "percent": self.percent, "kind": self.kind,
                "components": dict(self.components), "weights": dict(self.weights),
                "capped": self.capped, "cap": self.cap,
                "display": self.display.to_dict() if self.display else None, "band": self.band,
                "explanation": [m.to_dict() for m in self.explanation]}


@dataclass
class Edit:
    """A concrete change to the CV and what it would add to the score.

    `gains`: component -> points (0..100 of that component) the change adds;
    `points`: the resulting change of the simulated score, filled by the engine
    (computed on the uncapped score when a knock-out caps it). `conditional`:
    the change is only valid if true for the candidate ("if you have X, state
    it"): never a claim the CV does not support.
    """

    code: str
    message: Message
    gains: dict[str, float] = field(default_factory=dict)
    evidence: list[EvidenceRef] = field(default_factory=list)
    conditional: bool = False
    points: float = 0.0

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message.to_dict(), "gains": dict(self.gains),
                "evidence": [e.to_dict() for e in self.evidence], "conditional": self.conditional,
                "points": self.points}


@dataclass
class SimulationResult:
    """The outcome of running one CV through one simulated system."""

    profile_key: str
    profile_name: str
    approximation: Message  # the fixed "approximation built from public documentation" label
    findings: list[Finding]
    record: RecordPreview
    score: ScoreBreakdown
    top_fixes: list[Edit]
    match: object | None = None  # match.MatchResult
    degraded: list[str] = field(default_factory=list)  # e.g. ["semantic->lexical"]

    def by_check(self, check_id: str) -> list[Finding]:
        return [f for f in self.findings if f.check_id == check_id]

    def finding(self, code: str) -> Finding | None:
        return next((f for f in self.findings if f.code == code), None)

    def check_status(self, check_id: str) -> str:
        return worst_status(f.status for f in self.by_check(check_id))

    def to_dict(self) -> dict:
        return {
            "profile_key": self.profile_key, "profile_name": self.profile_name,
            "approximation": self.approximation.to_dict(),
            "findings": [f.to_dict() for f in self.findings], "record": self.record.to_dict(),
            "score": self.score.to_dict(), "top_fixes": [f.to_dict() for f in self.top_fixes],
            "match": self.match.to_dict() if hasattr(self.match, "to_dict") else None,
            "degraded": list(self.degraded),
        }


class SimulationCancelled(Exception):
    """Raised when the caller's cancel check returns True between two steps."""
