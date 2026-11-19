"""Orchestration of a search: company discovery, site reading, scoring.

No dependency on tkinter: progress is reported as `Event` objects passed to the
`on_progress` function (the GUI puts them in a queue). Every dependency that
uses the network or models can be injected, so tests never touch them.

Company discovery (discovery/): several independent sources behind one interface
(local OpenStreetMap extract, Overpass as fallback, Wikidata, optional Overture,
imported register lists, the user's favourites), merged and deduplicated, every
failure non-fatal, one INFO event "pipeline.source_status" per source. Only when
an `overpass` client is injected (and no `sources`) the old Overpass-only path runs.

Processing order (B4): manually entered URLs first (explicitly chosen by the
user), then OSM companies by decreasing `preliminary_relevance` (multi-source:
`rank_score`, which adds the register-ATECO match and a website bonus) and, on a
tie, by increasing distance. The limit `settings.max_companies` (0 = no limit) is
applied after sorting.

Every result is saved to the Store immediately: a cancelled (or interrupted)
search keeps its partial results and can be resumed by passing `search_id`;
companies already saved are skipped.

Optional job boards (`settings.job_boards_enabled`, off by default; see
`web.jobboards`): after the OSM discovery, employers found on Indeed/LinkedIn
for the top CV roles are merged into the companies already found (same site,
or very similar name) or added with origin "jobboard". Their postings are
attached to the company, so a company without a site is assessed on them
without any crawl. They are processed right after the manual URLs.

After each saved result, the company card ("My companies") is refreshed when
the company already has one.

Site analysis runs on `settings.parallel_companies` worker threads
(clamped to 1..16; 1 = one company at a time). Scoring, saving and events stay
on the calling thread, so results arrive as analyses finish, roughly in the
order above.

The embedding model is loaded right after the discovery. If it cannot be
loaded, the search never stops: it goes on with the built-in lexical embedder
(`scoring.embedding.LexicalEmbedder`), every confidence is lowered by
REDUCED_CONFIDENCE_FACTOR, an INFO event "pipeline.reduced_mode" gives the cause
and the details, and the search parameters record "reduced". A new search tries
the model again. With no company to evaluate an INFO event says why
("pipeline.no_companies_*"); a slow Overpass is reported while waiting.
"""

from __future__ import annotations

import inspect
import logging
import math
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .store import Store
from .config import Settings, data_folder, clamp_parallel
from .geo.geocoding import Place
from .geo.overpass import OverpassClient, OverpassError
from .geo.sectors import SECTORS, preliminary_relevance, sectors_for_profile
from .i18n import t
from .models import item_variants, Company, CVProfile, Assessment

log = logging.getLogger(__name__)

STORE_FILE_NAME = "store.sqlite"
PARAMS_NAMESPACE = "search_params"  # cache: manual urls and sectors of each search

# --- Time estimate ----------------------------------------------------------------
# Live (central Bologna) only about 18% of OSM companies have a website
# listed: only those are read. Each site costs about
# max_pages_per_site × request_delay_s (the delay applies between two requests
# to the same domain) plus a fixed cost for robots.txt and downloads; each
# company then costs a little computation for the scores.
SITE_SHARE = 0.18
FIXED_SECONDS_PER_SITE = 3.0
ASSESSMENT_SECONDS = 0.3
OVERPASS_QUERY_SECONDS = 30.0  # live: 24-33 s for a query in central Bologna
# Job boards (when enabled): one query per site and role, spaced out by
# jobboards.DELAY_BETWEEN_CALLS_S; live LinkedIn answered in about 1 s.
JOB_BOARD_SECONDS_PER_QUERY = 6.0
JOB_BOARD_QUERIES = 6  # jobboards.MAX_ROLES roles x 2 sites
LONG_SEARCH_THRESHOLD_S = 15 * 60

# --- Parallel site analysis -------------------------------------------------------
# Sites are analysed by `settings.parallel_companies` worker threads (1 = the
# old sequential loop). At most WINDOW_PER_WORKER × workers companies are
# queued or in flight at once, so a cancel never leaves hundreds of queued jobs
# and results keep following the priority order. The consumer polls the cancel
# flag every CONSUMER_WAIT_S while waiting for the next finished analysis.
WINDOW_PER_WORKER = 2
CONSUMER_WAIT_S = 0.2

# --- Company discovery ---------------------------------------------------------------
# The Overpass query runs on a helper thread so that a cancel is honoured at once
# (one HTTP request can last up to ~110 s and the client retries 3 times). While
# it waits, an INFO event "pipeline.overpass_slow" tells the user it is still
# waiting, first after OVERPASS_SLOW_NOTICE_S, then every OVERPASS_SLOW_REPEAT_S.
OVERPASS_POLL_S = 0.2
OVERPASS_SLOW_NOTICE_S = 20.0
OVERPASS_SLOW_REPEAT_S = 30.0

# --- Reduced accuracy -------------------------------------------------------------------
# When no embedding model can be loaded the search goes on with the built-in
# lexical embedder (scoring.embedding.LexicalEmbedder): every confidence is
# multiplied by this factor and the search parameters record it ("reduced").
REDUCED_CONFIDENCE_FACTOR = 0.6

# --- Multi-source discovery (discovery/) ------------------------------------------------
# Used unless an `overpass` client is injected (the old single-source path, kept for the
# callers and tests that inject one). Ranking adds to the preliminary relevance: a
# register ATECO code inside the chosen categories (imported lists only: the ATECO of a
# company is never guessed) and, slightly, having a website.
ATECO_MATCH_SCORE = 0.9
WEBSITE_BONUS = 0.1
DISCOVERY_LOCAL_SECONDS = 2.0        # local map data, imports, favourites
WIKIDATA_FIRST_SECONDS = 60.0        # live: 57 s for the Emilia-Romagna query, then cached 30 days
DISCOVERY_POLL_S = 0.2
DISCOVERY_SLOW_NOTICE_S = 20.0
DISCOVERY_SLOW_REPEAT_S = 30.0

# Event types
START = "start"
INFO = "info"
DISCOVERY = "discovery"
PROGRESS = "progress"
RESULT = "result"
COMPANY_ERROR = "company_error"
ERROR = "error"
END = "end"


@dataclass
class Event:
    """News about the progress of the search.

    `message` is already translated into the current language; `key` and
    `parameters` let the interface translate it again. For END, `status` is
    "completed" or "cancelled".
    """

    kind: str
    message: str = ""
    current: int = 0
    total: int = 0
    company: Company | None = None
    assessment: Assessment | None = None
    search_id: int | None = None
    key: str = ""
    parameters: dict = field(default_factory=dict)
    status: str = ""


@dataclass
class Estimate:
    companies: int            # companies found by Overpass (after the limit) + manual URLs
    with_site: int           # how many, estimated, have a site to read
    seconds: float          # expected time
    is_long: bool             # above LONG_SEARCH_THRESHOLD_S


def estimated_to_analyse(candidates: list, settings) -> int:
    """How many of the discovered candidates will be analysed: those with a website
    when companies without one are excluded, then the company limit. An estimate:
    the job boards may still add companies (the final number comes with the
    DISCOVERY event "pipeline.found")."""
    count = len(candidates)
    if not getattr(settings, "include_without_site", True):
        count = sum(1 for c in candidates if getattr(c, "site", None))
    maximum = getattr(settings, "max_companies", 0) or 0
    if maximum > 0:
        count = min(count, maximum)
    return count


class SearchError(Exception):
    """Error that prevents the search; `key` is an i18n message key."""

    def __init__(self, key: str, detail: str = ""):
        super().__init__(detail or key)
        self.key = key
        self.detail = detail


def search_parameters(store: Store, search_id: int) -> dict:
    """Saved parameters of a search: manual_urls, sectors and, when the search ran
    without the embedding model, "reduced": True with "model_cause"."""
    try:
        value = store.cache_read(PARAMS_NAMESPACE, str(search_id))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def estimate_from_count(settings: Settings, found_items: int, n_manual_urls: int = 0,
                        discovery_seconds: float | None = None) -> Estimate:
    """The time estimate for `found_items` OSM companies (before the limit) plus the
    manual URLs, with the limit `settings.max_companies` applied (no network).
    `discovery_seconds` replaces the Overpass query time (multi-source discovery)."""
    maximum = settings.max_companies
    limit = max(0, maximum - n_manual_urls) if maximum and maximum > 0 else None
    if settings.include_without_site:
        if limit is not None:
            found_items = min(found_items, limit)
        with_site = math.ceil(found_items * SITE_SHARE) + n_manual_urls
    else:
        # companies without a site are dropped first: the limit counts sites only
        sites = math.ceil(found_items * SITE_SHARE)
        with_site = (min(sites, limit) if limit is not None else sites) + n_manual_urls
        if limit is not None:
            found_items = min(found_items, limit if not sites else math.ceil(min(sites, limit) / SITE_SHARE))
    to_assess = found_items + n_manual_urls if settings.include_without_site else with_site
    site_seconds = (
        max(1, settings.max_pages_per_site) * max(0.0, settings.request_delay_s)
        + FIXED_SECONDS_PER_SITE
    )
    # Sites are read in parallel (different domains do not wait for each other):
    # the total site time is divided by the busy workers, but never drops below
    # one whole site, the longest single job.
    workers = min(clamp_parallel(settings.parallel_companies), with_site)
    sites_seconds = max(with_site * site_seconds / workers, site_seconds) if with_site > 0 else 0.0
    discovery = OVERPASS_QUERY_SECONDS if discovery_seconds is None else discovery_seconds
    seconds = discovery + sites_seconds + to_assess * ASSESSMENT_SECONDS
    if getattr(settings, "job_boards_enabled", False):
        seconds += JOB_BOARD_SECONDS_PER_QUERY * JOB_BOARD_QUERIES
    return Estimate(
        companies=found_items + n_manual_urls,
        with_site=with_site,
        seconds=round(seconds, 1),
        is_long=seconds > LONG_SEARCH_THRESHOLD_S,
    )


def store_path() -> Path:
    return data_folder() / STORE_FILE_NAME


def open_store() -> Store:
    return Store(store_path())


# --- URL manuali --------------------------------------------------------------------


def normalize_manual_url(row: str) -> str | None:
    """Clean http/https URL (https scheme added if missing), or None if invalid."""
    text = (row or "").strip()
    if not text or text.startswith("#"):
        return None
    if "://" not in text:
        text = "https://" + text.lstrip("/")
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    if " " in parts.netloc or "." not in parts.hostname:
        return None
    return text


def _host(url: str | None) -> str:
    if not url:
        return ""
    try:
        host = (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def name_from_url(url: str) -> str:
    return _host(url) or url


def manual_companies(rows: list[str]) -> list[Company]:
    """Companies with `origin="manual"` from the URLs (duplicate domains removed)."""
    companies: list[Company] = []
    seen: set[str] = set()
    for row in rows:
        url = normalize_manual_url(row)
        if url is None:
            continue
        host = _host(url)
        if host in seen:
            continue
        seen.add(host)
        companies.append(Company(name=name_from_url(url), lat=None, lon=None, site=url, origin="manual"))
    return companies


def company_key(company: Company) -> str:
    """Stable identity of a company, to skip the ones already saved on resume."""
    if company.site:
        parts = urlsplit(company.site if "://" in company.site else "https://" + company.site)
        path = parts.path.rstrip("/")
        return f"site:{_host(company.site)}{path.lower()}"
    lat = f"{company.lat:.5f}" if company.lat is not None else "-"
    lon = f"{company.lon:.5f}" if company.lon is not None else "-"
    return f"osm:{company.name.strip().lower()}|{lat}|{lon}"


MAX_ANALYSIS_ROLES = 6


def profile_roles(profile: CVProfile) -> list[str]:
    """Role names of the CV (profile roles, then recent job titles), without
    duplicates: they rank which ATS postings get their full text first."""
    names = [getattr(r, "value", "") for r in profile.roles or []]
    names += [getattr(e, "title", "") for e in getattr(profile, "experiences", None) or []]
    result: list[str] = []
    for name in names:
        name = (name or "").strip()
        if name and name.lower() not in {r.lower() for r in result}:
            result.append(name)
    return result[:MAX_ANALYSIS_ROLES]


def rank_score(company: Company, sectors: list[str] | None, ateco_codes: list[str] | None = None,
               website_bonus: float = 0.0) -> float:
    """Profile relevance of a discovered company: preliminary relevance (OSM tags and
    name), raised to ATECO_MATCH_SCORE when its REGISTER ATECO code (imported lists)
    falls in the chosen categories, plus `website_bonus` when it has a website."""
    score = preliminary_relevance(company, sectors or [])
    if ateco_codes:
        from .discovery.ateco import matches
        from .discovery.merge import ateco_of

        code = ateco_of(company)
        if code and matches(code, ateco_codes):
            score = max(score, ATECO_MATCH_SCORE)
    if website_bonus and company.site:
        score += website_bonus
    return score


def sort_companies(companies: list[Company], sectors: list[str] | None,
                   ateco_codes: list[str] | None = None, website_bonus: float = 0.0,
                   *, context=None) -> list[Company]:
    """Manual first, then job-board employers, then decreasing relevance, then distance.

    With a `discovery.ranking.RankContext` the relevance is the explainable pre-crawl
    ranking (name keywords, industrial tags, Wikidata industry, register ATECO,
    demotions) and every company gets `fields["why_selected"]`; without it,
    `rank_score` (with the defaults: the preliminary relevance)."""
    listing = sectors or []
    if context is not None:
        from .discovery.ranking import rank_all

        scores = {k: r.score for k, r in rank_all(companies, context).items()}

        def relevance(a: Company) -> float:
            return scores.get(id(a), 0.0)
    else:
        def relevance(a: Company) -> float:
            return rank_score(a, listing, ateco_codes, website_bonus)

    def key(a: Company):
        manual = 0 if a.origin == "manual" else 1 if a.origin == "jobboard" else 2
        distance = a.distance_km if a.distance_km is not None else math.inf
        return (manual, -relevance(a), distance, a.name.lower())

    return sorted(companies, key=key)


# --- Executor ----------------------------------------------------------------------


class SearchExecutor:
    """Runs (or resumes) a search and saves its results as it goes.

    Extra keyword-only parameters, meant for tests: `analyze` (signature of
    `web.analysis.analyze_site`), `assessor` (signature of
    `scoring.assessment.assess`), `fetcher_factory`, `embedder_factory`.
    """

    def __init__(
        self,
        settings: Settings,
        store: Store,
        embedder=None,
        fetcher=None,
        overpass=None,
        *,
        analyze: Callable | None = None,
        assessor: Callable | None = None,
        fetcher_factory: Callable | None = None,
        embedder_factory: Callable | None = None,
        job_board_search: Callable | None = None,
        skills_lookup: Callable | None = None,
        sources: list | None = None,
        gateway=None,
    ):
        self.settings = settings
        self.store = store
        self._embedder = embedder
        self._fetcher = fetcher
        self._overpass = overpass
        # Multi-source discovery unless only an Overpass client was injected (old path).
        self._sources = sources
        self._gateway = gateway
        self._legacy = overpass is not None and sources is None
        self.discovery_statuses: list = []   # SourceStatus of the last discovery
        self._analyze = analyze
        self._assessor = assessor
        self._fetcher_factory = fetcher_factory
        self._embedder_factory = embedder_factory
        self._job_board_search = job_board_search
        self._model_announced = False
        self._assessor_takes_settings: tuple[bool, bool] | None = None
        self._skills_lookup = skills_lookup
        self._roles: list[str] = []  # candidate's role names, set by run()
        self._reduced: dict | None = None  # set when the lexical fallback is in use

    # --- lazy dependencies

    def _overpass_client(self):
        if self._overpass is None:
            self._overpass = OverpassClient(self.settings, self.store)
        return self._overpass

    def _create_fetcher(self):
        if self._fetcher_factory is not None:
            return self._fetcher_factory(self.settings, self.store)
        from .web.fetch import Fetcher

        return Fetcher(self.settings, self.store)

    def _analysis_function(self) -> Callable:
        if self._analyze is None:
            from .web.analysis import analyze_site

            self._analyze = analyze_site
        return self._analyze

    def _assessment_function(self) -> Callable:
        if self._assessor is None:
            from .scoring.assessment import assess

            self._assessor = assess
        return self._assessor

    def _assess(self, assessor: Callable, profile, company, outcome, embedder,
                role_skills: list[str] | None = None):
        """Calls the assessor, passing `settings=` and `role_skills=` when its
        signature accepts them (injected test assessors may not)."""
        takes_settings, takes_skills = self._assessor_parameters(assessor)
        extra = {}
        if takes_settings:
            extra["settings"] = self.settings
        if takes_skills:
            extra["role_skills"] = role_skills or None
        return assessor(profile, company, outcome, embedder, self.settings.language, **extra)

    def _assessor_parameters(self, assessor: Callable) -> tuple[bool, bool]:
        """(accepts settings=, accepts role_skills=), read once from the signature."""
        if self._assessor_takes_settings is None:
            try:
                parameters = inspect.signature(assessor).parameters
                any_keyword = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())
                self._assessor_takes_settings = (
                    "settings" in parameters or any_keyword,
                    "role_skills" in parameters or any_keyword,
                )
            except (TypeError, ValueError):
                self._assessor_takes_settings = (False, False)
        return self._assessor_takes_settings

    @staticmethod
    def _takes(function: Callable, name: str) -> bool:
        try:
            return name in inspect.signature(function).parameters
        except (TypeError, ValueError):
            return False

    def _overpass_find(self, place: Place, radius_km: float, sectors, cancel):
        """OSM companies, including (sector widening) those with only a generic company
        tag, which `preliminary_relevance` ranks below the sector matches; injected
        clients without `include_generic` are called as before."""
        client = self._overpass_client()
        if sectors and self._takes(client.find, "include_generic"):
            return client.find(place.lat, place.lon, radius_km, sectors, max_companies=0,
                               cancel=cancel, include_generic=True)
        return client.find(place.lat, place.lon, radius_km, sectors, max_companies=0, cancel=cancel)

    def _role_skills(self, profile: CVProfile) -> list[str] | None:
        """ESCO essential skills of the profile's occupation, once per search (cached
        in the Store by `cv.esco`); None when unknown or unreachable."""
        if self._skills_lookup is not None:
            lookup = self._skills_lookup
        else:
            def lookup(p):
                from .cv.esco import essential_skills, profile_occupation

                occupation = profile_occupation(p)
                if not occupation:
                    return []
                return essential_skills(occupation, p.language, self.store, self.settings)
        try:
            skills = lookup(profile)
        except Exception as error:  # network, cache: scoring works without them
            log.info("ESCO skills not available (%s)", type(error).__name__)
            return None
        return list(skills) if skills else None

    def _job_boards(self, profile: CVProfile, place: Place, radius_km: float,
                    known: list[Company], notify: Callable[[Event], None], search_id: int,
                    cancel) -> tuple[list[Company], dict[str, list]]:
        """(new job-board companies, {company_key: board postings}); never raises."""
        from .web import jobboards

        search = self._job_board_search
        if search is None:
            ok, reason = jobboards.availability()
            if not ok:
                log.info("Job boards enabled but not available: %s", reason)
                notify(_event(INFO, "pipeline.jobboards_unavailable", search_id=search_id, reason=reason))
                return [], {}
            search = jobboards.search_job_boards
        # The CV wording first: local job boards are searched in the local language.
        roles = jobboards.search_terms(
            [v for item in profile.roles for v in reversed(item_variants(item))])
        if not roles:
            notify(_event(INFO, "pipeline.jobboards_no_roles", search_id=search_id))
            return [], {}
        if jobboards.town_of(place) is None:
            notify(_event(INFO, "pipeline.jobboards_no_town", search_id=search_id))
            return [], {}
        notify(_event(INFO, "pipeline.jobboards_searching", search_id=search_id, roles=", ".join(roles)))
        failures: list[str] = []
        try:
            found = search(roles, place, radius_km, self.settings, cancel=cancel,
                           store=self.store, failures=failures)
        except Exception as error:  # blocked, changed, network down: the search goes on
            log.warning("Job boards failed (%s)", type(error).__name__)
            notify(_event(INFO, "pipeline.jobboards_failed", search_id=search_id,
                          detail=", ".join(failures) or type(error).__name__))
            return [], {}
        new, attached = jobboards.merge_into(found, known, radius_km)
        extra: dict[str, list] = {}
        for index, postings in attached.items():
            extra.setdefault(company_key(known[index]), []).extend(postings)
        for company, postings in new:
            extra.setdefault(company_key(company), []).extend(postings)
        notify(_event(INFO, "pipeline.jobboards_found", search_id=search_id, companies=len(new),
                      postings=sum(len(p) for _, p in found), merged=len(attached)))
        if failures:
            notify(_event(INFO, "pipeline.jobboards_failed", search_id=search_id,
                          detail=", ".join(failures)))
        return [company for company, _ in new], extra

    def _refresh_card(self, company: Company, assessment: Assessment, search_id: int,
                      card_keys: set[str]) -> None:
        """Refresh the saved company card, if the company has one (never stops the search)."""
        if not card_keys:
            return
        try:
            from . import companies as cards

            if cards.card_key(company) in card_keys:
                cards.save_from_result(self.store, company, assessment, search_id)
        except Exception:
            log.warning("Company card not refreshed", exc_info=True)

    def _ready_embedder(self, notify: Callable[[Event], None], search_id: int):
        if self._embedder is None:
            if self._embedder_factory is not None:
                self._embedder = self._embedder_factory(self.store)
            else:
                from .scoring.embedding import create_embedder

                self._embedder = create_embedder(self.store)
        if not self._model_announced and not getattr(self._embedder, "loaded", True):
            # The first load (possibly with a download) can take minutes: warn
            # and do it right away. If it fails the search goes on with the
            # built-in lexical embedder ("reduced accuracy"), never stops.
            notify(_event(INFO, "pipeline.loading_model", search_id=search_id))
            start = time.monotonic()
            try:
                self._embedder.embed(["startup"], "query")
            except Exception as error:
                self._switch_to_reduced(error, notify, search_id)
            else:
                log.info("Embedding model ready in %.1f s", time.monotonic() - start)
        if not self._model_announced and getattr(self._embedder, "reduced", False) and not self._reduced:
            # An embedder that is already in its fallback (ResilientEmbedder).
            self._switch_to_reduced(getattr(self._embedder, "last_error", None)
                                    or RuntimeError("embedding model not available"), notify, search_id)
        self._model_announced = True
        return self._embedder

    def _switch_to_reduced(self, error: BaseException, notify: Callable[[Event], None],
                           search_id: int) -> None:
        """Use the lexical embedder for the rest of the search and say why."""
        from .scoring.embedding import LexicalEmbedder, failure_cause, failure_details

        cause = failure_cause(error)
        details = failure_details(error)
        log.error("Embedding model could not be loaded (cause: %s): the search goes on with "
                  "reduced accuracy\n%s", cause, details, exc_info=error)
        self._embedder = LexicalEmbedder()
        self._reduced = {"cause": cause, "details": details}
        try:
            parameters = search_parameters(self.store, search_id)
            parameters.update({"reduced": True, "model_cause": cause})
            self.store.cache_write(PARAMS_NAMESPACE, str(search_id), parameters)
        except Exception:
            log.warning("Reduced-accuracy flag not saved", exc_info=True)
        notify(_event(INFO, "pipeline.reduced_mode", search_id=search_id, cause=cause,
                      reason=t(f"model_cause.{cause}.short"), details=details))

    @property
    def reduced(self) -> dict | None:
        """{"cause", "details"} when this executor runs without the embedding model."""
        return self._reduced

    def _find_companies(self, place: Place, radius_km: float, sectors, cancel,
                        notify: Callable[[Event], None], search_id: int) -> list[Company]:
        """`_overpass_find` on a helper thread: a cancel returns at once (the request
        finishes in the background) and a slow server is reported while waiting."""
        box: dict = {}

        def job():
            try:
                box["value"] = self._overpass_find(place, radius_km, sectors, cancel)
            except BaseException as error:  # handed to the caller
                box["error"] = error

        worker = threading.Thread(target=job, daemon=True, name="overpass")
        start = time.monotonic()
        worker.start()
        next_notice = OVERPASS_SLOW_NOTICE_S
        while worker.is_alive():
            worker.join(OVERPASS_POLL_S)
            if not worker.is_alive():
                break
            if cancel.is_set():
                log.info("Company search cancelled while waiting for Overpass")
                return []
            elapsed = time.monotonic() - start
            if elapsed >= next_notice:
                notify(_event(INFO, "pipeline.overpass_slow", search_id=search_id,
                              seconds=int(elapsed)))
                next_notice += OVERPASS_SLOW_REPEAT_S
        if "error" in box:
            raise box["error"]
        log.info("Overpass answered in %.1f s", time.monotonic() - start)
        return box.get("value") or []

    # --- multi-source discovery

    def _discovery_choices(self, profile: CVProfile | None, area: str | None, province: str | None,
                           ateco: list[str] | None) -> tuple[str, str, list[str]]:
        """Area, province and ATECO categories: the explicit values, else the search
        panel's saved choices, else the categories proposed for the profile."""
        from .discovery import prefs as discovery_prefs

        saved = discovery_prefs.read(self.store)
        area = area or saved.area
        province = (province if province is not None else saved.province) or ""
        if area == "province" and not province:
            area = "radius"
        if ateco is None:
            if saved.ateco_custom:
                ateco = list(saved.ateco)
            elif profile is not None:
                from .discovery.ateco import ateco_for_profile

                try:
                    ateco = [code.code for code in ateco_for_profile(profile)]
                except Exception:
                    log.warning("ATECO categories not computed", exc_info=True)
                    ateco = []
            else:
                ateco = []
        return area, province.upper(), list(ateco)

    def _discovery_setup(self, place: Place, radius_km: float, sectors, area: str, province: str,
                         ateco: list[str]):
        from .discovery import DiscoveryQuery, PoliteGateway, build_sources, region_for
        from .discovery.prefs import effective_region

        region = region_for(effective_region(self.settings, self.store))
        sources = self._sources if self._sources is not None else build_sources(
            self.settings, self.store, region=region)
        gateway = self._gateway or PoliteGateway(self.settings, self.store)
        query = DiscoveryQuery(place.lat, place.lon, radius_km, list(sectors) if sectors else None,
                               area=area, province=province, region=region, ateco=list(ateco))
        return sources, gateway, query

    def _discover_all(self, place: Place, radius_km: float, sectors, cancel, notify, search_id: int,
                      area: str, province: str, ateco: list[str]) -> list[Company]:
        """Every enabled source, on a helper thread (a cancel returns at once); one INFO
        event per source status. Raises _DiscoveryFailed when every source failed."""
        from .discovery import discover
        from .discovery.texts import status_line

        sources, gateway, query = self._discovery_setup(place, radius_km, sectors, area, province, ateco)
        box: dict = {}
        last_percent = {"value": -10}
        first_ok = {"done": False}

        def on_status(status) -> None:
            first = status.state == "ok" and not first_ok["done"]
            if status.state == "ok":
                first_ok["done"] = True
            notify(_event(INFO, "pipeline.source_status", search_id=search_id,
                          line=status_line(status, first), source=status.name, state=status.state,
                          count=status.count, new=status.new, seconds=status.seconds,
                          reason=status.reason, detail=status.detail))

        def on_progress(source: str, phase: str, done: int, total: int) -> None:
            percent = int(100 * done / total) if total else 0
            if percent >= last_percent["value"] + 5 or percent >= 100:
                last_percent["value"] = percent
                notify(_event(INFO, "pipeline.map_index", search_id=search_id, percent=percent,
                              phase=phase, source=source))

        def job():
            try:
                box["value"] = discover(query, sources, gateway, cancel, on_status, on_progress)
            except BaseException as error:  # handed to the caller
                box["error"] = error

        worker = threading.Thread(target=job, daemon=True, name="discovery")
        start = time.monotonic()
        worker.start()
        next_notice = DISCOVERY_SLOW_NOTICE_S
        while worker.is_alive():
            worker.join(DISCOVERY_POLL_S)
            if not worker.is_alive():
                break
            if cancel.is_set():
                log.info("Company discovery cancelled")
                return []
            elapsed = time.monotonic() - start
            if elapsed >= next_notice:
                notify(_event(INFO, "pipeline.discovery_waiting", search_id=search_id, seconds=int(elapsed)))
                next_notice += DISCOVERY_SLOW_REPEAT_S
        if self._gateway is None:
            stats = gateway.stats()
            gateway.close()
        else:
            stats = gateway.stats()
        if "error" in box:
            raise box["error"]
        result = box["value"]
        self.discovery_statuses = list(result.statuses)
        log.info("Discovery in %.1f s: %d companies; %s; requests %s", time.monotonic() - start,
                 len(result.companies), "; ".join(status_line(s) for s in result.statuses), stats)
        try:
            parameters = search_parameters(self.store, search_id)
            parameters["discovery"] = [s.to_dict() for s in result.statuses]
            self.store.cache_write(PARAMS_NAMESPACE, str(search_id), parameters)
        except Exception:
            log.warning("Discovery statuses not saved", exc_info=True)
        if result.all_failed:
            raise _DiscoveryFailed("\n".join(status_line(s) for s in result.statuses))
        return result.companies

    def _estimate_multi(self, place: Place, radius_km: float, sectors, n_manual_urls: int) -> Estimate:
        from .discovery import OSM_EXTRACT, OVERPASS, REGIONAL_REGISTER, WIKIDATA
        from .discovery.network_sources import WIKIDATA_CACHE_TTL_S, WIKIDATA_ENDPOINT

        area, province, ateco = self._discovery_choices(None, None, None, [])
        sources, gateway, query = self._discovery_setup(place, radius_km, sectors, area, province, ateco)
        names = [s.name for s in sources]
        found = None
        seconds = DISCOVERY_LOCAL_SECONDS
        cancel = threading.Event()
        try:
            for source in sources:
                if source.name == OSM_EXTRACT:
                    found = source.count(query, gateway, cancel)
            if found is None and OVERPASS in names:
                overpass = next(s for s in sources if s.name == OVERPASS)
                found = overpass.count(query, gateway, cancel)
                seconds += OVERPASS_QUERY_SECONDS
            register = next((s for s in sources if s.name == REGIONAL_REGISTER), None)
            if register is not None and area in ("province", "region"):
                # the regional register adds its companies of the province / region (in radius
                # mode its count is region-wide, so it is left out rather than overstated)
                extra = register.count(query, gateway, cancel)
                if extra:
                    found = (found or 0) + int(extra)
            if WIKIDATA in names:
                source = next(s for s in sources if s.name == WIKIDATA)
                region = getattr(source, "region", None)
                cached = False
                if region is not None and region.wikidata and self.store is not None:
                    from .discovery.network_sources import SPARQL, WIKIDATA_LIMIT

                    key = gateway.cache_key("GET", WIKIDATA_ENDPOINT, {"query": SPARQL.format(
                        region=region.wikidata, limit=WIKIDATA_LIMIT), "format": "json"}, None)
                    cached = self.store.cache_read("discovery_http", key, WIKIDATA_CACHE_TTL_S) is not None
                if not cached:
                    seconds += WIKIDATA_FIRST_SECONDS
        finally:
            if self._gateway is None:
                gateway.close()
        return estimate_from_count(self.settings, found or 0, n_manual_urls, discovery_seconds=seconds)

    # --- estimate

    def estimate(self, place: Place, radius_km: float, sectors: list[str] | None,
                 n_manual_urls: int = 0) -> Estimate:
        """Number of companies and expected time (see the constants at the top of the module).

        Single-source path: raises `SearchError("error.overpass")` if Overpass does not
        respond. Multi-source path: counts from the local map data (or an Overpass
        "out count" when there is none); an unknown count is estimated as 0.
        """
        if not self._legacy:
            return self._estimate_multi(place, radius_km, sectors, n_manual_urls)
        try:
            client = self._overpass_client()
            if sectors and self._takes(client.count, "include_generic"):
                found_items = client.count(place.lat, place.lon, radius_km, sectors, include_generic=True)
            else:
                found_items = client.count(place.lat, place.lon, radius_km, sectors or None)
        except OverpassError as error:
            raise SearchError("error.overpass", str(error)) from error
        return estimate_from_count(self.settings, found_items, n_manual_urls)

    # --- execution

    def run(
        self,
        profile: CVProfile,
        place: Place,
        radius_km: float,
        manual_urls: list[str],
        sectors: list[str] | None,
        on_progress: Callable[[Event], None],
        cancel: threading.Event,
        search_id: int | None = None,
        *,
        area: str | None = None,
        province: str | None = None,
        ateco: list[str] | None = None,
    ) -> int:
        """Runs the search and returns its id in the Store.

        `area` ("radius" | "province" | "region"), `province` (code) and `ateco`
        (categories of interest) default to the search panel's saved choices
        (discovery/prefs.py) and to the categories proposed for the profile.

        Blocking errors (no company source answered and no manual URL): status
        "cancelled" (hence resumable), an ERROR event and then `SearchError`.
        """
        notify = _guarded(on_progress)
        if cancel is None:
            cancel = threading.Event()

        if not profile.sectors:
            profile.sectors = sectors_for_profile(profile)
        if sectors is None:
            sectors = list(profile.sectors)
        sectors = [s for s in sectors if s in SECTORS] or None
        if not self._legacy:
            area, province, ateco = self._discovery_choices(profile, area, province, ateco)

        already_saved: set[str] = set()
        if search_id is None:
            search_id = self.store.create_search(
                profile.cv_path, place.name, place.lat, place.lon, radius_km, profile
            )
            parameters = {"manual_urls": list(manual_urls or []), "sectors": sectors or []}
            if not self._legacy:
                parameters.update({"area": area, "province": province, "ateco": list(ateco or [])})
            self.store.cache_write(PARAMS_NAMESPACE, str(search_id), parameters)
        else:
            if self.store.read_search(search_id) is None:
                raise SearchError("error.search_not_found", str(search_id))
            self.store.update_search_status(search_id, "running")
            already_saved = {company_key(a) for a, _ in self.store.results_of(search_id)}

        notify(_event(START, "pipeline.started", search_id=search_id))
        self._roles = profile_roles(profile)
        start = time.monotonic()
        owns_fetcher = False
        try:
            notify(_event(INFO, "pipeline.searching_companies", search_id=search_id))
            if not self._legacy:
                try:
                    found_items = self._discover_all(place, radius_km, sectors, cancel, notify, search_id,
                                                     area, province, ateco)
                except _DiscoveryFailed as failure:
                    log.warning("No discovery source answered:\n%s", failure)
                    n_manual = len(manual_companies(manual_urls or []))
                    if not n_manual:
                        self.store.update_search_status(search_id, "cancelled")
                        notify(_event(ERROR, "error.discovery_failed", search_id=search_id,
                                      details=str(failure)))
                        raise SearchError("error.discovery_failed", str(failure)) from failure
                    found_items = []
                    notify(_event(INFO, "pipeline.discovery_failed_manual", search_id=search_id, n=n_manual))
            else:
                found_items = None
            try:
                if found_items is None:
                    found_items = self._find_companies(place, radius_km, sectors, cancel, notify, search_id)
            except OverpassError as error:
                log.warning("Overpass unavailable: %s", error)
                if not manual_companies(manual_urls or []):
                    self.store.update_search_status(search_id, "cancelled")
                    notify(_event(ERROR, "error.overpass", search_id=search_id))
                    raise SearchError("error.overpass", str(error)) from error
                # The websites added by hand can still be read and scored.
                found_items = []
                notify(_event(INFO, "pipeline.overpass_unavailable_manual", search_id=search_id,
                              n=len(manual_companies(manual_urls))))

            manual_list = manual_companies(manual_urls or [])
            host_osm = {_host(a.site) for a in found_items if a.site}
            manual_list = [m for m in manual_list if _host(m.site) not in host_osm]
            known = found_items + manual_list
            # The estimated number of companies, as soon as the candidate list is known
            # (the progress bar shows it before the job boards and the ranking finish).
            notify(_event(INFO, "pipeline.estimated", search_id=search_id,
                          estimated=estimated_to_analyse(known, self.settings), candidates=len(known)))
            board_postings: dict[str, list] = {}
            if getattr(self.settings, "job_boards_enabled", False) and not cancel.is_set():
                board_companies, board_postings = self._job_boards(
                    profile, place, radius_km, known, notify, search_id, cancel)
                known = known + board_companies
            if self._legacy:
                companies = sort_companies(known, sectors)
            else:
                from .discovery.ranking import context_for

                try:  # revenue trend of the companies the user already has a card for
                    from . import companies as cards

                    cards.annotate_revenue_trends(self.store, known)
                except Exception:  # noqa: BLE001 - a bonus must never stop the search
                    log.warning("Revenue trends not read", exc_info=True)
                companies = sort_companies(known, sectors, ateco, WEBSITE_BONUS,
                                           context=context_for(profile, sectors, ateco))
            # Without the companies lacking a site first, so that the limit counts only
            # companies that will actually be read.
            if not self.settings.include_without_site:
                companies = [a for a in companies if a.site or board_postings.get(company_key(a))]
            maximum = self.settings.max_companies
            if maximum and maximum > 0:
                companies = companies[:maximum]
            try:
                from . import companies as cards

                card_keys = cards.saved_keys(self.store)
            except Exception:
                log.warning("Company cards not readable", exc_info=True)
                card_keys = set()
            todo = [a for a in companies if company_key(a) not in already_saved]
            total = len(companies)
            processed = total - len(todo)
            notify(_event(DISCOVERY, "pipeline.found", current=processed, total=total,
                           search_id=search_id, found_items=total, with_site=sum(1 for a in companies if a.site)))
            if total == 0 and not cancel.is_set():
                # Say why, instead of an empty table: nothing on the map, or
                # nothing with a website while companies without one are excluded.
                reason = "pipeline.no_companies_no_site" if known else "pipeline.no_companies_map"
                notify(_event(INFO, reason, search_id=search_id, radius=f"{radius_km:g}",
                              found=len(known), sectors=len(sectors or [])))
            if todo and not cancel.is_set():
                self._ready_embedder(notify, search_id)  # load (or fall back) before the sites

            if any(a.site for a in todo) and self._fetcher is None:
                self._fetcher = self._create_fetcher()
                owns_fetcher = True
            analyze = self._analysis_function()
            assessor = self._assessment_function()
            counts = {"processed": processed, "errors": 0}
            role_skills: dict[str, list[str] | None] = {}  # looked up at the first assessment

            started = {"n": processed}

            def announce(company: Company) -> None:
                started["n"] += 1
                key = "pipeline.analysing" if company.site else "pipeline.scoring_without_site"
                notify(_event(PROGRESS, key, current=counts["processed"],
                               total=total, company=company, search_id=search_id,
                               name=company.name, position=min(started["n"], total)))

            def finish(company: Company, outcome, analysis_error: BaseException | None) -> None:
                """Score, save and report one company. Always on the calling thread."""
                try:
                    if analysis_error is not None:
                        raise analysis_error
                    embedder = self._ready_embedder(notify, search_id)
                    outcome = _with_board_postings(outcome, board_postings.get(company_key(company)))
                    if "skills" not in role_skills:
                        wanted = self._assessor_parameters(assessor)[1]
                        role_skills["skills"] = self._role_skills(profile) if wanted else None
                    assessment = self._assess(assessor, profile, company, outcome, embedder,
                                              role_skills["skills"])
                    if self._reduced is not None and isinstance(
                            getattr(assessment, "confidence", None), (int, float)):
                        assessment.confidence = round(assessment.confidence * REDUCED_CONFIDENCE_FACTOR, 3)
                except SearchError:
                    raise
                except Exception as error:  # a single company does not stop the search
                    counts["errors"] += 1
                    counts["processed"] += 1
                    log.warning("Assessment failed for a company (%s): %s",
                                type(error).__name__, _host(company.site) or "no site",
                                exc_info=True)
                    notify(_event(COMPANY_ERROR, "pipeline.company_error",
                                   current=counts["processed"], total=total, company=company,
                                   search_id=search_id, name=company.name))
                    return
                self.store.save_result(search_id, company, assessment)
                self._refresh_card(company, assessment, search_id, card_keys)
                counts["processed"] += 1
                notify(_event(RESULT, "pipeline.assessed", current=counts["processed"],
                               total=total, company=company, assessment=assessment,
                               search_id=search_id, name=company.name))

            n_sites = sum(1 for a in todo if a.site)
            workers = min(clamp_parallel(self.settings.parallel_companies), n_sites)
            if workers <= 1:
                self._process_in_sequence(todo, analyze, announce, finish, cancel)
            else:
                self._process_in_parallel(todo, workers, analyze, announce, finish, cancel)
            processed, errors = counts["processed"], counts["errors"]

            status = "cancelled" if cancel.is_set() else "completed"
            self.store.update_search_status(search_id, status)
            duration = time.monotonic() - start
            log.info("Search %s %s: %d/%d companies, %d errors, %.0f s",
                     search_id, status, processed, total, errors, duration)
            key = "pipeline.completed" if status == "completed" else "pipeline.cancelled"
            event = _event(END, key, current=processed, total=total, search_id=search_id,
                             processed=processed, errors=errors, reduced=self._reduced is not None)
            event.status = status
            notify(event)
            return search_id
        except BaseException:
            # Unexpected error: the search stays resumable.
            try:
                self.store.update_search_status(search_id, "cancelled")
            except Exception:
                log.exception("Search status could not be updated")
            raise
        finally:
            if owns_fetcher and self._fetcher is not None:
                try:
                    self._fetcher.close()
                finally:
                    self._fetcher = None

    # --- processing of the companies

    def _analyze_site(self, analyze: Callable, company: Company, cancel) -> tuple:
        """Run one site analysis; returns (outcome, error). SearchError propagates.

        The candidate's role names go along (`roles=`) when the analysis accepts
        them, so large ATS boards fetch the full text of the closest postings."""
        extra = {"roles": self._roles} if self._roles and self._takes(analyze, "roles") else {}
        try:
            return analyze(company.site, self._fetcher, self.settings, cancel, **extra), None
        except SearchError:
            raise
        except Exception as error:
            return None, error

    def _site_job(self, analyze: Callable, company: Company, cancel) -> tuple:
        """Worker-thread job: a job still queued when the stop flag is set never starts."""
        if cancel.is_set():
            return None, None  # discarded by the consumer, which has stopped too
        return self._analyze_site(analyze, company, cancel)

    def _process_in_sequence(self, todo: list[Company], analyze: Callable,
                             announce: Callable, finish: Callable,
                             cancel: threading.Event) -> None:
        """One company at a time (parallel_companies = 1, or at most one site)."""
        for company in todo:
            if cancel.is_set():
                break
            announce(company)
            outcome, error = None, None
            if company.site:
                outcome, error = self._analyze_site(analyze, company, cancel)
                if error is None and cancel.is_set():
                    break  # analysis interrupted halfway: it will be redone on resume
            finish(company, outcome, error)

    def _process_in_parallel(self, todo: list[Company], workers: int, analyze: Callable,
                              announce: Callable, finish: Callable,
                              cancel: threading.Event) -> None:
        """Site analyses on a thread pool; scoring, saving and events stay on this thread.

        Companies are taken in priority order and at most
        WINDOW_PER_WORKER × `workers` of them are queued or in flight.
        Companies without a site skip the pool. Among the companies ready to be
        scored, the one with the highest priority goes first, so results tend
        to follow the priority order without waiting for slow sites.

        Politeness is unchanged: every request still goes through the shared
        Fetcher, whose per-domain lock serialises (and spaces out) requests
        to the same domain, so two companies on one domain simply take turns.

        On cancel, nothing more is submitted, queued jobs are dropped, running
        analyses see `cancel` and return, and their results are discarded (the
        companies are not saved and will be redone on resume). If this loop
        stops for any other reason (SearchError from the model, an unexpected
        error), the running analyses are asked to stop through an internal flag.
        """
        window = max(1, WINDOW_PER_WORKER * workers)
        stop = threading.Event()
        cancel_jobs = _CombinedEvent(cancel, stop)
        upcoming = iter(enumerate(todo))
        exhausted = False
        in_flight: dict[Future, tuple[int, Company]] = {}
        without_site: deque[tuple[int, Company]] = deque()  # ready to score, in priority order

        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="site_analysis")
        try:
            while True:
                # Top up the window in priority order.
                while (not exhausted and not cancel.is_set()
                       and len(in_flight) + len(without_site) < window):
                    try:
                        index, company = next(upcoming)
                    except StopIteration:
                        exhausted = True
                        break
                    announce(company)
                    if company.site:
                        future = pool.submit(self._site_job, analyze, company, cancel_jobs)
                        in_flight[future] = (index, company)
                    else:
                        without_site.append((index, company))
                if cancel.is_set():
                    break

                # Next company to score: the highest-priority one already available.
                finished = [(in_flight[f][0], f) for f in in_flight if f.done()]
                chosen_future = min(finished, key=lambda c: c[0])[1] if finished else None
                if without_site and (chosen_future is None or without_site[0][0] < in_flight[chosen_future][0]):
                    _, company = without_site.popleft()
                    finish(company, None, None)
                    continue
                if chosen_future is not None:
                    _, company = in_flight.pop(chosen_future)
                    outcome, error = chosen_future.result()  # SearchError re-raised here
                    if error is None and cancel.is_set():
                        break  # analysis interrupted halfway: it will be redone on resume
                    finish(company, outcome, error)
                    continue
                if not in_flight:
                    break  # nothing queued, nothing running: all done
                wait(list(in_flight), timeout=CONSUMER_WAIT_S, return_when=FIRST_COMPLETED)
        finally:
            stop.set()
            pool.shutdown(wait=True, cancel_futures=True)

    def resume(self, search_id: int, on_progress: Callable[[Event], None],
                 cancel: threading.Event) -> int:
        """Resumes a saved search with its original parameters."""
        search = self.store.read_search(search_id)
        if search is None or search.get("profile") is None or search.get("lat") is None:
            raise SearchError("error.search_not_found", str(search_id))
        parameters = self.store.cache_read(PARAMS_NAMESPACE, str(search_id)) or {}
        place = Place(lat=search["lat"], lon=search["lon"], name=search.get("place") or "")
        # saved [] = the user had asked for all company types
        sectors = parameters.get("sectors") if isinstance(parameters.get("sectors"), list) else None
        extra = {}
        if not self._legacy and parameters.get("area"):
            extra = {"area": parameters.get("area"), "province": parameters.get("province") or "",
                     "ateco": list(parameters.get("ateco") or [])}
        return self.run(
            search["profile"], place, float(search["radius_km"] or 0),
            list(parameters.get("manual_urls") or []), sectors, on_progress, cancel,
            search_id=search_id, **extra,
        )


class _DiscoveryFailed(Exception):
    """Every discovery source failed; the message lists one status line per source."""


class _CombinedEvent:
    """Read-only, Event-like view that is set as soon as any wrapped event is set.

    Passed to the site analyses running on worker threads, so that they stop
    both on the user's cancel and when the search is aborted for another reason.
    """

    def __init__(self, *events: threading.Event):
        self._events = events

    def is_set(self) -> bool:
        return any(e.is_set() for e in self._events)

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while not self.is_set():
            rest = None if deadline is None else deadline - time.monotonic()
            if rest is not None and rest <= 0:
                return False
            self._events[0].wait(0.05 if rest is None else min(0.05, rest))
        return True


def _with_board_postings(outcome, postings: list | None):
    """The site outcome with the company's job-board postings added.

    Without a site (outcome None) the board postings make the whole outcome,
    with source "jobboard:<site>"; postings found on the site come first and
    win over board duplicates.
    """
    if not postings:
        return outcome
    from .models import HiringStatus
    from .web.analysis import SiteOutcome, remove_duplicates

    board_source = postings[0].source or "jobboard"
    if outcome is None:
        return SiteOutcome(HiringStatus.HIRING, remove_duplicates(list(postings)), "", 0,
                           None, board_source)
    own = list(getattr(outcome, "postings", None) or [])
    merged = remove_duplicates(own + list(postings))
    return SiteOutcome(
        HiringStatus.HIRING if merged else outcome.status, merged,
        getattr(outcome, "company_text", ""), getattr(outcome, "pages_read", 0),
        getattr(outcome, "careers_url", None),
        getattr(outcome, "postings_source", "") if own else board_source,
    )


def _event(kind: str, key: str, *, current: int = 0, total: int = 0,
            company: Company | None = None, assessment: Assessment | None = None,
            search_id: int | None = None, **parameters) -> Event:
    message = t(key, **parameters)
    if message == key:  # a discovery key not yet in the locale files: English fallback
        from .discovery.texts import text

        message = text(key, **parameters)
    return Event(
        kind=kind, message=message, current=current, total=total,
        company=company, assessment=assessment, search_id=search_id,
        key=key, parameters=parameters,
    )


def _guarded(on_progress: Callable[[Event], None] | None) -> Callable[[Event], None]:
    """An error in the event receiver must not interrupt the search."""

    def notify(event: Event) -> None:
        if on_progress is None:
            return
        try:
            on_progress(event)
        except Exception:
            log.exception("Error in the progress event handler")

    return notify
