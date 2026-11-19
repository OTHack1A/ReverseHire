"""A job search query typed like on a job search engine.

    "plc programmer bologna"
    "sviluppatore sql server tempo indeterminato reggio emilia 30 km"
    "\"project manager\" -commerciale modena smart working"

`parse_query(text, place_lookup=None, esco=None)` splits it into role words, skills,
place + radius, contract / working time / remote filters, quoted phrases (must appear)
and minus terms (must not appear), and expands the role with IT <-> EN equivalents
(built-in table, plus the ESCO helper when one is passed in).

`place_lookup(name) -> (lat, lon, province) | None` is typically
`OsmIndex(...).find_place` (local, no network); without it a small built-in gazetteer
is used. `esco` may be an `EscoNormalizer` (only the role title is sent to ESCO, the
existing privacy rule) or any callable `(role, language) -> list[str]`.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from . import lexicon, understanding
from ._util import contains_phrase, fold, log, words

DEFAULT_RADIUS_KM = 30.0
PROVINCE_RADIUS_KM = 40.0
MAX_RADIUS_KM = 300.0

_RADIUS = re.compile(r"(?:\b(?:entro|raggio(?: di)?|within|nel raggio di)\s+)?\b(\d{1,3})\s*(?:km|chilometri)\b")
_QUOTED = re.compile(r"[\"“”«»]([^\"“”«»]+)[\"“”«»]")
_MINUS = re.compile(r"(?:(?<=\s)|^)-([^\s\"]+)")
_PLACE_PREPOSITIONS = ("vicino a", "nei pressi di", "provincia di", "zona", "presso", "near", "in", "a", "di")


@dataclass
class SearchQuery:
    raw: str = ""
    role: str = ""                       # role words as typed (place and filters removed)
    role_words: list[str] = field(default_factory=list)   # content words of the role
    role_variants: list[str] = field(default_factory=list)  # role + translations (+ ESCO labels)
    skills: list[str] = field(default_factory=list)       # canonical skill names found
    phrases: list[str] = field(default_factory=list)      # quoted: must appear
    excluded: list[str] = field(default_factory=list)     # minus terms: must not appear
    place: str | None = None
    province: str | None = None
    lat: float | None = None
    lon: float | None = None
    place_level: str | None = None       # "town" | "province" | "region" | "country"
    region_key: str | None = None        # geo/regions.py key of a region chip ("emilia-romagna")
    country: str | None = None           # ISO code of a country chip ("IT", "CH")
    radius_km: float | None = None
    contract: str | None = None
    hours: str | None = None
    remote: str | None = None
    seniority: str | None = None
    language: str | None = None
    # understanding (jobsearch/understanding.py): words typed as alternatives
    # ("OT/Cybersecurity" -> [["ot", "cybersecurity"]]), the other terms searched for a
    # word or skill ({"cybersecurty": ["cybersecurity", ...]}), the typos corrected and
    # the terms the user removed from "Searched also for".
    alternatives: list[list[str]] = field(default_factory=list)
    expansions: dict[str, list[str]] = field(default_factory=dict)
    corrections: dict[str, str] = field(default_factory=dict)
    removed_terms: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SearchQuery":
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    # --- understanding

    def _kept(self, terms) -> list[str]:
        removed = set(self.removed_terms or [])
        return [t for t in terms if t not in removed]

    def also_terms(self) -> list[tuple[str, str]]:
        """("term", "typo" | "synonym") searched besides the typed words, for the chips
        "Searched also for"; the removed ones are left out."""
        fixed = set((self.corrections or {}).values())
        out: list[tuple[str, str]] = []
        seen = {fold(w) for w in self.role_words}
        latin = not re.search(r"[Ѐ-ӿ]", self.raw or "")
        for terms in (self.expansions or {}).values():
            for term in self._kept(terms):
                if latin and re.search(r"[Ѐ-ӿ]", term):
                    continue  # Ukrainian names are searched too, not listed for a Latin query
                if term not in seen:
                    seen.add(term)
                    out.append((term, "typo" if term in fixed else "synonym"))
        return out

    def remove_term(self, term: str) -> bool:
        term = fold(term)
        if not term or term in self.removed_terms:
            return False
        self.removed_terms.append(term)
        return True

    def _alternative_of(self) -> dict[str, int]:
        return {w: i for i, group in enumerate(self.alternatives or []) if len(group) > 1 for w in group}

    def _absorbed_skills(self) -> set[str]:
        """Skills typed inside a set of alternatives ("OT/Cybersecurity"): they are one of
        the alternatives, not a requirement of every ad."""
        alternative = self._alternative_of()
        out = set()
        for skill in self.skills:
            if any(w in alternative for form in lexicon.skill_forms(skill) for w in form.split()):
                out.add(skill)
        return out

    def _covered_words(self, absorbed: set[str]) -> set[str]:
        """Role words that name a skill (its labels, or a variant typed as a phrase)."""
        folded_role = fold(self.role)
        covered: set[str] = set()
        for skill in self.skills:
            if skill in absorbed:
                continue
            covered.update(w for form in lexicon.skill_labels(skill) for w in form.split())
            for form in lexicon.skill_forms(skill):
                parts = words(form)
                if len(parts) > 1 and contains_phrase(folded_role, form):
                    covered.update(w for w in parts if w not in lexicon.ROLE_NOUNS)
        return covered

    # --- matching

    def word_groups(self) -> list[list[str]]:
        """For each role word that is not generic: the word and its equivalents (plus the
        terms of "Searched also for"); words typed as alternatives share one group."""
        absorbed = self._absorbed_skills()
        covered = self._covered_words(absorbed)
        alternative = self._alternative_of()
        expansions = self.expansions or {}
        groups: list[list[str]] = []
        merged: dict[int, list[str]] = {}

        def extend(group: list[str], terms) -> None:
            for term in self._kept(terms):
                if term and term not in group:
                    group.append(term)

        for word in self.role_words:
            if word in lexicon.GENERIC_WORDS or len(word) < 2:
                continue
            if word in alternative:
                index = alternative[word]
                if index not in merged:
                    merged[index] = []
                    groups.append(merged[index])
                group = merged[index]
            elif word in covered:
                continue
            else:
                group = []
                groups.append(group)
            extend(group, [word])
            extend(group, lexicon.synonyms(word))
            extend(group, expansions.get(word, []))
        for skill in absorbed:
            alternatives = {w for form in lexicon.skill_forms(skill) for w in form.split()}
            for index, group in merged.items():
                if alternatives & set(self.alternatives[index]):
                    extend(group, lexicon.skill_forms(skill))
        return [g for g in groups if g]

    def skill_groups(self) -> list[list[str]]:
        absorbed = self._absorbed_skills()
        return [g for g in (self._kept(lexicon.skill_forms(s)) for s in self.skills if s not in absorbed) if g]

    def search_terms(self) -> list[str]:
        """Folded terms worth looking for in URLs and APIs (role variants, role words and
        their equivalents, skills)."""
        terms: list[str] = []
        for value in self.role_variants + [w for g in self.word_groups() for w in g] + \
                [f for g in self.skill_groups() for f in g]:
            value = fold(value)
            if value and value not in terms:
                terms.append(value)
        return terms

    def relevance(self, title: str, text: str = "") -> float:
        """0..1: how well an ad (title, full text) answers the query; 0 = drop it.

        Kept when: a role variant appears in the title; or more than half of the role word
        groups (at least one) appear in the title; or, with no role words, a skill appears
        in the title or text. Quoted phrases must appear, minus terms must not."""
        folded_title = fold(title)
        folded_all = folded_title + "\n" + fold(text)
        for term in self.excluded:
            if contains_phrase(folded_all, term):
                return 0.0
        for phrase in self.phrases:
            if not contains_phrase(folded_all, phrase):
                return 0.0
        groups = self.word_groups()
        skill_groups = self.skill_groups()
        in_title = sum(1 for g in groups if any(contains_phrase(folded_title, w) for w in g))
        skills_title = sum(1 for g in skill_groups if any(contains_phrase(folded_title, w) for w in g))
        skills_all = sum(1 for g in skill_groups if any(contains_phrase(folded_all, w) for w in g))
        skill_share = skills_all / len(skill_groups) if skill_groups else 0.0
        if any(contains_phrase(folded_title, v) for v in self.role_variants if v):
            return round(min(1.0, 0.8 + 0.2 * skill_share), 3)
        # A skill named inside the role ("programmatore PLC", "sviluppatore SQL Server") is
        # what the role is about: without it anywhere in the ad, "programmatore" alone would
        # keep every developer ad.
        role_skills = []
        for group in skill_groups:
            form_words = {w for form in group for w in fold(form).split()}
            typed = " ".join(w for w in self.role_words if w in form_words)  # "sql server" as written
            if typed:
                role_skills.append(list(group) + [typed])
        if role_skills and not any(contains_phrase(folded_all, w) for g in role_skills for w in g):
            return 0.0
        if groups:
            share = in_title / len(groups)
            if share > 0.5 or (in_title >= 1 and len(groups) == 1):
                return round(min(1.0, 0.4 + 0.4 * share + 0.2 * skill_share), 3)
            if skills_title:
                return round(min(1.0, 0.3 + 0.3 * share + 0.2 * skill_share), 3)
            return 0.0
        if skill_groups and skills_all:
            return round(0.3 + 0.5 * skill_share + (0.2 if skills_title else 0.0), 3)
        if not skill_groups and not self.phrases:
            return 0.5  # place / filter only query
        return 0.0 if not self.phrases else 0.5


def _take(pattern: str, text: str) -> tuple[str | None, str]:
    """Remove the first whole-word occurrence of a folded phrase; (phrase found, rest)."""
    found = re.search(r"(?<![a-z0-9])" + re.escape(pattern) + r"(?![a-z0-9])", text)
    if not found:
        return None, text
    return pattern, (text[:found.start()] + " " + text[found.end():])


def _lookup(name: str, place_lookup) -> tuple[float, float, str] | None:
    if place_lookup is not None:
        try:
            found = place_lookup(name)
        except Exception as error:  # the local index may be missing
            log.info("Place lookup failed: %s", type(error).__name__)
            found = None
        if found:
            lat, lon = float(found[0]), float(found[1])
            province = found[2] if len(found) > 2 else ""
            return lat, lon, province or ""
    return lexicon.builtin_place(name)


def _find_place(tokens: list[str], place_lookup) -> tuple[int, int, str, tuple] | None:
    """(start, end, name, (lat, lon, province)) of the longest place n-gram, preferring
    the end of the query."""
    best = None
    for size in (4, 3, 2, 1):
        for start in range(len(tokens) - size, -1, -1):
            name = " ".join(tokens[start:start + size])
            if len(name) < 3 or name in lexicon.GENERIC_WORDS or lexicon.group_id(name) != name:
                continue
            at_end = start + size == len(tokens)
            after_preposition = start > 0 and tokens[start - 1] in ("a", "in", "di", "presso", "near", "zona")
            if not (at_end or after_preposition):
                found = lexicon.builtin_place(name)  # inside the query: only well-known towns
            else:
                found = _lookup(name, place_lookup)
            if found:
                best = (start, start + size, name, found)
                break
        if best:
            break
    return best


def _is_area_name(name: str) -> bool:
    from ..geo import italy

    return bool(italy.region_key(name) or (len(name) > 2 and italy.country_code(name)))


def _structured_place(query: SearchQuery, tokens: list[str], province_mode: bool) -> list[str]:
    """Province, region and country chips from the Italian tables of geo/italy.py:
    "provincia di Monza e della Brianza", "Emilia-Romagna", "Lombardy", "Italia",
    "Svizzera". Fills the query and returns the tokens without the place."""
    from ..geo import italy

    for size in (5, 4, 3, 2, 1):
        for start in range(len(tokens) - size, -1, -1):
            name = " ".join(tokens[start:start + size])
            if len(name) < 3:
                continue
            end = start + size
            province = italy.province_by_name(name) if province_mode else None
            if province is not None:
                query.place, query.province, query.place_level = province.name, province.code, "province"
                query.lat, query.lon = province.lat, province.lon
            elif italy.region_key(name):
                query.region_key = italy.region_key(name)
                query.place, query.place_level = italy.region_name(query.region_key), "region"
            elif italy.country_code(name) and len(name) > 2:
                query.country = italy.country_code(name)
                query.place, query.place_level = italy.country_name(query.country, "it"), "country"
            else:
                continue
            while start > 0 and tokens[start - 1] in ("a", "in", "di", "presso", "near", "zona", "provincia",
                                                     "prov", "of", "province", "regione", "region"):
                start -= 1
            return tokens[:start] + tokens[end:]
    return tokens


def _esco_variants(role: str, esco, language: str) -> list[str]:
    if esco is None or not role:
        return []
    try:
        if hasattr(esco, "normalize"):
            occupation = esco.normalize(role, language)
            if occupation is None:
                return []
            return [x for x in (getattr(occupation, "label_it", None), getattr(occupation, "label_en", None),
                                getattr(occupation, "label", None)) if x]
        return [str(x) for x in (esco(role, language) or []) if x]
    except Exception as error:  # ESCO is optional and may be offline
        log.info("ESCO expansion unavailable: %s", type(error).__name__)
        return []


def _translations(role_words: list[str]) -> list[str]:
    """The role phrase with each word replaced by its first equivalent in the other
    language, both word orders for two words ("plc programmer" -> "programmatore plc")."""
    content = [w for w in role_words if w not in lexicon.GENERIC_WORDS]
    if not content:
        return []
    variants = []
    swapped = []
    for word in content:
        options = lexicon.synonyms(word)
        swapped.append(options[1] if len(options) > 1 else word)
    if swapped != content:
        variants.append(" ".join(swapped))
        if len(swapped) == 2:
            variants.append(" ".join(reversed(swapped)))
    return variants


def _understand(query: SearchQuery) -> None:
    """Typos corrected (the corrected skills join the skills), synonyms and IT <-> EN
    equivalents of the role words and skills (jobsearch/understanding.py)."""
    try:
        _expansions, corrections = understanding.expansions_for(query.role_words, query.skills)
        if corrections:
            corrected = " ".join(corrections.get(w, w) for w in words(query.role))
            for skill in lexicon.extract_skills(corrected + "\n" + corrected.upper()):
                if skill not in query.skills:
                    query.skills.append(skill)
        query.expansions, query.corrections = understanding.expansions_for(query.role_words, query.skills)
    except Exception as error:  # noqa: BLE001 - the exact words still work
        log.info("Query expansion skipped: %s", type(error).__name__)


def apply_understanding(query: SearchQuery) -> SearchQuery:
    """Fills the understanding fields of a query saved before they existed (or edited)."""
    if not query.expansions and query.role_words:
        _understand(query)
    return query


def parse_query(text: str, *, place_lookup=None, esco=None, default_radius_km: float = DEFAULT_RADIUS_KM
                ) -> SearchQuery:
    """Parse a free-text query. Never raises: odd input gives a query with fewer fields."""
    query = SearchQuery(raw=str(text or ""))
    try:
        rest = " " + str(text or "") + " "
        query.phrases = [fold(p) for p in _QUOTED.findall(rest) if fold(p)]
        rest = _QUOTED.sub(" ", rest)
        query.excluded = [fold(m) for m in _MINUS.findall(rest) if fold(m)]
        rest = _MINUS.sub(" ", rest)
        folded = " " + fold(rest).replace(",", " ").replace(";", " ") + " "
        radius = _RADIUS.search(folded)
        if radius:
            query.radius_km = max(1.0, min(MAX_RADIUS_KM, float(radius.group(1))))
            folded = folded[:radius.start()] + " " + folded[radius.end():]
        for table, attribute in ((lexicon.CONTRACT_WORDS, "contract"), (lexicon.HOURS_WORDS, "hours"),
                                 (lexicon.REMOTE_WORDS, "remote")):
            for phrase, value in table:
                found, folded = _take(phrase, folded)
                if found and getattr(query, attribute) is None:
                    setattr(query, attribute, value)
        tokens = [t for t in re.findall(r"[a-z0-9+#.'/-]+", folded) if t.strip(".'/-")]
        tokens = [t.strip(".'") for t in tokens]
        # "OT/Cybersecurity", "OT-Cybersecurity", "Sales+Marketing": separate words (the
        # slash: alternatives), unless the whole token is a known term ("pl/sql", "c++")
        split: list[str] = []
        alternatives: list[list[str]] = []
        for token in tokens:
            parts, either = understanding.split_token(token)
            if either:
                alternatives.append(parts)
            split += parts
        tokens = split
        for token in list(tokens):
            if token in lexicon.SENIORITY_WORDS and query.seniority is None:
                query.seniority = lexicon.SENIORITY_WORDS[token]
                tokens.remove(token)
        # "provincia di X"
        province_mode = False
        joined = " ".join(tokens)
        if re.search(r"\bprovincia di\b|\bprov\.? di\b|\bprovince of\b", joined):
            province_mode = True
        place = _find_place(tokens, place_lookup)
        if place is not None and _is_area_name(place[2]):
            place = None   # "Svizzera", "Lombardia": a country / region chip, not a hamlet of that name
        if place is not None:
            start, end, name, (lat, lon, province) = place
            # drop prepositions right before the place
            while start > 0 and tokens[start - 1] in ("a", "in", "di", "presso", "near", "zona", "vicino",
                                                     "provincia", "prov", "nei", "pressi", "of", "province"):
                start -= 1
                if tokens[start] == "di" and start > 0 and tokens[start - 1] in ("provincia", "prov", "pressi"):
                    continue
            query.place = name
            query.lat, query.lon = lat, lon
            query.province = province or None
            query.place_level = "province" if province_mode else "town"
            tokens = tokens[:start] + tokens[end:]
        else:
            tokens = _structured_place(query, tokens, province_mode)
        if query.place and query.radius_km is None and query.place_level not in ("region", "country"):
            query.radius_km = PROVINCE_RADIUS_KM if province_mode else default_radius_km
        role_tokens = [t for t in tokens if t not in ("km",)]
        while role_tokens and role_tokens[-1] in ("a", "in", "di", "presso", "near", "zona", "a/in"):
            role_tokens.pop()
        query.role = " ".join(role_tokens).strip()
        # upper-case copy too: short case-sensitive skills ("PLC", "SAP", "SQL") of a lower-case query
        skill_text = query.role + " " + " ".join(query.phrases)
        query.skills = lexicon.extract_skills(skill_text + "\n" + skill_text.upper())
        query.role_words = [w for w in words(query.role) if w not in lexicon.GENERIC_WORDS]
        query.alternatives = [[w for w in group if w in query.role_words] for group in alternatives]
        query.alternatives = [g for g in query.alternatives if len(g) > 1]
        _understand(query)
        query.language = lexicon.detect_language(query.raw) or (
            "it" if any(w in lexicon.WORD_GROUPS[0][:4] for w in query.role_words) else None)
        variants: list[str] = []
        for value in [query.role] + _translations(query.role_words) + \
                _esco_variants(query.role, esco, query.language or "it"):
            value = fold(value)
            if value and value not in variants:
                variants.append(value)
        query.role_variants = variants
    except Exception as error:  # never let a query crash the search
        log.warning("Query not fully understood: %s", type(error).__name__)
    return query
