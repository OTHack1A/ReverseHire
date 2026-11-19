"""Ranking by meaning of postings and companies for a job search, hybrid with the exact
words: the engine of the app's lists (`scoring/semantic_index.py`: e5 passages and
queries, cached vectors, exact-phrase and all-words tiers above meaning-only hits).

    hits, mode = rank_by_meaning(query, documents, embedder=embedder, store=store)

`query` is a SearchQuery: it is searched as the role typed, each of its alternatives
("OT/Cybersecurity": "ot" and "cybersecurity" separately) and the first terms of
"Searched also for" (synonyms, IT <-> EN, corrected typos); a document keeps its best hit.
Without the model (no embedder, or the reduced lexical one) `mode` is "keyword": only the
exact and fuzzy words and the synonyms count, which the interface says plainly.
"""

from __future__ import annotations

from ._util import CancelCheck, fold, log

MAX_QUERY_TEXTS = 8
MEANING_KEEP = 62.0     # calibrated similarity 0..100 from which a posting is kept by meaning alone
POSTING_PASSAGES = 4    # title, skills and the start of the description are enough to rank


def query_texts(query) -> list[str]:
    """The texts searched for a query (see the module docstring)."""
    texts: list[str] = []

    def add(value) -> None:
        value = " ".join(fold(value).split())
        if value and value not in texts:
            texts.append(value)

    add(getattr(query, "role", "") or " ".join(getattr(query, "skills", []) or []))
    for group in getattr(query, "alternatives", None) or []:
        for word in group:
            add(word)
    for phrase in getattr(query, "phrases", None) or []:
        add(phrase)
    try:
        terms = [term for term, _kind in query.also_terms()]
    except Exception:  # noqa: BLE001 - a query without understanding fields
        terms = []
    for term in terms:
        if len(texts) >= MAX_QUERY_TEXTS:
            break
        add(term)
    return texts[:MAX_QUERY_TEXTS]


def posting_documents(postings) -> list:
    """Short documents of the postings (key "p<index>")."""
    from ..scoring.semantic_index import posting_document

    documents = []
    for index, posting in enumerate(postings):
        document = posting_document(posting, key=f"p{index}")
        # the job's own text only: the publisher's name or the place say nothing of the role
        document.passages = [p for p in document.passages if p.source in ("posting", "sector")][:POSTING_PASSAGES]
        documents.append(document)
    return documents


def rank_by_meaning(query, documents, *, embedder=None, store=None, cancel=None) -> tuple[dict, str]:
    """({document key: best Hit}, mode). Never raises: on any failure the hits found so
    far are returned with the mode "keyword"."""
    from ..scoring.semantic_index import MODE_KEYWORD, SemanticIndex

    is_cancelled = CancelCheck(cancel)
    best: dict = {}
    documents = list(documents or [])
    if not documents:
        return best, MODE_KEYWORD if embedder is None else "semantic"
    index = SemanticIndex(embedder, store)
    try:
        index.build(documents, cancel=cancel if hasattr(cancel, "is_set") else None)
        for text in query_texts(query):
            if is_cancelled():
                break
            for hit in index.search(text):
                current = best.get(hit.key)
                if current is None or (hit.tier, hit.relevance) > (current.tier, current.relevance):
                    best[hit.key] = hit
        return best, index.mode
    except Exception as error:  # noqa: BLE001 - the exact words still rank
        log.warning("Ranking by meaning failed: %s", type(error).__name__)
        return best, MODE_KEYWORD


def kept_by_meaning(hit) -> bool:
    """A posting the exact words dropped but whose meaning is close enough to keep."""
    return hit is not None and float(getattr(hit, "semantic", 0.0) or 0.0) >= MEANING_KEEP
