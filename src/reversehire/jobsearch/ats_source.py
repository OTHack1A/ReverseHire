""""Who among my companies is hiring for this role": the public job boards (ATS APIs
already supported by `web/ats.py`: Greenhouse, Lever, Ashby, Workable, Recruitee,
SmartRecruiters, Personio, Workday CXS, BambooHR) and the JobPosting JSON-LD of the
careers page, for the companies the user already has in "My companies" / favourites.

Only the boards and careers pages already found on the company cards are read (no
guessing of slugs here); careers pages only when robots.txt allows them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..web import ats as web_ats
from ._util import log
from .base import JobSource, SourceCancelled, SourceContext
from .models import Posting
from .normalize import finish

BOARD_TTL_S = 6 * 3600
MAX_TARGETS = 60


@dataclass
class CompanyTarget:
    """A company of "My companies" with the URLs where its postings may be."""

    name: str
    site: str | None = None
    urls: list[str] = field(default_factory=list)   # careers page, ATS board links, postings source
    careers_url: str | None = None
    card_id: int | None = None
    lat: float | None = None
    lon: float | None = None
    town: str | None = None


def targets_from_cards(cards) -> list[CompanyTarget]:
    """CompanyTarget list from CompanyCard objects (or their dicts), duck-typed."""
    targets = []
    for card in cards or []:
        def get(name, default=None):
            if isinstance(card, dict):
                return card.get(name, default)
            return getattr(card, name, default)
        ats_info = get("ats") or {}
        hiring = get("hiring") or {}
        careers = (ats_info.get("careers_url") if isinstance(ats_info, dict) else None) or \
            (hiring.get("careers_url") if isinstance(hiring, dict) else None) or None
        urls = []
        if isinstance(ats_info, dict):
            urls += [p.get("url", "") for p in ats_info.get("platforms") or [] if isinstance(p, dict)]
        if isinstance(hiring, dict):
            urls += [hiring.get("postings_source") or "", hiring.get("careers_url") or ""]
        urls += [careers or ""]
        name = str(get("name") or "").strip()
        if not name:
            continue
        targets.append(CompanyTarget(name=name, site=get("site") or get("website") or None,
                                     urls=[u for u in dict.fromkeys(urls) if u], careers_url=careers,
                                     card_id=get("id"), lat=get("lat"), lon=get("lon")))
    return targets


class _GatewayFetcher:
    """The `download_data(url, json_body=None)` interface of web/ats.py over the gateway."""

    def __init__(self, ctx: SourceContext):
        self.ctx = ctx

    def download_data(self, url: str, json_body=None) -> str | None:
        self.ctx.check()
        try:
            if json_body is not None:
                reply = self.ctx.request("POST", url, content=json.dumps(json_body),
                                         headers={"Content-Type": "application/json", "Accept": "application/json"})
            else:
                reply = self.ctx.get(url, headers={"Accept": "application/json, text/xml;q=0.9, */*;q=0.5"})
        except SourceCancelled:
            raise
        except Exception as error:
            self.ctx.error(f"board: {type(error).__name__}")
            return None
        return reply.text if reply.status == 200 else None


class AtsCompaniesSource(JobSource):
    id = "my_companies"
    label = "My companies (job boards and careers pages)"
    terms_verdict = "allowed"
    terms_note = "Public job-board APIs and careers pages of companies you saved; robots.txt honoured."

    def __init__(self, targets, *, max_targets: int = MAX_TARGETS):
        self.targets = list(targets or [])[:max_targets]

    def estimate(self, query) -> int:
        return max(1, len(self.targets))

    def _board(self, ctx: SourceContext, ref, roles) -> list:
        key = f"{ref.platform}|{ref.slug}|{ref.domain}|{ref.site}"
        cached = ctx.cache.get("ats_board", key, BOARD_TTL_S)
        if cached is not None:
            ctx.count_request(from_cache=True)
            return cached.get("postings", [])
        found = web_ats.fetch_board(ref, _GatewayFetcher(ctx), roles)
        postings = [p.to_dict() for p in found[1]] if found else []
        ctx.cache.put("ats_board", key, {"postings": postings})
        return postings

    def _careers(self, ctx: SourceContext, url: str) -> list:
        cached = ctx.cache.get("careers_page", url, BOARD_TTL_S)
        if cached is not None:
            ctx.count_request(from_cache=True)
            return cached.get("postings", [])
        if not ctx.robots_allows(url):
            ctx.error("careers page disallowed by robots.txt")
            return []
        reply = ctx.get(url)
        postings = []
        if reply.status == 200:
            from ..web.jsonld import extract_jobposting

            postings = [p.to_dict() for p in extract_jobposting(reply.text, url, ctx.today)]
        ctx.cache.put("careers_page", url, {"postings": postings})
        return postings

    def run(self, ctx: SourceContext) -> list[Posting]:
        query = ctx.query
        roles = [query.role] if getattr(query, "role", "") else None
        result: list[Posting] = []
        for target in self.targets:
            ctx.check()
            rows: list[dict] = []
            source = "ats"
            try:
                refs = web_ats.detect_ats(*target.urls)
                if refs:
                    rows = self._board(ctx, refs[0], roles)
                    source = f"ats:{refs[0].platform}"
                elif target.careers_url and not web_ats.is_unsupported_ats(target.careers_url):
                    rows = self._careers(ctx, target.careers_url)
                    source = "site"
            except SourceCancelled:
                raise
            except Exception as error:
                ctx.error(f"{type(error).__name__}")
            for row in rows:
                posting = Posting.from_model_posting(row, company=target.name, source=source)
                if not posting.url or not posting.title:
                    continue
                if target.lat is not None and target.lon is not None and not posting.lat:
                    posting.lat, posting.lon, posting.geo_quality = target.lat, target.lon, "company"
                finish(posting)
                result.append(posting)
                ctx.add([posting])
            ctx.step(target.name)
        log.info("My companies: %d postings from %d companies", len(result), len(self.targets))
        return result
