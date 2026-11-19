"""The job search pipeline: fan out to the enabled sources (politely, in worker threads),
keep what answers the query, de-duplicate, filter by place/radius and contract, score
against the CV with the injected scorer, sort and explain.

    result = search_jobs("plc programmer bologna 30 km", sources, gateway=gateway,
                         cv_profile=profile, scorer=match, enabled={"agency:randstad", "local"},
                         progress=lambda done, total, message: ..., cancel=event)

Designed for a worker thread: progress callback `(done, estimated_total, message)`,
cancel check (threading.Event or callable), a timeout per source (the partial results
of a slow source are kept). Never raises: errors are logged (logger
`reversehire.jobsearch`) and returned in the per-source reports.
"""

from __future__ import annotations

import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from ..geo.distance import haversine_km
from . import lexicon
from ._util import CancelCheck, fold, log, parse_date
from .base import JobSource, SourceCancelled, SourceContext
from .cache import JobSearchCache, default_cache
from .dedup import deduplicate
from .models import Posting, SearchResult, SourceReport
from .query import SearchQuery, parse_query
from .scoring import score_posting
from .texts import source_label, text

DEFAULT_SOURCE_TIMEOUT_S = 120.0
POLL_S = 0.05
# rank = fit with the CV (or the relevance without a CV), how well the ad answers the
# query (exact words, synonyms, meaning), distance, recency
WEIGHT_FIT, WEIGHT_RELEVANCE, WEIGHT_DISTANCE, WEIGHT_RECENCY = 0.65, 0.10, 0.15, 0.10
DISTANCE_SCALE_KM = 25.0
RECENCY_SCALE_DAYS = 21.0


def enabled_ids(sources, enabled=None) -> set[str]:
    """Ids the user wants: `enabled` when given, else each source's default."""
    if enabled is None:
        return {s.id for s in sources if s.enabled_by_default}
    return {str(e) for e in enabled}


_NOT_A_PLACE = re.compile(r"^\s*\d+\s+(?:locations?|sedi|luoghi|places|offices)\s*$", re.I)


def _resolve_place(name: str, place_lookup, geocode, memo: dict, geocode_text: str | None = None):
    """(lat, lon) of a place name: the caller's lookup, the built-in towns, then the
    geocoder (asked `geocode_text` when given: the whole "Town, Region, Country")."""
    key = fold(geocode_text or name)
    if not key or _NOT_A_PLACE.match(name):
        return None
    if key in memo:
        return memo[key]
    found = None
    if place_lookup is not None:
        try:
            hit = place_lookup(name)
            if hit:
                found = (float(hit[0]), float(hit[1]))
        except Exception:
            found = None
    if found is None:
        builtin = lexicon.builtin_place(name)
        if builtin:
            found = (builtin[0], builtin[1])
    if found is None and geocode is not None:
        try:
            hit = geocode(geocode_text or name)
            if hit is not None:
                lat = getattr(hit, "lat", None) if not isinstance(hit, (tuple, list)) else hit[0]
                lon = getattr(hit, "lon", None) if not isinstance(hit, (tuple, list)) else hit[1]
                if lat is not None and lon is not None:
                    found = (float(lat), float(lon))
        except Exception as error:
            log.info("Geocoding failed: %s", type(error).__name__)
    memo[key] = found
    return found


def locate(posting: Posting, place_lookup=None, geocode=None, memo: dict | None = None) -> None:
    """Coordinates of the posting from its town (or province capital) when missing."""
    if posting.lat is not None and posting.lon is not None:
        return
    memo = {} if memo is None else memo
    location = (posting.location or "").strip()
    first = location.split(",")[0].split("(")[0].strip() if location else None
    # "Wollongong, New South Wales, Australia": the geocoder gets the whole text, so that
    # a town abroad is not looked up as an Italian one
    whole = location if "," in location else None
    for name, quality, asked in ((posting.town, "town", None), (first, "town", whole),
                                 (lexicon.PROVINCE_CAPITAL.get((posting.province or "").upper()), "province",
                                  None)):
        if not name:
            continue
        found = _resolve_place(name, place_lookup, geocode, memo, asked)
        if found:
            posting.lat, posting.lon = found
            posting.geo_quality = posting.geo_quality or quality
            return


def _area_ok(query: SearchQuery, posting: Posting) -> bool:
    """A region / country chip: the posting's place (normalised, geo/places.py) must be
    in it; a posting whose place is unknown, or fully remote, is kept."""
    if posting.remote == "remote":
        return True
    try:
        from ..geo.places import location_of_posting

        where = location_of_posting(posting)
    except Exception:  # noqa: BLE001 - unknown place: kept
        return True
    if query.place_level == "country":
        code = getattr(query, "country", None)
        return not (code and where.country_code) or where.country_code == code
    key = getattr(query, "region_key", None)
    if key and where.region_key:
        return where.region_key == key
    if key and where.country_code and where.country_code != "IT":
        return False
    if not key and posting.region:   # a query saved before region_key existed
        return fold(posting.region).replace("-", " ") == fold(query.place or "").replace("-", " ")
    return True


def _contract_ok(query: SearchQuery, posting: Posting) -> bool:
    if query.contract and posting.contract:
        return posting.contract in lexicon.CONTRACT_ACCEPTS.get(query.contract, {query.contract})
    return True


def _remote_ok(query: SearchQuery, posting: Posting) -> bool:
    if query.remote == "remote" and posting.remote:
        return posting.remote == "remote"
    if query.remote == "hybrid" and posting.remote:
        return posting.remote in ("hybrid", "remote")
    return True


def _hours_ok(query: SearchQuery, posting: Posting) -> bool:
    return not (query.hours and posting.hours and query.hours != posting.hours)


def _days_old(posting: Posting, today: date) -> int | None:
    posted = parse_date(posting.date_posted)
    return None if posted is None else max(0, (today - posted).days)


def _why_lines(query: SearchQuery, posting: Posting, scored, today: date) -> list[str]:
    lines = list(scored.why)
    if scored.blocked and scored.reason:
        lines.insert(0, text("jobsearch.why.blocked", reason=scored.reason))
    return lines + context_why(query, posting, today)


def context_why(query: SearchQuery, posting: Posting, today: date | None = None) -> list[str]:
    """The "why" lines that do not depend on the CV (place, date, salary, agency, other
    sources), in the current interface language: the GUI renders them again when it shows
    a stored search, so that they follow a language change."""
    today = today or date.today()
    lines: list[str] = []
    if posting.remote == "remote":
        lines.append(text("jobsearch.why.remote"))
    if posting.distance_km is not None and query.place:
        lines.append(text("jobsearch.why.distance", km=f"{posting.distance_km:.0f}", place=query.place.title()))
    elif query.place and posting.remote != "remote":
        lines.append(text("jobsearch.why.distance_unknown"))
    days = _days_old(posting, today)
    if days == 0:
        lines.append(text("jobsearch.why.today"))
    elif days is not None:
        lines.append(text("jobsearch.why.recent", days=days))
    if posting.salary:
        lines.append(text("jobsearch.why.salary", salary=posting.salary))
    if posting.employer_hidden:
        lines.append(text("jobsearch.why.agency", agency=posting.agency or "-"))
    if posting.also_on:
        names = sorted({source_label(a.get("source", ""), a.get("source", "")) for a in posting.also_on})
        lines.append(text("jobsearch.why.also_on", sources=", ".join(names)))
    return lines


class _Progress:
    def __init__(self, callback, total: int):
        self.callback = callback
        self.total = max(1, total)
        self.done = 0
        self.lock = threading.Lock()

    def step(self, message: str = "", n: int = 1) -> None:
        with self.lock:
            self.done = min(self.total, self.done + n)
            done, total = self.done, self.total
        if self.callback is not None:
            try:
                self.callback(done, total, message)
            except Exception:
                pass


def search_jobs(query, sources: list[JobSource], *, gateway=None, cv_profile=None, scorer=None, enabled=None,
                cache: JobSearchCache | None = None, progress=None, cancel=None,
                source_timeout_s: float = DEFAULT_SOURCE_TIMEOUT_S, place_lookup=None, geocode=None, esco=None,
                today: date | None = None, max_results: int = 300, max_workers: int = 3,
                keep_unknown_location: bool = True, embedder=None, vector_store=None) -> SearchResult:
    """Run a job search. See the module docstring. Never raises.

    `embedder` (optional, scoring/embedding.py): the postings are also ranked by meaning
    (jobsearch/semantic.py); a posting whose words do not match but whose meaning is
    close is kept with that reason. `vector_store`: where the vectors are cached."""
    started = time.monotonic()
    today = today or date.today()
    cache = cache or default_cache()
    is_cancelled = CancelCheck(cancel)
    if not isinstance(query, SearchQuery):
        query = parse_query(str(query or ""), place_lookup=place_lookup, esco=esco)
    result = SearchResult(query=query)
    stats = {"dropped_irrelevant": 0, "dropped_expired": 0, "dropped_filters": 0, "dropped_far": 0,
             "unknown_distance": 0, "merged_duplicates": 0}
    result.stats = stats
    try:
        wanted = enabled_ids(sources, enabled)
        runnable: list[JobSource] = []
        for source in sources:
            report = SourceReport(source.id)
            result.reports.append(report)
            if not source.allowed_by_terms:
                report.state = "forbidden_by_terms"
            elif source.id not in wanted:
                report.state = "disabled"
            elif source.needs_key and not source.has_key():
                report.state = "no_key"
            else:
                report.state = "running"
                runnable.append(source)
        own_gateway = None
        if gateway is None and any(s.hosts for s in runnable):
            from ..discovery.gateway import PoliteGateway

            gateway = own_gateway = PoliteGateway()
        estimates = {s.id: max(1, int(s.estimate(query) or 1)) for s in runnable}
        meter = _Progress(progress, sum(estimates.values()) + 1)
        contexts: dict[str, SourceContext] = {}
        steps_used: dict[str, int] = {s.id: 0 for s in runnable}

        def make_step(source: JobSource):
            label = source_label(source.id, source.label)

            def step(message: str = "") -> None:
                if steps_used[source.id] < estimates[source.id]:
                    steps_used[source.id] += 1
                    meter.step(text("jobsearch.progress.source", source=label, done=steps_used[source.id],
                                    total=estimates[source.id]))
            return step

        def work(source: JobSource, ctx: SourceContext):
            begun = time.monotonic()
            try:
                found = source.run(ctx)
                return "ok", found, time.monotonic() - begun
            except SourceCancelled:
                return "cancelled", list(ctx.found), time.monotonic() - begun
            except Exception as error:
                log.warning("Job source %s failed: %s", source.id, type(error).__name__)
                ctx.error(type(error).__name__)
                return "failed", list(ctx.found), time.monotonic() - begun

        collected: list[tuple[str, Posting]] = []
        reports = {r.source_id: r for r in result.reports}
        if runnable and not is_cancelled():
            executor = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="jobsearch")
            pending = {}
            try:
                for source in runnable:
                    ctx = SourceContext(gateway=gateway, query=query, cache=cache, progress=make_step(source),
                                        place_lookup=place_lookup, today=today, report=reports[source.id])
                    contexts[source.id] = ctx
                    pending[source.id] = (source, executor.submit(work, source, ctx), None)
                while pending:
                    if is_cancelled():
                        for source_id, (source, future, _) in list(pending.items()):
                            contexts[source_id].cancel_event.set()
                            report = reports[source_id]
                            report.state = "cancelled"
                            found = list(contexts[source_id].found)
                            report.found = len(found)
                            collected.extend((source_id, p) for p in found)
                        result.cancelled = True
                        pending.clear()
                        break
                    for source_id, (source, future, deadline) in list(pending.items()):
                        report = reports[source_id]
                        if future.running() and deadline is None:
                            pending[source_id] = (source, future, time.monotonic() + source_timeout_s)
                            continue
                        if future.done():
                            state, found, seconds = future.result()
                            report.seconds = round(seconds, 2)
                            report.found = len(found)
                            if state == "ok":
                                report.state = "partial" if report.errors and found else \
                                    ("failed" if report.errors and not found else ("ok" if found else "empty"))
                            else:
                                report.state = state
                            collected.extend((source_id, p) for p in found)
                            del pending[source_id]
                            remaining = estimates[source_id] - steps_used[source_id]
                            if remaining > 0:
                                steps_used[source_id] = estimates[source_id]
                                meter.step(source_label(source_id, source.label), remaining)
                        elif deadline is not None and time.monotonic() > deadline:
                            contexts[source_id].cancel_event.set()
                            found = list(contexts[source_id].found)
                            report.state = "timeout"
                            report.found = len(found)
                            report.seconds = round(source_timeout_s, 2)
                            collected.extend((source_id, p) for p in found)
                            del pending[source_id]
                            log.info("Job source %s timed out after %.0f s", source_id, source_timeout_s)
                    if pending:
                        time.sleep(POLL_S)
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
                if own_gateway is not None and not result.cancelled and \
                        not any(r.state == "timeout" for r in result.reports):
                    own_gateway.close()
        elif runnable:
            result.cancelled = True
            for source in runnable:
                reports[source.id].state = "cancelled"
        meter.step(text("jobsearch.progress.ranking", count=len(collected)))
        result.postings = _rank(query, collected, reports, stats, cv_profile, scorer, place_lookup, geocode,
                                today, max_results, keep_unknown_location, is_cancelled, embedder=embedder,
                                vector_store=vector_store, cancel=cancel)
        for source in runnable:
            if source.attribution and reports[source.id].found:
                result.attributions.append(source.attribution)
    except Exception as error:  # last guard: the caller always gets a result
        log.exception("Job search failed: %s", type(error).__name__)
        result.stats["error"] = type(error).__name__
    result.seconds = round(time.monotonic() - started, 2)
    return result


def rerank_postings(query: SearchQuery, postings: list[Posting], *, cv_profile=None, scorer=None, embedder=None,
                    vector_store=None, today: date | None = None, cancel=None) -> tuple[list[Posting], dict]:
    """The postings already found, ranked again for a changed query (a term of "Searched
    also for" removed): no source is asked again. (postings kept, stats). Never raises."""
    stats = {"dropped_irrelevant": 0, "dropped_expired": 0, "dropped_filters": 0, "dropped_far": 0,
             "unknown_distance": 0, "merged_duplicates": 0}
    try:
        ranked = _rank(query, [("", p) for p in postings], {}, stats, cv_profile, scorer, None, None,
                       today or date.today(), max(1, len(postings)), True, CancelCheck(cancel),
                       embedder=embedder, vector_store=vector_store, cancel=cancel)
    except Exception as error:  # noqa: BLE001
        log.warning("Postings not ranked again: %s", type(error).__name__)
        return list(postings), stats
    return ranked, stats


def _apply_meaning(query: SearchQuery, kept: list[Posting], maybe: set[int], stats, embedder, vector_store,
                   cancel) -> list[Posting]:
    """Hybrid relevance (jobsearch/semantic.py): exact words and meaning; the postings
    the words dropped stay only when their meaning is close."""
    from .semantic import kept_by_meaning, posting_documents, rank_by_meaning

    hits, mode = rank_by_meaning(query, posting_documents(kept), embedder=embedder, store=vector_store,
                                 cancel=cancel)
    stats["ranking"] = mode
    out = []
    for index, posting in enumerate(kept):
        hit = hits.get(f"p{index}")
        meaning = float(getattr(hit, "relevance", 0.0) or 0.0) / 100.0 if hit is not None else 0.0
        if id(posting) in maybe:
            if mode != "semantic" or not kept_by_meaning(hit):
                stats["dropped_irrelevant"] += 1
                continue
            posting.relevance = round(0.8 * meaning, 3)
            snippet = hit.snippet(140) if hit is not None else ""
            posting.score_details = dict(posting.score_details or {}, meaning=round(float(hit.semantic), 1),
                                         meaning_snippet=snippet, meaning_source=hit.source if hit else "")
        elif hit is not None and mode == "semantic":
            posting.relevance = round(max(posting.relevance or 0.0, min(1.0, meaning)), 3)
        out.append(posting)
    return out


def _rank(query: SearchQuery, collected, reports, stats, cv_profile, scorer, place_lookup, geocode, today: date,
          max_results: int, keep_unknown_location: bool, is_cancelled, *, embedder=None, vector_store=None,
          cancel=None) -> list[Posting]:
    origin: dict[int, str] = {}
    kept: list[Posting] = []
    maybe: set[int] = set()   # dropped by the words, judged again by meaning
    memo: dict = {}
    for source_id, posting in collected:
        try:
            relevance = query.relevance(posting.title, posting.description)
            if relevance <= 0:
                if embedder is None:
                    stats["dropped_irrelevant"] += 1
                    continue
                maybe.add(id(posting))
            posting.relevance = relevance
            posting.score_details = {k: v for k, v in (posting.score_details or {}).items()
                                     if not str(k).startswith("meaning")}
            valid = parse_date(posting.valid_through)
            if valid is not None and valid < today:
                stats["dropped_expired"] += 1
                continue
            if not (_contract_ok(query, posting) and _remote_ok(query, posting) and _hours_ok(query, posting)):
                stats["dropped_filters"] += 1
                continue
            if query.lat is not None and query.lon is not None:
                locate(posting, place_lookup, geocode, memo)
                if posting.lat is not None and posting.lon is not None:
                    posting.distance_km = round(haversine_km(query.lat, query.lon, posting.lat, posting.lon), 1)
                if posting.distance_km is not None and query.radius_km and posting.distance_km > query.radius_km \
                        and posting.remote != "remote":
                    stats["dropped_far"] += 1
                    continue
                if posting.distance_km is None and posting.remote != "remote":
                    same_province = query.province and posting.province and \
                        posting.province.upper() == query.province.upper()
                    if not same_province:
                        if not keep_unknown_location:
                            stats["dropped_far"] += 1
                            continue
                        stats["unknown_distance"] += 1
            elif query.place_level in ("region", "country") and not _area_ok(query, posting):
                stats["dropped_far"] += 1
                continue
            origin[id(posting)] = source_id
            kept.append(posting)
        except Exception as error:
            log.info("Posting skipped (%s)", type(error).__name__)
    if embedder is not None and kept and not is_cancelled():
        kept = _apply_meaning(query, kept, maybe, stats, embedder, vector_store, cancel)
    else:
        stats.setdefault("ranking", "keyword")
        kept = [p for p in kept if id(p) not in maybe]
    unique, merged = deduplicate(kept)
    stats["merged_duplicates"] = merged
    for posting in unique:
        report = reports.get(origin.get(id(posting), ""))
        if report is not None:
            report.kept += 1
    for posting in unique:
        if is_cancelled():
            break
        scored = score_posting(cv_profile, posting, scorer)
        fit = scored.score if scored.score is not None else 100.0 * (posting.relevance or 0.0)
        if posting.distance_km is not None:
            near = math.exp(-posting.distance_km / DISTANCE_SCALE_KM)
        else:
            near = 1.0 if posting.remote == "remote" else 0.5
        days = _days_old(posting, today)
        fresh = math.exp(-days / RECENCY_SCALE_DAYS) if days is not None else 0.5
        relevance = float(posting.relevance or 0.0)
        rank = WEIGHT_FIT * fit + 100 * (WEIGHT_RELEVANCE * relevance + WEIGHT_DISTANCE * near +
                                         WEIGHT_RECENCY * fresh)
        meaning = {k: v for k, v in (posting.score_details or {}).items() if k.startswith("meaning")}
        posting.score = round(scored.score, 1) if scored.score is not None else None
        posting.score_details = {"rank": round(rank, 2), "fit": round(fit, 1), "blocked": scored.blocked,
                                 **({k: v for k, v in scored.details.items() if isinstance(k, str)}
                                    if isinstance(scored.details, dict) else {}), **meaning}
        posting.why = _why_lines(query, posting, scored, today)
        if meaning.get("meaning_snippet"):
            posting.why.insert(0, text("jobsearch.why.meaning", snippet=meaning["meaning_snippet"]))
    unknown_last = query.lat is not None

    def order(p: Posting):
        located = p.distance_km is not None or p.remote == "remote" or not unknown_last
        return (bool(p.score_details.get("blocked")), not located, -float(p.score_details.get("rank", 0.0)))
    unique.sort(key=order)
    return unique[:max_results]
