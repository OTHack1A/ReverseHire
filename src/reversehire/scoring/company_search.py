"""Semantic search over the saved company cards ("My companies").

Each card becomes a few short text blocks (a header with name, sectors and
tags, then description, notes and job titles, split with
``split_into_blocks``). The blocks are embedded as "passage" and the query as
"query" (the e5 retrieval convention); a card's score is its best block's
cosine similarity, mapped to 0..100 with the model's ``similarity_range``
(``relevance.calibrate_similarity``). A lexical score (rapidfuzz on name,
tags and sectors) is combined with ``max`` so that a typed company name
always surfaces, even for names the model knows nothing about.

Brute-force numpy on all blocks: the cards are dozens to a few thousand and
the embedder caches each block's vector in the Store, so repeated searches
only embed the query. If the model cannot be loaded the search falls back to
the lexical score plus a plain substring match.
"""

from __future__ import annotations

import logging

import numpy as np
from rapidfuzz import fuzz

from .embedding import split_into_blocks, truncate
from .relevance import calibrate_similarity, normalize_text, range_of

log = logging.getLogger(__name__)

QUERY_KIND = "query"
CARD_TEXT_KIND = "passage"
CHARS_PER_BLOCK = 300
MAX_BLOCKS_PER_CARD = 12
MAX_POSTING_TITLES = 15
MAX_SIMILAR_QUERY_CHARS = 1000

LEXICAL_MIN = 85  # lexical scores below this do not count
MIN_LEXICAL_QUERY = 3  # shorter queries: no lexical boost
SUBSTRING_SCORE = 60.0  # fallback without a model: query found in the card text


# --- card text ---------------------------------------------------------------


def _header(card) -> str:
    parts = [card.name]
    if card.sectors:
        parts.append(", ".join(card.sectors))
    if card.tags:
        parts.append(", ".join(card.tags))
    return ". ".join(p for p in parts if p)


def card_blocks(card) -> list[str]:
    """Searchable blocks of a card: header first, then the longer texts."""
    blocks = [_header(card)]
    body = [card.description, card.notes]
    titles = card.posting_titles[:MAX_POSTING_TITLES]
    if titles:
        body.append("; ".join(titles) + ".")
    for text in body:
        for block in split_into_blocks(text or "", CHARS_PER_BLOCK):
            blocks.append(block)
    return [b for b in blocks if b.strip()][:MAX_BLOCKS_PER_CARD]


def card_search_text(card) -> str:
    """The whole searchable text of a card, in one string."""
    return "\n".join(card_blocks(card))


def _passage(card, block: str, index: int) -> str:
    # Body blocks carry the name, so a block about "our services" keeps its context.
    return block if index == 0 else f"{card.name}: {block}"


# --- lexical score -------------------------------------------------------------


def _fuzzy(query: str, target: str) -> float:
    if not query or not target:
        return 0.0
    if len(query) <= len(target) + 2:
        return float(fuzz.partial_ratio(query, target))
    return float(fuzz.ratio(query, target))


def lexical_score(query: str, card) -> tuple[float, str]:
    """(0..100, matched field) of the query against name, tags and sectors."""
    q = normalize_text(query)
    if len(q) < MIN_LEXICAL_QUERY:
        return 0.0, ""
    best, matched = 0.0, ""
    for target in [card.name, *card.tags, *card.sectors]:
        score = _fuzzy(q, normalize_text(target))
        if score > best:
            best, matched = score, target
    if best < LEXICAL_MIN:
        return 0.0, ""
    return best, matched


def _substring_score(query: str, card) -> tuple[float, str]:
    q = normalize_text(query)
    if len(q) < MIN_LEXICAL_QUERY:
        return 0.0, ""
    for block in card_blocks(card):
        if q in normalize_text(block):
            return SUBSTRING_SCORE, block
    return 0.0, ""


# --- semantic score ------------------------------------------------------------


def _best_blocks(cards, query_vector: np.ndarray, embedder) -> list[tuple[float, str]] | None:
    """(best raw cosine, best block) for every card, or None if embedding fails."""
    owners: list[int] = []
    blocks: list[str] = []
    passages: list[str] = []
    for i, card in enumerate(cards):
        for j, block in enumerate(card_blocks(card)):
            owners.append(i)
            blocks.append(block)
            passages.append(_passage(card, block, j))
    best: list[tuple[float, str]] = [(-1.0, "") for _ in cards]
    if not passages:
        return best
    try:
        matrix = np.asarray(embedder.embed(passages, kind=CARD_TEXT_KIND))
    except Exception as error:  # model missing or not downloadable
        log.warning("Card search without embeddings: %s", error)
        return None
    sims = matrix @ query_vector
    for owner, block, sim in zip(owners, blocks, sims):
        if sim > best[owner][0]:
            best[owner] = (float(sim), block)
    return best


def _embed_query(text: str, embedder) -> np.ndarray | None:
    try:
        return np.asarray(embedder.embed([truncate(text)], kind=QUERY_KIND))[0]
    except Exception as error:
        log.warning("Card search without embeddings: %s", error)
        return None


def _rank(results, limit: int, min_score: float | None):
    if min_score is not None:
        results = [r for r in results if r[1] >= min_score]
    # Stable: equal scores keep the order of the input cards.
    results.sort(key=lambda r: -r[1])
    return results[: max(0, limit)]


def search_cards(
    cards: list,
    query: str,
    embedder,
    *,
    limit: int = 50,
    min_score: float | None = None,
) -> list[tuple[object, float, str]]:
    """Cards ranked by meaning against ``query``: [(card, score 0..100, snippet)].

    The snippet is the card block that matched best (or the name/tag that
    matched lexically). An empty query returns the first ``limit`` cards in
    their order with score 0 and no snippet (``min_score`` is ignored).
    """
    cards = list(cards)
    query = " ".join((query or "").split())
    if not cards:
        return []
    if not query:
        return [(card, 0.0, "") for card in cards[: max(0, limit)]]

    interval = range_of(embedder)
    q = _embed_query(query, embedder)
    best = _best_blocks(cards, q, embedder) if q is not None and np.any(q) else None

    results = []
    for i, card in enumerate(cards):
        lexical, field = lexical_score(query, card)
        if best is not None:
            raw, block = best[i]
            semantic = calibrate_similarity(raw, interval) if block else 0.0
        else:
            semantic, block = _substring_score(query, card)
        if lexical > semantic:
            results.append((card, round(lexical, 1), field))
        else:
            results.append((card, round(semantic, 1), block))
    return _rank(results, limit, min_score)


def similar_cards(cards: list, card, embedder, limit: int = 10) -> list[tuple[object, float, str]]:
    """Cards most similar to ``card`` (itself excluded): [(card, score, snippet)]."""
    others = [c for c in cards if c is not card and (c.id, c.key) != (card.id, card.key)]
    if not others:
        return []
    text = truncate(card_search_text(card), MAX_SIMILAR_QUERY_CHARS)
    q = _embed_query(text, embedder)
    if q is None or not np.any(q):
        return []
    best = _best_blocks(others, q, embedder)
    if best is None:
        return []
    interval = range_of(embedder)
    results = [
        (other, round(calibrate_similarity(raw, interval), 1), block)
        for other, (raw, block) in zip(others, best)
        if block
    ]
    return _rank(results, limit, None)
