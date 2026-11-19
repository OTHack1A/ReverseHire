"""Manual entry and CSV import of financial figures (e.g. from a balance sheet the owner bought).

CSV: one row per fiscal year, header row required. Columns (case-insensitive, English or
Italian names; `;` `,` or tab as separator): year/anno, revenue/ricavi/fatturato,
profit/utile/risultato, employees/dipendenti, scope/ambito (consolidated/consolidato,
separate/separato/individuale; blank = `default_scope`), optional currency/valuta.
Blank cell = unknown (stays blank). Numbers accept "1234567", "1.234.567,89", "1,234,567.89",
"(1.250)" and "-1250"; an ambiguous single separator such as "1.234" (is it 1234 or 1.234?)
is REJECTED with a message instead of guessed. `multiplier` converts amounts given in
thousands or millions (employees are never multiplied).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from .base import FinancialSource, FinancialsIdentity, reason, register_source
from .model import SCOPES, FinancialSeries, FinancialYear, now_iso

SOURCE = "manual"

_ALIASES = {
    "year": {"year", "anno", "esercizio", "fiscal year", "fy"},
    "revenue": {"revenue", "revenues", "ricavi", "fatturato", "turnover", "valore della produzione"},
    "profit": {"profit", "net profit", "utile", "risultato", "utile netto", "utile/perdita",
               "utile (perdita)", "utile (perdita) d'esercizio", "net income"},
    "employees": {"employees", "dipendenti", "addetti", "headcount"},
    "scope": {"scope", "ambito", "perimetro", "tipo bilancio"},
    "currency": {"currency", "valuta", "divisa"},
}
_SCOPE_WORDS = {
    "consolidated": "consolidated", "consolidato": "consolidated", "group": "consolidated",
    "gruppo": "consolidated", "separate": "separate", "separato": "separate",
    "individuale": "separate", "single": "separate", "standalone": "separate",
    "unknown": "unknown", "": "",
}


class AmountError(ValueError):
    pass


def parse_amount(text) -> float | None:
    """"1.234.567,89" -> 1234567.89; "" -> None. Raises AmountError when not safely readable."""
    if text is None:
        return None
    raw = str(text).strip().replace(" ", " ")
    if not raw or raw in ("-", "--", "n/a", "N/A", "n.d.", "nd"):
        return None
    negative = False
    if raw.startswith("(") and raw.endswith(")"):
        negative, raw = True, raw[1:-1]
    raw = re.sub(r"[€$£]|EUR|USD|GBP|CHF", "", raw, flags=re.I).strip()
    if raw.startswith("-"):
        negative, raw = not negative, raw[1:].strip()
    elif raw.startswith("+"):
        raw = raw[1:].strip()
    raw = raw.replace(" ", "").replace("'", "")
    if not re.fullmatch(r"[0-9.,]+", raw) or raw.strip(".,") == "":
        raise AmountError(f"not a number: {text!r}")
    dots, commas = raw.count("."), raw.count(",")
    if dots and commas:
        decimal = "." if raw.rfind(".") > raw.rfind(",") else ","
        thousands = "," if decimal == "." else "."
        if raw.count(decimal) > 1 or raw.rfind(thousands) > raw.rfind(decimal):
            raise AmountError(f"ambiguous number: {text!r}")
        number = raw.replace(thousands, "").replace(decimal, ".")
    elif dots or commas:
        sep = "." if dots else ","
        parts = raw.split(sep)
        if len(parts) > 2:  # 1.234.567: separators are thousands marks
            if not all(len(p) == 3 for p in parts[1:]) or not 1 <= len(parts[0]) <= 3:
                raise AmountError(f"ambiguous number: {text!r}")
            number = "".join(parts)
        elif len(parts[1]) == 3:
            raise AmountError(f"ambiguous number (thousands or decimals?): {text!r}")
        else:
            number = parts[0] + "." + parts[1]
    else:
        number = raw
    try:
        value = float(number)
    except ValueError as error:
        raise AmountError(f"not a number: {text!r}") from error
    return -value if negative else value


def _column(header: str) -> str | None:
    name = " ".join(header.strip().lower().split())
    for key, names in _ALIASES.items():
        if name in names:
            return key
    return None


@dataclass
class ImportResult:
    series: FinancialSeries = field(default_factory=FinancialSeries)
    rows: int = 0
    errors: list[str] = field(default_factory=list)  # one readable line per rejected row/problem

    @property
    def ok(self) -> bool:
        return not self.errors and not self.series.empty


def parse_csv(text: str, *, source: str = SOURCE, default_scope: str = "unknown",
              default_currency: str = "EUR", multiplier: float = 1.0, note: str = "",
              source_url: str = "") -> ImportResult:
    """Parses CSV text into a series. Never raises: problems are listed in `errors`."""
    result = ImportResult()
    text = (text or "").lstrip("﻿")
    if not text.strip():
        result.errors.append("empty file")
        return result
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
    except csv.Error:
        class dialect(csv.excel):
            delimiter = ";" if sample.count(";") > sample.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        result.errors.append("empty file")
        return result
    columns = {}
    for index, header in enumerate(rows[0]):
        key = _column(header)
        if key and key not in columns:
            columns[key] = index
    if "year" not in columns or not ({"revenue", "profit", "employees"} & set(columns)):
        result.errors.append("header must contain year and at least one of revenue, profit, employees")
        return result
    stamp = now_iso()
    scope_default = default_scope if default_scope in SCOPES else "unknown"
    for number, row in enumerate(rows[1:], start=2):
        result.rows += 1

        def cell(key: str) -> str:
            index = columns.get(key)
            return row[index].strip() if index is not None and index < len(row) else ""

        try:
            year_text = cell("year")
            match = re.match(r"^\s*(\d{4})\b", year_text)
            if not match:
                raise ValueError(f"year not readable: {year_text!r}")
            scope_word = _SCOPE_WORDS.get(cell("scope").strip().lower())
            if scope_word is None:
                raise ValueError(f"scope not understood: {cell('scope')!r}")
            revenue, profit = parse_amount(cell("revenue")), parse_amount(cell("profit"))
            staff = parse_amount(cell("employees"))
            if staff is not None and (staff < 0 or abs(staff - round(staff)) > 1e-9):
                raise ValueError(f"employees must be a whole number: {cell('employees')!r}")
            if revenue is not None:
                revenue *= multiplier
            if profit is not None:
                profit *= multiplier
            currency = (cell("currency") or default_currency).upper()
            record = FinancialYear(
                year=int(match.group(1)), revenue=revenue, profit=profit, employees=staff,
                currency=currency, scope=scope_word or scope_default, source=source,
                source_url=source_url, retrieved_at=stamp, note=note)
            if record.empty:
                raise ValueError("no figure on this row")
            if not record.currency:
                raise ValueError(f"currency not understood: {currency!r}")
        except (ValueError, AmountError) as error:
            result.errors.append(f"row {number}: {error}")
            continue
        result.series.add(record)
    return result


def parse_csv_file(path, **kwargs) -> ImportResult:
    """Reads a CSV file (UTF-8, falling back to Windows-1252) and parses it."""
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        return ImportResult(errors=[f"cannot read file: {type(error).__name__}"])
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    return parse_csv(text, **kwargs)


def add_value(series: FinancialSeries, year: int, *, revenue=None, profit=None, employees=None,
              scope: str = "unknown", currency: str = "EUR", source: str = SOURCE,
              source_url: str = "", note: str = "", replace: bool = True, ranges: dict | None = None) -> str:
    """Adds one hand-typed year. With `replace` (default) the owner's new entry replaces the
    value he typed before for that (year, scope) without a conflict; otherwise the normal
    merge rules apply. Returns the merge outcome ("added", "replaced", ...). Raises
    ValueError for an impossible year, scope or amount."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    for name, value in (("revenue", revenue), ("profit", profit), ("employees", employees)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError(f"{name} must be a number")
    record = FinancialYear(year=year, revenue=revenue, profit=profit, employees=employees,
                           currency=currency, scope=scope, source=source, source_url=source_url,
                           retrieved_at=now_iso(), note=note, ranges=dict(ranges or {}))
    if record.empty:
        raise ValueError("give at least one figure")
    if replace:
        current = series.get(record.year, record.scope)
        if current is not None and current.source.split(":")[0] == source.split(":")[0]:
            del series.records[(record.year, record.scope)]
    return series.add(record)


@register_source
class ManualSource(FinancialSource):
    """Figures the owner typed or imported from CSV (no network)."""

    name = SOURCE
    needs_network = False

    def __init__(self, text: str | None = None, path=None, **options):
        super().__init__(None)
        self.text, self.path, self.options = text, path, options
        self.result: ImportResult | None = None

    def fetch(self, identity: FinancialsIdentity) -> FinancialSeries | None:
        self.last_reason = None
        if self.path is not None:
            self.result = parse_csv_file(self.path, **self.options)
        elif self.text is not None:
            self.result = parse_csv(self.text, **self.options)
        else:
            self.last_reason = reason("fin.why.no_manual")
            return None
        if self.result.series.empty:
            self.last_reason = reason(
                "fin.why.csv_empty",
                detail="; ".join(self.result.errors[:3]) or "no data")
            return None
        return self.result.series
