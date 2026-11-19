""""Open company-information sites": a web search for the company on the public
company-information sites, opened in the user's browser.

The app never reads those sites (their terms forbid automated reading and most figures are
behind a login): it only builds the search link. The user reads the page and, if he wants,
copies its text into "Paste figures…" (financials/paste.py). The query uses the VAT number
(partita IVA) or the tax code when known, which identifies the company exactly, else the
quoted name.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from .base import FinancialsIdentity

# Public sites that show revenue ranges, employees and balance-sheet summaries of Italian
# companies (searched, never read by the app).
INFO_SITES = ("reportaziende.it", "ufficiocamerale.it", "informazione-aziende.it", "companyreports.it",
              "registroaziende.it")
SEARCH_URL = "https://duckduckgo.com/?q="


def info_query(identity: FinancialsIdentity) -> str:
    """The search words: VAT digits / tax code / quoted name, plus the figures sought."""
    key = identity.vat_digits or identity.tax_code or (f'"{identity.name}"' if identity.name else "")
    if not key:
        return ""
    sites = " OR ".join(f"site:{site}" for site in INFO_SITES)
    return f"{key} fatturato dipendenti ({sites})"


def info_search_url(identity: FinancialsIdentity) -> str:
    """The browser link, or "" when nothing identifies the company."""
    query = info_query(identity)
    return SEARCH_URL + quote_plus(query) if query else ""
