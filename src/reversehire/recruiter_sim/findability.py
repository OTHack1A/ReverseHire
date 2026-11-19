"""Check 4, findability: the boolean searches a recruiter would type for the target
(job title, 2-4 hard skills, town; study §1.4/§1.5 #5) run on the parsed CV,
literally and after normalisation (synonyms of the app's skill dictionary).

A query is a list of AND groups, each an OR of terms. Each term is tested in
three ways: literal whole word, normalised (taxonomy) and with the profile's
keyword mode (which decides the hit for this system).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..cv.english import role_from_tables
from .cvindex import LITERAL, SEMANTIC, TAXONOMY, CvIndex
from .match import MatchResult, term_evidence
from .model import (CHECK_FINDABILITY, COMPONENT_FINDABILITY, COMPONENT_MATCH, FAIL, OK, SOURCE_CV,
                    SOURCE_POSTING, WARN, Edit, EvidenceRef, Finding, Target, msg)
from .parse_view import short

MAX_SKILLS = 4
MAX_QUERIES = 10
_TITLE_NOISE = re.compile(r"\((?:m/?f|f/?m|m/w/d|h/f|all genders?|[mf]/[fm]/[dx])\)|\b(?:m/f|f/m|m/w/d)\b",
                          re.IGNORECASE)
_TITLE_SPLIT = re.compile(r"\s+[-–—|]\s+|\s*,\s*")


@dataclass
class TermResult:
    term: str
    literal: bool
    normalised: bool
    mode_hit: bool
    cv_term: str | None = None
    cv_line: str | None = None


@dataclass
class Query:
    groups: list[list[str]]  # AND of ORs
    terms: list[list[TermResult]] = field(default_factory=list)
    titles: tuple[str, ...] = ()  # the job-title terms of the target

    @property
    def text(self) -> str:
        parts = []
        for group in self.groups:
            quoted = [f'"{t}"' if " " in t else t for t in group]
            parts.append(quoted[0] if len(quoted) == 1 else "(" + " OR ".join(quoted) + ")")
        return " AND ".join(parts)

    def hit(self, attribute: str) -> bool:
        return all(any(getattr(t, attribute) for t in group) for group in self.terms)

    def to_dict(self) -> dict:
        return {"query": self.text, "literal": self.hit("literal"), "normalised": self.hit("normalised"),
                "hit": self.hit("mode_hit"),
                "terms": [[t.__dict__ for t in g] for g in self.terms]}


def clean_title(title: str) -> str:
    title = _TITLE_NOISE.sub(" ", title or "")
    first = _TITLE_SPLIT.split(title.strip())[0] if title.strip() else ""
    return " ".join(first.split())


def title_terms(title: str, language: str = "it") -> list[str]:
    title = clean_title(title)
    if not title:
        return []
    terms = [title]
    for lang in (language, "it"):
        english = role_from_tables(title, lang)
        if english and english.lower() not in (t.lower() for t in terms):
            terms.append(english)
    return terms


def build_queries(target: Target, match: MatchResult | None, cv_language: str = "it") -> list[Query]:
    titles = title_terms(target.title, cv_language)
    skills: list[str] = []
    if match is not None:
        ranked = sorted((i for i in match.skill_items if i.priority in ("must", "nice") and len(i.text.split()) <= 4),
                        key=lambda i: (i.priority != "must", -i.weight))
        for i in ranked:
            if i.text.lower() not in (s.lower() for s in skills):
                skills.append(i.text)
    skills.extend(s for s in target.skills if s.lower() not in (x.lower() for x in skills))
    skills = skills[:MAX_SKILLS]
    town = (target.location or "").split(",")[0].strip()
    town = re.sub(r"\(.*?\)", "", town).strip()
    queries: list[list[list[str]]] = []
    if titles:
        queries.append([titles])
        if skills:
            queries.append([titles, [skills[0]]])
    if len(skills) >= 2:
        queries.append([[s] for s in skills[:3]])
    queries.extend([[s]] for s in skills)
    if titles and town and not re.search(r"remot|ibrid|hybrid|italia|italy", town, re.IGNORECASE):
        queries.append([titles, [town]])
    unique: list[Query] = []
    seen = set()
    for groups in queries:
        key = tuple(tuple(g) for g in groups)
        if key not in seen:
            seen.add(key)
            unique.append(Query(groups, titles=tuple(titles)))
    return unique[:MAX_QUERIES]


def _term_result(term: str, index: CvIndex, mode: str, embeddings) -> TermResult:
    literal_hits = index.find(term, LITERAL)
    normal_hits = literal_hits or index.find(term, TAXONOMY)
    if mode == LITERAL:
        mode_hit = bool(literal_hits)
    elif mode == SEMANTIC:
        mode_hit = bool(normal_hits) or term_evidence(term, index, SEMANTIC, embeddings).level >= 2
    else:
        mode_hit = bool(index.find(term, mode))
    hit = (literal_hits or normal_hits)
    line = index.lines[hit[0].line] if hit else None
    cv_term = None
    if hit and not literal_hits:
        from .match import _original_wording

        cv_term = _original_wording(line, hit[0].form)
    return TermResult(term, bool(literal_hits), bool(normal_hits), mode_hit, cv_term, line)


def run_queries(queries: list[Query], index: CvIndex, mode: str, embeddings=None) -> None:
    cache: dict[str, TermResult] = {}
    for q in queries:
        q.terms = []
        for group in q.groups:
            results = []
            for term in group:
                if term not in cache:
                    cache[term] = _term_result(term, index, mode, embeddings)
                results.append(cache[term])
            q.terms.append(results)


def findability_findings(queries: list[Query], profile, match: MatchResult | None,
                         match_gain=None) -> tuple[list[Finding], float | None]:
    """Findings and the findability component (share of queries that hit, 0..100).

    `match_gain(term)` (optional) gives the points of the match component that
    writing the term literally would add (used for the edit gains).
    """
    do = msg("rsim.findability.do")
    mode = profile.search.keyword_mode
    if not queries:
        return [Finding(CHECK_FINDABILITY, OK, do, msg("rsim.findability.none"), [], None,
                        "findability.none")], None
    hits = sum(1 for q in queries if q.hit("mode_hit"))
    share = 100.0 * hits / len(queries)
    status = OK if hits == len(queries) else WARN if hits * 2 >= len(queries) else FAIL
    details = {"queries": [q.to_dict() for q in queries], "mode": mode,
               "boolean_documented": profile.search.boolean is not None}
    summary = Finding(CHECK_FINDABILITY, status, do,
                      msg("rsim.findability.summary", hits=hits, total=len(queries),
                          mode=msg(f"rsim.findability.mode.{mode}").default),
                      [EvidenceRef(SOURCE_POSTING, q.text) for q in queries if not q.hit("mode_hit")][:4],
                      msg("rsim.findability.boolean_unknown") if profile.search.boolean is None else None,
                      "findability.summary", [], details)
    findings = [summary]
    per_query = 100.0 / len(queries)
    done: set[str] = set()
    for q in queries:
        if q.hit("mode_hit"):
            continue
        for group in q.terms:
            if any(t.mode_hit for t in group):
                continue
            t = group[0]
            if t.term in done:
                continue
            done.add(t.term)
            fixed = sum(1 for other in queries if not other.hit("mode_hit") and all(
                any(x.mode_hit or x.term == t.term for x in g) for g in other.terms))
            gains = {COMPONENT_FINDABILITY: fixed * per_query}
            extra = match_gain(t.term) if match_gain else 0.0
            if extra:
                gains[COMPONENT_MATCH] = extra
            if t.normalised and t.cv_term:
                evidence = [EvidenceRef(SOURCE_POSTING, q.text), EvidenceRef(SOURCE_CV, short(t.cv_line or ""))]
                edit = Edit(f"findability.synonym.{t.term}",
                            msg("rsim.whatif.synonym", term=t.term, cv_term=t.cv_term, line=short(t.cv_line or "", 80)),
                            gains, evidence)
                findings.append(Finding(CHECK_FINDABILITY, WARN, do,
                                        msg("rsim.findability.miss_synonym", query=q.text, cv_term=t.cv_term,
                                            term=t.term), evidence,
                                        msg("rsim.findability.miss_synonym.fix", term=t.term, cv_term=t.cv_term,
                                            line=short(t.cv_line or "", 80)),
                                        "findability.synonym", [edit], {"term": t.term}))
            else:
                evidence = [EvidenceRef(SOURCE_POSTING, q.text)]
                message = (msg("rsim.whatif.title", title=t.term) if t.term in q.titles
                           else msg("rsim.whatif.missing_skill", skill=t.term))
                edit = Edit(f"findability.absent.{t.term}", message, gains, evidence, conditional=True)
                fix_key = ("rsim.findability.miss_title.fix" if t.term in q.titles
                           else "rsim.findability.miss_absent.fix")
                findings.append(Finding(CHECK_FINDABILITY, WARN, do,
                                        msg("rsim.findability.miss_absent", query=q.text, term=t.term), evidence,
                                        msg(fix_key, term=t.term),
                                        "findability.absent", [edit], {"term": t.term}))
    return findings, share
