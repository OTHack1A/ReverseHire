"""Numbers as the interface language writes them.

English: 12,345,678.5   Italian: 12.345.678,5   Ukrainian: 12 345 678,5 (no-break spaces).
Every figure the user reads (amounts, employees, rows, megabytes) goes through these, so a
revenue of twelve million is never shown as "12345678" or with English marks in Italian.
The grid of the financials review window reads them back (financials/paste.parse_figure
accepts all three styles).
"""

from __future__ import annotations

NBSP = " "
_MARKS = {"en": (",", "."), "it": (".", ","), "uk": (NBSP, ",")}   # (thousands, decimal)


def _language(language: str | None) -> str:
    if language:
        return language
    try:
        from .i18n import current_language

        return current_language()
    except Exception:  # noqa: BLE001 - used outside the app
        return "en"


def marks(language: str | None = None) -> tuple[str, str]:
    """(thousands mark, decimal mark) of the language (English for an unknown one)."""
    return _MARKS.get(_language(language), _MARKS["en"])


def format_number(value: float, decimals: int = 0, language: str | None = None) -> str:
    """`value` with thousands marks and `decimals` decimals in the language's style; a
    real minus sign is not used here (the callers that want one add it)."""
    thousands, decimal = marks(language)
    text = f"{float(value):,.{max(0, int(decimals))}f}"
    return text.replace(",", "\x00").replace(".", decimal).replace("\x00", thousands)


def format_int(value: float, language: str | None = None) -> str:
    """A whole number with thousands marks: "1.234" (it), "1,234" (en), "1 234" (uk)."""
    return format_number(round(float(value)), 0, language)


def decimal_text(text: str, language: str | None = None) -> str:
    """A plain "2.5" written with the language's decimal mark ("2,5" in Italian)."""
    return text.replace(".", marks(language)[1])
