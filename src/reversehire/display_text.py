"""Display layer: dynamic texts in English when the interface is English.

Rule of the owner: with the interface in English, no Italian or Ukrainian words
are shown. Texts that come from the CV, from websites or from job postings
(evidence lines, reasons and gaps, posting titles, salaries, travel notes...)
go through `show()` right before they are drawn:

- interface not English -> the text as it is (there are no en->it / en->uk
  models: Italian and Ukrainian interfaces show the texts unchanged);
- the text (or each quoted part of a sentence, “…” or «…») is detected as
  Italian or Ukrainian -> its English translation from the cache, with the
  original kept for a tooltip / "Show original";
- not translated yet or no model -> the original text with a small
  "(original language)" marker, and `missing_languages()` reports it so that
  the window shows ONE notice explaining how to enable translation.

`show()` never runs the model: it only reads the caches, so it is safe on the
GUI thread. Translations are produced by `prewarm()` in worker threads (CV
analysis, assessment of a company, opening a saved search) and kept in memory
and in the Store cache (namespace `NAMESPACE`, key = hash of the text), so a
text is translated once. Order of the sources: the built-in tables of
`cv/english.py`, then the local model of `cv/translate.py`.

Quality guard: single words, acronyms, e-mail addresses, URLs, numbers and
text already English are never sent to the model; when the detection is not
confident the text is left untouched.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass
from typing import Callable, Iterable

log = logging.getLogger(__name__)

NAMESPACE = "display_en"
TRANSLATABLE = ("it", "uk")
MIN_CONFIDENCE = 0.55        # lingua confidence for a 2-word text
MIN_CONFIDENCE_LONG = 0.40   # for texts of 4 words or more
MIN_MARGIN = 0.15            # over the second language
MAX_CHARS = 600              # longer texts are cut in sentences before the model
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_URL = re.compile(r"(?:https?://|www\.)\S+|\S+@\S+\.\w+", re.IGNORECASE)
_WORD = re.compile(r"[^\W\d_][^\W\d_'’\-]*")
_QUOTED = re.compile(r"“([^”]+)”|«([^»]+)»|\"([^\"]+)\"")
# Italian function words: a text that contains them is Italian even when short.
_ITALIAN_HINTS = {
    "di", "del", "della", "dello", "dei", "degli", "delle", "nel", "nella", "nei", "con", "per", "sul",
    "sulla", "il", "lo", "gli", "le", "una", "uno", "che", "anni", "esperienza", "conoscenza", "lavoro",
    "azienda", "sede", "ufficio", "tempo", "indeterminato", "determinato", "lordi", "annui", "mese",
    "trasferte", "settimana", "giorni", "anno", "ed", "oppure", "presso", "dal", "alla", "agli", "ai",
}
_ENGLISH_HINTS = {
    "the", "and", "of", "for", "with", "to", "in", "on", "at", "a", "an", "is", "are", "your", "our",
    "years", "experience", "knowledge", "work", "remote", "hybrid", "office", "team", "per", "year",
}

Translator = Callable[[list[str], str], list[str]]


@dataclass
class Shown:
    """A text ready to be drawn: `text` (English or original), `original` (the
    source text when it was translated, else None) and `missing` (it needed a
    translation that is not available: `text` carries the marker)."""

    text: str
    original: str | None = None
    missing: bool = False

    @property
    def translated(self) -> bool:
        return self.original is not None


_lock = threading.Lock()
_memory: dict[str, str | None] = {}       # text -> English (None = keep the original)
_detected: dict[str, str | None] = {}     # text -> "it" | "uk" | None
_missing: set[str] = set()                # languages whose texts were shown untranslated
_absent: set[str] = set()                 # texts already looked up in the Store without result
_store = None
_translator: Translator | None = None
_detector = None


# --- configuration -----------------------------------------------------------------


def set_store(store) -> None:
    """The Store whose cache keeps the translations (the GUI sets its archive)."""
    global _store
    _store = store


def set_translator(translator: Translator | None) -> None:
    """Replaces the model (tests); None restores `cv.translate.translate`."""
    global _translator
    _translator = translator


def reset() -> None:
    """Forget the in-memory caches (tests, model installed)."""
    with _lock:
        _memory.clear()
        _detected.clear()
        _missing.clear()
        _absent.clear()


def interface_is_english() -> bool:
    from .i18n import current_language

    return current_language() == "en"


def missing_languages() -> set[str]:
    """Languages of the texts shown untranslated since the last `clear_missing()`."""
    with _lock:
        return set(_missing)


def clear_missing() -> None:
    with _lock:
        _missing.clear()


def model_installed(language: str) -> bool:
    """True when the local model of `language` -> English is on disk (no loading)."""
    if _translator is not None:
        return True
    try:
        from .cv import translate as mt

        return mt.is_installed(language)
    except Exception:  # noqa: BLE001
        return False


# --- detection ---------------------------------------------------------------------


def _lingua():
    global _detector
    if _detector is None:
        from lingua import Language, LanguageDetectorBuilder

        _detector = LanguageDetectorBuilder.from_languages(
            Language.ITALIAN, Language.ENGLISH, Language.FRENCH, Language.GERMAN, Language.SPANISH,
        ).build()
    return _detector


def warm_up() -> bool:
    """Builds the language detector (worker thread at start-up): its first use on the
    GUI thread would otherwise load the language data there."""
    try:
        _lingua()
        return True
    except Exception:  # noqa: BLE001 - no detector: the texts are simply left as they are
        log.debug("Language detector not available", exc_info=True)
        return False


def words_of(text: str) -> list[str]:
    return _WORD.findall(_URL.sub(" ", text or ""))


def source_language(text: str) -> str | None:
    """"it" or "uk" when `text` should be translated for an English interface, else None.

    None for: short or single-word texts, acronyms, e-mail/URLs/numbers, English
    text and texts whose detection is not confident (left untouched)."""
    if not text:
        return None
    with _lock:
        if text in _detected:
            return _detected[text]
    result = _detect(text)
    with _lock:
        if len(_detected) > 20000:
            _detected.clear()
        _detected[text] = result
    return result


def _detect(text: str) -> str | None:
    if len(text.split()) < 2:  # one token: a word, a file name, a code
        return None
    words = words_of(text)
    if len(words) < 2:
        return None
    if all(w.isupper() for w in words):  # acronyms only
        return None
    letters = "".join(words)
    cyrillic = len(_CYRILLIC.findall(letters))
    if cyrillic / max(1, len(letters)) >= 0.3:
        return "uk"
    lower = [w.lower() for w in words]
    italian_hints = sum(1 for w in lower if w in _ITALIAN_HINTS)
    english_hints = sum(1 for w in lower if w in _ENGLISH_HINTS)
    if english_hints > italian_hints and english_hints >= max(1, len(words) // 4):
        return None
    try:
        values = _lingua().compute_language_confidence_values(" ".join(words))
    except Exception:  # noqa: BLE001 - detection must never break the display
        log.debug("Language detection failed", exc_info=True)
        return None
    if not values:
        return None
    top = values[0]
    second = values[1].value if len(values) > 1 else 0.0
    if top.language.name != "ITALIAN":
        return None
    if italian_hints and top.value >= MIN_CONFIDENCE_LONG:
        return "it"
    needed = MIN_CONFIDENCE_LONG if len(words) >= 4 else MIN_CONFIDENCE
    if top.value >= needed and top.value - second >= MIN_MARGIN:
        return "it"
    return None


def maybe_italian(text: str) -> bool:
    """A short text (1-4 words) whose most likely language is Italian (low confidence)."""
    words = words_of(text)
    if not 1 <= len(words) <= 4 or all(w.isupper() for w in words):
        return False
    try:
        values = _lingua().compute_language_confidence_values(" ".join(words))
    except Exception:  # noqa: BLE001
        return False
    return bool(values) and values[0].language.name == "ITALIAN"


# --- caches ------------------------------------------------------------------------


def _key(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()[:40]


_UNKNOWN = object()


def cached(text: str):
    """The cached English text, None (keep the original) or `_UNKNOWN`."""
    with _lock:
        if text in _memory:
            return _memory[text]
        if text in _absent:
            return _UNKNOWN
    store = _store
    if store is not None:
        try:
            value = store.cache_read(NAMESPACE, _key(text))
        except Exception:  # noqa: BLE001 - a closed store is not an error here
            value = None
        if isinstance(value, dict) and "en" in value:
            with _lock:
                _memory[text] = value["en"]
            return value["en"]
    with _lock:
        if len(_absent) > 20000:
            _absent.clear()
        _absent.add(text)
    return _UNKNOWN


def _remember(text: str, english: str | None, store=None) -> None:
    with _lock:
        _memory[text] = english
        _absent.discard(text)
    store = store if store is not None else _store
    if store is not None:
        try:
            store.cache_write(NAMESPACE, _key(text), {"en": english})
        except Exception:  # noqa: BLE001
            log.debug("Display translation not cached", exc_info=True)


# --- translation (worker threads only) --------------------------------------------


def _from_tables(text: str, language: str) -> str | None:
    try:
        from .cv import english

        value = english.role_from_tables(text, language) or english.phrase_from_tables(text, language)
    except Exception:  # noqa: BLE001
        return None
    return value if value and value.casefold() != text.casefold() else None


def _clean(source: str, result: str) -> str | None:
    result = " ".join((result or "").split())
    if not result or result == source or not re.search(r"[^\W\d_]", result):
        return None
    if _CYRILLIC.search(result) or len(result) > 3 * len(source) + 30:
        return None
    if source[:1].isupper() and result[:1].islower():
        result = result[:1].upper() + result[1:]
    if not source.rstrip().endswith(".") and result.endswith(".") and not result.endswith("..."):
        result = result[:-1]
    return result


def _model(texts: list[str], language: str) -> list[str]:
    if _translator is not None:
        return _translator(texts, language)
    from .cv import translate as mt

    return mt.translate(texts, language)


def segments(text: str) -> list[str]:
    """The parts of `text` that are translated on their own: the quoted parts of
    a sentence (“…”, «…», "…"), or the whole text when it quotes nothing."""
    parts = [next(g for g in m.groups() if g is not None) for m in _QUOTED.finditer(text or "")]
    return [p for p in parts if p.strip()] or ([text] if text and text.strip() else [])


def translate_now(texts: Iterable[str], store=None) -> int:
    """Translates (blocking: worker threads only) the texts that need it and caches
    the results; returns how many new translations were made."""
    todo: dict[str, list[str]] = {}
    made = 0
    for text in dict.fromkeys(t for t in texts if t):
        if cached(text) is not _UNKNOWN:
            continue
        language = source_language(text)
        if language is None:
            # Short texts the detector is unsure about: only the built-in tables.
            if maybe_italian(text):
                table = _from_tables(text, "it")
                if table:
                    _remember(text, table, store)
                    made += 1
            continue
        table = _from_tables(text, language)
        if table:
            _remember(text, table, store)
            made += 1
            continue
        if not model_installed(language):
            continue
        todo.setdefault(language, []).append(text)
    for language, group in todo.items():
        try:
            out = _model([t[:MAX_CHARS] for t in group], language)
        except Exception:  # noqa: BLE001 - never break the worker
            log.warning("Display translation failed", exc_info=True)
            continue
        if len(out) != len(group):
            continue
        for source, result in zip(group, out):
            cleaned = _clean(source, result)
            _remember(source, cleaned, store)
            made += cleaned is not None
    return made


def prewarm(texts: Iterable[str], store=None, *, ui_language: str | None = None) -> int:
    """Pre-translates, in a worker thread, the parts of `texts` that `show()` will
    need. Nothing happens when the interface is not English or no model nor table
    can help (cheap: no detection without a model)."""
    from .i18n import current_language

    if (ui_language or current_language()) != "en":
        return 0
    parts = [p for text in texts if text for p in segments(text)]
    if not parts:
        return 0
    try:
        return translate_now(parts, store)
    except Exception:  # noqa: BLE001
        log.warning("Display pre-translation failed", exc_info=True)
        return 0


def pending(texts: Iterable[str]) -> list[str]:
    """Parts of `texts` that need a translation not cached yet (GUI thread safe)."""
    result = []
    for text in texts:
        for part in segments(text or ""):
            if cached(part) is _UNKNOWN and source_language(part) is not None:
                result.append(part)
    return result


# --- display (GUI thread) ----------------------------------------------------------


def _marker() -> str:
    from .i18n import t

    return t("display.original_marker")


def _resolve(part: str) -> tuple[str, bool, bool]:
    """(text to show, translated, missing) for one segment."""
    value = cached(part)
    if isinstance(value, str):
        return value, True, False
    if value is None:  # translated once: the model kept it as it is
        return part, False, False
    language = source_language(part)
    if language is None:
        return part, False, False
    with _lock:
        _missing.add(language)
    return part, False, True


def show(text: str | None, *, english: bool | None = None) -> Shown:
    """The text to draw now (see the module docstring)."""
    text = text or ""
    if not text.strip() or not (interface_is_english() if english is None else english):
        return Shown(text)
    matches = list(_QUOTED.finditer(text))
    if not matches:
        shown, translated, missing = _resolve(text)
        if translated:
            return Shown(shown, original=text)
        if missing:
            return Shown(f"{text} {_marker()}", missing=True)
        return Shown(text)
    pieces, last, any_translated, any_missing = [], 0, False, False
    for m in matches:
        group = next(i for i, g in enumerate(m.groups(), 1) if g is not None)
        part = m.group(group)
        shown, translated, missing = _resolve(part)
        any_translated |= translated
        any_missing |= missing
        pieces.append(text[last:m.start(group)])
        pieces.append(shown)
        last = m.end(group)
    pieces.append(text[last:])
    result = "".join(pieces)
    if any_missing:
        result = f"{result} {_marker()}"
    return Shown(result, original=text if any_translated else None, missing=any_missing)


def display(text: str | None) -> str:
    return show(text).text


def tooltip(text: str | None) -> str:
    """"Original: …" for a translated text, "" otherwise."""
    shown = show(text)
    if shown.original is None:
        return ""
    from .i18n import t

    return t("display.original", text=shown.original)


# --- what to pre-translate ---------------------------------------------------------


def assessment_texts(assessment) -> list[str]:
    """Texts of an Assessment that the results and detail panels show."""
    if assessment is None:
        return []
    texts = [getattr(r, "text", "") for r in getattr(assessment, "reasons", None) or []]
    texts += list(getattr(assessment, "gaps", None) or [])
    texts.append(getattr(assessment, "blocked_reason", None) or "")
    for posting in getattr(assessment, "postings", None) or []:
        texts += [posting.title or "", getattr(posting, "salary", None) or "",
                  getattr(posting, "travel_note", None) or ""]
    for check in getattr(assessment, "requirements", None) or []:
        texts += [getattr(check, "text", "") or "", getattr(check, "evidence", None) or ""]
    return [t for t in texts if t]


def analysis_texts(profile=None, review=None, signals=None) -> list[str]:
    """Texts of a CV analysis that the profile and recruiter pages show."""
    texts: list[str] = []
    for finding in getattr(review, "findings", None) or []:
        texts += list(finding.evidence or [])
        texts += [v for v in (finding.params or {}).values() if isinstance(v, str)]
    for coverage in getattr(review, "coverage", None) or []:
        texts.append(coverage.skill)
    for signal in getattr(signals, "signals", None) or []:
        texts += list(signal.evidence or [])
        texts += [v for v in (signal.params or {}).values() if isinstance(v, str)]
    if profile is not None:
        mobility = getattr(profile, "mobility", None)
        if mobility is not None:
            for name in ("travel_evidence", "relocation_evidence", "licence_evidence"):
                texts += [e.text for e in getattr(mobility, name, None) or []]
        texts += [e.text for e in getattr(profile, "seniority_evidence", None) or []]
        for experience in getattr(profile, "experiences", None) or []:
            texts += [e.text for e in experience.evidence or []]
    return [t for t in texts if t]
