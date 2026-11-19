"""The profile fields in English, whatever the language of the CV (it, uk -> en).

Everything is local: the CV is never sent to an online translation service.
Order of the sources, from the most to the least reliable:
1. structured mapping (deterministic): skills -> English canonical name of the
   dictionary (done by `skills.py`), spoken languages -> English name and level
   (done by `extraction.extract_languages`), degrees -> table of degree types and
   fields of study, roles -> English ESCO preferred label when the occupation is
   known, else the table of job titles (`data/translations.json`);
2. the local machine-translation model (`translate.py`) for the free text that
   the tables do not cover (role titles with details, degree subjects, skills
   that are not in the dictionary);
3. nothing available: the text stays as it is and is listed in
   `profile.untranslated`.
Every translated item keeps the CV wording in `original` (`original_title` for
the work history), which the scoring also matches (`models.item_variants`).
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache

from ..models import CVProfile, ProfileItem
from . import translate as mt
from .sections import DATA_FOLDER, normalize
from .skills import is_known

log = logging.getLogger(__name__)

TRANSLATABLE = ("it", "uk")
MAX_MT_CHARS = 160  # longer texts are not worth a machine translation in a profile field

_CONNECTORS = {
    "it": r"(?:in|di|del|dello|della|dei|degli|delle|ad indirizzo|indirizzo|ramo|curriculum|specializzazione in|"
          r"con indirizzo|settore)",
    "uk": r"(?:за спеціальністю|зі спеціальності|за напрямом|за напрямком|спеціальність|напрям|у галузі|в галузі|"
          r"з|із|зі)",
}
# English words of job titles: a title made only of these is already English.
_ENGLISH_TITLE_WORDS = {
    "senior", "junior", "lead", "head", "chief", "principal", "staff", "software", "hardware", "developer",
    "engineer", "engineering", "manager", "management", "project", "product", "program", "programme", "team",
    "leader", "director", "officer", "assistant", "specialist", "consultant", "analyst", "architect",
    "designer", "technician", "operator", "administrator", "coordinator", "supervisor", "executive",
    "mechanical", "electrical", "electronic", "electronics", "automation", "process", "production", "quality",
    "sales", "marketing", "account", "accountant", "accounting", "business", "data", "scientist", "web",
    "full", "stack", "full-stack", "front", "end", "front-end", "back-end", "backend", "frontend", "mobile",
    "cloud", "devops", "security", "cyber", "cybersecurity", "network", "system", "systems", "it", "plc",
    "scada", "control", "field", "service", "support", "customer", "maintenance", "plant", "site", "and", "&",
    "of", "the", "for", "in", "at", "r&d", "research", "development", "test", "tester", "qa", "intern",
    "trainee", "freelance", "freelancer", "owner", "founder", "co-founder", "ceo", "cto", "cfo", "coo", "hr",
    "human", "resources", "office", "operations", "operation", "logistics", "warehouse", "supply", "chain",
    "nurse", "teacher", "driver", "chef", "cook", "waiter", "embedded", "firmware", "machine", "learning",
    "robotics", "robot", "commissioning", "application", "applications", "solutions", "solution", "technical",
    "industrial", "computer", "programmer", "researcher", "student", "graduate", "apprentice", "worker",
    "clerk", "representative", "agent", "buyer", "planner", "inspector", "auditor", "advisor", "expert",
}
_ARTICLE = re.compile(r"^(?:the|an|a)\s+", re.IGNORECASE)


@lru_cache(maxsize=1)
def _tables() -> dict:
    data = json.loads((DATA_FOLDER / "translations.json").read_text(encoding="utf-8"))
    result: dict = {"roles": {}, "role_words": {}, "subjects": {}, "phrases": {}, "degree_types": {}}
    for group in ("roles", "role_words", "subjects", "phrases"):
        for language, table in data[group].items():
            result[group][language] = {normalize(k): v for k, v in table.items()}
    for language, pairs in data["degree_types"].items():
        result["degree_types"][language] = sorted(((normalize(k), v) for k, v in pairs),
                                                  key=lambda p: -len(p[0]))
    return result


def _key(text: str) -> str:
    return normalize(re.sub(r"[«»\"“”„]", "", text)).strip(" .,;:-–—")


def _capitalise(text: str, like: str) -> str:
    if text and like[:1].isupper() and text[:1].islower():
        return text[:1].upper() + text[1:]
    return text


def is_english(text: str) -> bool:
    """True if every word of a short title is a common English title word (or an acronym)."""
    words = re.findall(r"[\w&+#\-]+", text)
    if not words or re.search(r"[Ѐ-ӿ]", text):
        return False
    for w in words:
        low = normalize(w)
        if low in _ENGLISH_TITLE_WORDS or (w.isupper() and len(w) <= 5) or w.isdigit():
            continue
        return False
    return True


def role_from_tables(text: str, language: str) -> str | None:
    """English job title from the tables, or None."""
    tables = _tables()
    roles = tables["roles"].get(language, {})
    words = tables["role_words"].get(language, {})
    key = _key(text)
    if key in roles:
        return roles[key]
    # Seniority words written in English inside an Italian title ("Contabile senior").
    m = re.fullmatch(r"(?:(senior|junior)\s+)?(.+?)(?:\s+(senior|junior))?", key)
    if m and (m.group(1) or m.group(3)):
        base = roles.get(m.group(2)) or (words.get(m.group(2)) or "").capitalize() or None
        if base:
            level = (m.group(1) or m.group(3)).capitalize()
            return f"{level} {base[:1].lower() + base[1:]}"
    parts = key.split()
    if len(parts) == 1 and key in words:
        return words[key][:1].upper() + words[key][1:]
    if len(parts) == 2 and all(p in words for p in parts):
        noun, modifier = words[parts[0]], words[parts[1]]  # Italian/Ukrainian order: noun first
        if language == "uk":
            modifier, noun = words[parts[0]], words[parts[1]]
        return f"{modifier} {noun}"[:1].upper() + f"{modifier} {noun}"[1:]
    return None


def phrase_from_tables(text: str, language: str) -> str | None:
    tables = _tables()
    key = _key(text)
    for group in ("phrases", "subjects"):
        value = tables[group].get(language, {}).get(key)
        if value:
            return value
    return None


def split_degree(text: str, language: str) -> tuple[str | None, str]:
    """(English degree type, subject in the CV language) of a degree; (None, text) if unknown."""
    key = _key(text)
    for form, english in _tables()["degree_types"].get(language, []):
        if key == form or key.startswith(form + " "):
            rest = _strip_prefix(text, form)
            connector = _CONNECTORS.get(language)
            if connector:
                rest = re.sub(rf"^\s*(?:{connector})\b\s*[:\-–]?\s*", "", rest, flags=re.IGNORECASE)
            rest = rest.strip(" .,;:-–—«»\"“”")
            return english, rest
    return None, text


def _strip_prefix(text: str, norm_prefix: str) -> str:
    """Remove from `text` the words that normalise to `norm_prefix`."""
    words = text.split()
    count = len(norm_prefix.split())
    return " ".join(words[count:])


def _needs_mt(text: str, title: bool = False) -> bool:
    if not text or len(text) > MAX_MT_CHARS or not re.search(r"[^\W\d_]", text):
        return False
    if re.search(r"[Ѐ-ӿ]", text):
        return True
    words = text.split()
    # Acronyms and names of products ("LG", "S7", "Solid Rules", "Ad Hoc Revolution"):
    # never machine-translated.
    if all(w.isupper() or re.search(r"\d", w) for w in words):
        return False
    if not title:
        # A lone word out of context is often a name or an English word ("Hybrid"),
        # which the model mistranslates: only the tables translate it.
        if len(words) == 1:
            return False
        if all(w[:1].isupper() for w in words if len(w) > 3):
            return False
    return True


def _name_like(text: str) -> bool:
    """Product, brand or acronym ("PowerFlex", "Solid Rules", "LG"): nothing to translate."""
    if re.search(r"[Ѐ-ӿ]", text):
        return False
    words = text.split()
    return bool(words) and all(w[:1].isupper() or re.search(r"\d", w) for w in words)


def _clean_mt(source: str, result: str) -> str | None:
    result = " ".join((result or "").split()).strip()
    result = _ARTICLE.sub("", result).rstrip(".")
    if not result or not re.search(r"[^\W\d_]", result) or len(result) > 3 * len(source) + 20:
        return None
    if re.search(r"[Ѐ-ӿ]", result):
        return None
    return _capitalise(result, source)


class _Translator:
    """Collects the texts that need the model and translates them in one batch."""

    def __init__(self, language: str, folder=None, use_mt: bool = True):
        self.language = language
        self.folder = folder
        self.available = use_mt and mt.is_available(language, folder)
        self.used = False
        self._cache: dict[str, str | None] = {}

    def many(self, texts: list[str]) -> dict[str, str | None]:
        todo = [t for t in dict.fromkeys(texts) if t not in self._cache]
        if todo and self.available:
            out = mt.translate(todo, self.language, folder=self.folder)
            for source, result in zip(todo, out):
                cleaned = _clean_mt(source, result) if result != source else None
                self._cache[source] = cleaned
                self.used = self.used or cleaned is not None
        for t in todo:
            self._cache.setdefault(t, None)
        return {t: self._cache[t] for t in texts}


def _esco_label(item: ProfileItem) -> str | None:
    return next((e.text for e in item.evidence if e.source == "esco:en" and e.text.strip()), None)


def to_english(profile: CVProfile, *, folder=None, use_mt: bool = True) -> CVProfile:
    """Translates the profile fields into English in place (and returns the profile).

    `profile.language` stays the language of the CV. Values already English
    (English CV, dictionary skills) are untouched."""
    language = profile.language
    if language not in TRANSLATABLE:
        profile.translated = False
        profile.translation = ""
        return profile
    translator = _Translator(language, folder, use_mt)
    untranslated: list[str] = []

    # Roles and work history titles: ESCO label, tables, then the model.
    role_values: dict[str, str | None] = {}
    pending: list[str] = []

    def plan_title(text: str, esco: str | None = None) -> None:
        if text in role_values:
            return
        if is_english(text):
            role_values[text] = text
        elif esco:
            role_values[text] = _capitalise(esco, text)
        else:
            table = role_from_tables(text, language)
            role_values[text] = table
            if table is None and _needs_mt(text, title=True):
                pending.append(text)

    for item in profile.roles:
        plan_title(item.value, _esco_label(item))
    for exp in profile.experiences:
        same_role = next((r for r in profile.roles if r.value == exp.title), None)
        plan_title(exp.title, _esco_label(same_role) if same_role else None)

    # Skills not in the dictionary.
    skill_values: dict[str, str | None] = {}
    for item in profile.skills:
        if item.original is not None or is_known(item.value) or is_english(item.value):
            continue
        table = phrase_from_tables(item.value, language)
        skill_values[item.value] = table
        if table is None and _needs_mt(item.value):
            pending.append(item.value)

    # Degrees: type from the table, subject from the tables or the model.
    degree_plan: dict[str, tuple[str | None, str]] = {}
    for item in profile.degrees:
        degree_type, subject = split_degree(item.value, language)
        degree_plan[item.value] = (degree_type, subject)
        target = subject if degree_type else item.value
        if target and not phrase_from_tables(target, language) and _needs_mt(target, title=True) and not is_english(target):
            pending.append(target)

    results = translator.many(pending)

    def final_title(text: str) -> str | None:
        return role_values.get(text) or results.get(text)

    for item in profile.roles:
        value = final_title(item.value)
        if value and value != item.value:
            item.original = item.original or item.value
            item.value = value
        elif not value:
            untranslated.append(item.value)
    for exp in profile.experiences:
        value = final_title(exp.title)
        if value and value != exp.title:
            exp.original_title = exp.original_title or exp.title
            exp.title = value
    for item in profile.skills:
        if item.value not in skill_values:
            continue
        value = skill_values[item.value] or results.get(item.value)
        if value and normalize(value) != normalize(item.value):
            item.original = item.value
            item.value = value
        elif not value and not _name_like(item.value):
            untranslated.append(item.value)
    for item in profile.degrees:
        degree_type, subject = degree_plan[item.value]
        if degree_type:
            english_subject = (phrase_from_tables(subject, language) or results.get(subject)
                               or (subject if is_english(subject) else None)) if subject else ""
            if english_subject is None:
                untranslated.append(subject)
                english_subject = subject
            value = f"{degree_type} in {english_subject}" if english_subject else degree_type
        else:
            value = phrase_from_tables(item.value, language) or results.get(item.value)
            if not value:
                if not is_english(item.value):
                    untranslated.append(item.value)
                continue
        if value != item.value:
            item.original = item.original or item.value
            item.value = value

    _merge_duplicates(profile.roles)
    _merge_duplicates(profile.skills)
    profile.translated = True
    profile.translation = "local" if translator.used else "tables"
    profile.untranslated = list(dict.fromkeys(u for u in untranslated if u))
    if profile.untranslated:
        log.info("%d profile values stay in the CV language (%s)%s", len(profile.untranslated), language,
                 "" if translator.available else ": local translation model not installed")
    return profile


def _merge_duplicates(items: list[ProfileItem]) -> None:
    """Two CV wordings that became the same English value: one item, evidence merged."""
    seen: dict[str, ProfileItem] = {}
    kept: list[ProfileItem] = []
    for item in items:
        key = item.value.casefold()
        if key in seen:
            first = seen[key]
            texts = {e.text for e in first.evidence}
            first.evidence.extend(e for e in item.evidence if e.text not in texts)
            first.esco_code = first.esco_code or item.esco_code
            continue
        seen[key] = item
        kept.append(item)
    items[:] = kept
