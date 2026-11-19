"""Company/profile relevance (0..100): semantic similarity + rules.

Components (each in 0..1, then a weighted mean with the weights below;
components that cannot be computed are dropped and the weights renormalized):

- ``semantic``: cosine similarity between the profile "query" (roles, skills,
  sectors) and the blocks of the company text; the best block and the mean of
  the best ``TOP_K`` are combined and calibrated with ``calibrate_similarity``
  (linear between the two ends of the model's typical range, see
  ``embedding.SIMILARITY_RANGES``).
- ``sector``: OSM tags / company name consistent with ``profile.sectors``.
- ``words``: CV roles and skills found in the text (rapidfuzz, tolerant to
  accents, case and plurals; small IT/EN dictionary).
- ``role`` (only when the ESCO essential skills of the candidate's occupation
  are given, see ``cv.esco.essential_skills``): share of those skills named on
  the site ("role coverage"), saturating at ``ROLE_SKILLS_FOR_MAX``; the
  weights then become ``WEIGHTS_WITH_ROLE``.

A company whose only matching OSM tag is a generic one (``office=company``,
``office=yes``, see ``geo.sectors.UNINFORMATIVE_TAGS``) gets
``GENERIC_TAG_SCORE`` for the sector, not the full score.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

import numpy as np
from rapidfuzz import fuzz, process

from .embedding import DEFAULT_RANGE, split_into_blocks

# --- weights and thresholds -----------------------------------------------------------

WEIGHTS_WITH_TEXT = {"semantic": 0.60, "sector": 0.25, "words": 0.15}
WEIGHTS_WITHOUT_TEXT = {"sector": 0.75, "words": 0.25}
WEIGHTS_WITH_ROLE = {"semantic": 0.55, "sector": 0.20, "words": 0.15, "role": 0.10}
ROLE_SKILLS_FOR_MAX = 3
ROLE_SKILL_COVERAGE = 0.5

# The company text is compared with the profile as a symmetric similarity:
# the e5 model card recommends "query: " on both sides; in the real test it
# separates accounting firm / workshop better than "passage: ".
COMPANY_TEXT_KIND = "query"
CHARS_PER_BLOCK = 300
MIN_BLOCK_CHARS = 30
MAX_BLOCKS = 40
MIN_TEXT_CHARS = 80  # below this threshold the text is considered absent
TOP_K = 3
BEST_BLOCK_WEIGHT = 0.6  # s = 0.6 * max + 0.4 * media(top-K)

MAX_ROLES_QUERY = 3
MAX_SKILLS_QUERY = 10
MAX_SECTORS_QUERY = 3
MAX_ROLE_KEYWORDS = 5
MAX_SKILL_KEYWORDS = 15
ROLE_WEIGHT = 2.0
SKILL_WEIGHT = 1.0
FULL_KEYWORD_WEIGHT = 4.0  # sum of found weights that counts as "1"
PARTIAL_WORD_FRACTION = 0.5  # compound terms: share of words that is enough
PARTIAL_WEIGHT = 0.5

EXACT_TAG_SCORE = 1.0  # the company has one of the sector's OSM tags
SECTOR_WORD_SCORE = 0.6  # a sector word appears in the tags or name
GENERIC_TAG_SCORE = 0.3  # only a generic tag (office=company/yes) matches the sector

FUZZY_THRESHOLD = 86  # minimum rapidfuzz.fuzz.ratio to consider a term the same
MIN_FUZZY_LENGTH = 5  # shorter terms: exact match only
MAX_NGRAM_WORDS = 3

# Boilerplate blocks that do not describe the company (and that e5 finds "similar" to everything).
_SERVICE = re.compile(
    r"cookie|privacy|gdpr|p\.?\s?iva|partita iva|copyright|tutti i diritti|all rights reserved"
    r"|newsletter|informativa|termini e condizioni|terms of use"
    # bot-protection and error pages
    r"|verifying your browser|checking your browser|incident id|ray id|access denied|enable javascript",
    re.IGNORECASE,
)
# Lines that describe nothing (live Nov 2026: an office address list, a ticket price):
# addresses and scores are full of numbers, shops of prices, menus of capitalised words.
_NUMBER = re.compile(r"(?<![\w€$£])\d[\d/.,:-]*\b")
# A price, not a magnitude ("$10bn", "€2 million" describe the business).
_PRICE = re.compile(r"(?:[€$£]\s?\d+(?:[.,]\d+)?|\d+(?:[.,]\d+)?\s?(?:€|\beur\b|\beuro\b))"
                    r"(?!\s?(?:[kmb]n?\b|bn\b|mln\b|mld\b|million|billion|milion|miliard))", re.IGNORECASE)
_FUNCTION_WORDS = {
    "di", "a", "da", "in", "con", "su", "per", "tra", "fra", "il", "lo", "la", "i", "gli", "le", "un", "una",
    "e", "ed", "o", "che", "non", "del", "della", "dei", "delle", "al", "alla", "nel", "nella", "si", "ci",
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "by", "is", "are", "we", "our",
    "you", "your", "that", "this", "it", "as", "at", "from", "be", "not",
}
MIN_NUMBERS_FOR_LIST = 4
MAX_NUMBER_SHARE = 0.12
SHORT_BLOCK_WORDS = 40
MIN_SEPARATORS_FOR_MENU = 3  # "|" between menu entries or match results
MIN_MENU_WORDS = 8
MIN_CAPITAL_SHARE = 0.6
MAX_FUNCTION_SHARE = 0.1  # menus and lists of names: almost no "the", "di", "and"...
MAX_LIST_FUNCTION_SHARE = 0.15  # addresses and price lines; prose stays above ~0.2


def descriptive_block(block: str) -> bool:
    """False for blocks that do not describe the company: address and score lists,
    price lines, menus and lists of names. Conservative: every rule also needs few
    function words, so prose with a few figures always passes."""
    words = re.findall(r"[^\W\d_][\w'’-]*", block)
    if not words:
        return False
    # Lowercase only: "Serie A", "Al Khobar" are names, not function words.
    function = sum(1 for w in words if w in _FUNCTION_WORDS) / len(words)
    numbers = len(_NUMBER.findall(block))
    if (numbers >= MIN_NUMBERS_FOR_LIST and numbers / (len(words) + numbers) >= MAX_NUMBER_SHARE
            and function < MAX_LIST_FUNCTION_SHARE):
        return False
    prices = len(_PRICE.findall(block))
    if (prices >= 3 or (prices and len(words) < SHORT_BLOCK_WORDS)) and function < MAX_LIST_FUNCTION_SHARE:
        return False
    if block.count("|") >= MIN_SEPARATORS_FOR_MENU:
        return False
    if len(words) >= MIN_MENU_WORDS:
        capital = sum(1 for w in words if w[0].isupper()) / len(words)
        if capital >= MIN_CAPITAL_SHARE and function < MAX_FUNCTION_SHARE:
            return False
    return True

STOP_WORDS = {
    "di", "del", "della", "dei", "delle", "e", "ed", "in", "per", "con", "da", "il", "la",
    "lo", "le", "gli", "un", "una", "of", "and", "the", "for", "with", "to", "a", "an",
    "senior", "junior", "addetto", "addetta", "responsabile", "impiegato", "impiegata",
    "specialist", "assistant", "assistente", "manager", "tecnico", "operatore",
}

# Small IT <-> EN dictionary of job terms (normalized forms).
_IT_EN_PAIRS = [
    ("contabile", "accountant"), ("contabilita", "accounting"), ("contabilita", "bookkeeping"),
    ("commercialista", "accountant"), ("commercialista", "accounting firm"),
    ("bilancio", "financial statements"), ("bilanci", "financial statements"),
    ("bilancio", "balance sheet"), ("fiscale", "tax"), ("fatturazione", "invoicing"),
    ("fatturazione", "billing"), ("paghe", "payroll"), ("amministrazione", "administration"),
    ("meccanico", "mechanic"), ("officina", "workshop"), ("cuoco", "cook"), ("cuoco", "chef"),
    ("cameriere", "waiter"), ("sviluppatore", "developer"), ("programmatore", "programmer"),
    ("programmatore", "developer"), ("ingegnere", "engineer"), ("infermiere", "nurse"),
    ("magazziniere", "warehouse"), ("logistica", "logistics"), ("vendite", "sales"),
    ("commerciale", "sales"), ("acquisti", "purchasing"), ("acquisti", "procurement"),
    ("segretaria", "secretary"), ("receptionist", "receptionist"),
    ("elettricista", "electrician"), ("idraulico", "plumber"), ("saldatore", "welder"),
    ("autista", "driver"), ("insegnante", "teacher"), ("avvocato", "lawyer"),
    ("architetto", "architect"), ("grafico", "graphic designer"),
    ("risorse umane", "human resources"), ("consulenza", "consulting"),
    ("progettazione", "design"), ("manutenzione", "maintenance"), ("qualita", "quality"),
]


def _build_synonyms() -> dict[str, set[str]]:
    synonyms: dict[str, set[str]] = {}
    for a, b in _IT_EN_PAIRS:
        synonyms.setdefault(a, set()).add(b)
        synonyms.setdefault(b, set()).add(a)
    return synonyms


SYNONYMS = _build_synonyms()


# --- text -------------------------------------------------------------------


def unaccented(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize_text(text: str) -> str:
    """Lowercase, no accents, only letters/digits separated by single spaces."""
    text = unaccented(text or "").lower().replace("_", " ")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def variants(term: str) -> list[str]:
    """The normalized term and its known IT/EN equivalents."""
    t = normalize_text(term)
    if not t:
        return []
    result = [t]
    for s in sorted(SYNONYMS.get(t, ())):
        if s not in result:
            result.append(s)
    return result


class TextIndex:
    """Tolerant search of terms (also multi-word) in a text."""

    def __init__(self, text: str):
        self.normalized = normalize_text(text)
        self._words = self.normalized.split()
        self._padded = f" {self.normalized} "
        self._ngram: dict[int, list[str]] = {}

    def _ngrams(self, n: int) -> list[str]:
        if n not in self._ngram:
            p = self._words
            self._ngram[n] = list({" ".join(p[i : i + n]) for i in range(len(p) - n + 1)})
        return self._ngram[n]

    def _contains_exact_or_similar(self, t: str) -> bool:
        if not t or not self._words:
            return False
        if f" {t} " in self._padded:
            return True
        n = len(t.split())
        if n > MAX_NGRAM_WORDS or len(t) < MIN_FUZZY_LENGTH:
            return False
        candidates = self._ngrams(n)
        if not candidates:
            return False
        return process.extractOne(t, candidates, scorer=fuzz.ratio, score_cutoff=FUZZY_THRESHOLD) is not None

    def contains(self, term: str) -> bool:
        """True if the term (or an IT/EN equivalent) appears in the text."""
        return any(self._contains_exact_or_similar(v) for v in variants(term))

    def coverage(self, term: str) -> float:
        """1.0 if the whole term appears; otherwise the share of its significant
        words that are present (for compound terms), otherwise 0."""
        if self.contains(term):
            return 1.0
        words = [p for p in normalize_text(term).split() if p not in STOP_WORDS and len(p) >= 3]
        if len(words) < 2:
            return 0.0
        found_items = sum(1 for p in words if self.contains(p))
        return found_items / len(words)


# --- sectors -----------------------------------------------------------------


def sector_descriptors(key: str) -> tuple[list[tuple[str, str]], list[str]]:
    """(OSM tags, words) describing a sector.

    Uses ``geo.sectors.SECTORS`` if available; otherwise treats the key as
    text (``"key=value"`` becomes a tag, ``"studi_legali"`` a word).
    """
    sector = None
    try:
        from ..geo.sectors import SECTORS

        sector = SECTORS.get(key)
    except Exception:
        sector = None
    if sector is not None:
        tag = [tuple(t) for t in getattr(sector, "osm_tags", [])]
        words = [getattr(sector, "name_it", ""), getattr(sector, "name_en", "")]
        words += list(getattr(sector, "keywords", []))
        return tag, [p for p in words if p]
    if "=" in key:
        k, v = key.split("=", 1)
        return [(k.strip(), v.strip())], [v.replace("_", " ")]
    return [], [key.replace("_", " ")]


def sector_name(key: str, language: str = "it") -> str:
    try:
        from ..geo.sectors import SECTORS

        sector = SECTORS.get(key)
        if sector is not None:
            return getattr(sector, "name_en" if language == "en" else "name_it", key) or key
    except Exception:
        pass
    return key.split("=", 1)[-1].replace("_", " ")


def tags_text(osm_tags: dict[str, str]) -> str:
    """Descriptive tags as "key=value" words (internal "_" keys and identifiers left out)."""
    from ..models import public_tags

    return " ".join(f"{k}={v}" for k, v in public_tags(osm_tags).items())


def _generic_tags() -> set[tuple[str, str]]:
    try:
        from ..geo.sectors import UNINFORMATIVE_TAGS

        return {(k, v) for k, v in UNINFORMATIVE_TAGS}
    except Exception:
        return set()


def sector_score(profile, company) -> tuple[float | None, list[str]]:
    """(0..1, or None if the profile has no sectors; matching sectors found)."""
    sectors = list(getattr(profile, "sectors", []) or [])
    if not sectors:
        return None, []
    from ..models import public_tags

    tag = {(k.lower(), str(v).lower()) for k, v in public_tags(company.osm_tags).items()}
    tag_keys = {k for k, _ in tag}
    index = TextIndex(f"{company.name} {tags_text(company.osm_tags)}")
    generic = _generic_tags()
    best, matching = 0.0, []
    for key in sectors:
        sector_tags, words = sector_descriptors(key)
        value = 0.0
        for k, v in sector_tags:
            k, v = k.lower(), str(v).lower()
            if (k, v) in tag or (v in ("*", "") and k in tag_keys):
                value = max(value, GENERIC_TAG_SCORE if (k, v) in generic else EXACT_TAG_SCORE)
                if value >= EXACT_TAG_SCORE:
                    break
        if value < SECTOR_WORD_SCORE and any(index.contains(p) for p in words):
            value = SECTOR_WORD_SCORE
        if value >= SECTOR_WORD_SCORE:
            matching.append(key)
        best = max(best, value)
    return best, matching


# --- profile -----------------------------------------------------------------


def _values(items, n: int) -> list[str]:
    result = []
    for v in (items or [])[:n]:
        value = (getattr(v, "value", v) or "").strip()
        if value and value not in result:
            result.append(value)
    return result


def profile_terms(items, n: int) -> list[tuple[str, list[str]]]:
    """(displayed value, texts to match) of the first `n` items, without duplicates.

    The texts to match are the value and, for a translated CV, the CV wording
    (``original``): an Italian CV shown in English still matches Italian
    postings and pages as well as before."""
    from ..models import item_variants

    result: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for item in (items or [])[:n]:
        forms = item_variants(item)
        if not forms or forms[0] in seen:
            continue
        seen.add(forms[0])
        result.append((forms[0], forms))
    return result


def build_query(profile, language: str = "it") -> str:
    """Profile text for the embedding: "roles; skills; sectors" (CV wording added when translated)."""

    def joined(items, n: int) -> str:
        terms = profile_terms(items, n)
        values = [display for display, _ in terms]
        originals = [f for _, forms in terms for f in forms[1:] if f not in values]
        return ", ".join(values + originals)

    parts = [
        joined(profile.roles, MAX_ROLES_QUERY),
        joined(profile.skills, MAX_SKILLS_QUERY),
        ", ".join(sector_name(s, language) for s in (profile.sectors or [])[:MAX_SECTORS_QUERY]),
    ]
    return "; ".join(p for p in parts if p)


def keyword_score(profile, index: TextIndex) -> tuple[float, list[str]]:
    """(0..1, CV terms found in the text); each term counts once, with the best of
    its value and its CV wording."""
    weight, matches = 0.0, []
    terms = [(r, ROLE_WEIGHT) for r in profile_terms(profile.roles, MAX_ROLE_KEYWORDS)]
    terms += [(c, SKILL_WEIGHT) for c in profile_terms(profile.skills, MAX_SKILL_KEYWORDS)]
    for (term, forms), p in terms:
        coverage = max(index.coverage(f) for f in forms)
        if coverage >= 1.0:
            weight += p
            matches.append(term)
        elif coverage >= PARTIAL_WORD_FRACTION:
            weight += p * PARTIAL_WEIGHT
            matches.append(term)
    return min(1.0, weight / FULL_KEYWORD_WEIGHT), matches


# --- semantics ---------------------------------------------------------------


def calibrate_similarity(s: float, interval: tuple[float, float] = DEFAULT_RANGE) -> float:
    """Map a cosine similarity to 0..100.

    Linear between the ends of the model's typical range ``(low, high)``:
    ``s <= low`` -> 0, ``s >= high`` -> 100. With e5 the similarities lie in a
    narrow, high band (unrelated texts ~0.79-0.81, same topic ~0.87), so a
    linear mapping from 0 would squash everything towards 80-90.
    """
    low, high = interval
    if high <= low:
        return 0.0
    return float(np.clip((s - low) / (high - low), 0.0, 1.0) * 100.0)


def range_of(embedder) -> tuple[float, float]:
    return tuple(getattr(embedder, "similarity_range", DEFAULT_RANGE))


def company_blocks(text: str) -> list[str]:
    blocks = [
        b
        for b in split_into_blocks(text or "", CHARS_PER_BLOCK)
        if len(b) >= MIN_BLOCK_CHARS and not _SERVICE.search(b) and descriptive_block(b)
    ]
    return blocks[:MAX_BLOCKS]


def semantic_similarity(query: str, text: str, embedder) -> tuple[float | None, str | None]:
    """(raw combined similarity, most similar block) or (None, None)."""
    blocks = company_blocks(text)
    if not query or not blocks:
        return None, None
    q = np.asarray(embedder.embed([query], kind="query"))[0]
    p = np.asarray(embedder.embed(blocks, kind=COMPANY_TEXT_KIND))
    sim = p @ q
    order = np.argsort(-sim)
    top_scores = sim[order[:TOP_K]]
    s = BEST_BLOCK_WEIGHT * float(top_scores[0]) + (1 - BEST_BLOCK_WEIGHT) * float(top_scores.mean())
    return s, blocks[int(order[0])]


# --- result ---------------------------------------------------------------


@dataclass
class RelevanceResult:
    score: float
    semantic: float | None = None  # 0..100 calibrated
    similarity: float | None = None  # raw combined cosine
    sector: float | None = None  # 0..1
    words: float = 0.0  # 0..1
    found_terms: list[str] = field(default_factory=list)
    matching_sectors: list[str] = field(default_factory=list)
    best_sentence: str | None = None
    used_text: bool = False
    role: float | None = None  # 0..1 ESCO essential skills named on the site
    found_role_skills: list[str] = field(default_factory=list)


def _weighted_mean(components: dict[str, float | None], weights: dict[str, float]) -> float:
    valid_items = {k: v for k, v in components.items() if v is not None and k in weights}
    total = sum(weights[k] for k in valid_items)
    if total <= 0:
        return 0.0
    return sum(weights[k] * valid_items[k] for k in valid_items) / total


def role_coverage(index: TextIndex, role_skills: list[str] | None) -> tuple[float | None, list[str]]:
    """(0..1, skills found) for the ESCO essential skills of the candidate's occupation."""
    if not role_skills:
        return None, []
    found_items = [k for k in role_skills if index.coverage(k) >= ROLE_SKILL_COVERAGE]
    return min(1.0, len(found_items) / ROLE_SKILLS_FOR_MAX), found_items


def compute_relevance(profile, company, outcome, embedder,
                      role_skills: list[str] | None = None) -> RelevanceResult:
    text = (getattr(outcome, "company_text", "") or "") if outcome is not None else ""
    use_text = len(text.strip()) >= MIN_TEXT_CHARS

    sector, matching = sector_score(profile, company)
    index = TextIndex(" ".join([company.name or "", tags_text(company.osm_tags), text if use_text else ""]))
    words, matches = keyword_score(profile, index)

    sim, sentence, semantic = None, None, None
    if use_text:
        sim, sentence = semantic_similarity(build_query(profile), text, embedder)
        if sim is not None:
            semantic = calibrate_similarity(sim, range_of(embedder))

    role, role_found = role_coverage(TextIndex(text), role_skills) if use_text else (None, [])
    if semantic is not None and role is not None:
        value = _weighted_mean(
            {"semantic": semantic / 100.0, "sector": sector, "words": words, "role": role},
            WEIGHTS_WITH_ROLE,
        )
    elif semantic is not None:
        value = _weighted_mean(
            {"semantic": semantic / 100.0, "sector": sector, "words": words}, WEIGHTS_WITH_TEXT
        )
    else:
        value = _weighted_mean({"sector": sector, "words": words}, WEIGHTS_WITHOUT_TEXT)

    return RelevanceResult(
        score=round(float(np.clip(value, 0.0, 1.0)) * 100.0, 1),
        semantic=None if semantic is None else round(semantic, 1),
        similarity=sim,
        sector=sector,
        words=words,
        found_terms=matches,
        matching_sectors=matching,
        best_sentence=sentence,
        used_text=semantic is not None,
        role=role,
        found_role_skills=role_found,
    )
