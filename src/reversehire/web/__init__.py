"""Reading of company websites: page fetching, careers page, job postings.

Entry points: `Fetcher` (fetch.py) and `analyze_site` (analysis.py).
"""

from .analysis import SiteOutcome, analyze_site
from .fetch import Page, Fetcher

__all__ = ["SiteOutcome", "Page", "Fetcher", "analyze_site"]
