"""Wikidata as a LOW-authority source of revenue, net profit and employees.

Properties: P2139 total revenue, P2295 net profit, P1128 employees; each statement carries
the year in the qualifier P585 (point in time). Wikidata is volunteer data: it is rarely
present for Italian companies (the study found it only for the Accenture group), the scope
is not stated (so "unknown": it may be the group or the single company) and values may be
outdated. Deprecated statements are ignored; two different values for the same year and
property (with the same rank) are treated as ambiguous and left blank.

Requests (www.wikidata.org/w/api.php only, through the polite gateway):
- identity.wikidata (QID) given: 1 request (wbgetentities);
- otherwise the QID is searched by LEI (P1278) or EU VAT number (P3608) with
  `haswbstatement:` (1 request) and accepted only when exactly one entity matches.
"""

from __future__ import annotations

import logging
import re

from .base import FinancialSource, FinancialsIdentity, SourceError, reason, register_source
from .model import FinancialSeries, FinancialYear, now_iso

log = logging.getLogger("reversehire.financials")

API = "https://www.wikidata.org/w/api.php"
SOURCE = "wikidata"
TTL_S = 7 * 24 * 3600
P_REVENUE, P_PROFIT, P_EMPLOYEES = "P2139", "P2295", "P1128"
P_LEI, P_VAT, P_POINT_IN_TIME = "P1278", "P3608", "P585"
# Currency items of the quantity unit URL. Anything else is skipped, never guessed.
CURRENCY_ITEMS = {"Q4916": "EUR", "Q4917": "USD", "Q25224": "GBP", "Q25344": "CHF"}
_YEAR = re.compile(r"^[+-]?(\d{4})")
_UNIT_ITEM = re.compile(r"/(Q\d+)$")


def _amount(statement: dict) -> float | None:
    try:
        return float(statement["mainsnak"]["datavalue"]["value"]["amount"])
    except (KeyError, TypeError, ValueError):
        return None


def _currency(statement: dict) -> str:
    try:
        unit = statement["mainsnak"]["datavalue"]["value"]["unit"]
    except (KeyError, TypeError):
        return ""
    match = _UNIT_ITEM.search(str(unit))
    return CURRENCY_ITEMS.get(match.group(1), "") if match else ""


def _year(statement: dict) -> int | None:
    """Year of the point in time (precision at least year), else None."""
    for snak in (statement.get("qualifiers") or {}).get(P_POINT_IN_TIME, []):
        try:
            value = snak["datavalue"]["value"]
            if int(value.get("precision", 0)) < 9:
                continue
            match = _YEAR.match(value["time"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if match:
            return int(match.group(1))
    return None


def _by_year(claims: dict, prop: str, *, money: bool) -> dict[int, tuple[float, str]]:
    """{year: (amount, currency)} of one property. Preferred rank beats normal; deprecated
    is ignored; conflicting values of the same best rank for a year are dropped."""
    per_year: dict[int, dict[str, set]] = {}
    for statement in claims.get(prop, []) or []:
        if not isinstance(statement, dict):
            continue
        rank = statement.get("rank", "normal")
        if rank == "deprecated":
            continue
        year, amount = _year(statement), _amount(statement)
        if year is None or amount is None:
            continue
        currency = _currency(statement) if money else ""
        if money and not currency:
            continue
        per_year.setdefault(year, {}).setdefault(rank, set()).add((amount, currency))
    out = {}
    for year, ranks in per_year.items():
        values = ranks.get("preferred") or ranks.get("normal") or set()
        if len(values) == 1:
            out[year] = next(iter(values))
        elif values:
            log.info("Wikidata %s %s: %d different values; left blank", prop, year, len(values))
    return out


def parse_claims(claims: dict, qid: str, *, years: int = 5, retrieved_at: str = "") -> list[FinancialYear]:
    """Records (scope "unknown") of the years within `years` of the latest one that has any figure."""
    retrieved_at = retrieved_at or now_iso()
    revenue = _by_year(claims, P_REVENUE, money=True)
    profit = _by_year(claims, P_PROFIT, money=True)
    employees = _by_year(claims, P_EMPLOYEES, money=False)
    known = sorted(set(revenue) | set(profit) | set(employees))
    chosen = [y for y in known if known and y > known[-1] - max(1, years)]  # a window of years
    url = f"https://www.wikidata.org/wiki/{qid}"
    out = []
    for year in chosen:
        currencies = {x[year][1] for x in (revenue, profit) if year in x}
        if len(currencies) > 1:
            continue  # revenue and profit in different currencies: do not guess
        out.append(FinancialYear(
            year=year, revenue=revenue[year][0] if year in revenue else None,
            profit=profit[year][0] if year in profit else None,
            employees=employees[year][0] if year in employees else None,
            currency=next(iter(currencies), ""), scope="unknown", source=SOURCE, source_url=url,
            retrieved_at=retrieved_at,
            note="year of the Wikidata point-in-time; scope not stated"))
    return out


@register_source
class WikidataSource(FinancialSource):
    name = SOURCE
    automatic = True

    def __init__(self, gateway=None, *, years: int = 5):
        super().__init__(gateway)
        self.years = max(1, int(years))

    def _find_qid(self, identity: FinancialsIdentity) -> str:
        if identity.valid_qid:
            return identity.valid_qid
        statements = []
        if identity.valid_lei:
            statements.append(f"haswbstatement:{P_LEI}={identity.valid_lei}")
        if identity.vat_digits and identity.country == "IT":
            statements.append(f"haswbstatement:{P_VAT}=IT{identity.vat_digits}")
        for query in statements:
            data = self._json(API, params={"action": "query", "list": "search", "srsearch": query,
                                           "format": "json"}, cache_ttl_s=TTL_S)
            hits = (data or {}).get("query", {}).get("search", []) if isinstance(data, dict) else []
            titles = [h.get("title") for h in hits if isinstance(h, dict)]
            if len(titles) == 1 and re.fullmatch(r"Q[1-9][0-9]*", str(titles[0])):
                return titles[0]
            if len(titles) > 1:
                log.info("Wikidata: %d entities for %s; ambiguous, not used", len(titles), query)
        return ""

    def fetch(self, identity: FinancialsIdentity) -> FinancialSeries | None:
        self.last_reason = None
        if not (identity.valid_qid or identity.valid_lei or identity.vat_digits):
            self.last_reason = reason(
                "fin.why.no_wikidata_key")
            return None
        qid = self._find_qid(identity)
        if not qid:
            self.last_reason = reason(
                "fin.why.no_wikidata_item")
            return None
        data = self._json(API, params={"action": "wbgetentities", "ids": qid, "props": "claims",
                                       "format": "json"}, cache_ttl_s=TTL_S)
        entity = ((data or {}).get("entities") or {}).get(qid) if isinstance(data, dict) else None
        claims = entity.get("claims") if isinstance(entity, dict) else None
        if not isinstance(claims, dict):
            raise SourceError("bad_answer", "Wikidata answer without claims")
        series = FinancialSeries()
        for record in parse_claims(claims, qid, years=self.years):
            series.add(record)
        if series.empty:
            self.last_reason = reason(
                "fin.why.wikidata_empty")
            return None
        return series
