"""CV/posting fit (0..100) with requirements recognized by IT/EN rules.

For each posting:

- ``title``: closeness of the posting title to the CV roles, the maximum of
  the calibrated embedding similarity and rapidfuzz ``token_set_ratio``.
- ``skills``: CV skills found in the description (saturating at
  ``SKILLS_FOR_MAX``).
- base = weighted mean of the two (weights ``TITLE_WEIGHT``/``SKILLS_WEIGHT``).
- ``role`` (only when the ESCO essential skills of the candidate's occupation
  are given): share of those skills named in the posting, saturating at
  ``ROLE_SKILLS_FOR_MAX``; then the weights are ``WEIGHTS_WITH_ROLE``.
- requirement coverage (``scoring/requirements.py``): every requirement unit
  of the description with an anchored 0..4 level and the CV line quoted;
  ``blended = (1 - COVERAGE_SHARE) * base + COVERAGE_SHARE * coverage``.
- requirements (years of experience, language and level, degree): each unmet
  *blocking* requirement multiplies the score by ``BLOCKING_FACTOR``; an unmet
  *desirable* one by ``DESIRABLE_FACTOR``; a requirement that cannot be
  verified from the CV does not penalize. The same factors apply to licence,
  own car and business travel explicitly contradicted by the CV (level 0;
  travel level 1 = desirable factor).
- gate: ``blocked`` is True when a must-have of a hard type is explicitly
  contradicted (see ``requirements.GATE_KINDS``); silence never blocks.

    fit = 100 * blended * product(factors)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
from rapidfuzz import fuzz

from ..models import item_variants

from .requirements import (
    CVEvidence,
    build_cv_evidence,
    check_items,
    coverage as requirement_coverage,
    extract_items,
)
from .relevance import (
    SYNONYMS,
    TextIndex,
    calibrate_similarity,
    range_of,
    normalize_text,
    profile_terms,
    unaccented,
)

# --- weights and thresholds -----------------------------------------------------------

TITLE_WEIGHT = 0.6
SKILLS_WEIGHT = 0.4
WEIGHTS_WITH_ROLE = {"title": 0.5, "skills": 0.3, "role": 0.2}
ROLE_SKILLS_FOR_MAX = 3
ROLE_SKILL_COVERAGE = 0.5  # share of the words of an ESCO skill that must appear
COVERAGE_SHARE = 0.4
SKILLS_FOR_MAX = 3
MAX_ROLES = 5
MAX_SKILLS = 25
MIN_DESCRIPTION_CHARS = 40
# A posting without a readable description is judged on its title alone: the fit is
# discounted so that it cannot outrank postings whose requirements were actually checked.
TITLE_ONLY_FACTOR = 0.7
MIN_TITLE_FUZZY = 50  # token_set_ratio <= 50 -> 0, 100 -> 1 (linear)
BLOCKING_FACTOR = 0.35
DESIRABLE_FACTOR = 0.9
YEARS_TOLERANCE = 0.5
DEFAULT_LANGUAGE_LEVEL = "B1"  # language requirement without an explicit level
CV_LANGUAGE_LEVEL = "C1"  # language the CV is written in
MAX_LEVEL_WITHOUT_DATA = "B2"  # language in the CV without a level: fine up to this one

CEFR_LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]

SATISFIED = "satisfied"
NOT_MET = "not_met"
UNVERIFIABLE = "unverifiable"

# Language code -> words that name it (IT/EN, normalized).
LANGUAGES: dict[str, list[str]] = {
    "en": ["inglese", "english"],
    "fr": ["francese", "french"],
    "de": ["tedesco", "german"],
    "es": ["spagnolo", "spanish"],
    "it": ["italiano", "italian"],
    "pt": ["portoghese", "portuguese"],
    "ru": ["russo", "russian"],
    "zh": ["cinese", "chinese", "mandarino", "mandarin"],
    "ar": ["arabo", "arabic"],
    "ja": ["giapponese", "japanese"],
    "nl": ["olandese", "dutch"],
    "uk": ["ucraino", "ukrainian"],
    "pl": ["polacco", "polish"],
    "ro": ["rumeno", "romanian"],
}

# Words that make a language mention in a sentence a "requirement".
LANGUAGE_CONTEXT = (
    "lingua", "conoscenza", "fluent", "madrelingua", "livello", "parlat", "scritt",
    "padronanza", "language", "proficien", "native", "spoken", "written", "level",
    "speak", "bilingu", "comunicare", "communicat",
)

# CEFR level inferred from words (prefixes), in priority order.
LEVEL_WORDS: list[tuple[tuple[str, ...], str]] = [
    (("madrelingua", "native", "bilingu", "mother tongue"), "C2"),
    (("fluent", "fluenc", "ottim", "eccellent", "excellent", "avanzat", "advanced",
      "padronanza", "proficien", "perfett", "perfect"), "C1"),
    (("buon", "good", "intermedi", "discret", "solid"), "B1"),
    (("=base", "basic", "scolastic", "elementar", "elementary"), "A2"),
    (("learning", "in studio", "studying"), "A1"),  # "German (learning)"
]
# In the tuples: "prefix" matches the start of a word, "=word" only the whole
# word ("base" must not match "based"), "two words" matches within the sentence.

# Context that makes a requirement "desirable" (non-blocking).
DESIRABLE = re.compile(
    r"preferibil|gradit|preferenzial|costituisce (un )?(titolo|plus)|\bun plus\b|\bplus\b"
    r"|nice to have|preferred|preferably|is a plus|desirable|ideally|bonus|advantage|vantaggio",
)

_NUMBER_WORDS = {
    "un": 1, "uno": 1, "una": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6,
    "sette": 7, "otto": 8, "nove": 9, "dieci": 10, "one": 1, "two": 2, "three": 3,
    "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_N = r"\b(\d{1,2}|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"

YEARS_PATTERNS = [
    # IT: "almeno 3 anni di esperienza", "3+ anni di comprovata esperienza", "2-4 anni nel ruolo"
    re.compile(_N + r"\s*(?:\+|-\s*\d{1,2}|o piu)?\s*ann[oi]\s+(?:di\s+)?(?:\w+\s+){0,2}?esperienz"),
    # IT: "esperienza (pregressa nel ruolo) di almeno 3 anni"
    re.compile(r"esperienz\w*\s+(?:[\w']+\s+){0,5}?(?:di\s+)?(?:almeno|minimo|oltre|superiore a)?\s*"
               + _N + r"\s*\+?\s*ann[oi]"),
    # EN: "5+ years of experience", "3-5 years' relevant experience"
    re.compile(_N + r"\s*(?:\+|-\s*\d{1,2}|or more|plus)?\s*years?'?\s+(?:of\s+)?(?:[\w-]+\s+){0,3}?experience"),
    # EN: "at least 3 years", "minimum of 2 years"
    re.compile(r"(?:at least|minimum(?: of)?|min\.?|more than|over)\s*" + _N + r"\s*\+?\s*years?"),
    # EN: "experience of at least 3 years"
    re.compile(r"experience\s+(?:of\s+)?(?:at least\s+)?" + _N + r"\s*\+?\s*years?"),
]

# Degrees: level -> expressions (on lowercase text without accents).
DEGREES: list[tuple[int, re.Pattern]] = [
    (4, re.compile(r"\b(dottorato|phd|ph\.\s?d\.?|doctorate|doctoral)\b")),
    (3, re.compile(r"\blaurea (magistrale|specialistica|quinquennale|vecchio ordinamento)\b"
                   r"|\bmaster'?s?\s+degree\b|\bm\.?\s?sc\b")),
    (2, re.compile(r"\blaure[ae]\b|\blaureat[oaie]\b|\bdegree\b|\bbachelor\w*\b|\bb\.?\s?sc\b")),
    (1, re.compile(r"\bdiploma\b|\bhigh school\b|\bmaturita\b|\bperito\b|\bragionier\w*\b"
                   r"|\bliceo\b|\bistituto tecnico\b|\bqualifica professionale\b")),
]
_DEGREE_ALTERNATIVES = re.compile(
    r"\b(diploma|laurea|degree)\b[^,;.]{0,30}?\s(o|or|oppure|/)\s+(di\s+|a\s+)?\b(diploma|laurea|degree)\b"
)


@dataclass
class Requirement:
    kind: str  # "years", "language", "degree"
    status: str  # SATISFIED, NOT_MET, UNVERIFIABLE
    blocking: bool
    sentence: str
    data: dict = field(default_factory=dict)  # years / language, level / degree level; CV values


@dataclass
class PostingResult:
    posting: object
    score: float
    title: float | None = None  # 0..1
    nearest_role: str | None = None
    skills: float | None = None  # 0..1
    found_skills: list[str] = field(default_factory=list)
    requirements: list[Requirement] = field(default_factory=list)
    checks: list = field(default_factory=list)  # list[models.RequirementCheck]
    coverage: float | None = None  # 0..1 weighted requirement coverage
    role: float | None = None  # 0..1 ESCO essential skills of the CV occupation in the posting
    found_role_skills: list[str] = field(default_factory=list)
    blocked: bool = False
    blocked_by: object | None = None  # the first blocking RequirementCheck


# --- helpers -----------------------------------------------------------------


def _phrases(text: str) -> list[str]:
    raw_sentences = re.split(r"(?<=[.!?;:])\s+|\n+|\s*[•·▪●\-–]\s+(?=[A-ZÀ-Üa-z])", text or "")
    return [" ".join(f.split()) for f in raw_sentences if f and f.strip()]


def _lower(text: str) -> str:
    return " ".join(unaccented(text).lower().split())


def _to_number(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


def _level_from_text(norm_text: str) -> str | None:
    m = re.search(r"\b([abc][12])\b", norm_text)
    if m:
        return m.group(1).upper()
    words = norm_text.split()
    for prefixes, level in LEVEL_WORDS:
        for prefix in prefixes:
            if prefix.startswith("="):
                if prefix[1:] in words:
                    return level
            elif " " in prefix:
                if prefix in norm_text:
                    return level
            elif any(p.startswith(prefix) for p in words):
                return level
    return None


def _cefr_index(level: str) -> int:
    return CEFR_LEVELS.index(level) if level in CEFR_LEVELS else 0


def _languages_in(text: str) -> list[tuple[str, int]]:
    """(code, position) of the languages named in the text (lowercase, no accents)."""
    found_items = []
    for code, words in LANGUAGES.items():
        for p in words:
            root = p[:-1] if p[-1] in "aoie" else p
            m = re.search(rf"\b{root}[aoie]?\b", text)
            if m:
                found_items.append((code, m.start()))
                break
    return found_items


def _degree_level_pos(text: str) -> tuple[int, int] | None:
    """(level, position) of the degree mentioned, or None."""
    matches = []
    for level, pattern in DEGREES:
        m = pattern.search(text)
        if m:
            matches.append((level, m.start()))
    if not matches:
        return None
    if _DEGREE_ALTERNATIVES.search(text):
        return min(matches)
    return max(matches)


def _degree_level(text: str) -> int | None:
    found = _degree_level_pos(_lower(text))
    return None if found is None else found[0]


def _clause(text: str, pos: int) -> str:
    """Part of the sentence between the commas/parentheses around pos."""
    start = max(text.rfind(c, 0, pos) for c in ",;()") + 1
    ends = [i for i in (text.find(c, pos) for c in ",;()") if i >= 0]
    return text[start : min(ends) if ends else len(text)]


# --- requirements ---------------------------------------------------------------


def extract_requirements(description: str) -> list[tuple[str, dict, bool, str]]:
    """Recognized requirements: (kind, data, blocking, sentence).

    A requirement is desirable (non-blocking) if its clause contains words such
    as "preferibile", "gradita", "nice to have", or if it follows a heading of
    that kind ("Costituirà titolo preferenziale:").
    """
    matches: list[tuple[str, dict, bool, str]] = []
    seen: set = set()
    desirable_context = False
    for sentence in _phrases(description):
        lower_sentence = _lower(sentence)
        heading = lower_sentence.endswith(":")
        if heading:
            desirable_context = bool(DESIRABLE.search(lower_sentence))

        def blocking(pos: int) -> bool:
            return not (desirable_context or DESIRABLE.search(_clause(lower_sentence, pos)))

        years = []
        for pattern in YEARS_PATTERNS:
            for m in pattern.finditer(lower_sentence):
                n = _to_number(m.group(1))
                if n is not None and 0 < n <= 40:
                    years.append((n, m.start()))
        if years and ("years", None) not in seen:
            seen.add(("years", None))
            n, pos = max(years)
            matches.append(("years", {"years": n}, blocking(pos), sentence))

        norm = normalize_text(sentence)
        context = any(c in norm for c in LANGUAGE_CONTEXT) or re.search(r"\b[abc][12]\b", norm)
        for code, pos in _languages_in(lower_sentence):
            if ("language", code) in seen:
                continue
            parenthetical_level = _level_from_text(normalize_text(_clause(lower_sentence, pos)))
            # A language is a requirement if the sentence talks about languages/levels
            # or the clause states a level ("good English"); "Italian company" does not.
            if not (context or parenthetical_level):
                continue
            level = parenthetical_level or _level_from_text(norm) or DEFAULT_LANGUAGE_LEVEL
            seen.add(("language", code))
            matches.append(("language", {"language": code, "level": level}, blocking(pos), sentence))

        title = _degree_level_pos(lower_sentence)
        if title is not None and ("degree", None) not in seen:
            seen.add(("degree", None))
            matches.append(("degree", {"level": title[0]}, blocking(title[1]), sentence))
    return matches


def cv_languages(profile) -> dict[str, str | None]:
    """Language code -> CEFR level (None if not stated)."""
    languages: dict[str, str | None] = {}
    for item in getattr(profile, "spoken_languages", []) or []:
        for text in item_variants(item):  # English value and CV wording
            level = _level_from_text(normalize_text(text))
            for code, _ in _languages_in(_lower(text)):
                previous = languages.get(code)
                if previous is None or (level and _cefr_index(level) > _cefr_index(previous)):
                    languages[code] = level
    cv_language = (getattr(profile, "language", "") or "").lower()[:2]
    if cv_language in LANGUAGES:
        existing = languages.get(cv_language)
        if existing is None or _cefr_index(existing) < _cefr_index(CV_LANGUAGE_LEVEL):
            languages[cv_language] = CV_LANGUAGE_LEVEL
    return languages


def cv_degree_level(profile) -> int | None:
    levels = [
        _degree_level(text)
        for v in getattr(profile, "degrees", []) or []
        for text in item_variants(v)
    ]
    levels = [x for x in levels if x is not None]
    return max(levels) if levels else None


def check_requirement(kind: str, data: dict, profile) -> tuple[str, dict]:
    """(status, data updated with the CV values)."""
    data = dict(data)
    if kind == "years":
        cv_years = getattr(profile, "years_experience", None)
        data["cv"] = cv_years
        if cv_years is None:
            return UNVERIFIABLE, data
        return (SATISFIED if cv_years + YEARS_TOLERANCE >= data["years"] else NOT_MET), data
    if kind == "language":
        languages = cv_languages(profile)
        code, requested = data["language"], data["level"]
        if code not in languages:
            data["cv"] = None
            if not getattr(profile, "spoken_languages", None):
                return UNVERIFIABLE, data
            return NOT_MET, data
        cv_level = languages[code]
        data["cv"] = cv_level
        if cv_level is None:
            if _cefr_index(requested) <= _cefr_index(MAX_LEVEL_WITHOUT_DATA):
                return SATISFIED, data
            return UNVERIFIABLE, data
        return (SATISFIED if _cefr_index(cv_level) >= _cefr_index(requested) else NOT_MET), data
    if kind == "degree":
        cv_level = cv_degree_level(profile)
        data["cv"] = cv_level
        if cv_level is None:
            return UNVERIFIABLE, data
        return (SATISFIED if cv_level >= data["level"] else NOT_MET), data
    return UNVERIFIABLE, data


def posting_requirements(description: str, profile) -> list[Requirement]:
    result = []
    for kind, data, blocking, sentence in extract_requirements(description):
        status, data = check_requirement(kind, data, profile)
        result.append(Requirement(kind=kind, status=status, blocking=blocking, sentence=sentence, data=data))
    return result


# --- score ---------------------------------------------------------------


def _values(items, n: int) -> list[str]:
    result = []
    for v in (items or [])[:n]:
        value = (getattr(v, "value", v) or "").strip()
        if value and value not in result:
            result.append(value)
    return result


def title_variants(title: str) -> list[str]:
    """The normalized title and its word-by-word IT/EN translations
    ("Senior Accountant" -> "senior contabile", "senior commercialista")."""
    norm = normalize_text(title)
    forms = [norm]
    words = norm.split()
    for k, word in enumerate(words):
        for synonym in sorted(SYNONYMS.get(word, ())):
            form = " ".join(words[:k] + [synonym] + words[k + 1 :])
            if form not in forms:
                forms.append(form)
    for synonym in sorted(SYNONYMS.get(norm, ())):
        if synonym not in forms:
            forms.append(synonym)
    return forms


def _title_proximity(titles: list[str], roles: list[str], embedder) -> list[tuple[float | None, str | None]]:
    if not roles:
        return [(None, None)] * len(titles)
    interval = tuple(getattr(embedder, "title_range", range_of(embedder)))
    try:
        vt = np.asarray(embedder.embed(titles, kind="query"))
        vr = np.asarray(embedder.embed(roles, kind="query"))
        semantic = vt @ vr.T
    except Exception:
        semantic = None
    result = []
    for i, title in enumerate(titles):
        forms = title_variants(title)
        best, role = -1.0, None
        for j, r in enumerate(roles):
            r_norm = normalize_text(r)
            fuzzy = max(fuzz.token_set_ratio(f, r_norm) for f in forms)
            v_fuzzy = max(0.0, (fuzzy - MIN_TITLE_FUZZY) / (100 - MIN_TITLE_FUZZY))
            v_sem = calibrate_similarity(float(semantic[i, j]), interval) / 100 if semantic is not None else 0.0
            v = max(v_fuzzy, v_sem)
            if v > best:
                best, role = v, r
        result.append((min(1.0, best), role))
    return result


def role_coverage(text: str, role_skills: list[str] | None) -> tuple[float | None, list[str]]:
    """(0..1, ESCO skills found) for the essential skills of the candidate's occupation."""
    if not role_skills or len((text or "").strip()) < MIN_DESCRIPTION_CHARS:
        return None, []
    index = TextIndex(text)
    found_items = [k for k in role_skills if index.coverage(k) >= ROLE_SKILL_COVERAGE]
    return min(1.0, len(found_items) / ROLE_SKILLS_FOR_MAX), found_items


def assess_posting(posting, profile, title: float | None, role: str | None, *,
                   embedder=None, cv: CVEvidence | None = None,
                   role_skills: list[str] | None = None,
                   max_travel_days: int | None = None) -> PostingResult:
    description = getattr(posting, "description", "") or ""
    cv_skills = profile_terms(profile.skills, MAX_SKILLS)
    skills, found_items = None, []
    if cv_skills and len(description.strip()) >= MIN_DESCRIPTION_CHARS:
        index = TextIndex(f"{posting.title} {description}")
        found_items = [c for c, forms in cv_skills if any(index.contains(f) for f in forms)]
        skills = min(1.0, len(found_items) / SKILLS_FOR_MAX)
    role_value, role_found = role_coverage(f"{posting.title} {description}", role_skills)

    if role_value is None:
        weights = [(TITLE_WEIGHT, title), (SKILLS_WEIGHT, skills)]
    else:
        weights = [(WEIGHTS_WITH_ROLE["title"], title), (WEIGHTS_WITH_ROLE["skills"], skills),
                   (WEIGHTS_WITH_ROLE["role"], role_value)]
    valid_items = [(p, v) for p, v in weights if v is not None]
    base = sum(p * v for p, v in valid_items) / sum(p for p, _ in valid_items) if valid_items else 0.0

    checks = check_items(extract_items(description, posting), profile, embedder, cv, max_travel_days)
    cover = requirement_coverage(checks)
    value = base if cover is None else (1 - COVERAGE_SHARE) * base + COVERAGE_SHARE * cover

    requirements = posting_requirements(description, profile)
    for r in requirements:
        if r.status == NOT_MET:
            value *= BLOCKING_FACTOR if r.blocking else DESIRABLE_FACTOR
    for c in checks:
        if c.kind in ("licence", "car", "travel") and c.level == 0:
            value *= BLOCKING_FACTOR if c.priority == "must" else DESIRABLE_FACTOR
        elif c.kind == "travel" and c.level == 1:
            value *= DESIRABLE_FACTOR
    if len(description.strip()) < MIN_DESCRIPTION_CHARS:
        value *= TITLE_ONLY_FACTOR
    blocking = [c for c in checks if c.blocking]

    return PostingResult(
        posting=posting,
        score=round(float(np.clip(value, 0.0, 1.0)) * 100.0, 1),
        title=title,
        nearest_role=role,
        skills=skills,
        found_skills=found_items,
        requirements=requirements,
        checks=checks,
        coverage=None if cover is None else round(cover, 3),
        role=role_value,
        found_role_skills=role_found,
        blocked=bool(blocking),
        blocked_by=blocking[0] if blocking else None,
    )


def compute_fit(profile, postings, embedder, *, role_skills: list[str] | None = None,
                max_travel_days: int | None = None) -> list[PostingResult]:
    """One result per posting, sorted from best fit (ties keep the original order).

    ``role_skills``: ESCO essential skills of the candidate's occupation
    (``cv.esco.essential_skills``), optional. ``max_travel_days``: business
    travel days a year accepted (None or -1 = the CV value).
    """
    postings = list(postings or [])
    if not postings:
        return []
    # Every role is matched with its English value and its CV wording; the
    # nearest role is reported with the displayed value.
    terms = profile_terms(profile.roles, MAX_ROLES)
    forms = [f for _, fs in terms for f in fs]
    display = {f: d for d, fs in terms for f in fs}
    proximities = [(t, display.get(r, r) if r else r)
                   for t, r in _title_proximity([a.title or "" for a in postings], forms, embedder)]
    cv = build_cv_evidence(profile, embedder)
    results = [
        assess_posting(a, profile, t, r, embedder=embedder, cv=cv, role_skills=role_skills,
                       max_travel_days=max_travel_days)
        for a, (t, r) in zip(postings, proximities)
    ]
    return sorted(results, key=lambda r: -r.score)
