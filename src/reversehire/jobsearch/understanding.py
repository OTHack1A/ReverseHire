"""How a typed job search is understood beyond its exact words: punctuation, typos,
synonyms and Italian <-> English equivalents. Local tables only (no network).

- `split_token(token)`: "ot/cybersecurity" -> (["ot", "cybersecurity"], alternatives=True);
  "-", "+", "&", ",", parentheses split the words too, unless the whole token is a known
  term ("pl/sql", "c++", "node.js", "cyber-security", "e-commerce").
- `correct(word)`: the closest known term of a mistyped word ("cybersecurty" ->
  "cybersecurity"), by edit similarity against the vocabulary: the CV skill dictionary
  (`cv/data/skills.json`, every label and variant), the role words of the job search
  (`jobsearch/lexicon.py`), the role tables of `cv/data/translations.json` and the IT <-> EN
  terms of the semantic search (`scoring/semantic_index.py`).
- `equivalents(term)`: the other names of a term in those tables ("sicurezza informatica"
  <-> "cybersecurity", "sviluppatore" <-> "developer").

ESCO: only the labels already returned by the ESCO helper passed to `parse_query` are
used (the helper may be offline); no ESCO list is shipped with the program.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from . import lexicon
from ._util import fold, log

MIN_TYPO_LENGTH = 5        # shorter words are never "corrected"
TYPO_CUTOFF = 86.0         # rapidfuzz ratio (0..100) for a correction
MAX_EQUIVALENTS = 10       # per term
_SPLIT = re.compile(r"[/&+,;()\[\]{}|\\]+|(?<=[a-z0-9])-(?=[a-z0-9])")
_ALTERNATIVE = re.compile(r"[/|\\]")
_STOP = {"di", "del", "della", "e", "ed", "in", "per", "con", "da", "il", "la", "lo", "le", "gli", "un", "una",
         "of", "and", "the", "for", "with", "to", "a", "an", "or", "o"}


@lru_cache(maxsize=1)
def _translation_pairs() -> list[tuple[str, str]]:
    """(Italian or Ukrainian term, English term) of the role tables of translations.json."""
    pairs: list[tuple[str, str]] = []
    try:
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "cv" / "data" / "translations.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for table in ("roles", "role_words", "phrases"):
            for _language, mapping in (data.get(table) or {}).items():
                if not isinstance(mapping, dict):
                    continue
                for source, target in mapping.items():
                    a, b = fold(source), fold(target)
                    if a and b and a != b:
                        pairs.append((a, b))
    except Exception as error:  # noqa: BLE001 - the tables are optional here
        log.info("Translation tables not read: %s", type(error).__name__)
    return pairs


@lru_cache(maxsize=1)
def _semantic_pairs() -> list[tuple[str, str]]:
    try:
        from ..scoring.semantic_index import _IT_EN

        return [(fold(a), fold(b)) for a, b in _IT_EN]
    except Exception:  # noqa: BLE001
        return []


@lru_cache(maxsize=1)
def _skill_forms_by_entry() -> tuple[dict[str, int], dict[int, list[str]]]:
    """Folded lower-case form -> dictionary entry, and entry -> its forms."""
    dictionary = lexicon._dictionary()
    form_of: dict[str, int] = {}
    forms: dict[int, list[str]] = {}
    if dictionary is None:
        return form_of, forms
    for mapping in (getattr(dictionary, "_ci", {}) or {}, getattr(dictionary, "_cs", {}) or {}):
        for form, index in mapping.items():
            key = fold(form)
            if not key:
                continue
            form_of.setdefault(key, index)
            bucket = forms.setdefault(index, [])
            if key not in bucket:
                bucket.append(key)
    return form_of, forms


@lru_cache(maxsize=1)
def _equivalence_table() -> dict[str, list[str]]:
    table: dict[str, list[str]] = {}

    def link(a: str, b: str) -> None:
        if not a or not b or a == b:
            return
        table.setdefault(a, [])
        if b not in table[a]:
            table[a].append(b)

    for a, b in _semantic_pairs() + _translation_pairs():
        link(a, b)
        link(b, a)
    for group in lexicon.WORD_GROUPS:
        folded = [fold(w) for w in group]
        for a in folded:
            for b in folded:
                link(a, b)
    # a skill's label is linked to every variant; a variant only to the labels, and only
    # when it is not shorter than them ("cyber security" -> "cybersecurity", but "sales",
    # a variant of "retail sales", does not become "retail")
    _form_of, forms = _skill_forms_by_entry()
    labels_of = _skill_labels_by_entry()
    for index, members in forms.items():
        labels = labels_of.get(index, set())
        for label in labels:
            for member in members:
                link(label, member)
                if len(member.split()) >= len(label.split()):
                    link(member, label)
    return table


@lru_cache(maxsize=1)
def _skill_labels_by_entry() -> dict[int, set[str]]:
    dictionary = lexicon._dictionary()
    if dictionary is None:
        return {}
    return {entry.index: {fold(x) for x in (entry.en, entry.it) if x} for entry in dictionary.entries}


@lru_cache(maxsize=1)
def vocabulary() -> tuple[list[str], frozenset[str]]:
    """(known terms, known single words), folded."""
    terms = set(_equivalence_table())
    terms.update(fold(w) for group in lexicon.WORD_GROUPS for w in group)
    terms.update(_skill_forms_by_entry()[0])
    terms = {t for t in terms if t and len(t) >= 2}
    single = set()
    for term in terms:
        for word in re.findall(r"[a-z0-9+#.]+", term):
            if len(word) >= 2 and word not in _STOP:
                single.add(word)
    return sorted(terms), frozenset(single)


def is_known(term: str) -> bool:
    key = fold(term)
    if not key:
        return False
    terms, single = vocabulary()
    return key in single or key in _equivalence_table() or key in _skill_forms_by_entry()[0] or \
        lexicon.group_id(key) != key or lexicon.builtin_place(key) is not None or \
        any(form == key for _name, forms in lexicon.EXTRA_TECH.items() for form in forms)


def split_token(token: str) -> tuple[list[str], bool]:
    """(words, alternatives): the parts of one typed token (see the module docstring).
    `alternatives` is True when the parts were joined by "/" (the user means either)."""
    token = fold(token).strip(".'")
    if not token:
        return [], False
    if not _SPLIT.search(token) or is_known(token):
        return [token], False
    alternatives = bool(_ALTERNATIVE.search(token))
    parts = [p.strip(".'-") for p in _SPLIT.split(token)]
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return parts, False
    return parts, alternatives


def correct(word: str) -> str | None:
    """The known term closest to a mistyped word, or None (known, short, or too far)."""
    key = fold(word)
    if len(key) < MIN_TYPO_LENGTH or not re.fullmatch(r"[a-z][a-z0-9+#.\-']*", key):
        return None
    if key in lexicon.GENERIC_WORDS or is_known(key):
        return None
    try:
        from rapidfuzz import fuzz, process
    except Exception:  # noqa: BLE001 - without rapidfuzz no typo is corrected
        return None
    _terms, single = vocabulary()
    candidates = [w for w in single if abs(len(w) - len(key)) <= 2 and len(w) >= MIN_TYPO_LENGTH - 1]
    found = process.extractOne(key, candidates, scorer=fuzz.ratio, score_cutoff=TYPO_CUTOFF)
    if not found:
        return None
    return str(found[0])


def equivalents(term: str, limit: int = MAX_EQUIVALENTS) -> list[str]:
    """Other names of a term (folded, the term itself excluded), most direct first."""
    key = fold(term)
    if not key:
        return []
    out: list[str] = []
    for value in lexicon.synonyms(key)[1:] + list(_equivalence_table().get(key, [])):
        value = fold(value)
        if value and value != key and value not in out:
            out.append(value)
        if len(out) >= limit:
            break
    return out


def expansions_for(words: list[str], skills: list[str]) -> tuple[dict[str, list[str]], dict[str, str]]:
    """(expansions, corrections) of the role words and skills of a query.

    expansions: {typed word or skill name (folded): [other terms searched for it]};
    corrections: {typed word: corrected term}."""
    expansions: dict[str, list[str]] = {}
    corrections: dict[str, str] = {}
    covered = set()
    for skill in skills:
        forms = lexicon.skill_forms(skill)
        covered.update(w for f in forms for w in f.split())
        extra = [f for f in forms if f != fold(skill)]
        if extra:
            expansions[fold(skill)] = extra[:MAX_EQUIVALENTS]
    for word in words:
        if word in lexicon.GENERIC_WORDS or word in _STOP or len(word) < 2:
            continue
        fixed = correct(word)
        terms: list[str] = []
        if fixed:
            corrections[word] = fixed
            terms.append(fixed)
            terms += [e for e in equivalents(fixed) if e not in terms]
        elif word not in covered:
            terms += equivalents(word)
        terms = [x for x in terms if x != word]
        if terms:
            expansions[word] = terms[:MAX_EQUIVALENTS]
    return expansions, corrections
