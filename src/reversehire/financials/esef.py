"""ESEF annual financial reports of listed companies, from the filings.xbrl.org API.

Why this source: the European Single Electronic Format obliges listed issuers to file
their annual report as machine-readable iXBRL; filings.xbrl.org republishes them with a
JSON:API and the xBRL-JSON of each filing ("no restrictions on the ways the data can be
used", no robots.txt). It covers listed companies only (874 Italian filings seen in the
study), so for a small unlisted S.r.l. there is nothing here.

Flow (all through the polite gateway):
1. GET /api/filings?filter[entity.identifier]=<LEI>&sort=-period_end  (one request);
2. for the newest filings, GET their `json_url` (xBRL-JSON, a few MB). Every filing carries
   the previous year as comparative, so about 3 downloads give 5 years. Only the figures
   are kept; the file is dropped.
3. facts kept: exactly the base dimensions (concept, entity, period, unit) - more
   dimensions mean segment / equity-component breakdowns - and a period of about one
   year. `ifrs-full:Revenue` (fallback `RevenueFromContractsWithCustomers`),
   `ifrs-full:ProfitLoss` (fallback, noted, `ProfitLossAttributableToOwnersOfParent`),
   `ifrs-full:AverageNumberOfEmployees` when tagged (rare). Anything else
   (e.g. `RevenueAndOperatingIncome`, which includes other income) is NOT used: a blank
   is better than a figure of another meaning.
Scope is "consolidated": IFRS annual reports of listed groups are consolidated (the
parent-only statements are not tagged in the ESEF package). A newer filing wins for a year
it restates; the older value is then flagged as a conflict.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

from .base import FinancialSource, FinancialsIdentity, SourceError, reason, register_source
from .model import FinancialSeries, FinancialYear, now_iso

log = logging.getLogger("reversehire.financials")

BASE = "https://filings.xbrl.org"
SOURCE = "esef"
LIST_TTL_S = 7 * 24 * 3600
REVENUE_CONCEPTS = ("ifrs-full:Revenue", "ifrs-full:RevenueFromContractsWithCustomers")
PROFIT_CONCEPTS = ("ifrs-full:ProfitLoss", "ifrs-full:ProfitLossAttributableToOwnersOfParent")
EMPLOYEE_CONCEPTS = ("ifrs-full:AverageNumberOfEmployees",)
BASE_DIMENSIONS = {"concept", "entity", "period", "unit"}


def _parse_period(text: str):
    """("2025-01-01T00:00:00/2026-01-01T00:00:00") -> (start date, last day date) or None
    for instants and for periods that are not about one year."""
    if not isinstance(text, str) or "/" not in text:
        return None
    start_text, end_text = text.split("/", 1)
    try:
        start = datetime.fromisoformat(start_text.strip()).date()
        end_dt = datetime.fromisoformat(end_text.strip())
    except ValueError:
        return None
    exclusive_end = end_dt.date() if "T" in end_text else end_dt.date() + timedelta(days=1)
    days = (exclusive_end - start).days
    if not 350 <= days <= 380:
        return None
    return start, exclusive_end - timedelta(days=1)


def _amount(value) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def parse_xbrl_json(document, lei: str, *, source_url: str = "", retrieved_at: str = "") -> list[FinancialYear]:
    """FinancialYear records (scope "consolidated") from one xBRL-JSON filing, one per
    fiscal year it contains (current and comparative). Pure function (no network)."""
    facts = document.get("facts") if isinstance(document, dict) else None
    if isinstance(facts, dict):
        facts = list(facts.values())
    if not isinstance(facts, list):
        return []
    retrieved_at = retrieved_at or now_iso()
    # (concept, last day) -> {(value, currency)}; several equal duplicates are normal
    found: dict[tuple, set] = {}
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        dims = fact.get("dimensions")
        if not isinstance(dims, dict) or set(dims) - BASE_DIMENSIONS - {"language"}:
            continue
        concept = dims.get("concept")
        if concept not in REVENUE_CONCEPTS + PROFIT_CONCEPTS + EMPLOYEE_CONCEPTS:
            continue
        if str(dims.get("entity", "")).split(":")[-1].strip().upper() != lei.upper():
            continue
        period = _parse_period(dims.get("period", ""))
        number = _amount(fact.get("value"))
        if period is None or number is None:
            continue
        unit = str(dims.get("unit", ""))
        if concept in EMPLOYEE_CONCEPTS:
            if unit not in ("xbrli:pure", "pure", ""):
                continue
            currency = ""
        else:
            if not unit.startswith("iso4217:"):
                continue
            currency = unit.split(":", 1)[1].upper()
        found.setdefault((concept, period[1]), set()).add((number, currency))

    def pick(concepts, last_day):
        """First concept with exactly one distinct value (an ambiguous one is left blank)."""
        for concept in concepts:
            values = found.get((concept, last_day))
            if not values:
                continue
            if len(values) > 1:
                log.info("ESEF %s %s: %s has %d different values; left blank", lei, last_day,
                         concept, len(values))
                continue
            number, currency = next(iter(values))
            return concept, number, currency
        return None

    out = []
    for last_day in sorted({day for (_c, day) in found}):
        revenue = pick(REVENUE_CONCEPTS, last_day)
        profit = pick(PROFIT_CONCEPTS, last_day)
        employees = pick(EMPLOYEE_CONCEPTS, last_day)
        currencies = {x[2] for x in (revenue, profit) if x}
        if len(currencies) > 1:
            log.info("ESEF %s %s: revenue and profit in different currencies; skipped", lei, last_day)
            continue
        notes = []
        if revenue and revenue[0] != "ifrs-full:Revenue":
            notes.append(f"revenue: {revenue[0].split(':')[1]}")
        if profit and profit[0] != "ifrs-full:ProfitLoss":
            notes.append(f"profit: {profit[0].split(':')[1]}")
        record = FinancialYear(
            year=last_day.year,
            revenue=revenue[1] if revenue else None,
            profit=profit[1] if profit else None,
            employees=employees[1] if employees else None,
            currency=next(iter(currencies), ""), scope="consolidated", source=SOURCE,
            source_url=source_url, retrieved_at=retrieved_at, period_end=last_day.isoformat(),
            note="; ".join(notes))
        if not record.empty:
            out.append(record)
    return out


@register_source
class EsefSource(FinancialSource):
    """Annual figures of a listed company from its ESEF filings (needs the LEI)."""

    name = SOURCE
    automatic = True

    def __init__(self, gateway=None, *, base: str = BASE, years: int = 5):
        super().__init__(gateway)
        self.base = base.rstrip("/")
        self.years = max(1, int(years))

    def fetch(self, identity: FinancialsIdentity) -> FinancialSeries | None:
        self.last_reason = None
        lei = identity.valid_lei
        if not lei:
            self.last_reason = reason(
                "fin.why.no_lei")
            return None
        listing = self._json(f"{self.base}/api/filings",
                             params={"filter[entity.identifier]": lei, "sort": "-period_end",
                                     "page[size]": "12"},
                             cache_ttl_s=LIST_TTL_S)
        filings = self._filings(listing)
        if not filings:
            self.last_reason = reason(
                "fin.why.no_esef")
            return None
        series = FinancialSeries()
        covered: set[int] = set()
        stamp = now_iso()
        for filing in filings:
            if len(covered) >= self.years:
                break
            year = int(filing["period_end"][:4])
            if year in covered and (year - 1) in covered:
                continue  # nothing new would come out of this filing
            document = self._json(urljoin(self.base + "/", filing["json_url"]))
            url = urljoin(self.base + "/", filing["report_url"] or filing["json_url"])
            for record in parse_xbrl_json(document, lei, source_url=url, retrieved_at=stamp):
                series.add(record)  # newest filing first: it wins for restated years
                covered.add(record.year)
        if series.empty:
            self.last_reason = reason(
                "fin.why.esef_no_figures")
            return None
        return series

    @staticmethod
    def _filings(listing) -> list[dict]:
        """One entry per period (newest first), skipping filings without a JSON file."""
        data = listing.get("data") if isinstance(listing, dict) else None
        seen: set[str] = set()
        out = []
        for item in data or []:
            attributes = item.get("attributes") if isinstance(item, dict) else None
            if not isinstance(attributes, dict):
                continue
            period_end, json_url = attributes.get("period_end"), attributes.get("json_url")
            if not (isinstance(period_end, str) and len(period_end) >= 10 and isinstance(json_url, str)
                    and json_url):
                continue
            try:
                date.fromisoformat(period_end[:10])
            except ValueError:
                continue
            if period_end in seen:
                continue  # same period filed in two languages: one is enough
            seen.add(period_end)
            out.append({"period_end": period_end, "json_url": json_url,
                        "report_url": attributes.get("report_url") or ""})
        return sorted(out, key=lambda f: f["period_end"], reverse=True)
