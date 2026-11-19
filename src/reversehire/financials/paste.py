"""Figures from text the user copied by hand from a company-information page.

The app never reads those sites: the user selects the page text, copies it and pastes it
into "Paste figures…"; `parse_pasted(text)` finds, per fiscal year, the revenue, the net
profit (a "loss" is negative) and the employees, and returns them for an editable review
grid. Nothing is saved here: the dialog saves only what the user confirms.

Understood (Italian and English labels, tolerant layout):
- labels: Fatturato / Ricavi (delle vendite) / Valore della produzione / Revenue(s) /
  Turnover / Sales; Utile / Perdita / Risultato (d'esercizio) / Profit / Net income / Loss;
  Dipendenti / Addetti / Numero dipendenti / Employees / Staff / Headcount;
- one figure per line ("Fatturato 2023: € 12.345.678", "2022 revenue 5.4M€"), a year given
  on an earlier line ("Bilancio 2023" then "Ricavi: 1,2 mln"), or a table whose header row
  lists the years ("Anno 2021 2022 2023" then "Fatturato 10 11 12 mln");
- amounts: "1.234.567", "1,234,567", "1.234.567,89", "1,2 mln", "850 mila", "120 k€",
  "3 M€", "2 mld", "5.4M", "€ 1.234.567", negative "-120" or "(120)";
- ranges kept as ranges (low, high): "tra 10 e 50 milioni", "between 10 and 50 million",
  "da 2 a 5 mln", "10-50 M€", "10 – 49 dipendenti".

`parse_figure(text)` reads one cell of the review grid back ("12.345.678", "10–50 mln").
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

YEAR_MIN, YEAR_MAX = 1980, 2100
SOURCE = "pasted"                    # FinancialYear.source of the saved figures (model.AUTHORITY)
SOURCE_LABEL_KEY = "fin.source.pasted"

_LABELS: dict[str, tuple[str, ...]] = {
    # longest first inside each tuple is not required: matched as whole phrases
    "revenue": ("valore della produzione", "ricavi delle vendite", "ricavi delle vendite e delle prestazioni",
                "ricavi", "fatturato", "volume d'affari", "volume di affari", "revenues", "revenue",
                "turnover", "net sales", "sales"),
    "profit": ("utile netto", "utile d'esercizio", "utile (perdita)", "utile/perdita", "utile", "perdita",
               "risultato d'esercizio", "risultato netto", "risultato", "net income", "net profit",
               "profit", "net loss", "loss", "earnings"),
    "employees": ("numero dipendenti", "numero di dipendenti", "dipendenti", "addetti", "employees",
                  "employee count", "staff", "headcount", "workforce"),
}
_LOSS_WORDS = ("perdita", "loss")
_UNITS = (  # (regex, multiplier); checked right after a number
    (r"(?:mld|mrd|miliard[io]|billion[s]?|bn|b€|bln)", 1e9),
    (r"(?:mln|mil\.|milion[ei]|million[s]?|mio|mn|m€|m(?![a-z]))", 1e6),
    (r"(?:mila|migliaia|thousand[s]?|k€|k(?![a-z]))", 1e3),
)
_CURRENCY = r"(?:€|eur(?:o|os)?|\$|usd|£|gbp|chf)"
# thousands marks: "." "," "'" and the non-breaking / thin spaces (a plain space separates
# the cells of a table row, so it never joins two numbers)
_NUMBER = r"\(?[-+−]?\d(?:[\d.,'" + "\u00a0\u202f" + r"]*\d)?\)?"
_NUMBER_RE = re.compile(_NUMBER)
_START_RE = re.compile(rf"(?P<word>(?<![a-z])(?:tra|fra|between|da|from)\s)|\(?[-+−]?\d")
_PERCENT_RE = re.compile(r"\s*(?:%|percent\b|per\s*cento\b|pct\b)")
_DASH_RE = re.compile(rf"(?P<a>{_NUMBER})\s*(?P<ua>[a-z€.]*)\s*(?:-|–|—)\s*{_CURRENCY}?\s*(?P<b>{_NUMBER})"
                      rf"\s*(?P<ub>[a-z€.]*)")
_RANGE_RE = re.compile(
    rf"(?:tra|fra|between|da|from)\s*{_CURRENCY}?\s*(?P<a>{_NUMBER})\s*(?P<ua>[a-z€.]*)\s*{_CURRENCY}?\s*"
    rf"(?:e|and|a|to|-|–|—)\s*{_CURRENCY}?\s*(?P<b>{_NUMBER})\s*(?P<ub>[a-z€.]*)", re.I)
_YEAR_RE = re.compile(r"(?<![\d.,])((?:19|20)\d\d)(?![\d.,]*\d)")


@dataclass
class Figure:
    """An amount or a range (low < high); `text` is the original."""

    low: float
    high: float
    text: str = ""
    unit: float = 1.0  # the multiplier read with it (1e6 for "mln")

    @property
    def is_range(self) -> bool:
        return self.high != self.low

    @property
    def value(self) -> float | None:
        return None if self.is_range else self.low


@dataclass
class PastedYear:
    year: int
    revenue: Figure | None = None
    profit: Figure | None = None
    employees: Figure | None = None
    lines: list[str] = field(default_factory=list)  # the text the figures came from

    def get(self, name: str) -> Figure | None:
        return getattr(self, name)

    @property
    def empty(self) -> bool:
        return self.revenue is None and self.profit is None and self.employees is None


@dataclass
class PasteResult:
    years: list[PastedYear] = field(default_factory=list)  # newest first
    currency: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.years


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").replace("’", "'")
    return text.casefold()


def _number(raw: str, has_unit: bool) -> float | None:
    """A number token; a lone 3-digit group after "." or "," is a thousands mark unless a
    unit follows ("1.234" = 1234, "1,2 mln" = 1.2 M)."""
    text = raw.strip().replace(" ", "").replace(" ", "").replace("'", "").replace("−", "-")
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1]
    text = text.strip("()")
    if text.startswith("-"):
        negative, text = not negative, text[1:]
    text = text.lstrip("+")
    if not text or not re.fullmatch(r"[\d.,]+", text):
        return None
    dots, commas = text.count("."), text.count(",")
    if dots and commas:
        decimal = "." if text.rfind(".") > text.rfind(",") else ","
        thousands = "," if decimal == "." else "."
        number = text.replace(thousands, "").replace(decimal, ".")
    elif dots or commas:
        sep = "." if dots else ","
        parts = text.split(sep)
        if len(parts) > 2 or (len(parts[1]) == 3 and not has_unit):
            number = "".join(parts)
        else:
            number = parts[0] + "." + parts[1]
    else:
        number = text
    try:
        value = float(number)
    except ValueError:
        return None
    return -value if negative else value


def _unit(after: str) -> float:
    text = after.strip().lower()
    text = re.sub(rf"^{_CURRENCY}\s*", "", text, flags=re.I)  # "€ mln", "eur mila"
    for pattern, multiplier in _UNITS:
        if re.match(pattern, text, re.I):
            return multiplier
    return 1.0


def _is_year_token(token: str) -> bool:
    token = token.strip().strip("()")
    return bool(re.fullmatch(r"(?:19|20)\d\d", token)) and YEAR_MIN <= int(token) <= YEAR_MAX


def _amounts(text: str, *, skip_years: bool = True) -> list[Figure]:
    """Every amount or range of a piece of text, left to right."""
    out: list[Figure] = []
    folded = _fold(text)
    position = 0
    while position < len(folded):
        found = _START_RE.search(folded, position)
        if found is None:
            break
        at = found.start()
        rest = folded[at:]
        match = _RANGE_RE.match(rest) if found.group("word") else None
        if match is None and found.group("word"):
            position = found.end()
            continue
        if match is None:
            dash = _DASH_RE.match(rest)
            if dash is not None and not (_is_year_token(dash.group("a")) or _is_year_token(dash.group("b"))):
                match = dash
        if match is not None:
            ua, ub = match.group("ua"), match.group("ub")
            unit_b = _unit(ub + rest[match.end():match.end() + 12])
            unit_a = _unit(ua) if _unit(ua) != 1.0 else unit_b
            low, high = _number(match.group("a"), unit_a != 1.0), _number(match.group("b"), unit_b != 1.0)
            if low is not None and high is not None:
                low, high = low * unit_a, high * unit_b
                out.append(Figure(min(low, high), max(low, high), text[at:at + match.end()].strip(), unit_b))
                position = at + match.end()
                continue
        single = _NUMBER_RE.match(rest)
        if single is None:
            position = at + 1
            continue
        token = single.group(0)
        end = at + single.end()
        if at > 0 and folded[at - 1].isalpha():  # "Q4", "abc123": not an amount
            position = end
            continue
        after = folded[end:end + 14]
        if _PERCENT_RE.match(after):
            # "+12%", "12,5 %", "3 per cento": a growth rate or a share, never an amount
            position = end + _PERCENT_RE.match(after).end()
            continue
        unit = _unit(after)
        if skip_years and _is_year_token(token):
            # "2023" is a year unless a unit or a currency follows it and no other number
            # comes right after ("2023 € 12.345" and "2021 k€ 950" are years)
            marked = unit != 1.0 or re.match(rf"\s*{_CURRENCY}", after)
            followed = re.match(rf"\s*[a-z€$£.]*\s*{_CURRENCY}?\s*\(?[-+]?\d", after)
            if not marked or followed:
                position = end
                continue
        if unit == 1.0:  # a unit written before the number: "k€ 950", "€ mln 3,4"
            before = folded[max(0, at - 10):at]
            prefix = re.search(r"(mld|mln|mila|k€|m€|migliaia|milioni|million[s]?|thousand[s]?)\s*"
                               rf"{_CURRENCY}?\s*$", before)
            if prefix is not None:
                unit = _unit(prefix.group(1))
        value = _number(token, unit != 1.0)
        if value is not None:
            out.append(Figure(value * unit, value * unit, text[at:end].strip(), unit))
        position = end
    return out


def _label_of(line: str) -> tuple[str | None, int, bool]:
    """(field, end of the label, is a loss label) of the first label in a line."""
    folded = _fold(line)
    best: tuple[int, int, str, str] | None = None
    for name, phrases in _LABELS.items():
        for phrase in phrases:
            for match in re.finditer(rf"(?<![a-z]){re.escape(phrase)}(?![a-z])", folded):
                candidate = (match.start(), -(match.end() - match.start()), name, phrase)
                if best is None or candidate < best:
                    best = candidate
    if best is None:
        return None, 0, False
    start, negative_length, name, phrase = best
    return name, start - negative_length, phrase in _LOSS_WORDS or phrase in ("net loss",)


def _years(line: str) -> list[int]:
    return [int(y) for y in _YEAR_RE.findall(_fold(line)) if YEAR_MIN <= int(y) <= YEAR_MAX]


def _currency(text: str) -> str:
    folded = _fold(text)
    if "€" in folded or re.search(r"\beur", folded):
        return "EUR"
    if "$" in folded or "usd" in folded:
        return "USD"
    if "£" in folded or "gbp" in folded:
        return "GBP"
    if "chf" in folded:
        return "CHF"
    return ""


def parse_pasted(text: str) -> PasteResult:
    """See the module docstring. Never raises."""
    result = PasteResult(currency=_currency(text) or "EUR")
    by_year: dict[int, PastedYear] = {}
    header_years: list[int] = []
    context_year: int | None = None
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]

    def put(year: int, name: str, figure: Figure, loss: bool, source_line: str) -> None:
        if not YEAR_MIN <= year <= YEAR_MAX:
            return
        entry = by_year.setdefault(year, PastedYear(year))
        if name == "profit" and loss and figure.high > 0:
            figure = Figure(-figure.high, -figure.low, figure.text)
        if name == "employees" and (figure.low < 0 or figure.high > 5_000_000):
            return
        if entry.get(name) is None:
            setattr(entry, name, figure)
            if source_line not in entry.lines:
                entry.lines.append(source_line)

    for index, line in enumerate(lines):
        if not line:
            continue
        name, label_end, loss = _label_of(line)
        years = _years(line)
        if name is None:
            if len(years) >= 2 and not _amounts(line):
                header_years = years  # a table header: "Anno 2021 2022 2023"
            elif len(years) == 1 and not _amounts(line):
                context_year = years[0]  # "Bilancio 2023", "Esercizio 2023"
            continue
        tail = line[label_end:]
        amounts = _amounts(tail)
        # the value on the next lines ("Fatturato" / "2023" / "€ 1.234.567")
        for following in lines[index + 1:index + 3]:
            if amounts or not following or _label_of(following)[0] is not None:
                break
            amounts = _amounts(following)
            years = years or _years(following)
        if len(amounts) > 1:
            # a table row: a unit written once at the end ("10 11 12,5 mln") is for every cell
            units = {figure.unit for figure in amounts[:-1]}
            if units == {1.0} and amounts[-1].unit != 1.0:
                scale = amounts[-1].unit
                amounts = [Figure(f.low * scale, f.high * scale, f.text, scale) if f.unit == 1.0 else f
                           for f in amounts]
        if not amounts:
            continue
        if len(years) == 1 or (years and len(amounts) == 1):
            put(years[0], name, amounts[0], loss, line)
        elif len(years) >= 2 and len(amounts) == len(years):
            for year, figure in zip(years, amounts):
                put(year, name, figure, loss, line)
        elif header_years and len(amounts) == len(header_years):
            for year, figure in zip(header_years, amounts):
                put(year, name, figure, loss, line)
        elif header_years and len(amounts) < len(header_years):
            # missing cells at the start ("n.d."): align to the latest years
            for year, figure in zip(header_years[-len(amounts):], amounts):
                put(year, name, figure, loss, line)
        elif context_year is not None:
            put(context_year, name, amounts[0], loss, line)
        else:
            result.warnings.append(f"no year for: {line[:80]}")
    result.years = sorted((y for y in by_year.values() if not y.empty), key=lambda y: -y.year)
    return result


def parse_figure(text: str) -> Figure | None:
    """One cell of the review grid ("12.345.678", "1,2 mln", "10–50 mln", "") -> Figure or
    None; raises ValueError when the cell has text that is not an amount."""
    if text is None or not str(text).strip() or str(text).strip() in ("-", "–", "n.d.", "n/a"):
        return None
    # "12 345 678" (Ukrainian style, no-break or thin spaces between groups of three)
    text = re.sub(r"(?<=\d)[   ](?=\d{3}(?!\d))", "", str(text))
    found = _amounts(text, skip_years=False)
    if len(found) != 1:
        raise ValueError(f"not an amount: {text!r}")
    return found[0]


def format_figure(figure: Figure | None, *, whole: bool = False, language: str | None = None) -> str:
    """The grid text of a figure with the interface language's thousands marks:
    "12.345.678" (it), "12,345,678" (en), "10.000.000–50.000.000"; `parse_figure` reads
    every style back. `language`: the interface language when None."""
    from ..numbers import format_number

    if figure is None:
        return ""

    def one(value: float) -> str:
        if whole or abs(value - round(value)) < 1e-6:
            return format_number(value, 0, language)
        return format_number(value, 2, language)

    return one(figure.low) if not figure.is_range else f"{one(figure.low)}–{one(figure.high)}"
