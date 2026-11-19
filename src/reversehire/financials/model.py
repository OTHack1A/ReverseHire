"""Data model of the company financials: one figure set per fiscal year and scope.

Owner's principles (never invent; every figure carries source, fiscal year, currency and
scope):

- a `FinancialYear` is ONE fiscal year of ONE scope; an amount that is unknown is `None`
  (blank), never 0;
- scope is "consolidated" (group figures), "separate" (single company) or "unknown"
  (e.g. Wikidata does not say). The research found that consolidated and separate
  figures of the same company differ by a factor of about 3, so a series keeps them
  apart: the key of a record is (year, scope) and the chart draws ONE scope at a time;
- merge rules: for the same (year, scope) the source with the higher authority wins; with
  equal authority the record with the later `retrieved_at` wins (the first one added when
  both carry the same time). Whenever the two records disagree on a figure the loser is
  KEPT as a `Conflict` (never silently overwritten). When they agree, blanks of the
  winner are filled from the loser and noted in `filled_from`;
- no currency conversion: records in different currencies are never compared, they
  conflict.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

SCOPES = ("consolidated", "separate", "unknown")
SCOPE_ORDER = {s: i for i, s in enumerate(SCOPES)}
AMOUNT_FIELDS = ("revenue", "profit", "employees")
FORMAT_VERSION = 1

# Higher wins. A figure typed by the owner from a document he bought is the strongest,
# then the official ESEF filing, then a paid aggregator, then Wikidata (volunteer data).
# Figures the owner pasted from a company-information page and reviewed ("pasted") rank
# below the official and paid sources and above Wikidata.
AUTHORITY = {"manual": 90, "esef": 80, "openapi": 70, "pasted": 60, "wikidata": 30}
UNKNOWN_AUTHORITY = 10
REL_TOLERANCE = 0.0005  # two amounts closer than this are "the same figure" (rounding)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def authority_of(source: str, table: dict | None = None) -> int:
    table = AUTHORITY if table is None else table
    name = (source or "").split(":", 1)[0].strip().lower()
    return int(table.get(name, UNKNOWN_AUTHORITY))


def _number(value) -> float | None:
    """A finite number or None (bool, text, NaN and infinity are not amounts)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        return None
    return number if math.isfinite(number) else None


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= max(1.0, REL_TOLERANCE * max(abs(a), abs(b)))


@dataclass
class FinancialYear:
    """Figures of one fiscal year (labelled with the calendar year in which it ENDS)."""

    year: int
    revenue: float | None = None
    profit: float | None = None
    employees: float | None = None
    currency: str = ""
    scope: str = "unknown"
    source: str = ""
    source_url: str = ""
    retrieved_at: str = ""
    period_end: str = ""   # ISO date of the end of the fiscal year, when known
    note: str = ""         # e.g. which accounting concept was used
    filled_from: dict = field(default_factory=dict)  # {"employees": "wikidata"}
    # figures known only as a range ("between 10 and 50 million"): {"revenue": [low, high]};
    # the amount itself stays None (a range is never turned into an invented value)
    ranges: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.year = int(self.year)
        if not 1800 <= self.year <= 2200:
            raise ValueError(f"fiscal year out of range: {self.year}")
        for name in AMOUNT_FIELDS:
            setattr(self, name, _number(getattr(self, name)))
        if self.scope not in SCOPES:
            self.scope = "unknown"
        currency = (self.currency or "").strip().upper()
        self.currency = currency if len(currency) == 3 and currency.isalpha() else ""
        self.source = (self.source or "").strip()
        self.source_url = (self.source_url or "").strip()
        self.retrieved_at = (self.retrieved_at or "").strip()
        self.period_end = (self.period_end or "").strip()
        self.note = (self.note or "").strip()
        self.filled_from = {str(k): str(v) for k, v in (self.filled_from or {}).items()}
        ranges = {}
        for name, pair in (self.ranges or {}).items():
            try:
                low, high = (_number(float(x)) for x in pair)
            except (TypeError, ValueError):
                continue
            if name in AMOUNT_FIELDS and low is not None and high is not None and getattr(self, name) is None:
                ranges[name] = [min(low, high), max(low, high)]
        self.ranges = ranges

    @property
    def empty(self) -> bool:
        return all(getattr(self, name) is None for name in AMOUNT_FIELDS) and not self.ranges

    def to_dict(self) -> dict:
        data = {"year": self.year, "revenue": self.revenue, "profit": self.profit,
                "employees": self.employees, "currency": self.currency, "scope": self.scope,
                "source": self.source, "source_url": self.source_url,
                "retrieved_at": self.retrieved_at}
        for name in ("period_end", "note"):
            if getattr(self, name):
                data[name] = getattr(self, name)
        if self.filled_from:
            data["filled_from"] = dict(self.filled_from)
        if self.ranges:
            data["ranges"] = {k: list(v) for k, v in self.ranges.items()}
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "FinancialYear":
        keys = {"year", "revenue", "profit", "employees", "currency", "scope", "source",
                "source_url", "retrieved_at", "period_end", "note", "filled_from", "ranges"}
        return cls(**{k: v for k, v in data.items() if k in keys})


def differing_fields(a: FinancialYear, b: FinancialYear) -> list[str]:
    """Figures present in both records that disagree (all of them if the currencies differ)."""
    fields = []
    for name in AMOUNT_FIELDS:
        x, y = getattr(a, name), getattr(b, name)
        if x is None or y is None:
            continue
        if name != "employees" and a.currency != b.currency:
            fields.append(name)
        elif not _same(x, y):
            fields.append(name)
    return fields


@dataclass
class Conflict:
    """Two sources disagree on a (year, scope): `kept` stays in the series, `other` is the loser."""

    year: int
    scope: str
    fields: list[str]
    kept_source: str
    other: FinancialYear

    def to_dict(self) -> dict:
        return {"year": self.year, "scope": self.scope, "fields": list(self.fields),
                "kept_source": self.kept_source, "other": self.other.to_dict()}

    @classmethod
    def from_dict(cls, data: dict) -> "Conflict":
        return cls(int(data["year"]), data.get("scope", "unknown"), list(data.get("fields") or []),
                   str(data.get("kept_source", "")), FinancialYear.from_dict(data["other"]))


@dataclass
class SeriesView:
    """What a chart draws: ONE scope, ONE currency, years in ascending order."""

    scope: str = "unknown"
    currency: str = ""
    rows: list[FinancialYear] = field(default_factory=list)
    dropped: int = 0  # records of this scope left out because of another currency
    sources: list[str] = field(default_factory=list)
    conflict_years: list[int] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.rows


@dataclass
class FinancialSeries:
    """All known figures of one company, keyed by (year, scope), with the conflicts found."""

    records: dict = field(default_factory=dict)  # (year, scope) -> FinancialYear
    conflicts: list = field(default_factory=list)

    # --- building

    def add(self, record: FinancialYear, authority: dict | None = None) -> str:
        """Adds a record following the merge rules. Returns "added", "replaced" (the new one
        won), "kept" (the existing one won or they were identical) or "ignored" (no figure)."""
        if record.empty:
            return "ignored"
        key = (record.year, record.scope)
        current = self.records.get(key)
        if current is None:
            self.records[key] = replace(record, filled_from=dict(record.filled_from))
            return "added"
        new_rank = authority_of(record.source, authority)
        old_rank = authority_of(current.source, authority)
        new_wins = new_rank > old_rank or (new_rank == old_rank and record.retrieved_at > current.retrieved_at)
        winner, loser = (record, current) if new_wins else (current, record)
        winner = replace(winner, filled_from=dict(winner.filled_from), ranges=dict(winner.ranges))
        fields = differing_fields(winner, loser)
        if fields:
            self._flag(winner, loser, fields)
        else:
            for name in AMOUNT_FIELDS:
                if getattr(winner, name) is None and getattr(loser, name) is not None \
                        and (name == "employees" or winner.currency == loser.currency):
                    setattr(winner, name, getattr(loser, name))
                    winner.filled_from[name] = loser.source
        self.records[key] = winner
        return "replaced" if new_wins else "kept"

    def _flag(self, winner: FinancialYear, loser: FinancialYear, fields: list[str]) -> None:
        for known in self.conflicts:
            if (known.year, known.scope, known.other.source) == (winner.year, winner.scope, loser.source) \
                    and all(getattr(known.other, n) == getattr(loser, n) for n in AMOUNT_FIELDS):
                known.kept_source = winner.source
                known.fields = sorted(set(known.fields) | set(fields))
                return
        self.conflicts.append(Conflict(winner.year, winner.scope, list(fields), winner.source, loser))

    def merge(self, other: "FinancialSeries", authority: dict | None = None) -> "FinancialSeries":
        """Adds every record (and the conflicts already found) of `other`; returns self."""
        for record in other.records_sorted():
            self.add(record, authority)
        for conflict in other.conflicts:
            self.add(conflict.other, authority)  # re-evaluates against the kept record
        return self

    # --- reading

    def records_sorted(self) -> list[FinancialYear]:
        return sorted(self.records.values(), key=lambda r: (r.year, SCOPE_ORDER.get(r.scope, 9)))

    def get(self, year: int, scope: str) -> FinancialYear | None:
        return self.records.get((int(year), scope))

    @property
    def empty(self) -> bool:
        return not self.records

    def scopes(self) -> list[str]:
        """Scopes that have data, most useful first (most years; ties: consolidated, separate, unknown)."""
        counts: dict[str, int] = {}
        for _year, scope in self.records:
            counts[scope] = counts.get(scope, 0) + 1
        return sorted(counts, key=lambda s: (-counts[s], SCOPE_ORDER.get(s, 9)))

    def years(self, scope: str | None = None) -> list[int]:
        return sorted({y for (y, s) in self.records if scope is None or s == scope})

    def conflicts_for(self, scope: str | None = None) -> list[Conflict]:
        return [c for c in self.conflicts if scope is None or c.scope == scope]

    def view(self, scope: str | None = None, last: int = 5) -> SeriesView:
        """The years of ONE scope within `last` years of its latest one (default: `scopes()[0]`), in one currency
        (that of the latest year); records in another currency are counted in `dropped`."""
        if not self.records:
            return SeriesView()
        scope = scope if scope in SCOPES else self.scopes()[0]
        rows = [r for r in self.records_sorted() if r.scope == scope]
        if not rows:
            return SeriesView(scope=scope)
        rows = [r for r in rows if r.year > rows[-1].year - max(1, int(last))]  # a window of years
        currency = next((r.currency for r in reversed(rows) if r.currency), "")
        kept = [r for r in rows if r.currency == currency]
        sources = []
        for row in kept:
            if row.source and row.source not in sources:
                sources.append(row.source)
        years = {r.year for r in kept}
        return SeriesView(scope, currency, kept, len(rows) - len(kept), sources,
                          sorted({c.year for c in self.conflicts_for(scope) if c.year in years}))

    # --- JSON

    def to_dict(self) -> dict:
        return {"v": FORMAT_VERSION, "records": [r.to_dict() for r in self.records_sorted()],
                "conflicts": [c.to_dict() for c in self.conflicts]}

    @classmethod
    def from_dict(cls, data) -> "FinancialSeries":
        """Tolerant: bad rows are skipped (a damaged card field must not break the card)."""
        series = cls()
        if not isinstance(data, dict):
            return series
        for row in data.get("records") or []:
            try:
                record = FinancialYear.from_dict(row)
            except (TypeError, ValueError, KeyError):
                continue
            if not record.empty:
                series.records[(record.year, record.scope)] = record
        for row in data.get("conflicts") or []:
            try:
                series.conflicts.append(Conflict.from_dict(row))
            except (TypeError, ValueError, KeyError):
                continue
        return series

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "FinancialSeries":
        try:
            return cls.from_dict(json.loads(text)) if text else cls()
        except ValueError:
            return cls()
