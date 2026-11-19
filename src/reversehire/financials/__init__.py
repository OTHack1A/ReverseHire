"""Company financials: revenue, net profit and employees of the last years.

Public API (the chart needs Qt; everything else does not and is imported lazily):

    from reversehire.financials import (FinancialYear, FinancialSeries, FinancialsIdentity,
        EsefSource, WikidataSource, OpenapiSource, ManualSource, collect_financials,
        parse_csv, add_value, STRINGS)
    from reversehire.financials.chart import FinancialChart   # Qt widget

Every figure carries source, fiscal year, currency and scope; "consolidated" and "separate"
figures are never mixed in one series view. See model.py for the merge rules.
"""

from .base import FinancialSource, FinancialsIdentity, Reason, SourceError
from .collect import CollectResult, SourceReport, collect_financials
from .esef import EsefSource
from .manual import ImportResult, ManualSource, add_value, parse_amount, parse_csv, parse_csv_file
from .model import (AUTHORITY, SCOPES, Conflict, FinancialSeries, FinancialYear, SeriesView)
from .openapi import OpenapiSource
from .paste import PastedYear, PasteResult, parse_figure, parse_pasted
from .strings import STRINGS
from .trend import RevenueTrend, latest_employees, ranking_bonus, revenue_trend
from .wikidata import WikidataSource

__all__ = [
    "AUTHORITY", "SCOPES", "STRINGS", "CollectResult", "Conflict", "EsefSource", "FinancialSeries",
    "FinancialSource", "FinancialYear", "FinancialsIdentity", "ImportResult", "ManualSource",
    "OpenapiSource", "Reason", "SeriesView", "SourceError", "SourceReport", "WikidataSource",
    "add_value", "collect_financials", "parse_amount", "parse_csv", "parse_csv_file",
    "PastedYear", "PasteResult", "RevenueTrend", "latest_employees", "parse_figure", "parse_pasted",
    "ranking_bonus", "revenue_trend",
]
