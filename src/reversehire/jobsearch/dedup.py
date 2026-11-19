"""The same ad seen on several sources (agency site, Adzuna, Jooble, a company card...).

Rules (RECRUITER_STUDY §2.4 step 4): same source + id or same URL; else same
de-duplication key (folded title + town/province + publisher) with dates within
±10 days; else very similar titles in the same place whose descriptions share at least
80% of their 5-word shingles. The richest record is kept; the others are listed in
`also_on` ({"source", "url"}).
"""

from __future__ import annotations

from ._util import fold, parse_date, words
from .models import Posting, title_key

DATE_WINDOW_DAYS = 10
SHINGLE_SIZE = 5
SHINGLE_JACCARD = 0.8
TITLE_SIMILARITY = 90


def _richness(posting: Posting) -> float:
    filled = sum(1 for name in ("company", "town", "lat", "contract", "remote", "salary", "date_posted",
                                "valid_through", "hours") if getattr(posting, name) not in (None, ""))
    direct = 1 if posting.company else 0  # an ad naming the employer is worth more
    return min(len(posting.description), 4000) / 100 + 3 * filled + 5 * direct


def _shingles(text: str) -> set[str]:
    tokens = words(text)
    return {" ".join(tokens[i:i + SHINGLE_SIZE]) for i in range(max(0, len(tokens) - SHINGLE_SIZE + 1))}


def _close_dates(a: Posting, b: Posting) -> bool:
    da, db = parse_date(a.date_posted), parse_date(b.date_posted)
    if da is None or db is None:
        return True
    return abs((da - db).days) <= DATE_WINDOW_DAYS


def _place(posting: Posting) -> str:
    return fold(posting.town or posting.province or posting.location.split(",")[0])


def _similar_titles(a: str, b: str) -> bool:
    try:
        from rapidfuzz import fuzz

        return fuzz.token_set_ratio(a, b) >= TITLE_SIMILARITY
    except Exception:
        return a == b


def _merge(keep: Posting, other: Posting) -> None:
    entry = {"source": other.source, "url": other.url}
    if entry not in keep.also_on and other.url != keep.url:
        keep.also_on.append(entry)
    for extra in other.also_on:
        if extra not in keep.also_on and extra.get("url") != keep.url:
            keep.also_on.append(extra)
    for name in ("company", "lat", "lon", "town", "province", "contract", "remote", "hours", "salary",
                 "salary_min", "salary_max", "salary_period", "salary_currency", "date_posted", "valid_through"):
        if getattr(keep, name) in (None, "") and getattr(other, name) not in (None, ""):
            setattr(keep, name, getattr(other, name))
    if keep.lat is not None and keep.geo_quality is None:
        keep.geo_quality = other.geo_quality


def deduplicate(postings: list[Posting]) -> tuple[list[Posting], int]:
    """(unique postings in their first-seen order, number merged away)."""
    groups: list[list[Posting]] = []
    by_id: dict[tuple, int] = {}
    by_url: dict[str, int] = {}
    by_key: dict[str, list[int]] = {}
    shingles: list[set[str]] = []
    for posting in postings:
        index = None
        if posting.source_id:
            index = by_id.get((posting.source.split(":")[0], posting.source_id))
        if index is None and posting.url:
            index = by_url.get(posting.url.rstrip("/"))
        key = posting.dedup_key
        if index is None:
            for candidate in by_key.get(key, []):
                if _close_dates(groups[candidate][0], posting):
                    index = candidate
                    break
        own = None
        if index is None and len(posting.description) > 200:
            own = _shingles(posting.description)
            title = title_key(posting.title)
            place = _place(posting)
            for candidate, group in enumerate(groups):
                first = group[0]
                if place and _place(first) and place != _place(first):
                    continue
                if not _similar_titles(title, title_key(first.title)):
                    continue
                other = shingles[candidate]
                if own and other and len(own & other) / len(own | other) >= SHINGLE_JACCARD:
                    index = candidate
                    break
        if index is None:
            index = len(groups)
            groups.append([posting])
            shingles.append(own if own is not None else _shingles(posting.description))
        else:
            groups[index].append(posting)
        if posting.source_id:
            by_id[(posting.source.split(":")[0], posting.source_id)] = index
        if posting.url:
            by_url[posting.url.rstrip("/")] = index
        by_key.setdefault(key, [])
        if index not in by_key[key]:
            by_key[key].append(index)
    result = []
    merged = 0
    for group in groups:
        keep = max(group, key=_richness)
        for other in group:
            if other is not keep:
                _merge(keep, other)
                merged += 1
        result.append(keep)
    return result, merged
