"""Optional extra source: hiring companies from job boards (Indeed, LinkedIn) via JobSpy.

OFF by default (`Settings.job_boards_enabled`). It needs the optional extra
`pip install "reversehire[jobboards]"` (python-jobspy, MIT, which pulls pandas
and curl_cffi). When the package is missing or cannot load, `availability()`
says why and the rest of the app works unchanged.

CAUTION: automated access to Indeed and LinkedIn is against those sites' terms
of use and may be blocked at any time (HTTP 403/429). Using it is the user's
own choice, made by switching `job_boards_enabled` on. To keep the load low
the queries are few (the top MAX_ROLES role titles, RESULTS_PER_QUERY results
each, one site at a time, a pause of DELAY_BETWEEN_CALLS_S between calls) and
their results are cached for CACHE_TTL_S. LinkedIn is read through its public
guest pages only (no login); Glassdoor and the other boards are never used.

Only JobSpy's scraper classes are used (not `scrape_jobs`): rows are the
`JobPost.model_dump()` dictionaries, so pandas is not needed at run time. If
JobSpy's package `__init__` cannot load (pandas DLLs blocked, for example) the
scraper modules are loaded without it.

Mapping (REPO_STUDY 4.d): employer -> Company (site = company_url_direct, else
the host of the direct apply URL when it is not a job board); row -> Posting
(job_type / is_remote / salary columns, then `complete_posting` on the text).
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import re
import sys
import threading
import time
import types
from datetime import date, datetime
from typing import Callable
from urllib.parse import urlsplit

from rapidfuzz import fuzz

from ..models import Company, Posting
from .posting_attrs import complete_posting
from .text import compact, fragment_lines

log = logging.getLogger(__name__)

SITES = ("indeed", "linkedin")
MAX_ROLES = 3
RESULTS_PER_QUERY = 15
DELAY_BETWEEN_CALLS_S = 4.0
HOURS_OLD = 30 * 24  # postings of the last 30 days
KM_PER_MILE = 1.609344
DESCRIPTION_LENGTH = 6000
CACHE_NAMESPACE = "jobboards"
CACHE_TTL_S = 24 * 3600
NAME_MATCH_THRESHOLD = 92  # rapidfuzz token_set_ratio on normalised names
MERGE_EXTRA_KM = 5.0  # an OSM company merged by name must lie within radius + this
DEFAULT_COUNTRY = "italy"

# Hosts that are job boards or aggregators, never a company's own site.
JOB_BOARD_HOSTS = (
    "indeed.", "linkedin.com", "glassdoor.", "ziprecruiter.", "monster.", "infojobs.", "jooble.",
    "careerjet.", "subito.it", "bakeca.it", "kijiji.", "trovit.", "jobrapido.", "talent.com",
    "adzuna.", "simplyhired.", "stepstone.", "randstad.", "adecco.", "manpower.", "gigroup.",
    "lavoro.corriere.it", "jobatus.", "helplavoro.", "click.appcast.io", "appcast.io",
)

_COUNTRY_WORDS = {
    "italia": "italy", "italy": "italy", "it": "italy", "deutschland": "germany", "germany": "germany",
    "france": "france", "españa": "spain", "espana": "spain", "spain": "spain", "schweiz": "switzerland",
    "suisse": "switzerland", "svizzera": "switzerland", "switzerland": "switzerland",
    "österreich": "austria", "osterreich": "austria", "austria": "austria", "united kingdom": "uk",
    "uk": "uk", "ireland": "ireland", "nederland": "netherlands", "netherlands": "netherlands",
    "belgique": "belgium", "belgië": "belgium", "belgium": "belgium", "portugal": "portugal",
    "polska": "poland", "poland": "poland", "україна": "ukraine", "ukraine": "ukraine",
    "united states": "usa", "usa": "usa",
}
_STREET_WORDS = re.compile(
    r"^(?:via|viale|piazza|piazzale|corso|largo|vicolo|strada|contrada|localita|località|frazione|"
    r"quartiere|zona|street|road|avenue|rue|calle|straße|strasse)\b", re.I)
_LEGAL_FORMS = re.compile(
    r"\b(?:s\.?\s?r\.?\s?l\.?s?|s\.?\s?p\.?\s?a\.?|s\.?\s?n\.?\s?c\.?|s\.?\s?a\.?\s?s\.?|soc\.?\s?coop\.?|"
    r"societa|società|cooperativa|onlus|ltd|limited|gmbh|inc|ag|sa|bv|spa|srl|group|gruppo|italia|italy)\b",
    re.I)


# --- loading of JobSpy ------------------------------------------------------------

_lock = threading.Lock()
_loaded: dict | None = None
_load_error: str | None = None


def _import_scrapers() -> dict:
    """{"indeed": class, "linkedin": class, "model": module}; raises ImportError."""
    try:
        import jobspy  # noqa: F401  (normal path: the package __init__ loads)
    except ImportError:
        # JobSpy's __init__ imports pandas only for scrape_jobs(); load the
        # scraper modules without it (e.g. pandas DLLs blocked on the PC).
        spec = importlib.util.find_spec("jobspy")
        if spec is None or not spec.submodule_search_locations:
            raise
        for name in [n for n in sys.modules if n == "jobspy" or n.startswith("jobspy.")]:
            del sys.modules[name]
        package = types.ModuleType("jobspy")
        package.__path__ = list(spec.submodule_search_locations)
        package.__spec__ = spec
        sys.modules["jobspy"] = package
    model = importlib.import_module("jobspy.model")
    return {
        "indeed": importlib.import_module("jobspy.indeed").Indeed,
        "linkedin": importlib.import_module("jobspy.linkedin").LinkedIn,
        "model": model,
    }


def _scrapers() -> dict | None:
    global _loaded, _load_error
    with _lock:
        if _loaded is None and _load_error is None:
            try:
                _loaded = _import_scrapers()
            except Exception as error:  # missing, or broken on this Python/PC
                _load_error = f"{type(error).__name__}: {error}"[:200]
                log.info("JobSpy not available (%s)", _load_error)
        return _loaded


def availability() -> tuple[bool, str]:
    """(True, "") when JobSpy can run; otherwise (False, short reason)."""
    if _scrapers() is not None:
        return True, ""
    return False, _load_error or "python-jobspy is not installed"


def _jobspy_runner(site: str, query: dict) -> list[dict]:
    """One JobSpy scraper call; rows as plain dicts. Raises RuntimeError on board errors."""
    loaded = _scrapers()
    if loaded is None:
        raise RuntimeError(_load_error or "python-jobspy is not installed")
    model = loaded["model"]
    country = model.Country.from_string(query["country"]) if hasattr(model.Country, "from_string") else None
    scraper_input = model.ScraperInput(
        site_name=[model.Site(site)],
        search_term=query["search_term"],
        location=query["location"],
        distance=query["distance"],
        results_wanted=query["results_wanted"],
        hours_old=query.get("hours_old"),
        country_indeed=country or model.Country.ITALY,
        description_format=model.DescriptionFormat.PLAIN,
        verbose=0,
    )
    scraper = loaded[site]()
    jobs = scraper.scrape(scraper_input)
    if getattr(scraper, "error", None) and not jobs:
        raise RuntimeError(str(scraper.error)[:200])
    rows = []
    for job in jobs:
        row = job.model_dump()
        row["site"] = site
        rows.append({k: _plain(v) for k, v in row.items()})
    return rows


def _plain(value):
    """JSON-friendly value (dates as ISO strings)."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


# --- place, roles, countries ------------------------------------------------------


def country_of(place) -> str:
    """JobSpy country name of the place (attribute `country` if any, else the last
    part of its name), DEFAULT_COUNTRY when unknown."""
    explicit = getattr(place, "country", None)
    candidates = [explicit] if isinstance(explicit, str) and explicit.strip() else []
    name = getattr(place, "name", "") or ""
    if "," in name:
        candidates.append(name.rsplit(",", 1)[-1])
    for candidate in candidates:
        key = compact(candidate).lower()
        if key in _COUNTRY_WORDS:
            return _COUNTRY_WORDS[key]
    return DEFAULT_COUNTRY


def town_of(place) -> str | None:
    """Town of the search point from its (geocoded) name; None for bare coordinates."""
    name = compact(getattr(place, "name", "") or "")
    if not name or re.fullmatch(r"[-+\d.,;\s°NSEOW]+", name):
        return None
    parts = [compact(p) for p in name.split(",") if compact(p)]
    if len(parts) > 1 and compact(parts[-1]).lower() in _COUNTRY_WORDS:
        parts = parts[:-1]
    for part in parts:
        if re.search(r"\d", part) or _STREET_WORDS.match(part) or len(part) < 2:
            continue
        return part
    return None


def search_terms(roles: list[str]) -> list[str]:
    """The first MAX_ROLES distinct role titles."""
    result: list[str] = []
    for role in roles or []:
        text = compact(str(role or ""))
        if text and text.lower() not in {r.lower() for r in result}:
            result.append(text)
        if len(result) >= MAX_ROLES:
            break
    return result


# --- mapping of rows --------------------------------------------------------------


def is_job_board(url: str | None) -> bool:
    host = _host(url)
    return bool(host) and any(h in host for h in JOB_BOARD_HOSTS)


def _host(url: str | None) -> str:
    if not url or not isinstance(url, str):
        return ""
    try:
        host = (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def company_site(row: dict) -> str | None:
    """The employer's own site: company_url_direct, else the host of job_url_direct
    when that is not a job board; None otherwise."""
    for key in ("company_url_direct", "job_url_direct"):
        url = row.get(key)
        if not isinstance(url, str) or not url.startswith(("http://", "https://")) or is_job_board(url):
            continue
        host = _host(url)
        if not host:
            continue
        if key == "company_url_direct":
            return url
        return f"https://{host}/"
    return None


_CONTRACTS = {"internship": "internship", "temporary": "fixed_term", "contract": "freelance"}
_PERIODS = {"yearly": "year", "monthly": "month", "hourly": "hour"}


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number > 0 else None  # NaN check


def row_to_posting(row: dict) -> Posting | None:
    """Posting from a JobSpy row (structured columns first, then the text rules)."""
    title = compact(str(row.get("title") or ""))
    url = row.get("job_url_direct") or row.get("job_url")
    if not title or not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    site = str(row.get("site") or "board")
    posting = Posting(
        title=title,
        url=url,
        description=fragment_lines(str(row.get("description") or ""), DESCRIPTION_LENGTH),
        location=compact(str(row.get("location") or "")),
        source=f"jobboard:{site}",
    )
    # "fulltime" says nothing about permanence: only clear job types are mapped.
    for job_type in str(row.get("job_type") or "").lower().replace(" ", "").split(","):
        if job_type in _CONTRACTS:
            posting.contract = _CONTRACTS[job_type]
            break
    if row.get("is_remote") is True:
        posting.remote = "remote"
    low, high = _number(row.get("min_amount")), _number(row.get("max_amount"))
    if low is not None or high is not None:
        period = _PERIODS.get(str(row.get("interval") or "").lower())
        currency = str(row.get("currency") or "").upper() or None
        low = low if low is not None else high
        high = high if high is not None else low
        amounts = f"{low:,.0f}" if low == high else f"{low:,.0f}–{high:,.0f}"
        posting.salary = " ".join(x for x in (currency, amounts) if x) + (f" / {period}" if period else "")
        posting.salary_min, posting.salary_max = low, high
        posting.salary_period, posting.salary_currency = period, currency
    return complete_posting(posting)


_CONNECTORS = {"e", "and", "the", "und", "et", "y"}


def normalize_name(name: str) -> str:
    """Lowercase name without legal forms, connectors ("e", "&") and punctuation
    (for matching employers)."""
    text = _LEGAL_FORMS.sub(" ", (name or "").lower())
    return " ".join(w for w in re.sub(r"[^\w]+", " ", text).split() if w not in _CONNECTORS)


def group_rows(rows: list[dict]) -> list[tuple[Company, list[Posting]]]:
    """One Company per employer (same site, or same normalised name), with its postings.

    The first name seen is kept; a site found on a later row is attached to the
    employer seen before without one.
    """
    groups: list[tuple[Company, list[Posting]]] = []
    by_site: dict[str, int] = {}
    by_name: dict[str, int] = {}
    for row in rows:
        name = compact(str(row.get("company") or ""))
        posting = row_to_posting(row)
        if not name or posting is None:
            continue
        site = company_site(row)
        name_key = normalize_name(name) or name.lower()
        index = by_site.get(_host(site)) if site else None
        if index is None:
            index = by_name.get(name_key)
            if index is not None and site:
                company = groups[index][0]
                if company.site and _host(company.site) != _host(site):
                    index = None  # same name, different site: another employer
                elif not company.site:
                    company.site = site
        if index is None:
            address = compact(str(row.get("company_addresses") or row.get("location") or ""))
            groups.append((Company(name=name, lat=None, lon=None, address=address, site=site,
                                   origin="jobboard"), []))
            index = len(groups) - 1
        by_name.setdefault(name_key, index)
        if site:
            by_site.setdefault(_host(site), index)
        postings = groups[index][1]
        if all(p.url != posting.url and (p.title, p.location) != (posting.title, posting.location)
               for p in postings):
            postings.append(posting)
    return groups


def merge_into(found: list[tuple[Company, list[Posting]]], existing: list[Company],
               radius_km: float) -> tuple[list[tuple[Company, list[Posting]]], dict[int, list[Posting]]]:
    """Split board employers into new companies and postings for companies already found.

    An employer matches an existing company with the same site host, or with a
    very similar name (rapidfuzz token_set_ratio >= NAME_MATCH_THRESHOLD) lying
    within radius_km + MERGE_EXTRA_KM of the search point (board results are not
    geocoded, so the search radius stands in for "a few km").
    Returns (new companies with their postings, {index in `existing`: postings}).
    """
    by_host = {_host(c.site): i for i, c in enumerate(existing) if c.site}
    names = [(i, normalize_name(c.name)) for i, c in enumerate(existing) if c.name]
    new: list[tuple[Company, list[Posting]]] = []
    attached: dict[int, list[Posting]] = {}
    for company, postings in found:
        index = by_host.get(_host(company.site)) if company.site else None
        if index is None:
            wanted = normalize_name(company.name)
            best, best_score = None, 0.0
            for i, name in names:
                if not wanted or not name:
                    continue
                distance = existing[i].distance_km
                if distance is not None and distance > radius_km + MERGE_EXTRA_KM:
                    continue
                score = fuzz.token_set_ratio(wanted, name)
                if score >= NAME_MATCH_THRESHOLD and score > best_score and min(len(wanted), len(name)) >= 3:
                    best, best_score = i, score
            index = best
        if index is None:
            new.append((company, postings))
        else:
            attached.setdefault(index, []).extend(postings)
    return new, attached


# --- search -------------------------------------------------------------------------


def _wait(seconds: float, cancel, sleep: Callable[[float], None]) -> None:
    if seconds <= 0:
        return
    if cancel is not None and hasattr(cancel, "wait") and sleep is time.sleep:
        cancel.wait(seconds)
    else:
        sleep(seconds)


def search_job_boards(
    roles: list[str],
    place,
    radius_km: float,
    settings,
    *,
    cancel: threading.Event | None = None,
    limit: int = RESULTS_PER_QUERY,
    sites: tuple[str, ...] = SITES,
    store=None,
    runner: Callable[[str, dict], list[dict]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    failures: list[str] | None = None,
) -> list[tuple[Company, list[Posting]]]:
    """Hiring employers near the place from Indeed/LinkedIn, grouped with their postings.

    One query per (site, role) for the top MAX_ROLES roles around the town of
    `place` (radius converted to miles), in the place's country (Italy by
    default). A site that fails (blocked, changed) is skipped for the rest of
    the search. `runner(site, query) -> rows` replaces JobSpy in tests; `store`
    caches each query for CACHE_TTL_S; the names of the sites that failed are
    appended to `failures` when given. Raises RuntimeError("unavailable: ...")
    when JobSpy cannot run and no runner is given.
    """
    if runner is None:
        ok, reason = availability()
        if not ok:
            raise RuntimeError(f"unavailable: {reason}")
        runner = _jobspy_runner
    town = town_of(place)
    terms = search_terms(roles)
    if not town or not terms:
        return []
    miles = max(1, round(float(radius_km or 0) / KM_PER_MILE))
    country = country_of(place)
    rows: list[dict] = []
    failed: set[str] = set()
    first_call = True
    for site in sites:
        for term in terms:
            if cancel is not None and cancel.is_set():
                return group_rows(rows)
            if site in failed:
                continue
            query = {"search_term": term, "location": town, "distance": miles,
                     "results_wanted": max(1, int(limit)), "hours_old": HOURS_OLD, "country": country}
            key = f"{site}|{term.lower()}|{town.lower()}|{miles}|{country}|{query['results_wanted']}"
            cached = _cache_read(store, key)
            if cached is not None:
                rows.extend(cached)
                continue
            if not first_call:
                _wait(DELAY_BETWEEN_CALLS_S, cancel, sleep)
            first_call = False
            try:
                found = [r for r in runner(site, query) if isinstance(r, dict)]
            except Exception as error:  # blocked (403/429), changed page, network down
                log.warning("Job board %s failed (%s)", site, type(error).__name__)
                failed.add(site)
                if failures is not None and site not in failures:
                    failures.append(site)
                continue
            _cache_write(store, key, found)
            rows.extend(found)
    if failed and not rows:
        raise RuntimeError("failed: " + ", ".join(sorted(failed)))
    return group_rows(rows)


def _cache_read(store, key: str):
    if store is None:
        return None
    try:
        data = store.cache_read(CACHE_NAMESPACE, key, CACHE_TTL_S)
    except Exception:
        return None
    return data if isinstance(data, list) else None


def _cache_write(store, key: str, rows: list[dict]) -> None:
    if store is None:
        return
    try:
        store.cache_write(CACHE_NAMESPACE, key, rows)
    except Exception as error:
        log.info("Job board cache not writable (%s)", type(error).__name__)
