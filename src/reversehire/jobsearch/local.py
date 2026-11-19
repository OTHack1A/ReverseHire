"""Postings already collected by the company searches (in the store): no network.

The caller passes an iterable of items, each one of:
- a jobsearch `Posting`;
- a card posting (`reversehire.models.Posting`) or its dict;
- a pair `(company_name, posting)`;
- a dict {"company": name, "posting": posting, "lat": .., "lon": .., "town": ..}.
"""

from __future__ import annotations

from .base import JobSource, SourceContext
from .models import Posting
from .normalize import finish


def _convert(item) -> Posting | None:
    if isinstance(item, Posting):
        return item
    company, extra = None, {}
    if isinstance(item, tuple) and len(item) == 2:
        company, item = item
    elif isinstance(item, dict) and "posting" in item:
        company = item.get("company")
        extra = item
        item = item["posting"]
    if isinstance(item, Posting):
        posting = item
    else:
        posting = Posting.from_model_posting(item, company=str(company) if company else None)
    if not posting.title or not posting.url:
        return None
    # The company's coordinates stand for the workplace only when the posting names no
    # place of its own: a posting of a Bologna company may be in Rome ("4 Locations",
    # "Milano"), and must then be located from its own text (or stay unknown).
    own_place = bool((posting.location or "").strip() or posting.town or posting.province)
    for name in ("lat", "lon", "town", "province"):
        if own_place and name in ("lat", "lon"):
            continue
        if extra.get(name) is not None and getattr(posting, name) is None:
            setattr(posting, name, extra[name])
    if posting.lat is not None and posting.geo_quality is None:
        posting.geo_quality = "company"
    if not posting.source or posting.source in ("jsonld", "html", "local"):
        posting.source = "local:" + (posting.source or "card")
    return finish(posting)


class LocalPostingsSource(JobSource):
    id = "local"
    label = "Postings already found by company searches"
    terms_verdict = "allowed"
    terms_note = "Data already on this computer."

    def __init__(self, items):
        self.items = items

    def run(self, ctx: SourceContext) -> list[Posting]:
        result = []
        for item in self.items or []:
            if ctx.cancelled():
                break
            try:
                posting = _convert(item)
            except Exception as error:
                ctx.error(f"item: {type(error).__name__}")
                continue
            if posting is not None:
                result.append(posting)
        ctx.add(result)
        ctx.step(self.label)
        return result
