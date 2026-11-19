"""When the normal OCR reading looks poor, and which of two readings is better.

Trigger rule (from a study on invented CVs; it caught 24 of 28 failing pages with 3
false alarms out of 41 good ones), computed on the normal reading of a document
read (at least partly) by OCR:

  A  mean OCR confidence (length-weighted) below 0.97
  B  an OCR page with fewer than 400 useful characters
  C  no "experience" section heading
  D  fewer than 3 section headings (the head excluded)
  E  Cyrillic found on a page but not read with the Cyrillic model

Any signal is enough. A false alarm only costs time: the advanced reading is kept
only when it is better by `ReadingQuality`, which counts what the profile is built
from: skills plus experiences first, then section headings, then the confidence.
A tie keeps the normal reading, so a reading is never replaced by a worse one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CONFIDENCE_MIN = 0.97
PAGE_CHARS_MIN = 400
HEADINGS_MIN = 3


def useful_chars(text: str) -> int:
    return len(re.sub(r"\W", "", text or ""))


@dataclass
class Signals:
    """The trigger signals of one normal reading (see the module docstring)."""

    confidence: float | None = None
    min_page_chars: int | None = None
    has_experience: bool = True
    headings: int = 0
    cyrillic_unread: bool = False
    fired: list[str] = field(default_factory=list)

    @property
    def triggered(self) -> bool:
        return bool(self.fired)


def section_summary(text: str) -> tuple[bool, int]:
    """(an experience section exists, number of distinct section keys without the head)."""
    from .sections import HEAD, split_sections

    sections = split_sections(text)
    keys = {s.key for s in sections} - {HEAD}
    return "experience" in keys, len(keys)


def signals(text: str, confidence: float | None, page_chars: list[int], scripts: list[str]) -> Signals:
    """The signals of a reading: its whole text, its OCR confidence, the useful
    characters of each OCR page and the script each OCR page was read in."""
    has_experience, headings = section_summary(text)
    result = Signals(confidence, min(page_chars) if page_chars else None, has_experience, headings,
                     any(s == "cyrillic_unread" for s in scripts))
    if confidence is not None and confidence < CONFIDENCE_MIN:
        result.fired.append("A")
    if result.min_page_chars is not None and result.min_page_chars < PAGE_CHARS_MIN:
        result.fired.append("B")
    if not has_experience:
        result.fired.append("C")
    if headings < HEADINGS_MIN:
        result.fired.append("D")
    if result.cyrillic_unread:
        result.fired.append("E")
    return result


@dataclass(frozen=True, order=True)
class ReadingQuality:
    """What a reading gives the profile; compared as a tuple, field by field."""

    profile_items: int      # skills + experiences found
    headings: int           # distinct section headings
    confidence: float       # mean OCR confidence (0 when unknown)
    skills: int = field(default=0, compare=False)
    experiences: int = field(default=0, compare=False)


def measure(text: str, confidence: float | None) -> ReadingQuality:
    """Skills and experiences the extraction finds in `text` (no ESCO, no translation).

    Only skills of the built-in dictionary count: garbled OCR text in a skills section
    would otherwise become many free-text "skills" and look like a richer reading."""
    from .extraction import _experience_rows, extract_experiences
    from .reading import detect_language
    from .sections import HEAD, split_sections
    from .skills import extract_skills, is_known

    language = detect_language(text)
    sections = split_sections(text)
    skills = sum(1 for item in extract_skills(sections, language, language) if is_known(item.value))
    experiences = len(extract_experiences(_experience_rows(sections))[0])
    headings = len({s.key for s in sections} - {HEAD})
    return ReadingQuality(skills + experiences, headings, float(confidence or 0.0), skills, experiences)


def advanced_is_better(normal: ReadingQuality, advanced: ReadingQuality) -> bool:
    """True only when the advanced reading is strictly better (a tie keeps the normal one)."""
    return advanced > normal
