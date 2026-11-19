"""Meaning-based search over the companies and postings the app knows (one engine for
every list: My companies, Favourites, Results, Job search results, Previous searches).

Why a new engine (diagnosis of the old "My companies" search, scoring/company_search.py):
the e5 model was loaded and used correctly (query/passage prefixes, calibrated scores),
but a card was indexed from its name, OSM sectors, tags, the "closest website text"
description, notes and posting TITLES only. Everything a full card collects later —
the presentation and history sentences of the site, products and services, the ATECO
title, the reasons of the assessment, posting descriptions — was never embedded. A
networking / cybersecurity integrator whose only indexed text was its name and the
posting title "SALES" therefore scored 26 for "cybersecurity", below a company whose
description merely mentioned the word. There was also no exact-word guarantee: a
technology name typed by the user could rank below an unrelated semantic neighbour.

How this engine works:
- documents: `card_document`, `company_document`, `posting_document`,
  `search_document` turn every object into short passages (sentences grouped up to
  PASSAGE_CHARS), each with its source ("presentation", "products", "posting: …",
  "note"…), shown with the hit;
- embeddings: passages as e5 "passage", the query (and its IT<->EN variants) as
  "query"; every vector is cached in memory and in the Store (namespace CACHE_NAMESPACE,
  key = sha256(model, kind, text)), so a changed card only re-embeds the passages whose
  text changed and a repeated search embeds only the query;
- hybrid ranking in tiers: the whole query phrase found as words in a passage (or one
  of its IT<->EN variants) -> PHRASE tier, 75..100 %; all the query's words in one
  passage -> ALL_WORDS tier, 60..85 %; otherwise meaning only -> 0..59 %. So an exact
  hit (a technology name, a word) never ranks below an unrelated semantic neighbour;
- relevance 0..100 %: the best passage's cosine mapped linearly over the model's
  typical range (`embedder.similarity_range`, e5-small 0.80..0.88: unrelated texts
  ~0.80, same topic ~0.87; see scoring/embedding.py), then placed in its tier;
- reduced ("keyword") mode: without the model (no embedder, or the lexical fallback
  of ResilientEmbedder) the semantic part is the n-gram similarity and `mode` is
  "keyword", which the interface says plainly.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Iterable

import numpy as np

log = logging.getLogger(__name__)

CACHE_NAMESPACE = "semantic_v1"
PASSAGE_CHARS = 280
MAX_PASSAGES = 60           # per document
MAX_POSTINGS = 30
MIN_QUERY_CHARS = 2
MATCH_THRESHOLD = 35.0      # below this, a meaning-only hit does not "match" (lists hide it)
SNIPPET_CHARS = 220

PHRASE_FLOOR, PHRASE_SPAN = 75.0, 25.0
ALL_WORDS_FLOOR, ALL_WORDS_SPAN = 60.0, 25.0
SEMANTIC_CAP = 59.0
PARTIAL_WORD_BONUS = 4.0    # each query word found (meaning tier only), capped by SEMANTIC_CAP

TIER_PHRASE, TIER_ALL_WORDS, TIER_SEMANTIC = 2, 1, 0
MODE_SEMANTIC, MODE_KEYWORD = "semantic", "keyword"

_STOP = frozenset("""di del della dei delle e ed in per con da il la lo le gli un una of and the for with to a an
at on or o al alla nel nella su sul sulla""".split())

# IT <-> EN terms of companies and technologies (folded). Used both ways.
_IT_EN = [
    ("sicurezza informatica", "cybersecurity"), ("sicurezza informatica", "cyber security"),
    ("sicurezza informatica", "information security"), ("cybersicurezza", "cybersecurity"),
    ("sicurezza delle reti", "network security"), ("reti", "networking"), ("rete", "network"),
    ("automazione", "automation"), ("automazione industriale", "industrial automation"),
    ("intelligenza artificiale", "artificial intelligence"), ("apprendimento automatico", "machine learning"),
    ("sviluppo software", "software development"), ("sviluppo", "development"),
    ("software gestionale", "management software"), ("gestionale", "erp"),
    ("contabilita", "accounting"), ("paghe", "payroll"), ("risorse umane", "human resources"),
    ("logistica", "logistics"), ("magazzino", "warehouse"), ("trasporti", "transport"),
    ("meccanica", "mechanical engineering"), ("meccanica di precisione", "precision engineering"),
    ("elettronica", "electronics"), ("energia", "energy"), ("energie rinnovabili", "renewable energy"),
    ("fotovoltaico", "photovoltaic"), ("telecomunicazioni", "telecommunications"),
    ("nuvola", "cloud"), ("archiviazione", "storage"), ("centro dati", "data center"),
    ("consulenza", "consulting"), ("formazione", "training"), ("vendite", "sales"),
    ("commerciale", "sales"), ("assistenza tecnica", "technical support"), ("manutenzione", "maintenance"),
    ("progettazione", "design"), ("ingegneria", "engineering"), ("qualita", "quality"),
    ("alimentare", "food"), ("imballaggio", "packaging"), ("stampa", "printing"),
    ("edilizia", "construction"), ("sanita", "healthcare"), ("farmaceutico", "pharmaceutical"),
    ("ricerca e sviluppo", "research and development"), ("analisi dei dati", "data analysis"),
    ("banca dati", "database"), ("sito web", "website"), ("commercio elettronico", "e-commerce"),
]


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or "").casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9+#.\-]*[a-z0-9+#]|[a-z0-9]", fold(text))


def _content_words(text: str) -> list[str]:
    return [w for w in words(text) if w not in _STOP]


def _build_lexicon() -> dict[str, set[str]]:
    lexicon: dict[str, set[str]] = {}
    for a, b in _IT_EN:
        lexicon.setdefault(a, set()).add(b)
        lexicon.setdefault(b, set()).add(a)
    try:  # the job-term dictionary of the relevance score
        from .relevance import SYNONYMS

        for key, values in SYNONYMS.items():
            lexicon.setdefault(key, set()).update(values)
    except Exception:  # noqa: BLE001
        pass
    return lexicon


_LEXICON = _build_lexicon()


def expand_query(query: str) -> list[str]:
    """The query and its IT<->EN variants (whole-phrase translations first, then the
    phrase with one known term replaced). Folded, without duplicates."""
    base = " ".join(words(query))
    if not base:
        return []
    variants = [base]
    for other in sorted(_LEXICON.get(base, ())):
        if other not in variants:
            variants.append(other)
    for term, others in _LEXICON.items():
        if term == base or not re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", base):
            continue
        for other in sorted(others):
            variant = re.sub(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", other, base)
            if variant not in variants:
                variants.append(variant)
    return variants[:8]


def _phrase_in(phrase: str, folded: str) -> re.Match | None:
    if not phrase:
        return None
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", folded)


# --- documents ------------------------------------------------------------------------------

@dataclass
class Passage:
    text: str
    source: str          # "name", "presentation", "products", "posting", "note"…
    label: str = ""      # e.g. the posting title

    def source_text(self) -> str:
        return f"{self.source}: {self.label}" if self.label else self.source


@dataclass
class Document:
    key: str
    title: str
    passages: list[Passage] = field(default_factory=list)


def split_sentences(text: str, max_chars: int = PASSAGE_CHARS) -> list[str]:
    """Sentences grouped into passages of at most ~max_chars (long ones cut at a space)."""
    text = str(text or "").replace("\r", "\n")
    out: list[str] = []
    current = ""
    for row in text.split("\n"):
        row = " ".join(row.split())
        if not row:
            continue
        for sentence in re.split(r"(?<=[.!?;])\s+", row):
            while len(sentence) > max_chars:
                cut = sentence.rfind(" ", 0, max_chars)
                cut = cut if cut > max_chars // 2 else max_chars
                if current:
                    out.append(current)
                    current = ""
                out.append(sentence[:cut].strip())
                sentence = sentence[cut:].strip()
            if not sentence:
                continue
            if current and len(current) + 1 + len(sentence) > max_chars:
                out.append(current)
                current = sentence
            else:
                current = f"{current} {sentence}".strip()
        if current:
            out.append(current)
            current = ""
    return [p for p in out if p.strip()]


def _add(passages: list[Passage], text, source: str, label: str = "") -> None:
    for part in split_sentences(text):
        if not any(p.text == part for p in passages):
            passages.append(Passage(part, source, label))


def _ateco_title(code: str) -> str:
    if not code:
        return ""
    try:
        from ..discovery.ateco import describe

        entry = describe(code)
    except Exception:  # noqa: BLE001
        entry = None
    if entry is None:
        return ""
    return " / ".join(x for x in (getattr(entry, "name_it", ""), getattr(entry, "name_en", "")) if x)


def _posting_passages(passages: list[Passage], postings: Iterable) -> None:
    for posting in list(postings)[:MAX_POSTINGS]:
        get = posting.get if isinstance(posting, dict) else (lambda k, d="", p=posting: getattr(p, k, d))
        title = str(get("title", "") or "")
        if title:
            _add(passages, title, "posting", title)
        _add(passages, get("description", "") or "", "posting", title)
        skills = get("skills", None) or []
        if skills:
            _add(passages, ", ".join(str(s) for s in skills), "posting", title)


def card_document(card) -> Document:
    """Every text of a saved company card."""
    passages: list[Passage] = []
    _add(passages, card.name, "name")
    if getattr(card, "legal_name", ""):
        _add(passages, card.legal_name, "name")
    if card.sectors:
        _add(passages, ", ".join(card.sectors), "sector")
    code = getattr(card, "ateco", "") or ""
    if not code and hasattr(card, "registry_value"):
        code = card.registry_value("ateco")
    ateco = _ateco_title(code)
    if ateco:
        _add(passages, ateco, "ateco")
    if card.tags:
        _add(passages, ", ".join(card.tags), "tag")
    presentation = getattr(card, "presentation", {}) or {}
    _add(passages, presentation.get("text", ""), "presentation")
    history = getattr(card, "history", {}) or {}
    _add(passages, history.get("text", ""), "history")
    products = getattr(card, "products", {}) or {}
    _add(passages, products.get("summary", ""), "products")
    items = [str(i.get("name", "")) for i in products.get("items", []) or [] if isinstance(i, dict) and i.get("name")]
    if items:
        _add(passages, ", ".join(items), "products")
    for name in ("technologies", "services"):
        values = products.get(name) or (getattr(card, "profile", {}) or {}).get(name) or []
        if isinstance(values, list) and values:
            _add(passages, ", ".join(str(v.get("name", v) if isinstance(v, dict) else v) for v in values),
                 "technology" if name == "technologies" else "products")
    _add(passages, _strip_description(card.description), "website")
    for reason in (card.assessment or {}).get("reasons", []) or []:
        quote = reason.get("quote") or reason.get("text") if isinstance(reason, dict) else None
        if quote:
            _add(passages, quote, "website")
    _posting_passages(passages, card.postings)
    _add(passages, card.notes, "note")
    osm = getattr(card, "osm_tags", {}) or {}
    _add(passages, osm.get("description", ""), "map")
    return Document(f"card:{card.id}", card.name, passages[:MAX_PASSAGES])


def _strip_description(text: str) -> str:
    """The stored description starts with a label ("Website text closest to the
    profile: “…”"): keep the quoted text."""
    text = str(text or "")
    quoted = re.findall(r"[“«\"]([^”»\"]{20,})[”»\"]", text)
    return "\n".join(quoted) if quoted else text


def company_document(company, evaluation=None, key: str | None = None) -> Document:
    """A company of a search result (pipeline Company + Assessment)."""
    passages: list[Passage] = []
    _add(passages, company.name, "name")
    sectors = getattr(company, "sectors", None) or []
    if sectors:
        _add(passages, ", ".join(sectors), "sector")
    ids = getattr(company, "ids", None) or {}
    ateco = _ateco_title(ids.get("ateco", "") if isinstance(ids, dict) else "")
    if ateco:
        _add(passages, ateco, "ateco")
    _add(passages, getattr(company, "description", ""), "website")
    _add(passages, getattr(company, "address", ""), "place")
    if evaluation is not None:
        for reason in getattr(evaluation, "reasons", None) or []:
            _add(passages, getattr(reason, "quote", "") or getattr(reason, "text", ""), "website")
        _posting_passages(passages, getattr(evaluation, "postings", None) or [])
    return Document(key or f"company:{company.name}", company.name, passages[:MAX_PASSAGES])


def posting_document(posting, key: str | None = None) -> Document:
    passages: list[Passage] = []
    _add(passages, posting.title, "posting", posting.title)
    who = getattr(posting, "publisher", "") or ""
    if who:
        _add(passages, who, "name")
    _add(passages, getattr(posting, "description", ""), "posting", posting.title)
    if getattr(posting, "skills", None):
        _add(passages, ", ".join(posting.skills), "posting", posting.title)
    if getattr(posting, "industry", None):
        _add(passages, posting.industry, "sector")
    _add(passages, getattr(posting, "location", ""), "place")
    return Document(key or f"posting:{posting.url}", posting.title, passages[:MAX_PASSAGES])


def search_document(search: dict, companies: Iterable[str] = ()) -> Document:
    """A saved search of "Previous searches" (place, CV, parameters, status)."""
    passages: list[Passage] = []
    _add(passages, search.get("place", ""), "place")
    cv = str(search.get("cv_path") or "").replace("\\", "/").rsplit("/", 1)[-1]
    _add(passages, cv, "cv")
    for name in ("sectors", "ateco", "roles"):
        value = search.get(name)
        if isinstance(value, (list, tuple)) and value:
            _add(passages, ", ".join(str(v) for v in value), name)
    _add(passages, search.get("status", ""), "status")
    names = [str(n) for n in companies if n]
    if names:
        _add(passages, ", ".join(names), "results")
    return Document(f"search:{search.get('id')}", str(search.get("place") or ""), passages)


# --- hits -----------------------------------------------------------------------------------

@dataclass
class Hit:
    key: str
    relevance: float            # 0..100
    tier: int
    passage: Passage | None
    semantic: float = 0.0       # 0..100 before tiering
    terms: list[str] = field(default_factory=list)  # folded words / phrases that matched

    @property
    def exact(self) -> bool:
        return self.tier > TIER_SEMANTIC

    @property
    def matches(self) -> bool:
        return self.exact or self.relevance >= MATCH_THRESHOLD

    @property
    def source(self) -> str:
        return self.passage.source_text() if self.passage else ""

    def snippet(self, limit: int = SNIPPET_CHARS) -> str:
        """The passage that matched, cut around the first matched term."""
        if self.passage is None:
            return ""
        text = self.passage.text
        if len(text) <= limit:
            return text
        start = 0
        span = self.spans(text)
        if span:
            start = max(0, span[0][0] - limit // 3)
        piece = text[start:start + limit]
        return ("…" if start else "") + piece + ("…" if start + limit < len(text) else "")

    def spans(self, text: str | None = None) -> list[tuple[int, int]]:
        """(start, end) of the matched terms in `text` (accent- and case-insensitive)."""
        text = self.snippet() if text is None else text
        folded = fold(text)
        if len(folded) != len(text):  # rare characters change length when folded: no highlight
            return []
        spans = []
        for term in sorted(self.terms, key=len, reverse=True):
            for match in re.finditer(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", folded):
                if not any(s <= match.start() < e for s, e in spans):
                    spans.append((match.start(), match.end()))
        return sorted(spans)

    def snippet_html(self, tag: str = "b") -> str:
        import html

        text = self.snippet()
        out, last = [], 0
        for start, end in self.spans(text):
            out.append(html.escape(text[last:start]))
            out.append(f"<{tag}>{html.escape(text[start:end])}</{tag}>")
            last = end
        out.append(html.escape(text[last:]))
        return "".join(out)


# --- the index ------------------------------------------------------------------------------

def _text_hash(model: str, kind: str, text: str) -> str:
    return hashlib.sha256(f"{model}\x1f{kind}\x1f{text}".encode("utf-8")).hexdigest()


def _calibrate(cosine: float, interval: tuple[float, float]) -> float:
    low, high = interval
    if high <= low:
        return 0.0
    return float(np.clip((cosine - low) / (high - low), 0.0, 1.0) * 100.0)


class SemanticIndex:
    """Documents and their passage vectors; `search(query)` -> ranked hits.

    Thread-safe for one builder and readers (the GUI builds and searches on a worker).
    """

    def __init__(self, embedder=None, store=None):
        self.embedder = embedder
        self.store = store
        self.documents: dict[str, Document] = {}
        self._vectors: dict[str, np.ndarray] = {}   # text hash -> vector
        self._lock = threading.RLock()
        self.failed = False                          # embedding failed: keyword mode
        self.embedded = 0                            # passages embedded by the model (not from a cache)

    # --- mode
    @property
    def model_name(self) -> str:
        return str(getattr(self.embedder, "model_name", "") or "model")

    @property
    def mode(self) -> str:
        if self.embedder is None or self.failed or bool(getattr(self.embedder, "reduced", False)):
            return MODE_KEYWORD
        return MODE_SEMANTIC

    # --- building
    def set_documents(self, documents: Iterable[Document]) -> None:
        with self._lock:
            self.documents = {d.key: d for d in documents}

    def build(self, documents: Iterable[Document] | None = None, *,
              progress: Callable[[int, int], None] | None = None, cancel=None) -> int:
        """Embeds every passage not cached yet; returns how many were computed now."""
        if documents is not None:
            self.set_documents(documents)
        texts = []
        seen = set()
        for document in self.documents.values():
            for passage in document.passages:
                if passage.text not in seen:
                    seen.add(passage.text)
                    texts.append(passage.text)
        return self._ensure(texts, "passage", progress=progress, cancel=cancel)

    def _ensure(self, texts: list[str], kind: str, *, progress=None, cancel=None) -> int:
        if self.embedder is None:
            return 0
        model = self.model_name
        missing = []
        for text in texts:
            key = _text_hash(model, kind, text)
            if key in self._vectors:
                continue
            cached = None
            if self.store is not None:
                try:
                    cached = self.store.cache_read(CACHE_NAMESPACE, key)
                except Exception:  # noqa: BLE001 - a busy store only costs a recomputation
                    cached = None
            if isinstance(cached, list) and cached:
                self._vectors[key] = np.asarray(cached, dtype=np.float32)
            else:
                missing.append((key, text))
        total = len(missing)
        done = 0
        batch = 32
        for start in range(0, total, batch):
            if cancel is not None and cancel.is_set():
                break
            chunk = missing[start:start + batch]
            try:
                vectors = np.asarray(self.embedder.embed([t for _k, t in chunk], kind=kind), dtype=np.float32)
            except Exception as error:  # noqa: BLE001 - model missing: keyword mode
                log.warning("Semantic index in keyword mode: %s", type(error).__name__)
                self.failed = True
                return done
            for (key, _text), vector in zip(chunk, vectors):
                norm = float(np.linalg.norm(vector)) or 1.0
                vector = vector / norm
                self._vectors[key] = vector
                if self.store is not None:
                    try:
                        self.store.cache_write(CACHE_NAMESPACE, key, [round(float(x), 6) for x in vector])
                    except Exception:  # noqa: BLE001
                        log.debug("Semantic vector not cached", exc_info=True)
            done += len(chunk)
            self.embedded += len(chunk)
            if progress is not None:
                progress(done, total)
        return done

    def _vector(self, kind: str, text: str) -> np.ndarray | None:
        return self._vectors.get(_text_hash(self.model_name, kind, text))

    # --- searching
    def search(self, query: str, *, keys: Iterable[str] | None = None) -> list[Hit]:
        """Ranked hits for `query` (all documents, or only `keys`), best first."""
        query = " ".join(str(query or "").split())
        if len(query) < MIN_QUERY_CHARS:
            return []
        variants = expand_query(query)
        query_words = [w for w in _content_words(query)]
        variant_words = {w for v in variants for w in _content_words(v)}
        with self._lock:
            documents = [self.documents[k] for k in keys if k in self.documents] if keys is not None \
                else list(self.documents.values())
        if not documents:
            return []
        query_vectors = []
        if self.mode == MODE_SEMANTIC:
            self._ensure(variants, "query")
            vectors = [v for v in (self._vector("query", q) for q in variants) if v is not None]
            if vectors:
                # the centre of the query and its translations: one odd variant cannot dominate
                centre = np.mean(np.vstack(vectors), axis=0)
                norm = float(np.linalg.norm(centre)) or 1.0
                query_vectors = [centre / norm, vectors[0]]
        interval = tuple(getattr(self.embedder, "similarity_range", (0.0, 1.0)) or (0.0, 1.0))
        hits = []
        for document in documents:
            hits.append(self._score(document, variants, query_words, variant_words, query_vectors, interval))
        hits.sort(key=lambda h: (-h.relevance, -h.tier))
        return hits

    def _score(self, document: Document, variants, query_words, variant_words, query_vectors, interval) -> Hit:
        best: Hit | None = None
        for passage in document.passages:
            folded = fold(passage.text)
            tier, terms = TIER_SEMANTIC, []
            for phrase in variants:
                if _phrase_in(phrase, folded):
                    tier, terms = TIER_PHRASE, [phrase]
                    break
            if tier == TIER_SEMANTIC and len(query_words) > 1 and all(_phrase_in(w, folded) for w in query_words):
                tier, terms = TIER_ALL_WORDS, list(query_words)
            partial = [w for w in variant_words if len(w) > 2 and _phrase_in(w, folded)]
            semantic = 0.0
            if query_vectors:
                vector = self._vector("passage", passage.text)
                if vector is not None:
                    # the centre of the variants, or the query as typed when that is closer
                    cosine = max(float(vector @ q) for q in query_vectors)
                    semantic = _calibrate(cosine, interval)
            if tier == TIER_PHRASE:
                relevance = PHRASE_FLOOR + PHRASE_SPAN * semantic / 100.0
            elif tier == TIER_ALL_WORDS:
                relevance = ALL_WORDS_FLOOR + ALL_WORDS_SPAN * semantic / 100.0
            else:
                relevance = min(SEMANTIC_CAP, SEMANTIC_CAP * semantic / 100.0 + PARTIAL_WORD_BONUS * len(partial))
                terms = partial
            candidate = Hit(document.key, round(relevance, 1), tier, passage, round(semantic, 1), terms)
            if best is None or (candidate.tier, candidate.relevance) > (best.tier, best.relevance):
                best = candidate
        return best or Hit(document.key, 0.0, TIER_SEMANTIC, None)

    def hits_by_key(self, query: str, keys: Iterable[str] | None = None) -> dict[str, Hit]:
        return {hit.key: hit for hit in self.search(query, keys=keys)}


def mode_of(embedder) -> str:
    if embedder is None or bool(getattr(embedder, "reduced", False)):
        return MODE_KEYWORD
    return MODE_SEMANTIC
