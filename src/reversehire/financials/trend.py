"""Revenue trend and employees from the STORED figures only (nothing is estimated).

- `revenue_trend(series, scope=None) -> RevenueTrend`: the compound annual growth rate
  (CAGR) of revenue between the first and the last year of ONE scope and ONE currency
  (`FinancialSeries.view`), computed only when at least MIN_YEARS yearly revenue values
  exist and both ends are positive; otherwise the state is "insufficient" ("not enough
  data"). State: "growing" (CAGR >= +STABLE_BAND), "declining" (<= -STABLE_BAND), else
  "stable".
- `employees_by_year(series, scope=None)` and `latest_employees(series, scope=None)`.
- `ranking_bonus(trend)`: the small, explicit bonus of a growing company in the
  "promising company" ordering (discovery/ranking.py): GROWTH_BONUS for a CAGR of at
  least +STABLE_BAND, STRONG_GROWTH_BONUS from +STRONG_GROWTH; never negative, so a
  declining company or one without figures is never pushed down nor hidden.

Texts go through a `tr(key, default)` callable like the rest of the package.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .base import TrFunc, default_tr
from .model import FinancialSeries

MIN_YEARS = 3              # yearly revenue values needed for a trend
STABLE_BAND = 0.02         # |CAGR| below 2 %/year: "stable"
STRONG_GROWTH = 0.10       # from +10 %/year: the larger bonus
GROWTH_BONUS = 0.05        # added to the pre-crawl ranking score (scale about 0-2)
STRONG_GROWTH_BONUS = 0.10

TREND_STRINGS = {
    "fin.trend.growing": "Revenue growing {rate} %/year over {first}–{last}",
    "fin.trend.stable": "Revenue stable ({rate} %/year over {first}–{last})",
    "fin.trend.declining": "Revenue declining {rate} %/year over {first}–{last}",
    "fin.trend.cell": "{rate} %/year ({first}–{last})",
    "fin.trend.insufficient": "Revenue trend: not enough data (at least 3 years needed)",
    "fin.trend.source": "source: {sources}",
}


@dataclass
class RevenueTrend:
    state: str = "insufficient"          # growing | stable | declining | insufficient
    cagr: float | None = None            # fraction per year (0.05 = +5 %/year)
    first_year: int | None = None
    last_year: int | None = None
    years: list[int] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    currency: str = ""
    scope: str = ""

    @property
    def known(self) -> bool:
        return self.cagr is not None

    @property
    def percent(self) -> float | None:
        return None if self.cagr is None else round(self.cagr * 100, 1)

    def to_dict(self) -> dict:
        return {"state": self.state, "cagr": self.cagr, "first_year": self.first_year,
                "last_year": self.last_year, "years": list(self.years), "sources": list(self.sources),
                "currency": self.currency, "scope": self.scope}


def cagr(first: float, last: float, years: int) -> float | None:
    """Compound annual growth rate between two positive values `years` apart."""
    if years <= 0 or first is None or last is None or first <= 0 or last <= 0:
        return None
    return (last / first) ** (1.0 / years) - 1.0


def revenue_trend(series: FinancialSeries | None, scope: str | None = None, *, last: int = 10) -> RevenueTrend:
    if series is None or series.empty:
        return RevenueTrend()
    view = series.view(scope, last=last)
    rows = [r for r in view.rows if r.revenue is not None]
    trend = RevenueTrend(years=[r.year for r in rows], currency=view.currency, scope=view.scope)
    for row in rows:
        name = row.source.split(":")[0]
        if name and name not in trend.sources:
            trend.sources.append(name)
    if len(rows) < MIN_YEARS:
        return trend
    first, final = rows[0], rows[-1]
    rate = cagr(first.revenue, final.revenue, final.year - first.year)
    if rate is None:
        return trend
    trend.cagr, trend.first_year, trend.last_year = rate, first.year, final.year
    trend.state = "growing" if rate >= STABLE_BAND else "declining" if rate <= -STABLE_BAND else "stable"
    return trend


def trend_text(trend: RevenueTrend, tr: TrFunc | None = None) -> str:
    localised = tr is not None   # the interface's texts: its decimal mark too
    tr = tr or default_tr
    key = f"fin.trend.{trend.state}"
    text = tr(key, TREND_STRINGS[key])
    if trend.cagr is None:
        return text
    rate = f"{abs(trend.percent):.1f}" if trend.state != "stable" else f"{trend.percent:+.1f}"
    rate = _decimal(rate) if localised else rate
    if trend.state == "growing":
        rate = "+" + rate
    elif trend.state == "declining":
        rate = "-" + rate
    try:
        return text.format(rate=rate, first=trend.first_year, last=trend.last_year)
    except (KeyError, IndexError, ValueError):
        return text


def _decimal(number: str) -> str:
    """The interface language's decimal mark ("6,6" in Italian); unchanged outside the app."""
    try:
        from ..numbers import decimal_text

        return decimal_text(number)
    except Exception:  # noqa: BLE001 - the package used on its own
        return number


def trend_cell(trend: RevenueTrend, tr: TrFunc | None = None) -> str:
    """Short text of the "Revenue trend" column: "+6.3 %/year (2021–2024)" ("+6,3 %/anno"
    in Italian) or ""."""
    if trend.cagr is None:
        return ""
    rate = f"{trend.percent:+.1f}"
    rate = _decimal(rate) if tr is not None else rate
    template = (tr or default_tr)("fin.trend.cell", TREND_STRINGS["fin.trend.cell"])
    try:
        return template.format(rate=rate, first=trend.first_year, last=trend.last_year)
    except (KeyError, IndexError, ValueError):
        return f"{trend.percent:+.1f} %/year ({trend.first_year}–{trend.last_year})"


def employees_by_year(series: FinancialSeries | None, scope: str | None = None) -> list[tuple[int, float, str]]:
    """(year, employees, source) of the chosen scope (employees are not money: every
    currency of that scope counts), oldest first."""
    if series is None or series.empty:
        return []
    chosen = scope if scope in series.scopes() else series.scopes()[0]
    return [(r.year, r.employees, r.source.split(":")[0]) for r in series.records_sorted()
            if r.scope == chosen and r.employees is not None]


def latest_employees(series: FinancialSeries | None, scope: str | None = None) -> tuple[float, int, str] | None:
    """(employees, year, source) of the latest year that has them, any scope when the
    chosen one has none; None when no figure is stored."""
    rows = employees_by_year(series, scope)
    if not rows and series is not None and not series.empty:
        rows = [(r.year, r.employees, r.source.split(":")[0]) for r in series.records_sorted()
                if r.employees is not None]
    if not rows:
        return None
    year, value, source = max(rows, key=lambda item: item[0])
    return value, year, source


def ranking_bonus(trend: RevenueTrend | dict | None) -> float:
    """Bonus of the "promising company" ordering (see the module docstring)."""
    rate = trend.get("cagr") if isinstance(trend, dict) else getattr(trend, "cagr", None)
    if rate is None:
        return 0.0
    if rate >= STRONG_GROWTH:
        return STRONG_GROWTH_BONUS
    if rate >= STABLE_BAND:
        return GROWTH_BONUS
    return 0.0
