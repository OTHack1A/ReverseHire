"""Job search: postings from sources that permit automated reading, ranked against the CV.

Public API (all functions never raise; errors are logged on "reversehire.jobsearch" and
returned in the results):

    parse_query(text, place_lookup=None, esco=None) -> SearchQuery
    build_sources(keys=None, cards=None, local_postings=None, max_pages=20) -> list[JobSource]
    search_jobs(query, sources, gateway=..., cv_profile=..., scorer=..., enabled=..., cache=...,
                progress=..., cancel=..., source_timeout_s=..., place_lookup=..., geocode=...) -> SearchResult
        (embedder=..., vector_store=...: ranked by meaning too, jobsearch/semantic.py)
    pipeline.rerank_postings(query, postings, cv_profile=, scorer=, embedder=) -> (postings, stats)
        (the same postings ranked again for a changed query: no source asked)
    query.also_terms() / query.remove_term(term): "Searched also for" (understanding.py: punctuation,
        typos, synonyms, IT <-> EN)
    spontaneous.rank_companies(query, cards=, register=, register_by_sector=, map_companies=,
        embedder=) -> (CompanyCandidate list, mode); spontaneous.direction_for_query(query, prefs)
    parse_pasted(text, html=None, hint_url=None) -> PastedDraft
    company_seed_from_posting(posting) -> dict (kwargs of companies.add_manual + "extra")
    possible_employers(posting, company_index, cards, agency_clients) -> list[EmployerHypothesis]
    estimate_employers(...) -> EmployerEstimate (same, plus the "not in our list" share)
    Posting, SearchQuery, SearchResult, SourceReport, JobSearchCache, texts.text(key)
"""

from __future__ import annotations

from .agencies import AGENCY_SPECS, AgencySitemapSource, AgencySpec, agency_sources
from .ats_source import AtsCompaniesSource, CompanyTarget, targets_from_cards
from .base import JobSource, SourceContext
from .cache import JobSearchCache, default_cache
from .dedup import deduplicate
from .employers import (EmployerEstimate, EmployerHypothesis, company_seed_from_posting, estimate_employers,
                        possible_employers)
from .keyed import AdzunaSource, CareerjetSource, JoobleSource
from .local import LocalPostingsSource
from .models import Posting, SearchResult, SourceReport
from .paste import DraftField, PastedDraft, parse_pasted
from .pipeline import enabled_ids, search_jobs
from .query import SearchQuery, parse_query
from .scoring import ScoreResult, skills_overlap_scorer

# Settings keys of the API keys (see the integration notes in the report).
KEY_NAMES = ("adzuna_app_id", "adzuna_app_key", "jooble_key", "careerjet_key")


def build_sources(keys: dict | None = None, *, cards=None, local_postings=None, max_pages: int = 20,
                  careerjet_user_ip: str = "", careerjet_user_agent: str = "") -> list[JobSource]:
    """All sources in display order. Agencies whose terms forbid automated reading are
    included (so Settings can show them greyed out with the reason) but are never run.

    keys: {"adzuna_app_id", "adzuna_app_key", "jooble_key", "careerjet_key"} (user's own);
    cards: CompanyCard list ("My companies" / favourites) for the ATS / careers source;
    local_postings: iterable of postings already in the store (see local.py)."""
    keys = keys or {}
    sources: list[JobSource] = []
    if local_postings is not None:
        sources.append(LocalPostingsSource(local_postings))
    if cards is not None:
        sources.append(AtsCompaniesSource(targets_from_cards(cards)))
    sources += agency_sources(max_pages=max_pages)
    sources.append(AdzunaSource(keys.get("adzuna_app_id", ""), keys.get("adzuna_app_key", "")))
    sources.append(JoobleSource(keys.get("jooble_key", "")))
    sources.append(CareerjetSource(keys.get("careerjet_key", ""), user_ip=careerjet_user_ip,
                                   user_agent=careerjet_user_agent))
    return sources


__all__ = [
    "AGENCY_SPECS", "AdzunaSource", "AgencySitemapSource", "AgencySpec", "AtsCompaniesSource", "CareerjetSource",
    "CompanyTarget", "DraftField", "EmployerEstimate", "EmployerHypothesis", "JobSearchCache", "JobSource",
    "JoobleSource", "KEY_NAMES", "LocalPostingsSource", "PastedDraft", "Posting", "ScoreResult", "SearchQuery",
    "SearchResult", "SourceContext", "SourceReport", "agency_sources", "build_sources",
    "company_seed_from_posting", "deduplicate", "default_cache", "enabled_ids", "estimate_employers",
    "parse_pasted", "parse_query", "possible_employers", "search_jobs", "skills_overlap_scorer",
    "targets_from_cards",
]
