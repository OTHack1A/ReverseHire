"""Data model of the job search: a posting from any source, the search result and reports.

`Posting` here is richer than `reversehire.models.Posting` (the one stored on company
cards): it knows who published the ad (employer, agency, board), where it is (with
coordinates when resolvable), its dates, the skills read in it, a de-duplication key
and, after ranking, its score and the "why" lines. `to_model_posting()` converts it to
the card model so the existing scoring and card code can use it unchanged.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields

from ._util import fold

# Gender/inclusive markers and decorations removed from titles before comparing them.
_TITLE_NOISE = re.compile(
    r"\([a-z]{1,4}(?:\s*/\s*[a-z]{1,4}){1,2}\)|\b[mf]\s*/\s*[fm](?:\s*/\s*nb)?\b|\bmfnb\b|\bfmnb\b")


def title_key(title: str) -> str:
    """Folded title without gender markers and punctuation ("Magazziniere/a (M/F)" ->
    "magazziniere")."""
    text = _TITLE_NOISE.sub(" ", fold(title))
    text = re.sub(r"(\w)\s*/\s*[aeio]\b", r"\1", text)  # magazziniere/a -> magazziniere
    text = re.sub(r"(\w)\([aeio]\)", r"\1", text)  # addetto(a) -> addetto
    text = re.sub(r"[^a-z0-9+# ]+", " ", text)
    return " ".join(text.split())


@dataclass
class Posting:
    """One job ad, from any source.

    company: the employer only when the ad states it; None for agency ads that hide
    the client (`is_agency` True, `agency` = the agency's name).
    contract: "permanent" | "permanent_possible" | "fixed_term" | "internship" |
              "apprenticeship" | "freelance" | "temporary_agency" | None
    remote: "remote" | "hybrid" | "onsite" | None; hours: "full_time" | "part_time" | None
    salary*: as published only, never estimated.
    dates: ISO "YYYY-MM-DD" strings (None when unknown or nonsensical).
    geo_quality: "exact" | "town" | "province" | "region" | "branch" | None.
    """

    source: str
    url: str
    title: str
    company: str | None = None
    is_agency: bool = False
    agency: str | None = None
    agency_branch: str | None = None
    location: str = ""
    town: str | None = None
    province: str | None = None
    region: str | None = None
    country: str | None = None
    lat: float | None = None
    lon: float | None = None
    geo_quality: str | None = None
    contract: str | None = None
    hours: str | None = None
    remote: str | None = None
    salary: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_period: str | None = None
    salary_currency: str | None = None
    description: str = ""
    date_posted: str | None = None
    valid_through: str | None = None
    language: str | None = None
    skills: list[str] = field(default_factory=list)
    industry: str | None = None
    source_id: str | None = None
    employer_hints: dict = field(default_factory=dict)
    also_on: list[dict] = field(default_factory=list)
    fetched_at: str | None = None
    # --- filled by the search pipeline
    distance_km: float | None = None
    score: float | None = None
    why: list[str] = field(default_factory=list)
    score_details: dict = field(default_factory=dict)
    relevance: float | None = None

    @property
    def dedup_key(self) -> str:
        """Same ad elsewhere: folded title + town (or province) + who published it."""
        place = fold(self.town or self.province or (self.location.split(",")[0] if self.location else ""))
        who = fold(self.company or self.agency or "")
        who = re.sub(r"\b(s\.?p\.?a|s\.?r\.?l|spa|srl|italia|group|gruppo)\b\.?", " ", who)
        return "|".join((title_key(self.title), " ".join(place.split()), " ".join(who.split())))

    @property
    def publisher(self) -> str:
        return self.agency if self.is_agency and self.agency else (self.company or self.agency or "")

    @property
    def employer_hidden(self) -> bool:
        return self.is_agency and not self.company

    def to_dict(self) -> dict:
        data = asdict(self)
        data["dedup_key"] = self.dedup_key
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Posting":
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in (data or {}).items() if k in known}
        values.setdefault("source", "")
        values.setdefault("url", "")
        values.setdefault("title", "")
        for name in ("skills", "why", "also_on"):
            if not isinstance(values.get(name), list):
                values[name] = []
        for name in ("employer_hints", "score_details"):
            if not isinstance(values.get(name), dict):
                values[name] = {}
        return cls(**values)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Posting":
        return cls.from_dict(json.loads(text))

    def to_model_posting(self):
        """The card model (`reversehire.models.Posting`) with the same attributes."""
        from ..models import Posting as ModelPosting

        return ModelPosting(
            title=self.title, url=self.url, description=self.description, location=self.location,
            source=self.source, contract=self.contract, remote=self.remote, salary=self.salary,
            salary_min=self.salary_min, salary_max=self.salary_max, salary_period=self.salary_period,
            salary_currency=self.salary_currency)

    @classmethod
    def from_model_posting(cls, posting, *, company: str | None = None, source: str | None = None
                           ) -> "Posting":
        """From a card posting (`reversehire.models.Posting` or its dict)."""
        if isinstance(posting, dict):
            get = posting.get
        else:
            def get(name, default=None):
                return getattr(posting, name, default)
        return cls(
            source=source or str(get("source") or "local"), url=str(get("url") or ""),
            title=str(get("title") or ""), company=company, location=str(get("location") or ""),
            description=str(get("description") or ""), contract=get("contract"), remote=get("remote"),
            salary=get("salary"), salary_min=get("salary_min"), salary_max=get("salary_max"),
            salary_period=get("salary_period"), salary_currency=get("salary_currency"))


@dataclass
class SourceReport:
    """What one source did in a search.

    state: "ok" | "empty" | "disabled" | "no_key" | "forbidden_by_terms" | "failed" |
           "timeout" | "cancelled" | "partial"
    found: postings returned by the source; kept: after filters and de-duplication.
    errors: short technical messages (never keys, never CV text).
    """

    source_id: str
    state: str = "ok"
    found: int = 0
    kept: int = 0
    errors: list[str] = field(default_factory=list)
    requests: int = 0
    cache_hits: int = 0
    seconds: float = 0.0
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SearchResult:
    query: object  # SearchQuery
    postings: list[Posting] = field(default_factory=list)
    reports: list[SourceReport] = field(default_factory=list)
    attributions: list[str] = field(default_factory=list)
    cancelled: bool = False
    stats: dict = field(default_factory=dict)
    seconds: float = 0.0

    def report(self, source_id: str) -> SourceReport | None:
        return next((r for r in self.reports if r.source_id == source_id), None)

    def to_dict(self) -> dict:
        query = self.query.to_dict() if hasattr(self.query, "to_dict") else self.query
        return {"query": query, "postings": [p.to_dict() for p in self.postings],
                "reports": [r.to_dict() for r in self.reports], "attributions": list(self.attributions),
                "cancelled": self.cancelled, "stats": dict(self.stats), "seconds": self.seconds}
