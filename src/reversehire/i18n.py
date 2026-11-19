"""Interface translations (English, Ukrainian, Italian) with English as the fallback.

Each language lives in `locales/<code>.json`: a flat dictionary from key to text,
with `{name}` placeholders filled by `t(key, name=...)`. Every file has the same
keys and the same placeholders (checked by the tests).
"""

import json
from pathlib import Path

DEFAULT_LANGUAGE = "en"

# Order in which the languages are offered in menus and settings.
LANGUAGE_ORDER = ("en", "uk", "it")
# Each language is shown with its own name, whatever the current language is.
LANGUAGE_NAMES = {"en": "English", "uk": "Українська", "it": "Italiano"}

_LOCALES_DIR = Path(__file__).parent / "locales"

_loaded: dict[str, dict[str, str]] = {}
_current = DEFAULT_LANGUAGE


def available_languages() -> list[str]:
    """Codes of the languages that have a translation file, in menu order."""
    codes = {p.stem for p in _LOCALES_DIR.glob("*.json")}
    ordered = [c for c in LANGUAGE_ORDER if c in codes]
    return ordered + sorted(codes - set(ordered))


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code, code)


def _translations(code: str) -> dict[str, str]:
    """Loads (once) the dictionary of one language."""
    if code not in _loaded:
        path = _LOCALES_DIR / f"{code}.json"
        _loaded[code] = json.loads(path.read_text(encoding="utf-8"))
    return _loaded[code]


def set_language(code: str) -> None:
    """Sets the current language; ValueError if it is not available."""
    global _current
    if code not in available_languages():
        raise ValueError(f"Language not available: {code!r}")
    _translations(code)
    _current = code


def current_language() -> str:
    return _current


def t(key: str, **params) -> str:
    """Translated text. Falls back to English, then to the key itself."""
    text = _translations(_current).get(key)
    if text is None and _current != DEFAULT_LANGUAGE:
        text = _translations(DEFAULT_LANGUAGE).get(key)
    if text is None:
        return key
    if params:
        try:
            return text.format(**params)
        except (KeyError, IndexError):
            return text
    return text
