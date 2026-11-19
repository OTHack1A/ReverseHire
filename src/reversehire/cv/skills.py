"""Skills of a CV: the IT/EN/UK dictionary of `data/skills.json` and the items of the
skills sections.

Rules:
- inside a skills section every sensible item is kept, also when it is not in
  the dictionary; items are separated by `, ; · • | ▪`, by " / " and by spaced
  dashes, and "Drives (A, B, C)" gives "Drives", "A", "B", "C". Rows that are
  plain lists without separators ("TIA Portal WinCC STEP7") are segmented with
  the dictionary; the remaining capitalised tokens become items too. Sentences
  only contribute dictionary skills;
- everywhere else (experience, profile, education, the keyword banner at the top
  of the CV) only dictionary skills are taken. Ambiguous entries ("Go", "Delta",
  "Cursor": `section_only`) count only in the skills sections and in keyword
  lists ("SIEMENS · OMRON · DELTA");
- short acronyms of `case_sensitive` entries ("C", "R", "Go", "ABB", "AI") must
  match with their exact case, so "go" or "c" in prose never count.
The value of a dictionary skill is its English canonical name; `original` keeps
the CV wording when it differs.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from ..models import Evidence, ProfileItem
from .sections import DATA_FOLDER, HEAD, IGNORED_SECTIONS, Section, normalize, rows_of

SOURCE = "cv"
CASE_SENSITIVE_MAX = 5  # forms up to this length of case_sensitive entries keep their case
MAX_PHRASE_WORDS = 8  # longest free item that is not a list of names
MAX_ITEM_CHARS = 60
MAX_EVIDENCE = 3

_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'", "‘": "'", "`": "'", "´": "'"})
_BULLET = re.compile(r"^\s*(?:[-•*·▪◦●○■□➢►▸>»–—✓✔]|\d+[.)]\s)\s*")
# Labels of sub-groups inside a skills section: never skills themselves.
GENERIC_LABELS = {
    "programming", "programmazione", "programming languages", "linguaggi", "linguaggi di programmazione",
    "general", "generale", "generali", "other", "others", "altro", "altre", "altri", "varie", "misc",
    "miscellaneous", "tools", "strumenti", "software", "hardware", "skills", "competenze", "interests",
    "interessi", "computation", "calcolo", "disegno", "drawing", "technologies", "tecnologie", "frameworks",
    "framework", "databases", "database", "platforms", "piattaforme", "operating systems", "sistemi operativi",
    "soft skills", "hard skills", "languages", "lingue", "knowledge", "conoscenze", "it", "ict", "level",
    "livello", "інше", "загальне", "програмування", "інструменти", "мови програмування", "навички",
}
_FUNCTION_WORDS = {
    "the", "and", "of", "to", "for", "with", "in", "a", "an", "my", "i", "on", "at", "by", "is", "are", "as",
    "di", "del", "della", "dei", "delle", "e", "ed", "per", "con", "il", "lo", "la", "le", "gli", "mi", "da",
    "che", "un", "una", "nel", "nella", "sono", "ho", "su", "al", "alla", "і", "та", "з", "для", "в", "у", "на",
    "що", "я", "до", "по",
}


def fold(text: str) -> str:
    """Accents removed, apostrophes unified, whitespace collapsed; the case is kept."""
    text = unicodedata.normalize("NFKD", (text or "").translate(_APOSTROPHES))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class Entry:
    index: int
    en: str
    it: str
    uk: str
    section_only: bool
    family: str

    def label(self, language: str) -> str:
        if language == "it":
            return self.it or self.en
        if language == "uk":
            return self.uk or self.en
        return self.en


@dataclass
class Hit:
    start: int
    end: int
    entry: Entry
    surface: str  # text of the CV that matched (folded)


_BEFORE = r"(?<![\w+#.])(?<!\w-)"
_AFTER = r"(?![\w+#])"


class SkillDictionary:
    """The skills of `data/skills.json` with one combined regular expression."""

    def __init__(self, items: list[dict]):
        self.entries: list[Entry] = []
        ci: dict[str, int] = {}
        cs: dict[str, int] = {}
        for i, item in enumerate(items):
            entry = Entry(i, item["en"], item.get("it") or item["en"], item.get("uk") or "",
                          bool(item.get("section_only")), item.get("family") or "")
            self.entries.append(entry)
            forms = [item["en"], item.get("it") or "", item.get("uk") or "", *item.get("variants", [])]
            for form in forms:
                f = fold(form)
                if not f:
                    continue
                if item.get("case_sensitive") and len(f) <= CASE_SENSITIVE_MAX:
                    cs.setdefault(f, i)
                else:
                    ci.setdefault(f.lower(), i)
        self._ci, self._cs = ci, cs
        self._ci_re = self._compile(ci, 0)
        self._cs_re = self._compile(cs, 0)

    @staticmethod
    def _compile(forms: dict[str, int], flags: int) -> re.Pattern | None:
        if not forms:
            return None
        alternatives = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
        return re.compile(f"{_BEFORE}(?:{alternatives}){_AFTER}", flags)

    def find(self, text: str, *, list_context: bool = False) -> list[Hit]:
        """Non-overlapping dictionary hits in the text (longest first at each position)."""
        folded = fold(text)
        lower = folded.lower()
        if len(lower) != len(folded):  # rare case-mapping changes of length
            folded = lower
        hits: list[Hit] = []
        if self._ci_re is not None:
            for m in self._ci_re.finditer(lower):
                hits.append(Hit(m.start(), m.end(), self.entries[self._ci[m.group(0)]], folded[m.start():m.end()]))
        if self._cs_re is not None:
            for m in self._cs_re.finditer(folded):
                hits.append(Hit(m.start(), m.end(), self.entries[self._cs[m.group(0)]], m.group(0)))
        hits.sort(key=lambda h: (h.start, -(h.end - h.start)))
        result: list[Hit] = []
        end = -1
        for h in hits:
            if h.start >= end:
                if list_context or not h.entry.section_only:
                    result.append(h)
                end = h.end
        return result

    def lookup(self, text: str) -> Entry | None:
        """The entry of which the whole text is a form (ignoring case, accents, punctuation)."""
        folded = fold(text).strip(" .:;,")
        if not folded:
            return None
        index = self._cs.get(folded)
        if index is None:
            index = self._ci.get(folded.lower())
        return self.entries[index] if index is not None else None


@lru_cache(maxsize=1)
def dictionary() -> SkillDictionary:
    data = json.loads((DATA_FOLDER / "skills.json").read_text(encoding="utf-8"))
    return SkillDictionary(data["items"])


# --- item splitting -------------------------------------------------------------

_SEPARATORS = re.compile(r"[,;•|·▪◦]|\s/\s|\s[-–—]\s|\s{2,}|\t|\.\s+(?=[^\W\d_])")
_LABEL = re.compile(r"^\s*([^\W\d_][^:]{0,30}):\s+(?=\S)")


def _tidy(text: str) -> str:
    text = re.sub(r"\(\s*\)", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t|,;:-–—·/").strip()


def _split_top(text: str) -> list[str]:
    """Split on separators outside parentheses."""
    parts, depth, current = [], 0, []
    i = 0
    while i < len(text):
        c = text[i]
        if c in "([":
            depth += 1
        elif c in ")]":
            depth = max(0, depth - 1)
        if depth == 0:
            m = _SEPARATORS.match(text, i)
            if m:
                parts.append("".join(current))
                current = []
                i = m.end()
                continue
        current.append(c)
        i += 1
    parts.append("".join(current))
    return [p for p in (_tidy(p) for p in parts) if p]


def _elements(text: str) -> list[str]:
    """Items of one skills row: separators, labels and parenthesised lists."""
    text = _BULLET.sub("", text)
    m = _LABEL.match(text)
    if m and len(m.group(1).split()) <= 3:
        label = _tidy(m.group(1))
        text = text[m.end():]
        if normalize(label) not in GENERIC_LABELS and dictionary().lookup(label):
            text = f"{label}, {text}"
    result: list[str] = []
    for part in _split_top(text):
        label_inside = _LABEL.match(part)
        if label_inside and len(label_inside.group(1).split()) <= 3:
            part = part[label_inside.end():]  # "... Interests: automation processes"
        m = re.match(r"^(?P<label>[^()\[\]]*)[(\[](?P<inner>.*)[)\]]\s*(?P<tail>[^()]*)$", part)
        if m:
            label = _tidy(m.group("label") + " " + m.group("tail"))
            if label:
                result.append(label)
            result.extend(_elements(m.group("inner")))
        else:
            result.append(part.rstrip("."))
    return [e for e in result if e]


def _sentence_like(text: str) -> bool:
    words = re.findall(r"[^\W\d_][\w'’\-]*", text)
    if not words:
        return False
    function = sum(1 for w in words if w.lower() in _FUNCTION_WORDS)
    if text.rstrip().endswith((".", "!")) and len(words) >= 5:
        return True
    return function >= 2 and function / len(words) >= 0.2


def _sensible(text: str) -> bool:
    norm = normalize(text)
    if not 2 <= len(text) <= MAX_ITEM_CHARS or norm in GENERIC_LABELS:
        return False
    if not re.search(r"[^\W\d_]", text) or re.search(r"@|https?://|www\.", text):
        return False
    first = norm.split()[0]
    if first in _FUNCTION_WORDS or re.fullmatch(r"[\d\W]+", text):
        return False
    # an all-capitals group heading inside a skills section ("PROGRAMMING AND SYSTEMS")
    words = text.split()
    connectors = {"AND", "&", "E", "ED", "OF", "DI", "DEL", "DELLA", "І", "ТА"}
    if len(words) >= 2 and text.isupper() and any(w in connectors for w in words):
        return False
    return True


def _merge_rows(rows: list[str]) -> list[str]:
    """Rows of a skills section with page-layout breaks re-joined."""
    merged: list[str] = []
    for r in rows:
        cleaned = _BULLET.sub("", r).strip()
        previous = merged[-1] if merged else ""
        open_paragraph = previous.count(",") >= 2 and len(previous) > 60 and not re.search(r"[.;:,]$", previous)
        if (merged and not _BULLET.match(r) and not re.search(r"[.;:]$", previous)
                and (cleaned[:1].islower() or open_paragraph)):
            merged[-1] = f"{previous} {cleaned}"
        else:
            merged.append(r)
    return merged


def _leftover_items(element: str, hits: list[Hit]) -> list[str]:
    """Tokens of a list-like row not covered by dictionary hits, as items."""
    folded = fold(element)
    covered = [False] * len(folded)
    for h in hits:
        for k in range(h.start, min(h.end, len(folded))):
            covered[k] = True
    runs: list[list[str]] = []
    current: list[str] = []
    for m in re.finditer(r"\S+", folded):
        if any(covered[m.start():m.end()]):
            if current:
                runs.append(current)
            current = []
        else:
            current.append(element[m.start():m.end()] if len(element) == len(folded) else m.group(0))
    if current:
        runs.append(current)
    items = []
    for run in runs:
        tokens = [t.strip(".,;:()") for t in run if t.strip(".,;:()")]
        if not tokens:
            continue
        if not all(_name_token(t) for t in tokens):
            if len(tokens) <= MAX_PHRASE_WORDS:  # a phrase ("modelli matematici neuronali")
                items.append(" ".join(tokens))
        elif len(tokens) <= 3:
            items.append(" ".join(tokens))
        else:
            items.extend(tokens)
    return [i for i in items if _sensible(i)]


def _name_token(token: str) -> bool:
    """Product/brand-like token: capitalised, all caps or with digits/symbols."""
    return bool(token[:1].isupper() or re.search(r"[\d+#.\-]", token))


def _names_list(element: str) -> bool:
    """A list of names separated only by spaces ("TIA Portal WinCC STEP7")."""
    tokens = element.split()
    return len(tokens) >= 3 and sum(1 for t in tokens if _name_token(t)) / len(tokens) >= 0.6


@dataclass
class Found:
    surface: str  # wording of the CV
    row: str  # CV row (evidence)
    entry: Entry | None  # None: free item, not in the dictionary


def section_items(rows: list[str]) -> list[Found]:
    """Items of the rows of the skills sections."""
    d = dictionary()
    found: list[Found] = []
    for row in _merge_rows(rows):
        for element in _elements(row):
            entry = d.lookup(element)
            if entry is not None:
                found.append(Found(element, row, entry))
                continue
            words = element.split()
            hits = d.find(element, list_context=True)
            found.extend(Found(h.surface, row, h.entry) for h in hits)
            if _sentence_like(element):
                continue  # sentences give dictionary skills only
            if _names_list(element):
                found.extend(Found(i, row, None) for i in _leftover_items(element, hits))
            elif len(words) <= MAX_PHRASE_WORDS:
                share = sum(h.end - h.start for h in hits) / max(1, len(fold(element)))
                if share < 0.5 and _sensible(element):
                    found.append(Found(element, row, None))
    return found


def list_like(row: str) -> bool:
    """A keyword list ("SIEMENS · OMRON · DELTA", "PLC / SCADA / DCS") rather than prose."""
    parts = [p for p in re.split(r"\s*[·•|/,;]\s*|\s[-–—]\s", row) if p.strip()]
    return len(parts) >= 3 and all(len(p.split()) <= 3 for p in parts) and not _sentence_like(row)


def _head_rows(sections: list[Section]) -> list[str]:
    """Keyword banner and headline at the top of the CV (no contact rows)."""
    rows = []
    for row in rows_of(sections, HEAD)[:12]:
        if re.search(r"\d|@|https?://|www\.|\.(?:com|it|dev|net|org|io)\b", row, re.IGNORECASE):
            continue
        rows.append(row)
    return rows


def extract_skills(sections: list[Section], cv_language: str, display: str | None = None) -> list[ProfileItem]:
    """Skills: items of the skills sections, then dictionary skills of the other rows.

    `display`: language of the values ("en" for the English profile); defaults to
    the CV language (dictionary label of that language)."""
    display = display or cv_language
    d = dictionary()
    items: list[ProfileItem] = []
    index: dict[str, ProfileItem] = {}

    def add(found: Found) -> None:
        if found.entry is not None:
            key = f"dict:{found.entry.index}"
            value = found.entry.label(display)
        else:
            value = _tidy(found.surface)
            key = "text:" + normalize(value)
        if key in index:
            item = index[key]
            if all(e.text != found.row for e in item.evidence) and len(item.evidence) < MAX_EVIDENCE:
                item.evidence.append(Evidence(found.row, SOURCE))
            return
        original = None
        if found.entry is not None and display != cv_language and normalize(found.surface) != normalize(value):
            original = _tidy(found.surface)
        item = ProfileItem(value, [Evidence(found.row, SOURCE)], original=original)
        index[key] = item
        items.append(item)

    skill_rows = rows_of(sections, "skills")
    for found in section_items(skill_rows):
        add(found)
    skill_set = set(skill_rows)
    other_rows: list[str] = _head_rows(sections)
    for section in sections:
        if section.key in (HEAD, "languages", "skills", *IGNORED_SECTIONS):
            continue
        other_rows.extend(section.rows)
    for row in other_rows:
        if row in skill_set:
            continue
        for hit in d.find(row, list_context=list_like(row)):
            add(Found(hit.surface, row, hit.entry))
    return items


def is_known(value: str) -> bool:
    """True if the value is a dictionary skill (any of its forms)."""
    return dictionary().lookup(value) is not None
