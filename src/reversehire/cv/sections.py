"""Recognition of CV sections through typical IT/EN/UK headings.

The headings are in `data/headings.json`. Matching ignores case, accents,
trailing punctuation and numbering ("1. Esperienze:"). These styles are also
handled:
- decorated headings: "// PROFILE", "# Skills", "■ LINGUE", "01 EXPERIENCE",
  "▸ Education:";
- suffixes after "·", "-", "|" or "(": "WORK EXPERIENCE · CONTINUED",
  "Esperienze lavorative · continua", "Experience (cont.)", "WORK EXPERIENCE ·
  KEY ACHIEVEMENTS"; an unknown suffix is treated as content ("Skills - Python");
- headings joined with "&", "and", "e", "/" ("CERTIFICATIONS & COURSES"): the
  first part decides the section;
- "Lingue: inglese B2" (heading followed by ':' and content on the same row);
- Europass extracted from PDF, where the left-column label ends up on the same
  row as the content ("Lingua madre italiano"): allowed only for the labels in
  the 'labels' list.

Before the split, `clean_rows` removes the page furniture (repeated headers and
footers with page counters, "Page 1 of 2") and re-joins words hyphenated at the
end of a row. A section marked as continued that stays empty (two-column
layouts: the heading at the bottom of page 1, its content after the page break)
receives the rows that follow the next page break. Rows of a privacy consent
("I authorise the processing of my personal data...") form the "privacy"
section, which no extractor reads.
"""

import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DATA_FOLDER = Path(__file__).with_name("data")

# Section of the rows before the first heading (name, contacts...).
HEAD = "head"
# Sections that the extractors never read.
IGNORED_SECTIONS = ("references", "privacy")
MAX_HEADING_WORDS = 6


@dataclass
class Section:
    key: str  # "experience", "education", "skills", "languages", ...
    heading: str  # original heading row ("" for the head)
    rows: list[str] = field(default_factory=list)
    label: str = ""  # Europass label that opened the section, normalised
    continued: bool = False  # the heading says "continued" (rest of an earlier section)


_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'", "‘": "'", "`": "'", "´": "'"})


def normalize(text: str) -> str:
    """Lowercase, without accents, apostrophes unified, whitespace collapsed.

    Works for Latin and Cyrillic text (Ukrainian "ї"/"й" lose their diacritics
    on both sides of every comparison, so matching stays consistent)."""
    text = unicodedata.normalize("NFKD", text.translate(_APOSTROPHES))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


# Decorations before/after a heading: "//", "#", ">", bullets, bars, dashes...
_LEAD = re.compile(r"^[\s/\\#>•■□▪▫▸▹▶►◆◇●○◦|—–\-*~=+_:.»›]+")
_NUMBERING = re.compile(r"^(?:\d{1,2}|[ivx]{1,4})(?:[.)]\s*|\s+)(?=\D)")
_TRAIL = re.compile(r"[\s:;.\-–—|_*#/\\<•■▪◆●=~+«‹]+$")


def _heading_key(row: str) -> str:
    """Comparable form of a possible heading."""
    t = normalize(row)
    for _ in range(2):
        t = _LEAD.sub("", t)
        t = _NUMBERING.sub("", t)
    t = _TRAIL.sub("", t)
    return t.strip()


# Suffixes that only decorate a heading (not content).
_CONTINUED = re.compile(
    r"^(?:continued|continuation|continues|cont'?d?|cont\.?|segue|seguito|continua|continuazione|"
    r"продовження|далі)\b"
)
_DECORATIVE_SUFFIX = re.compile(
    r"^(?:key achievements?|achievements?|highlights?|selected|main|principali|risultati(?: chiave)?|"
    r"obiettivi raggiunti|in breve|summary|details?|dettagli|overview|cefr|qcer|europass|recent|recenti|"
    r"основні|ключові досягнення)\b"
)
# "·", "•" and "|" separate a suffix also without spaces (an OCR reading often drops the
# space before them: "ESPERIENZE LAVORATIVE· OBIETTIVI RAGGIUNTI"); dashes need spaces,
# or hyphenated words would be split.
_SUFFIX_SPLIT = re.compile(r"\s*[·•|]\s*|\s+[-–—]\s+|\s*\(|\s*\[")
_SUFFIX_SPLIT_ORIGINAL = re.compile(r"\s*[·•|]\s*|\s+[-–—]\s+")
_JOINERS = re.compile(r"\s*&\s*|\s+(?:and|e|ed|und|et|та|і|й)\s+|\s*/\s*|\s*\+\s*")


@lru_cache(maxsize=1)
def _tables() -> tuple[dict[str, str], list[tuple[str, str]]]:
    data = json.loads((DATA_FOLDER / "headings.json").read_text(encoding="utf-8"))
    exact: dict[str, str] = {}
    labels: list[tuple[str, str]] = []
    for key, item in data.items():
        if key.startswith("_"):
            continue
        for i in item.get("headers", []):
            exact[_heading_key(i)] = key
        for e in item.get("labels", []):
            k = normalize(e)
            exact.setdefault(_heading_key(e), key)
            labels.append((k, key))
    # Longest labels first ("competenze organizzative e gestionali").
    labels.sort(key=lambda x: -len(x[0]))
    return exact, labels


def _lookup(k: str, exact: dict[str, str]) -> str | None:
    """Section key of a cleaned heading text, also for joined headings ("A & B")."""
    if k in exact:
        return exact[k]
    parts = [p for p in _JOINERS.split(k) if p]
    if 1 < len(parts) <= 3 and all(len(p.split()) <= 4 for p in parts):
        keys = [exact.get(_heading_key(p)) for p in parts]
        if keys[0]:
            return keys[0]
    return None


def _classify(row: str) -> tuple[str, str, bool] | None:
    """(key, content after the heading, continued) for a decorated heading row."""
    exact, _ = _tables()
    k = _heading_key(row)
    if not k or len(k.split()) > MAX_HEADING_WORDS + 4:
        return None
    key = _lookup(k, exact) if len(k.split()) <= MAX_HEADING_WORDS else None
    if key:
        return key, "", False
    m = _SUFFIX_SPLIT.search(k)
    if not m or m.start() == 0:
        return None
    head = _heading_key(k[:m.start()])
    if len(head.split()) > MAX_HEADING_WORDS:
        return None
    key = _lookup(head, exact)
    if not key:
        return None
    if m.group(0).strip() in ("(", "["):
        closing = re.search(r"[)\]]", k[m.end():])
        if closing is None or k[m.end() + closing.end():].strip():
            return None  # "Mother tongue(s) English": not a decorated heading
    suffix = _heading_key(k[m.end():]).strip(" )]")
    continued = bool(_CONTINUED.match(suffix))
    if continued or not suffix or m.group(0).strip() in ("(", "[") or _DECORATIVE_SUFFIX.match(suffix):
        return key, "", continued
    # "Skills - Python, Java": the suffix is content (original case kept).
    original = _SUFFIX_SPLIT_ORIGINAL.split(row.strip(), maxsplit=1)
    return key, original[1].strip() if len(original) == 2 else suffix, False


def detect_heading(row: str) -> tuple[str, str, str] | None:
    """(section_key, remaining_content, label) if the row opens a section."""
    match = detect_heading_ex(row)
    return None if match is None else match[:3]


def detect_heading_ex(row: str) -> tuple[str, str, str, bool] | None:
    """Like `detect_heading`, plus whether the heading says "continued"."""
    exact, labels = _tables()
    if not row.strip() or len(row) > 120:
        return None
    classified = _classify(row)
    if classified:
        key, rest, continued = classified
        return key, rest, _heading_key(row), continued
    # "Heading: content"
    m = re.match(r"^\s*([^:]{2,60}?)\s*:\s*(.+)$", row)
    if m:
        k2 = _heading_key(m.group(1))
        key = _lookup(k2, exact) if len(k2.split()) <= MAX_HEADING_WORDS else None
        if key:
            return key, m.group(2).strip(), k2, False
    # Europass label followed by the content on the same row.
    norm = normalize(row)
    for label, key in labels:
        if norm.startswith(label) and len(norm) > len(label):
            rest_norm = norm[len(label):]
            if rest_norm[0] in " :\t":
                rest = _cut_prefix(row, len(label)).lstrip(" :\t")
                return key, rest, label, False
    return None


def _cut_prefix(row: str, norm_length: int) -> str:
    """Remove from `row` the prefix that, once normalised, is `norm_length` long."""
    row = row.strip()
    for i in range(len(row) + 1):
        if len(normalize(row[:i])) >= norm_length:
            return row[i:]
    return ""


# --- page furniture and hyphenation ---------------------------------------------

_COUNTER_ONLY = re.compile(
    r"^(?:-\s*)?(?:page|pagina|pag\.?|p\.|seite|стор(?:інка)?\.?|страница|стр\.)?\s*"
    r"\d{1,3}\s*(?:(?:/|of|di|de|von|з|із|из)\s*\d{1,3})?(?:\s*-)?$",
    re.IGNORECASE,
)
_COUNTER_AT_END = re.compile(
    r"(?:\b(?:page|pagina|pag\.?|seite|стор(?:інка)?\.?)\s*\d{1,3}(?:\s*(?:/|of|di|з)\s*\d{1,3})?"
    r"|(?<![\d/.\-])\d{1,2}\s*/\s*\d{1,2})\s*$",
    re.IGNORECASE,
)
_RUNNING_WORDS = re.compile(r"\b(?:cv|curriculum|vitae|resume|europass|page|pagina|резюме)\b", re.IGNORECASE)
_PRIVACY = re.compile(
    r"(?:autorizzo|acconsento|do il (?:mio )?consenso)\b.{0,60}\btrattamento"
    r"|\bi (?:hereby )?(?:authori[sz]e|consent|agree)\b.{0,60}\b(?:processing|personal data)"
    r"|\bai sensi del(?:l'art\.?)? (?:d\.? ?lgs|decreto|regolamento|reg\.)"
    r"|\bregulation \(eu\) 2016/679|\breg(?:olamento)?\.? \(?ue\)? 2016/679"
    r"|\bдаю згоду на обробку|\bзгоду на обробку (?:моїх )?персональних"
    r"|\bin accordance with (?:the )?(?:gdpr|regulation)",
    re.IGNORECASE,
)


@dataclass
class Row:
    text: str
    page_break: bool = False  # a page header/footer was removed right before this row


def _furniture_key(row: str) -> str:
    return re.sub(r"\d+", "#", normalize(row))


def _is_furniture(row: str, repeated: Counter) -> bool:
    if _COUNTER_ONLY.match(row.strip()) and re.search(r"\d", row):
        # A bare number is a page number only when short ("2", "2 / 3"), never a year.
        digits = re.findall(r"\d+", row)
        return all(len(d) <= 3 for d in digits) and not re.fullmatch(r"(?:19|20)\d{2}", row.strip())
    if len(row.split()) > 12:
        return False
    if _COUNTER_AT_END.search(row):
        m = re.search(r"(\d{1,3})\s*(?:/|of|di|з)\s*(\d{1,3})\s*$", row, re.IGNORECASE)
        if m and int(m.group(1)) > int(m.group(2)):
            return False
        return repeated[_furniture_key(row)] >= 2 or bool(_RUNNING_WORDS.search(row))
    return False


_HYPHEN_END = re.compile(r"[^\W\d_]-$")


def clean_rows(text: str) -> list[Row]:
    """Non-empty rows without page furniture, with hyphenated words re-joined."""
    raw = [r.strip() for r in text.split("\n")]
    raw = [r for r in raw if r]
    repeated = Counter(_furniture_key(r) for r in raw if _COUNTER_AT_END.search(r))
    rows: list[Row] = []
    page_break = False
    for r in raw:
        if _is_furniture(r, repeated):
            page_break = True
            continue
        if (rows and not page_break and _HYPHEN_END.search(rows[-1].text)
                and r[:1].isalpha() and r[:1].islower()):
            rows[-1].text = rows[-1].text[:-1] + r  # "plat-" + "forms"
            continue
        rows.append(Row(r, page_break))
        page_break = False
    return rows


MIN_STYLED_HEADINGS = 3  # headings in capitals that establish the CV's heading style


def _letters(row: str) -> str:
    return "".join(c for c in row if c.isalpha())


def _off_style(row: str, match, upper_style: bool) -> bool:
    """A bare heading word in lower case ("formazione") in a CV whose headings are all in
    capitals: a word of a diagram or of a wrapped sentence (often in an OCR reading),
    not a heading."""
    if not upper_style or match[1]:
        return False
    letters = _letters(row)
    return bool(letters) and letters.islower()


def split_sections(text: str) -> list[Section]:
    """Sections in CV order; the first is always the head (key HEAD)."""
    sections = [Section(HEAD, "")]
    current = sections[0]
    pending: Section | None = None  # empty "continued" section waiting for the next page
    before_privacy: Section | None = None
    rows = clean_rows(text)
    matches = [detect_heading_ex(row.text) for row in rows]
    # The CV's heading style: when its headings are in capitals ("// FORMAZIONE"), a
    # lower-case one is content (see _off_style).
    styled = [_letters(row.text) for row, match in zip(rows, matches) if match and not match[1]]
    upper = sum(1 for letters in styled if letters and letters.isupper())
    lower = sum(1 for letters in styled if letters and letters.islower())
    upper_style = upper >= MIN_STYLED_HEADINGS and lower < upper
    for row, match in zip(rows, matches):
        r = row.text
        if row.page_break:
            if pending is not None:
                current, pending, before_privacy = pending, None, None
            elif before_privacy is not None:
                current, before_privacy = before_privacy, None
        if match and _off_style(r, match, upper_style):
            match = None
        if match:
            key, rest, label, continued = match
            if current.continued and not current.rows and current is sections[-1]:
                pending = current
            # Consecutive Europass labels of the same section ("Lingua madre",
            # "Altre lingue") stay in the same section.
            if current.key == key and not current.rows and not rest and current is sections[-1]:
                continue
            current = Section(key, r, [], label, continued)
            sections.append(current)
            before_privacy = None
            if rest:
                current.rows.append(rest)
            continue
        if current.key != "privacy" and _PRIVACY.search(r):
            before_privacy = current
            current = Section("privacy", r, [r])
            sections.append(current)
            continue
        current.rows.append(r)
    return sections


def rows_of(sections: list[Section], key: str) -> list[str]:
    """All rows of the sections with that key, in order (continued sections merged)."""
    rows: list[str] = []
    for s in sections:
        if s.key == key:
            rows.extend(s.rows)
    return rows


def has_section(sections: list[Section], key: str) -> bool:
    return any(s.key == key for s in sections)
