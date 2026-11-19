"""Staffing-agency sites read through their public sitemap + the JobPosting JSON-LD of
each posting page (Google for Jobs markup).

Polite rules (RECRUITER_STUDY §2.2, §5): one sitemap download per agency per day (then
conditional requests), URL-slug pre-filter, at most MAX_PAGES posting pages per agency
per search, robots.txt honoured, postings cached until validThrough.

Terms of use, read once on 2026-11-18 (see AGENCY_SPECS below, `terms_*` fields):
- Randstad (www.randstad.it/terms-conditions-page/): candidate-account terms only;
  automated reading not addressed -> enabled.
- Manpower (www.manpower.it/it/terms-of-use): forbids "software automatizzati che
  compromettano i servizi" (12.4) and unauthorised reproduction (10.2); polite reading
  for the user's own search does neither -> enabled, results shown as links + summary.
- Synergie (portal terms PDF SP_MD0080E rev. 10/08/2022, linked from the site footer):
  candidate-portal terms only; automated reading not addressed -> enabled.
- Adecco (www.adecco.com/it-it/terms-of-use): forbids to "estrarre, riutilizzare ...
  con qualsiasi mezzo o procedura" any part of the site -> DISABLED.
- Gi Group (www.gigroup.it/condizioni-di-utilizzo/): forbids to "estrarre ... con
  qualsiasi mezzo o procedura" any content beyond personal use -> DISABLED (ambiguous
  for a candidate's personal use; kept off until the owner decides).
- Umana (www.umana.it/condizioni-d-uso-del-sito/): forbids "estrazione con qualsiasi
  mezzo, anche automatico" without written consent -> DISABLED.
A source whose terms forbid automated reading has `allowed_by_terms = False`: the
pipeline never contacts it, whatever the settings say.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

from ..geo.distance import haversine_km
from . import lexicon
from ._util import fold, log, parse_date
from .base import JobSource, SourceCancelled, SourceContext
from .cache import DAY_S
from .jsonld import postings_from_html
from .models import Posting

MAX_PAGES = 20
MAX_CHILD_SITEMAPS = 3
PLACE_CHECKS = 300            # candidates whose slug town is resolved
POSTING_TTL_S = 7 * DAY_S     # postings without validThrough
# Cache namespace of the parsed posting pages: a new name when the parsing changes, so that
# postings read by an older version are read again (v2: "assistenza da remoto" is not remote work).
POSTING_CACHE = "posting_v2"
SLUG_TOWN_SLACK_KM = 10.0
_LOC = re.compile(r"<(?:\w+:)?(sitemap|url)\b[^>]*>(.*?)</(?:\w+:)?\1>", re.S | re.I)
_TAG = re.compile(r"<(?:\w+:)?(loc|lastmod)\b[^>]*>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</(?:\w+:)?\1>",
                  re.S | re.I)


def parse_sitemap(xml: str) -> tuple[str, list[tuple[str, str]]]:
    """("index" | "urlset", [(loc, lastmod)]) of a sitemap, whatever its namespace."""
    entries = []
    kind = "urlset"
    for element, body in _LOC.findall(xml or ""):
        if element.lower() == "sitemap":
            kind = "index"
        values = {name.lower(): value for name, value in _TAG.findall(body)}
        loc = values.get("loc", "").replace("&amp;", "&").strip()
        if loc:
            entries.append((loc, values.get("lastmod", "").strip()))
    return kind, entries


@dataclass(frozen=True)
class AgencySpec:
    id: str
    name: str
    sitemap_url: str
    posting_pattern: str            # regex a posting URL must match (robots-allowed detail path)
    allowed_by_terms: bool
    terms_verdict: str
    terms_note: str
    terms_url: str
    child_sitemap_pattern: str = ""  # for sitemap indexes: which child sitemaps hold postings
    slug_town: str = ""              # "underscore": town is the "_<town>_" segment (Randstad)
    branch_in_street: bool = False   # JSON-LD streetAddress is the agency branch (Umana)
    terms_checked: str = "2026-11-18"

    @property
    def host(self) -> str:
        return urlsplit(self.sitemap_url).hostname or ""


AGENCY_SPECS: tuple[AgencySpec, ...] = (
    AgencySpec("randstad", "Randstad", "https://www.randstad.it/sitemaps/jobs/it/sitemap-jobdetails.xml",
               r"^https://www\.randstad\.it/offerte-lavoro/[^/?#]+/?$", True, "not_addressed",
               "Terms cover the candidate account only; automated reading is not addressed.",
               "https://www.randstad.it/terms-conditions-page/", slug_town="underscore"),
    AgencySpec("manpower", "Manpower", "https://www.manpower.it/sitemap/italy/it-manpower/sitemap_job-offer.xml",
               r"^https://www\.manpower\.it/it/annuncio-lavoro/[^?#]+/\d+/?$", True, "harmful_automation_only",
               "Terms forbid automated software that compromises the services and unauthorised reproduction; "
               "slow reading for your own search is neither. Results are shown as links.",
               "https://www.manpower.it/it/terms-of-use"),
    AgencySpec("synergie", "Synergie", "https://www.synergie-italia.it/sitemap-jobs.xml",
               r"^https://www\.synergie-italia\.it/candidato/offerte-di-lavoro/[^/?#]+-\d+/?$", True,
               "not_addressed", "Portal terms cover registered users only; automated reading is not addressed.",
               "https://www.synergie-italia.it/ (footer: Condizioni generali di utilizzo, SP_MD0080E)"),
    AgencySpec("adecco", "Adecco", "https://www.adecco.com/sitemap-jobs-italy-it.xml",
               r"^https://www\.adecco\.com/it-it/cerca-lavoro/[^?#]+$", False, "forbidden",
               "Terms forbid extracting or reusing any part of the site by any means or procedure.",
               "https://www.adecco.com/it-it/terms-of-use"),
    AgencySpec("gigroup", "Gi Group", "https://www.gigroup.it/offerte-sitemap.xml",
               r"^https://www\.gigroup\.it/offerte-lavoro-dettaglio/[^?#]+/?$", False, "forbidden",
               "Terms forbid extracting content by any means or procedure beyond personal use.",
               "https://www.gigroup.it/condizioni-di-utilizzo/"),
    AgencySpec("umana", "Umana", "https://www.umana.it/jobs-sitemap.xml",
               r"^https://www\.umana\.it/[^?#]+$", False, "forbidden",
               "Terms forbid extraction by any means, including automatic, without written consent.",
               "https://www.umana.it/condizioni-d-uso-del-sito/", branch_in_street=True),
)


def _slug_words(url: str) -> list[str]:
    path = unquote(urlsplit(url).path)
    return [w for w in re.split(r"[^a-z0-9]+", fold(path)) if w]


class AgencySitemapSource(JobSource):
    """One agency: sitemap -> slug pre-filter -> JSON-LD of the best candidate pages."""

    def __init__(self, spec: AgencySpec, *, max_pages: int = MAX_PAGES):
        self.spec = spec
        self.id = f"agency:{spec.id}"
        self.label = spec.name
        self.enabled_by_default = spec.allowed_by_terms
        self.allowed_by_terms = spec.allowed_by_terms
        self.terms_verdict = spec.terms_verdict
        self.terms_note = spec.terms_note
        self.terms_url = spec.terms_url
        self.terms_checked = spec.terms_checked
        self.hosts = (spec.host,)
        self.max_pages = max_pages
        self._pattern = re.compile(spec.posting_pattern, re.I)

    def estimate(self, query) -> int:
        return 1 + self.max_pages

    # --- sitemap

    def _download(self, ctx: SourceContext, url: str, cached: dict | None) -> dict | None:
        headers = {}
        if cached:
            if cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                headers["If-Modified-Since"] = cached["last_modified"]
        reply = ctx.get(url, headers=headers)
        if reply.status == 304 and cached:
            return cached
        if reply.status != 200:
            ctx.error(f"sitemap HTTP {reply.status}")
            return None
        kind, entries = parse_sitemap(reply.text)
        lower = {k.lower(): v for k, v in (reply.headers or {}).items()}
        return {"kind": kind, "entries": entries, "etag": lower.get("etag", ""),
                "last_modified": lower.get("last-modified", "")}

    def sitemap_entries(self, ctx: SourceContext) -> list[tuple[str, str]]:
        """Posting URLs of the agency (cached a day; then a conditional request)."""
        key = self.spec.sitemap_url
        fresh = ctx.cache.get("sitemap", key, DAY_S)
        if fresh is not None:
            ctx.count_request(from_cache=True)
            return [tuple(e) for e in fresh.get("entries", [])]
        stale = ctx.cache.get("sitemap", key)
        if not ctx.robots_allows(key):
            ctx.error("sitemap disallowed by robots.txt")
            return [tuple(e) for e in (stale or {}).get("entries", [])]
        data = self._download(ctx, key, stale)
        if data is None:
            return [tuple(e) for e in (stale or {}).get("entries", [])]
        entries = list(data.get("entries", []))
        if data.get("kind") == "index":
            children = [loc for loc, _ in entries
                        if not self.spec.child_sitemap_pattern or re.search(self.spec.child_sitemap_pattern, loc)]
            entries = []
            for child in children[:MAX_CHILD_SITEMAPS]:
                ctx.check()
                sub = self._download(ctx, child, None)
                if sub is not None:
                    entries.extend(sub.get("entries", []))
        stored = {"entries": [list(e) for e in entries], "etag": data.get("etag", ""),
                  "last_modified": data.get("last_modified", "")}
        ctx.cache.put("sitemap", key, stored)
        return [tuple(e) for e in entries]

    # --- slug pre-filter

    def _slug_town(self, url: str, words: list[str], used: set[str], lookup) -> tuple | None:
        if self.spec.slug_town == "underscore":
            parts = [p for p in unquote(urlsplit(url).path).strip("/").split("_") if p]
            if len(parts) >= 3:
                name = fold(parts[-2]).replace("-", " ")
                found = lookup(name)
                if found:
                    return found
            return None
        free = [w for w in words if w not in used and not w.isdigit() and len(w) > 2]
        for size in (3, 2, 1):
            for start in range(len(free) - size, -1, -1):
                found = lookup(" ".join(free[start:start + size]))
                if found:
                    return found
        return None

    def rank_candidates(self, entries, query, place_lookup=None) -> list[str]:
        """Posting URLs worth opening, best first (role words in the slug, then town)."""
        groups = query.word_groups()
        skill_groups = query.skill_groups()
        phrases = [p.replace(" ", "") for p in query.phrases]
        scored = []
        for loc, lastmod in entries:
            if not self._pattern.match(loc):
                continue
            words = _slug_words(loc)
            joined = "".join(words)
            used: set[str] = set()
            role_hits = 0
            for group in groups:
                forms = [f.replace(" ", "") for f in group if len(f.replace(" ", "")) >= 3]
                hit = next((f for f in forms if f in words or (len(f) >= 4 and f in joined)), None)
                if hit:
                    role_hits += 1
                    used.add(hit)
            skill_hits = 0
            for group in skill_groups:
                forms = [f.replace(" ", "") for f in group if len(f.replace(" ", "")) >= 3]
                hit = next((f for f in forms if f in words or (len(f) >= 4 and f in joined)), None)
                if hit:
                    skill_hits += 1
                    used.add(hit)
            if any(e in words for e in query.excluded):
                continue
            if groups and role_hits == 0 and skill_hits == 0:
                continue
            if not groups and skill_groups and skill_hits == 0:
                continue
            if phrases and not all(p in joined for p in phrases):
                score_phrase = 0
            else:
                score_phrase = 1
            stamp = parse_date(lastmod)
            scored.append([2 * role_hits + skill_hits + score_phrase, stamp.toordinal() if stamp else 0, loc,
                           words, used])
        scored.sort(key=lambda s: (-s[0], -s[1]))
        if query.lat is not None and query.lon is not None and query.radius_km:
            def lookup(name: str):
                if place_lookup is not None:
                    try:
                        found = place_lookup(name)
                        if found:
                            return found
                    except Exception:
                        pass
                return lexicon.builtin_place(name)

            kept = []
            for index, item in enumerate(scored):
                if index < PLACE_CHECKS:
                    town = self._slug_town(item[2], item[3], item[4], lookup)
                    if town is not None:
                        distance = haversine_km(query.lat, query.lon, float(town[0]), float(town[1]))
                        if distance > query.radius_km + SLUG_TOWN_SLACK_KM:
                            continue
                        item[0] += 3
                kept.append(item)
            kept.sort(key=lambda s: (-s[0], -s[1]))
            scored = kept
        return [item[2] for item in scored]

    # --- postings

    def _posting(self, ctx: SourceContext, url: str) -> Posting | None:
        cached = ctx.cache.get(POSTING_CACHE, url)
        if cached is not None:
            ctx.count_request(from_cache=True)
            return Posting.from_dict(cached["posting"]) if cached.get("posting") else None
        if not ctx.robots_allows(url):
            ctx.error("posting disallowed by robots.txt")
            return None
        reply = ctx.get(url)
        if reply.status != 200:
            if reply.status not in (404, 410):  # a removed ad is not an error
                ctx.error(f"posting HTTP {reply.status}")
            else:
                ctx.cache.put(POSTING_CACHE, url, {"posting": None},
                              expires=datetime.now(timezone.utc).timestamp() + DAY_S)
            return None
        found = postings_from_html(reply.text, url, source=self.id, agency=self.spec.name,
                                   branch_in_street=self.spec.branch_in_street, today=ctx.today)
        posting = found[0] if found else None
        if posting is not None and not posting.url.startswith("http"):
            posting.url = url
        expires = datetime.now(timezone.utc).timestamp() + POSTING_TTL_S
        valid = parse_date(posting.valid_through) if posting else None
        if valid is not None:
            expires = min(expires + 60 * DAY_S,
                          datetime.combine(valid + timedelta(days=1), datetime.min.time(),
                                           tzinfo=timezone.utc).timestamp())
        ctx.cache.put(POSTING_CACHE, url, {"posting": posting.to_dict() if posting else None}, expires=expires)
        return posting

    def run(self, ctx: SourceContext) -> list[Posting]:
        if not self.allowed_by_terms:
            return []
        query = ctx.query
        try:
            entries = self.sitemap_entries(ctx)
        except SourceCancelled:
            raise
        except Exception as error:
            ctx.error(f"sitemap: {type(error).__name__}")
            return []
        ctx.step(self.label)
        candidates = self.rank_candidates(entries, query, ctx.place_lookup)
        result: list[Posting] = []
        for url in candidates[:self.max_pages]:
            ctx.check()
            try:
                posting = self._posting(ctx, url)
            except SourceCancelled:
                raise
            except Exception as error:
                ctx.error(f"posting: {type(error).__name__}")
                posting = None
            ctx.step(self.label)
            if posting is None:
                continue
            if posting.valid_through and ctx.today and (parse_date(posting.valid_through) or ctx.today) < ctx.today:
                continue
            result.append(posting)
            ctx.add([posting])
        log.info("Agency %s: %d candidates, %d postings read", self.spec.id, len(candidates), len(result))
        return result


def agency_sources(*, max_pages: int = MAX_PAGES) -> list[AgencySitemapSource]:
    return [AgencySitemapSource(spec, max_pages=max_pages) for spec in AGENCY_SPECS]
