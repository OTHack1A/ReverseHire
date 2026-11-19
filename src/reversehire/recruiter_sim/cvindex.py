"""Index of the CV text as a parser sees it, and term matching per keyword mode.

Keyword modes (profile `search.keyword_mode`):
- "literal": the term as written, whole words only, ignoring case and accents
  ("java" never matches "javascript", "SQL" never matches "MSSQL");
- "stem": every content word of the term, lightly stemmed, in the same line;
- "taxonomy": literal, or any form of the same skill in the app's skill
  dictionary (`cv/data/skills.json` variants: "k8s" = "Kubernetes") or of the
  small IT/EN job-term table of `scoring.relevance`;
- "semantic": taxonomy, plus the embedder (when given) on whole lines.

Whole-term matching follows the idea of Resume-Matcher `text_match.keyword_in_text`
(Apache-2.0; idea only, own code). Experience blocks (the rows of each dated job)
give each line a date range, used for months and last-used per skill.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache

from ..cv import skills as cv_skills
from ..cv.extraction import Experience, extract_experiences, find_period
from ..cv.sections import HEAD, clean_rows, has_section, normalize, rows_of, split_sections
from ..scoring.relevance import SYNONYMS, normalize_text
from ..scoring.requirements import stems as content_stems

LITERAL, STEM, TAXONOMY, SEMANTIC = "literal", "stem", "taxonomy", "semantic"
_BEFORE = r"(?<![\w+#.])(?<!\w-)"
_AFTER = r"(?![\w+#])"
_EXCLUDED_FOR_EXPERIENCE = {"education", "certifications", "languages", "skills", "projects", "other",
                            "references", "privacy"}
_RE_BULLET = re.compile(r"^\s*(?:[-•*·▪◦●○■□➢►>»–—✓✔]|\d+[.)]\s)\s*")


def fold(text: str) -> str:
    """Lowercase, accents removed, whitespace collapsed (case-insensitive matching)."""
    return cv_skills.fold(text or "").lower()


@lru_cache(maxsize=4096)
def _term_regex(form: str) -> re.Pattern:
    return re.compile(_BEFORE + re.escape(form) + _AFTER)


def contains_literal(term: str, text: str) -> bool:
    form = fold(term).strip()
    return bool(form) and bool(_term_regex(form).search(fold(text)))


def skill_entry(term: str):
    """The skill-dictionary entry the whole term is a form of, or None."""
    return cv_skills.dictionary().lookup(term)


@lru_cache(maxsize=2048)
def _entry_forms(index: int) -> tuple[str, ...]:
    import json

    from ..cv.sections import DATA_FOLDER

    items = json.loads((DATA_FOLDER / "skills.json").read_text(encoding="utf-8"))["items"]
    item = items[index]
    forms = [item.get("en") or "", item.get("it") or "", item.get("uk") or "", *item.get("variants", [])]
    return tuple(dict.fromkeys(f for f in (fold(x) for x in forms) if f))


def forms_for(term: str, mode: str) -> list[str]:
    """The folded forms of a term searched under a keyword mode (literal first)."""
    base = fold(term).strip()
    if not base:
        return []
    result = [base]
    if mode in (TAXONOMY, SEMANTIC):
        entry = skill_entry(term)
        if entry is not None:
            result.extend(f for f in _entry_forms(entry.index) if f not in result)
        for synonym in sorted(SYNONYMS.get(normalize_text(term), ())):
            if synonym not in result:
                result.append(synonym)
    return result


@dataclass
class ExperienceBlock:
    """The rows of one dated job (title row, date row, description bullets)."""

    title: str
    employer: str
    start: int  # month index year*12 + month-1
    end: int
    in_progress: bool
    period_text: str
    rows: list[str] = field(default_factory=list)

    @property
    def months(self) -> int:
        return self.end - self.start + 1


@dataclass
class TermHit:
    line: int  # 0-based index in CvIndex.lines
    form: str  # folded form that matched
    literal: bool  # True if the term itself (as written) matched


@dataclass
class CvIndex:
    """The parsed CV: clean lines, sections, dated job blocks, folded/stemmed lines."""

    lines: list[str]
    folded: list[str]
    stems: list[frozenset]
    blocks: list[ExperienceBlock]
    skills_rows: set[str]
    head_rows: list[str]
    today: date

    @classmethod
    def build(cls, text: str, today: date | None = None) -> "CvIndex":
        today = today or date.today()
        lines = [r.text for r in clean_rows(text or "")]
        sections = split_sections(text or "")
        if has_section(sections, "experience"):
            rows = rows_of(sections, "experience")
        else:
            rows = [r for s in sections if s.key not in _EXCLUDED_FOR_EXPERIENCE and s.key != HEAD
                    for r in s.rows]
        blocks = experience_blocks(rows, today)
        return cls(lines=lines, folded=[fold(x) for x in lines], stems=[content_stems(x) for x in lines],
                   blocks=blocks, skills_rows=set(rows_of(sections, "skills")),
                   head_rows=rows_of(sections, HEAD), today=today)

    @property
    def today_index(self) -> int:
        return self.today.year * 12 + self.today.month - 1

    def block_of(self, line: str) -> ExperienceBlock | None:
        for block in self.blocks:
            if line in block.rows:
                return block
        return None

    def find(self, term: str, mode: str) -> list[TermHit]:
        """Every line where the term (or a form of it, per mode) occurs."""
        hits: list[TermHit] = []
        base = fold(term).strip()
        if not base:
            return hits
        if mode == STEM:
            wanted = content_stems(term)
            if not wanted:
                wanted = frozenset([base])
            for i, line_stems in enumerate(self.stems):
                if wanted <= line_stems or _term_regex(base).search(self.folded[i]):
                    hits.append(TermHit(i, base, bool(_term_regex(base).search(self.folded[i]))))
            return hits
        forms = forms_for(term, mode) if mode != LITERAL else [base]
        for i, line in enumerate(self.folded):
            for form in forms:
                if _term_regex(form).search(line):
                    hits.append(TermHit(i, form, form == base))
                    break
        return hits

    def found(self, term: str, mode: str) -> bool:
        return bool(self.find(term, mode))

    def text(self) -> str:
        return "\n".join(self.lines)


def experience_blocks(rows: list[str], today: date) -> list[ExperienceBlock]:
    """Dated jobs with their rows: from the first evidence row of a job to the row
    before the next job (jobs whose date sits only in a bullet are skipped)."""
    experiences, _ = extract_experiences(rows, today)
    kept: list[tuple[int, Experience]] = []
    for e in experiences:
        date_rows = [r for r in e.evidence if e.period.text in r]
        if date_rows and all(_RE_BULLET.match(r) for r in date_rows):
            continue
        starts = [rows.index(r) for r in e.evidence if r in rows]
        if starts:
            kept.append((min(starts), e))
    kept.sort(key=lambda x: x[0])
    blocks: list[ExperienceBlock] = []
    for n, (start, e) in enumerate(kept):
        stop = kept[n + 1][0] if n + 1 < len(kept) else len(rows)
        block_rows = rows[start:max(stop, start + 1)]
        p = e.period
        blocks.append(ExperienceBlock(e.title, e.employer, p.start, p.end, p.in_progress, p.text,
                                      list(dict.fromkeys(block_rows))))
    return blocks


def merged_months(ranges: list[tuple[int, int]]) -> int:
    """Months covered by the union of (start, end) month ranges (overlaps once)."""
    total, current = 0, None
    for start, end in sorted(ranges):
        if current is None or start > current[1] + 1:
            if current:
                total += current[1] - current[0] + 1
            current = [start, end]
        else:
            current[1] = max(current[1], end)
    if current:
        total += current[1] - current[0] + 1
    return total


def month_text(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def is_year_only(period_text: str) -> bool:
    """True if the start or the end of the period is a bare year ("2015 - 03/2018")."""
    from ..cv.extraction import _year_only

    period = find_period(period_text)
    if period is None:
        return False
    start, end = _year_only(period)
    return bool(start or (end and not period.in_progress))


def normalize_heading(text: str) -> str:
    return normalize(text)
