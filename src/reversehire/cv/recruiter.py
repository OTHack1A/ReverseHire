"""Recruiter view of a CV: how a recruiter, and the applicant-tracking software
in front of them, would screen the candidate's CV, with advice to improve it.

Everything is local and rule-based (Italian and English CVs). The result is a
list of `Finding`s that carry i18n KEYS and parameters, never display text: the
GUI translates them with the locale files (`recruiter.*` keys).

Areas (keys of `AREA_WEIGHTS`):
- readability: what applicant-tracking software sees (extractable text,
  standard headings, contacts, length, layout disorder, odd characters, file name);
- career: gaps, short stints, overlaps, date formats, order, undated roles;
- substance: results with numbers, buzzwords, action verbs vs weak openings,
  first person, long paragraphs, repeated words;
- coverage: essential skills of the candidate's ESCO occupation found or missing
  (only when `esco_skills` is given);
- personal: personal data a recruiter does not need or that invites bias;
- hidden: hidden text (invisible, white, tiny, off-page) and keyword stuffing.

Scoring (explainable): every finding has a `penalty`; an area score is
100 minus the penalties of its findings, clamped to 0..100. The overall score is
the weighted mean of the assessed areas (`AREA_WEIGHTS`, renormalised over the
areas that were assessed). If hidden text is evidenced in the file, the overall
score is capped at `HIDDEN_TEXT_CAP` (`RecruiterReview.capped` is then True).
"""

from __future__ import annotations

import json
import math
import re
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz

from ..models import CVProfile, item_variants
from .extraction import Period, extract_experiences, find_period, years_from_periods
from .sections import DATA_FOLDER, HEAD, Section, has_section, normalize, rows_of, split_sections

# --- weights and thresholds ----------------------------------------------------

AREAS = ("readability", "career", "substance", "coverage", "personal", "hidden")
AREA_WEIGHTS = {
    "readability": 0.25,
    "career": 0.20,
    "substance": 0.30,
    "coverage": 0.10,
    "personal": 0.05,
    "hidden": 0.10,
}
SEVERITIES = ("problem", "warning", "info", "good")
# Default penalty per severity (points subtracted from the area score).
PENALTY = {"problem": 25, "warning": 10, "info": 0, "good": 0}
HIDDEN_TEXT_CAP = 60

# readability
MIN_TEXT_CHARS = 30  # fewer letters/digits than this: image-only CV
IMAGE_KINDS = ("png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp", "gif")  # CV sent as a picture
WORDS_MIN = 200
WORDS_MAX = 1100
PAGES_IDEAL = 2
PAGES_MAX = 3
MIN_LINES_FOR_LAYOUT = 15
SHORT_LINE_SHARE = 0.45  # share of 1-2 word lines that suggests a table/columns layout
BROKEN_WORDS_MIN = 3
SPACED_LETTERS_MIN = 2
ODD_CHARS_MIN = 3
FANCY_BULLETS_MIN = 3
# career
GAP_MONTHS = 6  # a gap must be longer than this
CURRENT_GAP_MONTHS = 6
SHORT_STINT_MONTHS = 9  # a stint shorter than this is "short"
SHORT_STINTS_WARNING = 2
SHORT_STINTS_PROBLEM = 4
OVERLAP_MIN_MONTHS = 2
OVERLAP_YEAR_ONLY_MIN_MONTHS = 13  # year-only dates make 1-year overlaps meaningless
# substance
RESULTS_SHARE_GOOD = 0.30
RESULTS_SHARE_LOW = 0.15
MIN_CONTENT_LINES = 3
THIN_LINES_PER_ROLE = 2  # fewer description lines per role than this: thin descriptions
WEAK_SHARE = 0.25
WEAK_MIN = 2
ACTION_SHARE_GOOD = 0.50
BUZZ_HITS_WARNING = 3
PRONOUNS_WARNING = 5
LONG_PARAGRAPH_WORDS = 60
REPEATED_WORD_MIN = 6
REPEATED_WORD_SHARE = 0.015
# coverage
# ESCO lists are long and abstract: even a strong CV names only a part of them.
COVERAGE_GOOD = 0.30
COVERAGE_LOW = 0.10
FUZZY_LINE_SCORE = 88
TOKEN_SHARE = 0.60
SEMANTIC_POSITION = 0.75  # position inside the embedder's similarity range
MAX_LISTED_SKILLS = 10
# personal
PHOTO_MIN_PT = 50.0  # minimum side of a placed image to be a possible photo
PHOTO_ASPECT = (0.6, 1.6)
# hidden text / stuffing
TINY_FONT_PT = 2.0
WHITE_LEVEL = 0.95
OFFPAGE_MARGIN_PT = 5.0
HIDDEN_MIN_LETTERS = 3
STUFFING_REPEAT = 7
STUFFING_SHARE = 0.02
LONG_LIST_ITEMS = 15
DUPLICATE_BLOCK_RATIO = 90
MAX_EVIDENCE = 5
EVIDENCE_CHARS = 140

_KEY_PREFIX = "recruiter"


# --- data model -------------------------------------------------------------------

@dataclass
class Finding:
    """One result of the review. `key`/`advice_key` are i18n keys; `params` fill
    their placeholders (str/int/float only); `evidence` are short CV lines."""

    area: str
    key: str
    severity: str  # "good" | "info" | "warning" | "problem"
    params: dict = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    advice_key: str | None = None
    penalty: int = 0


@dataclass
class SkillCoverage:
    """An essential skill of the occupation and whether the CV shows it."""

    skill: str
    covered: bool
    method: str = ""  # "exact", "fuzzy", "tokens", "semantic" or "" when missing
    evidence: list[str] = field(default_factory=list)


@dataclass
class RecruiterReview:
    score: int
    area_scores: dict[str, int | None]
    findings: list[Finding]
    coverage: list[SkillCoverage] = field(default_factory=list)
    language: str = "it"
    words: int = 0
    pages: int | None = None
    capped: bool = False

    def by_area(self, area: str) -> list[Finding]:
        return [f for f in self.findings if f.area == area]

    def keys(self) -> list[str]:
        return [f.key for f in self.findings]

    def to_dict(self) -> dict:
        return asdict(self)


# --- helpers -------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _lexicon() -> dict:
    return json.loads((DATA_FOLDER / "recruiter_lexicon.json").read_text(encoding="utf-8"))


def _words_of(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", text)


def _short(line: str) -> str:
    line = re.sub(r"\s+", " ", line).strip()
    return line if len(line) <= EVIDENCE_CHARS else line[:EVIDENCE_CHARS - 1].rstrip() + "…"


def _evidence(lines, limit: int = MAX_EVIDENCE) -> list[str]:
    result: list[str] = []
    for line in lines:
        s = _short(line)
        if s and s not in result:
            result.append(s)
        if len(result) >= limit:
            break
    return result


def _phrase_regex(phrases: list[str]) -> re.Pattern | None:
    forms = sorted({normalize(p) for p in phrases if p.strip()}, key=len, reverse=True)
    if not forms:
        return None
    return re.compile(r"(?<![\w])(?:" + "|".join(re.escape(f) for f in forms) + r")(?![\w])")


def _lang(language: str) -> str:
    return language if language in ("it", "en") else "en"


def _both_languages(name: str, language: str) -> list[str]:
    """Phrases of the CV language first, then of the other one (mixed CVs)."""
    data = _lexicon()[name]
    first = _lang(language)
    other = "en" if first == "it" else "it"
    return list(data.get(first, [])) + list(data.get(other, []))


_RE_BULLET = re.compile(r"^\s*(?:[-•*·▪◦●○■□➢►>»–—✓✔❖◆♦→]|\d+[.)]\s)\s*")
_FANCY_BULLETS = "➢►✓✔❖◆♦→◦▪□■●○"


def _strip_bullet(line: str) -> str:
    return _RE_BULLET.sub("", line).strip()


def _is_bullet(line: str) -> bool:
    return bool(_RE_BULLET.match(line))


def _month_text(index: int) -> str:
    return f"{index % 12 + 1:02d}/{index // 12}"


class _Collector:
    """Accumulates findings with the key convention `recruiter.<area>.<check>`."""

    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def add(self, area: str, check: str, severity: str, params: dict | None = None,
            evidence=None, penalty: int | None = None, advice: bool | None = None) -> Finding:
        key = f"{_KEY_PREFIX}.{area}.{check}"
        if advice is None:
            advice = severity in ("warning", "problem")
        finding = Finding(
            area=area, key=key, severity=severity, params=dict(params or {}),
            evidence=_evidence(evidence or []),
            advice_key=f"{key}.advice" if advice else None,
            penalty=PENALTY[severity] if penalty is None else int(penalty),
        )
        self.findings.append(finding)
        return finding


# --- file inspection (PDF / DOCX) ---------------------------------------------------

@dataclass
class FileFacts:
    """What the file itself tells beyond its text."""

    kind: str = ""  # "pdf", "docx" or ""
    pages: int | None = None
    text_pages: int = 0
    image_only_pages: int = 0
    photo_candidates: int = 0
    hidden: dict[str, list[str]] = field(default_factory=dict)  # kind -> text lines
    producer: str = ""
    readable: bool = True


def _mult(m1: list[float], m2: list[float]) -> list[float]:
    """Product of PDF matrices: apply m1, then m2."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return [a1 * a2 + b1 * c2, a1 * b2 + b1 * d2, c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2]


def _apply(m: list[float], x: float, y: float) -> tuple[float, float]:
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def _bbox(m: list[float], x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float]:
    points = [_apply(m, x, y) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _floats(operands) -> list[float] | None:
    try:
        return [float(o) for o in operands]
    except (TypeError, ValueError):
        return None


def _is_white(values: list[float] | None, space: str) -> bool:
    if not values:
        return False
    if len(values) == 1 and space in ("gray", ""):
        return values[0] >= WHITE_LEVEL
    if len(values) == 3:
        return all(v >= WHITE_LEVEL for v in values)
    if len(values) == 4:
        return all(v <= 1 - WHITE_LEVEL for v in values)
    return False


_TEXT_SHOW = (b"Tj", b"TJ", b"'", b'"')


def _scan_pdf_page(page, is_first: bool) -> tuple[dict[str, set[int]], int, list]:
    """Walk the content stream of a page.

    Returns (indexes of text-showing operations per hidden kind, number of
    possible photos, operations). Hidden kinds: "invisible" (render mode 3/7
    not over an image, which would be an OCR layer), "tiny" (< TINY_FONT_PT),
    "offpage" (origin outside the media box), "white" (white fill not over a
    filled shape or an image).
    """
    contents = page.get_contents()
    if contents is None:
        return {}, 0, []
    operations = contents.operations
    box = page.mediabox
    left, bottom, right, top = float(box.left), float(box.bottom), float(box.right), float(box.top)
    try:
        xobjects = page["/Resources"].get_object().get("/XObject")
        xobjects = xobjects.get_object() if xobjects is not None else {}
    except (KeyError, AttributeError, TypeError):
        xobjects = {}

    state = {"ctm": [1.0, 0, 0, 1.0, 0, 0], "fill": [0.0], "space": "gray", "tr": 0,
             "size": 0.0, "tl": 0.0}
    stack: list[dict] = []
    tm = [1.0, 0, 0, 1.0, 0, 0]
    tlm = list(tm)
    path_box: list[float] | None = None
    backgrounds: list[tuple[float, float, float, float]] = []  # filled shapes (non-white)
    images: list[tuple[float, float, float, float]] = []
    photos = 0
    hidden: dict[str, set[int]] = {}

    def add_point(x: float, y: float) -> None:
        nonlocal path_box
        px, py = _apply(state["ctm"], x, y)
        if path_box is None:
            path_box = [px, py, px, py]
        else:
            path_box = [min(path_box[0], px), min(path_box[1], py),
                        max(path_box[2], px), max(path_box[3], py)]

    def inside(x: float, y: float, boxes) -> bool:
        return any(b[0] - 1 <= x <= b[2] + 1 and b[1] - 1 <= y <= b[3] + 1 for b in boxes)

    def next_line() -> None:
        nonlocal tm, tlm
        tlm = _mult([1, 0, 0, 1, 0, -state["tl"]], tlm)
        tm = list(tlm)

    for index, (operands, operator) in enumerate(operations):
        if operator == b"q":
            stack.append({k: (list(v) if isinstance(v, list) else v) for k, v in state.items()})
        elif operator == b"Q":
            if stack:
                state = stack.pop()
        elif operator == b"cm":
            values = _floats(operands)
            if values and len(values) == 6:
                state["ctm"] = _mult(values, state["ctm"])
        elif operator in (b"g", b"rg", b"k"):
            state["fill"] = _floats(operands) or []
            state["space"] = {b"g": "gray", b"rg": "rgb", b"k": "cmyk"}[operator]
        elif operator == b"cs":
            name = str(operands[0]) if operands else ""
            state["space"] = {"/DeviceGray": "gray", "/DeviceRGB": "rgb",
                              "/DeviceCMYK": "cmyk"}.get(name, "other")
            state["fill"] = [0.0] if state["space"] == "gray" else []
        elif operator in (b"sc", b"scn"):
            state["fill"] = (_floats(operands) or []) if state["space"] != "other" else []
        elif operator == b"re":
            values = _floats(operands)
            if values and len(values) == 4:
                x, y, w, h = values
                add_point(x, y)
                add_point(x + w, y + h)
        elif operator in (b"m", b"l"):
            values = _floats(operands)
            if values and len(values) >= 2:
                add_point(values[0], values[1])
        elif operator in (b"c", b"v", b"y"):
            values = _floats(operands)
            if values:
                for i in range(0, len(values) - 1, 2):
                    add_point(values[i], values[i + 1])
        elif operator in (b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"):
            if path_box is not None and not _is_white(state["fill"], state["space"]):
                backgrounds.append(tuple(path_box))
            path_box = None
        elif operator in (b"n", b"S", b"s"):
            path_box = None
        elif operator == b"Do":
            name = operands[0] if operands else None
            try:
                xobject = xobjects[name].get_object() if name is not None else None
            except (KeyError, TypeError):
                xobject = None
            if xobject is not None and xobject.get("/Subtype") == "/Image":
                b = _bbox(state["ctm"], 0, 0, 1, 1)
                images.append(b)
                width, height = b[2] - b[0], b[3] - b[1]
                if (is_first and min(width, height) >= PHOTO_MIN_PT and height > 0
                        and PHOTO_ASPECT[0] <= width / height <= PHOTO_ASPECT[1]):
                    photos += 1
        elif operator == b"INLINE IMAGE":
            images.append(_bbox(state["ctm"], 0, 0, 1, 1))
        elif operator == b"BT":
            tm = [1.0, 0, 0, 1.0, 0, 0]
            tlm = list(tm)
        elif operator == b"Tf":
            values = _floats(operands[1:2])
            state["size"] = values[0] if values else 0.0
        elif operator == b"Tr":
            values = _floats(operands[:1])
            state["tr"] = int(values[0]) if values else 0
        elif operator == b"TL":
            values = _floats(operands[:1])
            state["tl"] = values[0] if values else 0.0
        elif operator in (b"Td", b"TD"):
            values = _floats(operands[:2])
            if values and len(values) == 2:
                if operator == b"TD":
                    state["tl"] = -values[1]
                tlm = _mult([1, 0, 0, 1, values[0], values[1]], tlm)
                tm = list(tlm)
        elif operator == b"Tm":
            values = _floats(operands[:6])
            if values and len(values) == 6:
                tm = list(values)
                tlm = list(values)
        elif operator == b"T*":
            next_line()
        if operator in _TEXT_SHOW:
            if operator in (b"'", b'"'):
                next_line()
            matrix = _mult(tm, state["ctm"])
            x, y = matrix[4], matrix[5]
            scale = math.sqrt(abs(matrix[0] * matrix[3] - matrix[1] * matrix[2]))
            size = abs(state["size"]) * scale
            kind = None
            if state["tr"] in (3, 7):
                if not inside(x, y, images):
                    kind = "invisible"
            elif 0 < size < TINY_FONT_PT:
                kind = "tiny"
            elif (x < left - OFFPAGE_MARGIN_PT or x > right + OFFPAGE_MARGIN_PT
                  or y < bottom - OFFPAGE_MARGIN_PT or y > top + OFFPAGE_MARGIN_PT):
                kind = "offpage"
            elif (state["tr"] in (0, 2, 4, 6) and _is_white(state["fill"], state["space"])
                  and not inside(x, y + size / 3, backgrounds) and not inside(x, y + size / 3, images)):
                kind = "white"
            if kind:
                hidden.setdefault(kind, set()).add(index)
    return hidden, photos, operations


def _text_of_operations(page, reader, operations: list, keep: set[int]) -> str:
    """Text of the page when only the text operations in `keep` are drawn."""
    from pypdf import PageObject
    from pypdf.generic import ContentStream, NameObject

    new_operations = []
    for index, (operands, operator) in enumerate(operations):
        if operator in _TEXT_SHOW and index not in keep:
            if operator == b"'":
                new_operations.append(([], b"T*"))
            elif operator == b'"':
                if len(operands) >= 2:
                    new_operations.append(([operands[0]], b"Tw"))
                    new_operations.append(([operands[1]], b"Tc"))
                new_operations.append(([], b"T*"))
            continue
        new_operations.append((operands, operator))
    stream = ContentStream(None, reader)
    stream.operations = new_operations
    clone = PageObject(pdf=reader)
    for key, value in page.items():
        clone[NameObject(key)] = value
    clone[NameObject("/Contents")] = stream
    return clone.extract_text() or ""


def _letters(text: str) -> int:
    return len(re.findall(r"[^\W\d_]", text))


def _pdf_facts(path: Path) -> FileFacts:
    from pypdf import PdfReader

    facts = FileFacts(kind="pdf")
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            reader.decrypt("")
        facts.pages = len(reader.pages)
        metadata = reader.metadata or {}
        facts.producer = " ".join(str(metadata.get(k, "") or "") for k in ("/Producer", "/Creator")).strip()
    except Exception:  # noqa: BLE001 - unreadable PDF: nothing to add
        facts.readable = False
        return facts
    for number, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            text = ""
        has_images = False
        try:
            resources = page.get("/Resources")
            xobjects = resources.get_object().get("/XObject") if resources is not None else None
            has_images = bool(xobjects) and any(
                x.get_object().get("/Subtype") == "/Image" for x in xobjects.get_object().values())
        except Exception:  # noqa: BLE001
            pass
        if len(re.sub(r"\W", "", text)) >= MIN_TEXT_CHARS:
            facts.text_pages += 1
        elif has_images:
            facts.image_only_pages += 1
        try:
            hidden, photos, operations = _scan_pdf_page(page, number == 0)
            facts.photo_candidates += photos
            for kind, indexes in hidden.items():
                lines = [ln.strip() for ln in _text_of_operations(page, reader, operations, indexes).split("\n")]
                lines = [ln for ln in lines if _letters(ln) >= 1]
                if _letters(" ".join(lines)) >= HIDDEN_MIN_LETTERS:
                    facts.hidden.setdefault(kind, []).extend(lines)
        except Exception:  # noqa: BLE001 - hidden-text check is best effort
            continue
    return facts


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_facts(path: Path) -> FileFacts:
    from lxml import etree

    facts = FileFacts(kind="docx")
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            document = archive.read("word/document.xml")
            app = archive.read("docProps/app.xml") if "docProps/app.xml" in names else b""
            parts = [document] + [archive.read(n) for n in names
                                  if re.fullmatch(r"word/header\d*\.xml", n)]
    except (zipfile.BadZipFile, KeyError, OSError):
        facts.readable = False
        return facts
    if app:
        m = re.search(rb"<Pages>(\d+)</Pages>", app)
        if m and int(m.group(1)) > 0:
            facts.pages = int(m.group(1))
        m = re.search(rb"<Application>([^<]*)</Application>", app)
        if m:
            facts.producer = m.group(1).decode("utf-8", "replace")
    emu_per_pt = 12700
    for part in parts:
        for m in re.finditer(rb'<wp:extent cx="(\d+)" cy="(\d+)"', part):
            width, height = int(m.group(1)) / emu_per_pt, int(m.group(2)) / emu_per_pt
            if (min(width, height) >= PHOTO_MIN_PT and height > 0
                    and PHOTO_ASPECT[0] <= width / height <= PHOTO_ASPECT[1]):
                facts.photo_candidates += 1
    try:
        root = etree.fromstring(document)
    except etree.XMLSyntaxError:
        return facts

    def shaded(element) -> bool:
        """True if the run, its paragraph or its table cell has a coloured background."""
        node = element
        while node is not None:
            for shd in node.iterfind(f"./{_W}rPr/{_W}shd") if node.tag == f"{_W}r" else []:
                if (shd.get(f"{_W}fill") or "auto").lower() not in ("auto", "ffffff"):
                    return True
            for tag in (f"./{_W}rPr/{_W}highlight",):
                if node.tag == f"{_W}r" and node.find(tag) is not None:
                    return True
            for container in (f"./{_W}pPr/{_W}shd", f"./{_W}tcPr/{_W}shd"):
                shd = node.find(container)
                if shd is not None and (shd.get(f"{_W}fill") or "auto").lower() not in ("auto", "ffffff"):
                    return True
            node = node.getparent()
        return False

    for run in root.iter(f"{_W}r"):
        text = "".join(t.text or "" for t in run.iter(f"{_W}t"))
        if not text.strip():
            continue
        properties = run.find(f"{_W}rPr")
        if properties is None:
            continue
        kind = None
        vanish = properties.find(f"{_W}vanish")
        if vanish is not None and (vanish.get(f"{_W}val") or "true").lower() not in ("0", "false", "off"):
            kind = "invisible"
        size = properties.find(f"{_W}sz")
        if kind is None and size is not None:
            try:
                if 0 < int(size.get(f"{_W}val") or "0") / 2 < TINY_FONT_PT:
                    kind = "tiny"
            except ValueError:
                pass
        color = properties.find(f"{_W}color")
        if (kind is None and color is not None
                and (color.get(f"{_W}val") or "").upper() in ("FFFFFF", "FEFEFE", "FDFDFD")
                and not shaded(run)):
            kind = "white"
        if kind:
            facts.hidden.setdefault(kind, []).append(text.strip())
    for kind in list(facts.hidden):
        if _letters(" ".join(facts.hidden[kind])) < HIDDEN_MIN_LETTERS:
            del facts.hidden[kind]
    return facts


def inspect_file(path: str | Path | None) -> FileFacts:
    """Pages, image-only pages, possible photos, hidden text and producer of a PDF/DOCX.

    Best effort: an unreadable file gives `FileFacts(readable=False)`.
    """
    if not path:
        return FileFacts()
    p = Path(path)
    if not p.is_file():
        return FileFacts(readable=False)
    if p.suffix.lower() == ".pdf":
        return _pdf_facts(p)
    if p.suffix.lower() == ".docx":
        return _docx_facts(p)
    return FileFacts(kind=p.suffix.lower().lstrip("."))


# --- readability ---------------------------------------------------------------------

_RE_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_RE_PHONE = re.compile(r"(?<![\w/.])(?:\+|00)?\d[\d\s./\-()]{6,}\d(?![\w/])")
_RE_LINKEDIN = re.compile(r"linkedin\.com/(?:in|pub)/", re.IGNORECASE)
_RE_STREET = re.compile(
    r"\b(?:via|viale|v\.le|piazza|p\.za|piazzale|corso|c\.so|vicolo|strada|largo|contrada|"
    r"localit[aà]|loc\.)\s+[^\W\d_][^\n,]{1,40}?\s*,?\s*\d{1,4}[a-z]?\b"
    r"|\b\d{1,4}[a-z]?,?\s+(?:[A-Z][\w'\-]*\s+){1,4}(?:street|st\.|road|rd\.?|lane|avenue|ave\.?|"
    r"drive|close|way|crescent|place|court|square|terrace|boulevard)\b",
    re.IGNORECASE,
)
_RE_POSTCODE = re.compile(r"\b\d{5}\b(?!\s*(?:€|eur|euro|\$|%))|\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b")
_RE_LOCATION_LABEL = re.compile(
    r"\b(?:residenza|residente|domicilio|indirizzo|citt[aà]|location|address|based in|city)\b\s*:?",
    re.IGNORECASE,
)
_RE_ODD = re.compile(r"[-�\u0000-\u0008\u000b\u000c\u000e-\u001f]|\(cid:\d+\)|Ã[\x80-\xbf]|â€")
_RE_SPACED = re.compile(r"(?<!\w)(?:[^\W\d_] ){4,}[^\W\d_](?!\w)")

_GENERIC_STEMS = {
    "cv", "curriculum", "curriculumvitae", "resume", "document", "documento", "doc", "untitled",
    "senzatitolo", "nuovo", "new", "scan", "scansione", "file", "download", "mycv", "ilmiocv",
    "cvita", "cveng", "cvitaliano", "cvenglish", "europass", "cveuropass", "curriculumeuropass",
}
_RE_VERSION = re.compile(
    r"(?:^|[\s_\-.(])(?:v\d+|ver\d*|final[e]?|def(?:initivo)?|nuovo|new|copy|copia|old|vecchio|"
    r"bozza|draft|\(\d+\)|\d+\)|aggiornato|updated|ultimo|latest)(?:$|[\s_\-.)])",
    re.IGNORECASE,
)

_REQUIRED_SECTIONS = ("experience", "education", "skills", "languages")


def _check_readability(c: _Collector, text: str, lines: list[str], sections: list[Section],
                       words: int, facts: FileFacts, path: str | None) -> None:
    area = "readability"
    if facts.kind in IMAGE_KINDS:
        c.add(area, "image_file", "problem", penalty=30)
    if facts.image_only_pages:
        # Text recognised by OCR in the app is not what the recruiter's software reads.
        severity = "warning" if facts.text_pages else "problem"
        c.add(area, "image_pages", severity, {"pages": facts.image_only_pages},
              penalty=30 if severity == "problem" else None)

    # Standard headings.
    missing = [k for k in _REQUIRED_SECTIONS if not has_section(sections, k)]
    for key in missing:
        severity = "warning" if key in ("experience", "education") else "info"
        c.add(area, f"heading_missing.{key}", severity, penalty=8 if severity == "warning" else 3,
              advice=True)
    if not missing:
        c.add(area, "headings_ok", "good")

    # Contacts.
    head = rows_of(sections, HEAD)
    email = _RE_EMAIL.search(text)
    phones = [m.group(0) for m in _RE_PHONE.finditer(text)
              if len(re.sub(r"\D", "", m.group(0))) >= 8 and not find_period(m.group(0))]
    if email:
        c.add(area, "contact_email", "good", evidence=[email.group(0)])
    else:
        c.add(area, "contact_email_missing", "problem", penalty=20)
    if phones:
        c.add(area, "contact_phone", "good")
    else:
        c.add(area, "contact_phone_missing", "warning")
    if not _has_town(head[:10], email.group(0) if email else ""):
        c.add(area, "contact_town_missing", "warning", penalty=5)
    if _RE_LINKEDIN.search(text):
        c.add(area, "contact_linkedin", "good")
    else:
        c.add(area, "contact_linkedin_missing", "info", advice=True)

    # Length.
    if facts.pages:
        if facts.pages > PAGES_MAX:
            c.add(area, "pages_too_many", "warning", {"pages": facts.pages})
        elif facts.pages > PAGES_IDEAL:
            c.add(area, "pages_many", "info", {"pages": facts.pages}, advice=True)
    if words < WORDS_MIN:
        severity = "problem" if words < WORDS_MIN // 2 else "warning"
        c.add(area, "too_short", severity, {"words": words, "minimum": WORDS_MIN},
              penalty=PENALTY[severity] if severity == "problem" else 15)
    elif words > WORDS_MAX:
        c.add(area, "too_long", "warning", {"words": words, "maximum": WORDS_MAX})
    else:
        c.add(area, "length_ok", "good", {"words": words})

    # Layout disorder (tables / columns extracted out of order).
    content = [ln for ln in lines if not _is_heading_like(ln)]
    if len(content) >= MIN_LINES_FOR_LAYOUT:
        short = [ln for ln in content if len(_words_of(ln)) <= 2 and not find_period(ln)
                 and not _RE_EMAIL.search(ln) and not _RE_PHONE.search(ln)]
        share = len(short) / len(content)
        if share >= SHORT_LINE_SHARE:
            c.add(area, "layout_fragmented", "warning", {"share": round(share * 100)}, short)
    broken = [f"{lines[i]} / {lines[i + 1]}" for i in range(len(lines) - 1)
              if re.search(r"[^\W\d_]{2,}-$", lines[i]) and re.match(r"[a-zà-ÿ]{2,}", lines[i + 1])]
    if len(broken) >= BROKEN_WORDS_MIN:
        c.add(area, "broken_words", "warning", {"count": len(broken)}, broken)
    spaced = [ln for ln in lines if _RE_SPACED.search(ln)]
    if len(spaced) >= SPACED_LETTERS_MIN:
        c.add(area, "spaced_letters", "warning", {"count": len(spaced)}, spaced, penalty=5)

    # Odd characters and bullets.
    odd_lines = [ln for ln in lines if _RE_ODD.search(ln)]
    odd_count = sum(len(_RE_ODD.findall(ln)) for ln in odd_lines)
    if odd_count >= ODD_CHARS_MIN:
        c.add(area, "odd_characters", "warning", {"count": odd_count}, odd_lines)
    fancy = [ln for ln in lines if ln[:1] in _FANCY_BULLETS]
    if len(fancy) >= FANCY_BULLETS_MIN:
        c.add(area, "fancy_bullets", "info", {"count": len(fancy)}, fancy, advice=True)

    # File name.
    if path:
        _check_file_name(c, Path(path))


def _is_heading_like(line: str) -> bool:
    from .sections import detect_heading

    return detect_heading(line) is not None


def _has_town(head: list[str], email: str) -> bool:
    for row in head:
        if _RE_LOCATION_LABEL.search(row) or _RE_STREET.search(row) or _RE_POSTCODE.search(row):
            return True
        if not (_RE_EMAIL.search(row) or _RE_PHONE.search(row)):
            continue
        cleaned = row.replace(email, " ") if email else row
        cleaned = _RE_EMAIL.sub(" ", cleaned)
        cleaned = _RE_PHONE.sub(" ", cleaned)
        cleaned = re.sub(r"https?://\S+|www\.\S+|\S+\.(?:com|it|net|org|eu)\S*", " ", cleaned)
        for part in re.split(r"\s[-–—|·•]\s|[|·•,;]", cleaned):
            part = re.sub(r"\b(?:tel|cell|phone|mobile|email|e-mail|mail)\b\.?:?", " ", part,
                          flags=re.IGNORECASE).strip(" .:-–—")
            tokens = part.split()
            if (1 <= len(tokens) <= 4 and not re.search(r"\d", part)
                    and all(t[:1].isupper() or t.lower() in ("di", "del", "della", "upon", "on", "de")
                            for t in tokens)
                    and _letters(part) >= 3):
                return True
    return False


def _check_file_name(c: _Collector, p: Path) -> None:
    stem = p.stem
    compact = re.sub(r"[\s_\-.()]+", "", stem.lower())
    problems = []
    if compact in _GENERIC_STEMS or re.fullmatch(r"(?:document|documento|doc|scan|img|image)\d*", compact):
        problems.append("generic")
    if _RE_VERSION.search(stem):
        problems.append("version")
    if re.search(r"[^\w\s\-.()]", stem) or len(stem) > 60:
        problems.append("characters")
    if "generic" in problems:
        c.add("readability", "filename_generic", "warning", {"name": p.name}, penalty=5)
    if "version" in problems:
        c.add("readability", "filename_version", "info", {"name": p.name}, advice=True)
    if "characters" in problems:
        c.add("readability", "filename_characters", "info", {"name": p.name}, advice=True)
    if not problems:
        c.add("readability", "filename_ok", "good", {"name": p.name})


# --- career ------------------------------------------------------------------------

_MONTH_WORDS = (
    "gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre|"
    "january|february|march|april|may|june|july|august|september|october|november|december|"
    "gen|feb|mar|apr|mag|giu|lug|ago|sett?|ott|nov|dic|jan|jun|jul|aug|sept?|oct|dec"
)
_RE_MONTH_NAME = re.compile(rf"\b(?:{_MONTH_WORDS})\.?\s*'?\s*(?:19|20)\d{{2}}", re.IGNORECASE)
_RE_INTERNSHIP = re.compile(
    r"\b(?:stage|stagista|tirocinio|tirocinante|intern|internship|trainee|apprendista|apprentice|"
    r"praticante|praticantato|erasmus|summer job|lavoro estivo|stagionale|seasonal)\b",
    re.IGNORECASE,
)
_EXCLUDED_FOR_EXPERIENCE = {"education", "certifications", "languages", "skills", "projects", "other"}


def _date_format(period_text: str) -> str:
    if re.search(r"\d{1,2}[/.\-]\d{1,2}[/.\-](?:19|20)\d{2}", period_text):
        return "dd/mm/yyyy"
    if re.search(r"\d{1,2}\s?[/.\-]\s?(?:19|20)\d{2}", period_text):
        return "mm/yyyy"
    if _RE_MONTH_NAME.search(period_text):
        return "month yyyy"
    return "yyyy"


def _experience_rows(sections: list[Section]) -> list[str]:
    if has_section(sections, "experience"):
        return rows_of(sections, "experience")
    return [r for s in sections if s.key not in _EXCLUDED_FOR_EXPERIENCE for r in s.rows]


def _experiences(rows: list[str], today: date):
    """Dated experiences, without those whose date sits in a bullet ("- ... from 2019")."""
    experiences, periods = extract_experiences(rows, today)
    kept = []
    for e in experiences:
        date_rows = [r for r in e.evidence if e.period.text in r]
        if date_rows and all(_is_bullet(r) for r in date_rows):
            continue
        kept.append(e)
    return kept, periods


def _check_career(c: _Collector, profile: CVProfile, sections: list[Section], today: date) -> None:
    area = "career"
    rows = _experience_rows(sections)
    experiences, periods = _experiences(rows, today)
    years = profile.years_experience if profile.years_experience is not None else years_from_periods(periods)
    if years is not None:
        c.add(area, "total_years", "info", {"years": years}, advice=False)

    if has_section(sections, "experience") and len(rows) >= 3 and not periods:
        c.add(area, "no_dates", "problem", evidence=rows[:3])
        return
    if not experiences:
        return

    # Undated roles: "Title presso/at Employer" rows with no period nearby.
    dated = {i for i, r in enumerate(rows) if find_period(r, today)}
    undated = []
    for i, row in enumerate(rows):
        if (not _is_bullet(row) and len(row.split()) <= 12
                and re.search(r"\s(?:presso|at|@)\s", row)
                and not any(j in dated for j in range(i - 2, i + 3))):
            undated.append(row)
    if undated:
        c.add(area, "undated_roles", "warning", {"count": len(undated)}, undated)

    def evidence_of(e) -> str:
        return e.evidence[0] if e.evidence else e.period.text

    # Order (reverse chronological = most recent first, in document order).
    out_of_order = [
        (a, b) for a, b in zip(experiences, experiences[1:])
        if b.period.start > a.period.start and b.period.end > a.period.end
    ]
    if out_of_order:
        a, b = out_of_order[0]
        c.add(area, "not_reverse_chronological", "warning", {"count": len(out_of_order)},
              [evidence_of(a), evidence_of(b)])
    elif len(experiences) >= 2:
        c.add(area, "reverse_chronological", "good")

    # Gaps between roles (union of the periods).
    ordered = sorted(experiences, key=lambda e: (e.period.start, e.period.end))
    gaps = []
    reach = ordered[0].period.end
    reach_exp = ordered[0]
    for e in ordered[1:]:
        if e.period.start - reach - 1 > GAP_MONTHS:
            gaps.append((reach_exp, e, e.period.start - reach - 1))
        if e.period.end >= reach:
            reach, reach_exp = e.period.end, e
    for before, after, months in gaps:
        c.add(area, "gap", "warning",
              {"months": months, "start": _month_text(before.period.end + 1),
               "end": _month_text(after.period.start - 1)},
              [evidence_of(before), evidence_of(after)], penalty=8)
    today_index = today.year * 12 + today.month - 1
    if not any(e.period.in_progress for e in experiences):
        since = today_index - reach
        if since > CURRENT_GAP_MONTHS:
            c.add(area, "current_gap", "info", {"months": since, "since": _month_text(reach + 1)},
                  [evidence_of(reach_exp)], advice=True)
    if not gaps and len(experiences) >= 2:
        c.add(area, "no_gaps", "good")

    # Short stints (current role and internships excluded).
    short = [e for e in experiences
             if not e.period.in_progress and e.period.months < SHORT_STINT_MONTHS
             and not _RE_INTERNSHIP.search(" ".join(e.evidence + [e.title]))]
    if short:
        severity = ("problem" if len(short) >= SHORT_STINTS_PROBLEM
                    else "warning" if len(short) >= SHORT_STINTS_WARNING else "info")
        c.add(area, "short_stints", severity,
              {"count": len(short), "months": SHORT_STINT_MONTHS},
              [evidence_of(e) for e in short], advice=True)

    # Overlaps.
    overlaps = []
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            months = min(a.period.end, b.period.end) - max(a.period.start, b.period.start) + 1
            year_only = "yyyy" in (_date_format(a.period.text), _date_format(b.period.text))
            minimum = OVERLAP_YEAR_ONLY_MIN_MONTHS if year_only else OVERLAP_MIN_MONTHS
            if months >= minimum:
                overlaps.append((a, b, months))
    if overlaps:
        a, b, months = overlaps[0]
        c.add(area, "overlaps", "info", {"count": len(overlaps), "months": months},
              [evidence_of(a), evidence_of(b)], advice=True)

    # Date formats.
    formats = [_date_format(e.period.text) for e in experiences]
    distinct = list(dict.fromkeys(formats))
    if len(distinct) > 1:
        c.add(area, "date_formats_mixed", "info", {"formats": ", ".join(distinct)},
              [e.period.text for e in experiences], advice=True, penalty=3)
    year_only = sum(1 for f in formats if f == "yyyy")
    if year_only and year_only * 2 >= len(formats):
        c.add(area, "dates_year_only", "warning", {"count": year_only},
              [e.period.text for e in experiences if _date_format(e.period.text) == "yyyy"], penalty=6)


# --- substance ---------------------------------------------------------------------

_RE_RESULT = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:%|percent|per cento|k\b|mln|m\b|milioni|million|mila|thousand)"
    r"|[€$£]\s?\d|\d\s?(?:€|eur|euro|usd|gbp)\b"
    r"|\b\d+(?:[.,]\d+)?\s+[^\W\d_]{3,}",
    re.IGNORECASE,
)
_RE_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_PRONOUNS = {
    "en": re.compile(r"\b(?:I|my|me|myself|I'm|I've)\b"),
    "it": re.compile(r"\b(?:io|mio|mia|miei|mie|me stesso|me stessa|ho lavorato|mi occupo|mi occupavo|"
                     r"sono stato|sono stata)\b", re.IGNORECASE),
}


def _has_result(line: str) -> bool:
    cleaned = _RE_YEAR.sub(" ", line)
    period = find_period(line)
    if period:
        cleaned = cleaned.replace(period.text, " ")
    return bool(_RE_RESULT.search(cleaned))


def _content_lines(sections: list[Section], today: date) -> list[str]:
    """Description lines of the experiences (bullets, or prose rows without dates)."""
    rows = _experience_rows(sections) + rows_of(sections, "projects")
    bullets = [_strip_bullet(r) for r in rows if _is_bullet(r)]
    bullets = [b for b in bullets if len(_words_of(b)) >= 2]
    if len(bullets) >= 2:
        return bullets
    prose = []
    for r in rows:
        if find_period(r, today) or len(_words_of(r)) < 5:
            continue
        prose.append(_strip_bullet(r))
    return prose


def _check_substance(c: _Collector, text: str, lines: list[str], sections: list[Section],
                     language: str, today: date, profile: CVProfile) -> None:
    area = "substance"
    content = _content_lines(sections, today)
    roles_count = len(_experiences(_experience_rows(sections), today)[0])
    if len(content) >= MIN_CONTENT_LINES:
        with_results = [ln for ln in content if _has_result(ln)]
        share = len(with_results) / len(content)
        params = {"share": round(share * 100), "count": len(with_results), "total": len(content)}
        if share >= RESULTS_SHARE_GOOD:
            c.add(area, "results_good", "good", params, with_results)
        elif share >= RESULTS_SHARE_LOW:
            c.add(area, "results_few", "info", params, with_results, advice=True, penalty=10)
        else:
            c.add(area, "results_missing", "warning", params, content, penalty=25)
        roles = max(1, roles_count)
        if len(content) / roles < THIN_LINES_PER_ROLE:
            c.add(area, "descriptions_thin", "info",
                  {"lines": len(content), "roles": roles_count}, advice=True, penalty=8)

        weak_re = _phrase_regex(_both_languages("weak_openings", language))
        action = {normalize(v) for v in _both_languages("action_starts", language)}
        weak, strong = [], []
        for ln in content:
            norm = normalize(ln)
            m = weak_re.match(norm) if weak_re else None
            if m:
                weak.append(ln)
                continue
            first_words = re.findall(r"[^\W\d_]+", norm)[:2]
            if first_words and (first_words[0] in action or " ".join(first_words) in action):
                strong.append(ln)
        weak_share = len(weak) / len(content)
        if len(weak) >= WEAK_MIN and weak_share >= WEAK_SHARE:
            c.add(area, "weak_openings", "warning", {"count": len(weak), "share": round(weak_share * 100)},
                  weak)
        elif weak:
            c.add(area, "weak_openings_few", "info", {"count": len(weak)}, weak, advice=True)
        if len(strong) / len(content) >= ACTION_SHARE_GOOD:
            c.add(area, "action_verbs", "good", {"share": round(len(strong) / len(content) * 100)})
    elif has_section(sections, "experience"):
        c.add(area, "descriptions_missing", "warning", penalty=15)

    buzz_re = _phrase_regex(_both_languages("buzzwords", language))
    buzz_lines = [ln for ln in lines if buzz_re and buzz_re.search(normalize(ln))]
    if buzz_lines:
        hits = [m.group(0) for ln in buzz_lines for m in buzz_re.finditer(normalize(ln))]
        terms = list(dict.fromkeys(hits))
        severity = "warning" if len(hits) >= BUZZ_HITS_WARNING else "info"
        c.add(area, "buzzwords", severity, {"count": len(hits), "terms": ", ".join(terms[:8])},
              buzz_lines, advice=True, penalty=8 if severity == "warning" else 2)

    pronoun_re = _PRONOUNS[_lang(language)]
    pronoun_lines = [ln for ln in lines if pronoun_re.search(ln)]
    pronoun_count = sum(len(pronoun_re.findall(ln)) for ln in pronoun_lines)
    if pronoun_count >= PRONOUNS_WARNING:
        c.add(area, "first_person", "warning", {"count": pronoun_count}, pronoun_lines, penalty=5)
    elif pronoun_count:
        c.add(area, "first_person_few", "info", {"count": pronoun_count}, pronoun_lines, advice=True)

    long_lines = [ln for ln in lines if len(_words_of(ln)) > LONG_PARAGRAPH_WORDS]
    if long_lines:
        c.add(area, "long_paragraphs", "warning", {"count": len(long_lines), "words": LONG_PARAGRAPH_WORDS},
              long_lines, penalty=5)

    _check_repeated_words(c, text, language, profile)


def _check_repeated_words(c: _Collector, text: str, language: str, profile: CVProfile) -> None:
    stop = {normalize(w) for w in _both_languages("stop_words", language)}
    exclude = set(stop)
    for item in profile.roles + profile.skills:
        for form in item_variants(item):  # CV wording too (translated profiles)
            exclude.update(normalize(w) for w in _words_of(form))
    words = [normalize(w) for w in _words_of(text)]
    counts = Counter(w for w in words if len(w) >= 6 and w not in exclude)
    if not counts or not words:
        return
    word, count = counts.most_common(1)[0]
    if count >= REPEATED_WORD_MIN and count / len(words) >= REPEATED_WORD_SHARE:
        evidence = [ln for ln in text.split("\n") if word in normalize(ln)]
        c.add("substance", "repeated_word", "info", {"word": word, "count": count}, evidence,
              advice=True, penalty=3)


# --- role coverage -------------------------------------------------------------------

def _stem(word: str) -> str:
    return word[:5] if len(word) > 5 else word


def _skill_coverage(skills: list[str], lines: list[str], language: str, embedder=None) -> list[SkillCoverage]:
    filler = {normalize(w) for w in _both_languages("skill_filler_words", language)}
    norm_lines = [normalize(ln) for ln in lines]
    text_norm = "\n".join(norm_lines)
    text_stems = {_stem(w) for w in re.findall(r"[^\W_]+", text_norm)}
    result: list[SkillCoverage] = []
    pending: list[int] = []
    for skill in skills:
        s = normalize(skill)
        if not s:
            continue
        item = SkillCoverage(skill=skill, covered=False)
        result.append(item)
        if re.search(rf"(?<![\w]){re.escape(s)}(?![\w])", text_norm):
            item.covered, item.method = True, "exact"
            item.evidence = _evidence([lines[i] for i, n in enumerate(norm_lines) if s in n], 2)
            continue
        if len(s) >= 4:
            scored = [(fuzz.partial_ratio(s, n), i) for i, n in enumerate(norm_lines) if len(n) >= len(s) // 2]
            best = max(scored, default=(0, -1))
            if best[0] >= FUZZY_LINE_SCORE and len(s.split()) <= 4:
                item.covered, item.method = True, "fuzzy"
                item.evidence = _evidence([lines[best[1]]], 1)
                continue
        tokens = [_stem(w) for w in re.findall(r"[^\W_]+", s) if w not in filler and len(w) >= 3]
        if tokens:
            share = sum(1 for t in tokens if t in text_stems) / len(tokens)
            if share >= TOKEN_SHARE and len(tokens) >= 2:
                item.covered, item.method = True, "tokens"
                hits = [lines[i] for i, n in enumerate(norm_lines)
                        if sum(1 for t in tokens if re.search(rf"\b{re.escape(t)}", n)) >= max(1, len(tokens) // 2)]
                item.evidence = _evidence(hits, 2)
                continue
        pending.append(len(result) - 1)
    if embedder is not None and pending:
        _semantic_coverage(result, pending, lines, embedder)
    return result


def _semantic_coverage(result: list[SkillCoverage], pending: list[int], lines: list[str], embedder) -> None:
    import numpy as np

    candidates = [ln for ln in lines if len(_words_of(ln)) >= 2][:200]
    if not candidates:
        return
    try:
        low, high = tuple(getattr(embedder, "similarity_range", (0.0, 1.0)))
        threshold = low + SEMANTIC_POSITION * (high - low)
        queries = embedder.embed([result[i].skill for i in pending], kind="query")
        passages = embedder.embed(candidates, kind="passage")
        similarity = np.asarray(queries) @ np.asarray(passages).T
    except Exception:  # noqa: BLE001 - semantic check is optional
        return
    for row, i in enumerate(pending):
        best = int(similarity[row].argmax())
        if float(similarity[row, best]) >= threshold:
            result[i].covered, result[i].method = True, "semantic"
            result[i].evidence = _evidence([candidates[best]], 1)


def _check_coverage(c: _Collector, esco_skills: list[str], lines: list[str], language: str,
                    embedder) -> list[SkillCoverage]:
    coverage = _skill_coverage(esco_skills, lines, language, embedder)
    if not coverage:
        return coverage
    covered = [x for x in coverage if x.covered]
    missing = [x for x in coverage if not x.covered]
    share = len(covered) / len(coverage)
    params = {"covered": len(covered), "total": len(coverage), "share": round(share * 100)}
    if share >= COVERAGE_GOOD:
        c.add("coverage", "good", "good", params)
    elif share >= COVERAGE_LOW:
        c.add("coverage", "partial", "warning", params, penalty=round((COVERAGE_GOOD - share) * 100))
    else:
        c.add("coverage", "low", "problem", params, penalty=round((COVERAGE_GOOD - share) * 100))
    if covered:
        c.add("coverage", "covered_skills", "good",
              {"skills": ", ".join(x.skill for x in covered[:MAX_LISTED_SKILLS]), "count": len(covered)},
              [e for x in covered for e in x.evidence])
    if missing:
        c.add("coverage", "missing_skills", "info",
              {"skills": ", ".join(x.skill for x in missing[:MAX_LISTED_SKILLS]), "count": len(missing)},
              advice=True)
    return coverage


# --- personal data -----------------------------------------------------------------

_PERSONAL_PATTERNS = {
    "birth_date": re.compile(
        r"\b(?:data di nascita|data e luogo di nascita|luogo e data di nascita|nat[oa] (?:a|il)\b|"
        r"date of birth|birth date|born (?:on|in)\b|d\.o\.b\.?|dob\b)", re.IGNORECASE),
    "age": re.compile(
        r"\b(?:et[aà]\s*:?\s*\d{2}\b|age\s*:\s*\d{2}\b|\d{2}\s*years old\b|"
        r"\d{2}\s*anni\b(?!\s*(?:di|d')\s*esperienza))", re.IGNORECASE),
    "marital_status": re.compile(
        r"\b(?:stato civile|marital status|coniugat[oa]|sposat[oa]|celibe|nubile|divorziat[oa]|"
        r"vedov[oa]|separat[oa] legalmente|married|divorced|widowed|\d\s+figli|con figli|"
        r"children\s*:)", re.IGNORECASE),
    "nationality": re.compile(r"\b(?:nazionalit[aà]|cittadinanza|nationality|citizenship)\b", re.IGNORECASE),
    "gender": re.compile(r"\b(?:sesso\s*:\s*[mf]\b|sesso\s*:\s*(?:maschile|femminile)|gender\s*:|sex\s*:)",
                         re.IGNORECASE),
}
_RE_TAX_CODE = re.compile(r"\b[A-Z]{6}\d{2}[A-EHLMPRST]\d{2}[A-Z]\d{3}[A-Z]\b", re.IGNORECASE)
_RE_TAX_LABEL = re.compile(r"\b(?:codice fiscale|cod\.\s*fisc\.?|c\.f\.\s*:)", re.IGNORECASE)
_RE_GDPR = re.compile(
    r"(?:autorizzo|acconsento|consenso).{0,60}trattamento|trattamento dei (?:miei )?dati personali"
    r"|d\.?\s?lgs\.?\s*(?:n\.?\s*)?196|(?:reg(?:olamento)?\.?\s*)?(?:\(?ue\)?|eu)\s*(?:n\.?\s*)?2016/679"
    r"|\bgdpr\b",
    re.IGNORECASE,
)
_RE_PHOTO_MENTION = re.compile(r"\b(?:foto(?:grafia)? allegata|photo attached|\[foto\]|\[photo\])", re.IGNORECASE)


def _mask_tax_code(line: str) -> str:
    return _RE_TAX_CODE.sub(lambda m: m.group(0)[:3] + "*" * (len(m.group(0)) - 3), line)


def _check_personal(c: _Collector, lines: list[str], language: str, facts: FileFacts) -> None:
    area = "personal"
    severities = {"birth_date": "warning", "age": "warning", "marital_status": "warning",
                  "nationality": "info", "gender": "warning"}
    penalties = {"birth_date": 10, "age": 10, "marital_status": 15, "nationality": 0, "gender": 10}
    for name, pattern in _PERSONAL_PATTERNS.items():
        hits = [ln for ln in lines if pattern.search(ln)]
        if hits:
            c.add(area, name, severities[name], evidence=[_mask_tax_code(h) for h in hits],
                  penalty=penalties[name], advice=True)
    tax = [ln for ln in lines if _RE_TAX_CODE.search(ln) or _RE_TAX_LABEL.search(ln)]
    if tax:
        c.add(area, "tax_code", "problem", evidence=[_mask_tax_code(t) for t in tax], penalty=25)
    address = [ln for ln in lines[:15] if _RE_STREET.search(ln)]
    if address:
        c.add(area, "full_address", "info", evidence=address, advice=True, penalty=3)
    if facts.photo_candidates or any(_RE_PHOTO_MENTION.search(ln) for ln in lines):
        c.add(area, "photo", "info", {"count": max(1, facts.photo_candidates)}, advice=True)
    gdpr = [ln for ln in lines if _RE_GDPR.search(ln)]
    if gdpr:
        c.add(area, "gdpr_present", "info", evidence=gdpr, advice=False)
    elif language == "it":
        c.add(area, "gdpr_missing", "info", advice=True)
    if not any(f.area == area and f.severity in ("warning", "problem") for f in c.findings):
        c.add(area, "clean", "good")


# --- hidden text and keyword stuffing ----------------------------------------------

def _check_hidden(c: _Collector, text: str, lines: list[str], language: str, profile: CVProfile,
                  facts: FileFacts) -> bool:
    """Returns True if hidden text was evidenced in the file."""
    area = "hidden"
    found = False
    for kind in ("invisible", "white", "tiny", "offpage"):
        hidden_lines = facts.hidden.get(kind) or []
        if hidden_lines:
            found = True
            c.add(area, f"text_{kind}", "problem",
                  {"count": len(hidden_lines), "words": len(_words_of(" ".join(hidden_lines)))},
                  hidden_lines, penalty=40)

    # The same term many times.
    words = max(1, len(_words_of(text)))
    text_norm = normalize(text)
    terms = list(dict.fromkeys(normalize(form) for item in profile.skills for form in item_variants(item)
                               if len(form) >= 3))
    repeated = []
    for term in terms:
        count = len(re.findall(rf"(?<![\w]){re.escape(term)}(?![\w])", text_norm))
        if count >= STUFFING_REPEAT and count / words >= STUFFING_SHARE:
            repeated.append((term, count))
    if repeated:
        repeated.sort(key=lambda x: -x[1])
        term, count = repeated[0]
        evidence = [ln for ln in lines if term in normalize(ln)]
        c.add(area, "repeated_term", "warning", {"term": term, "count": count}, evidence, penalty=15)

    # Long keyword lists, and the same long list twice.
    long_lists = [ln for ln in lines if len([p for p in re.split(r"[,;|•·]", ln) if p.strip()]) >= LONG_LIST_ITEMS]
    if long_lists:
        c.add(area, "long_keyword_list", "info", {"count": len(long_lists), "items": LONG_LIST_ITEMS},
              long_lists, advice=True, penalty=3)
    candidates = [ln for ln in lines if len(_words_of(ln)) >= 8]
    duplicates = []
    for i, a in enumerate(candidates):
        for b in candidates[i + 1:]:
            if fuzz.ratio(normalize(a), normalize(b)) >= DUPLICATE_BLOCK_RATIO:
                duplicates.append(a)
                break
    if duplicates:
        c.add(area, "duplicated_block", "warning", {"count": len(duplicates)}, duplicates, penalty=15)

    # Text copied from a job advertisement.
    ad_re = _phrase_regex(_both_languages("job_ad_markers", language))
    ad_lines = [ln for ln in lines if ad_re and ad_re.search(normalize(ln))]
    if len(ad_lines) >= 2:
        c.add(area, "job_ad_text", "warning", {"count": len(ad_lines)}, ad_lines, penalty=15)

    if not any(f.area == area and f.severity in ("warning", "problem") for f in c.findings):
        c.add(area, "clean", "good", {"file_checked": 1 if facts.kind in ("pdf", "docx") else 0})
    return found


# --- public API ---------------------------------------------------------------------

def _area_scores(findings: list[Finding], assessed: list[str]) -> dict[str, int | None]:
    scores: dict[str, int | None] = {}
    for area in AREAS:
        if area not in assessed:
            scores[area] = None
            continue
        penalty = sum(f.penalty for f in findings if f.area == area)
        scores[area] = max(0, min(100, 100 - penalty))
    return scores


def _overall(scores: dict[str, int | None]) -> int:
    total = sum(AREA_WEIGHTS[a] for a, s in scores.items() if s is not None)
    if not total:
        return 0
    return round(sum(AREA_WEIGHTS[a] * s for a, s in scores.items() if s is not None) / total)


def review(profile: CVProfile, path: str | None = None, *, esco_skills: list[str] | None = None,
           today: date | None = None, embedder=None) -> RecruiterReview:
    """Screen the CV as a recruiter would.

    `path` (PDF/DOCX, optional; defaults to `profile.cv_path`) enables the file
    checks: pages, image-only pages, photo, hidden text, file name.
    `esco_skills`: essential skills of the candidate's ESCO occupation, in the CV
    language; enables the role-coverage area. `embedder` (optional, interface of
    `scoring.embedding.Embedder`) adds a semantic match for the skills not found
    literally. No network access.
    """
    today = today or date.today()
    path = path if path is not None else (profile.cv_path or None)
    text = profile.text or ""
    language = profile.language or "it"
    facts = inspect_file(path)
    c = _Collector()
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    words = len(_words_of(text))

    if len(re.sub(r"\W", "", text)) < MIN_TEXT_CHARS:
        c.add("readability", "no_text", "problem", penalty=100)
        scores = _area_scores(c.findings, ["readability"])
        return RecruiterReview(score=0, area_scores=scores, findings=c.findings, language=language,
                               words=words, pages=facts.pages)

    sections = split_sections(text)
    _check_readability(c, text, lines, sections, words, facts, path)
    _check_career(c, profile, sections, today)
    _check_substance(c, text, lines, sections, language, today, profile)
    coverage: list[SkillCoverage] = []
    assessed = ["readability", "career", "substance", "personal", "hidden"]
    if esco_skills:
        coverage = _check_coverage(c, esco_skills, lines, language, embedder)
        if coverage:
            assessed.append("coverage")
    _check_personal(c, lines, language, facts)
    hidden_found = _check_hidden(c, text, lines, language, profile, facts)

    order = {s: i for i, s in enumerate(SEVERITIES)}
    findings = sorted(c.findings, key=lambda f: (AREAS.index(f.area), order[f.severity]))
    scores = _area_scores(findings, assessed)
    score = _overall(scores)
    capped = hidden_found and score > HIDDEN_TEXT_CAP
    if capped:
        score = HIDDEN_TEXT_CAP
    return RecruiterReview(score=score, area_scores=scores, findings=findings, coverage=coverage,
                           language=language, words=words, pages=facts.pages, capped=capped)


def message_keys() -> dict[str, set[str]]:
    """Every finding key this module can emit, with the parameter names it fills.

    Advice keys (`advice_keys()`) may use the same parameters as their finding.
    Used by the tests to check the locale files.
    """
    return dict(_MESSAGE_KEYS)


def advice_keys() -> dict[str, set[str]]:
    """Every advice key, with the parameters available to it (those of its finding)."""
    return {f"{k}.advice": set(v) for k, v in _MESSAGE_KEYS.items() if k not in _NO_ADVICE}


def _k(area: str, check: str) -> str:
    return f"{_KEY_PREFIX}.{area}.{check}"


_MESSAGE_KEYS: dict[str, set[str]] = {
    **{_k("readability", f"heading_missing.{s}"): set() for s in _REQUIRED_SECTIONS},
    _k("readability", "no_text"): set(),
    _k("readability", "image_pages"): {"pages"},
    _k("readability", "image_file"): set(),
    _k("readability", "headings_ok"): set(),
    _k("readability", "contact_email"): set(),
    _k("readability", "contact_email_missing"): set(),
    _k("readability", "contact_phone"): set(),
    _k("readability", "contact_phone_missing"): set(),
    _k("readability", "contact_town_missing"): set(),
    _k("readability", "contact_linkedin"): set(),
    _k("readability", "contact_linkedin_missing"): set(),
    _k("readability", "pages_too_many"): {"pages"},
    _k("readability", "pages_many"): {"pages"},
    _k("readability", "too_short"): {"words", "minimum"},
    _k("readability", "too_long"): {"words", "maximum"},
    _k("readability", "length_ok"): {"words"},
    _k("readability", "layout_fragmented"): {"share"},
    _k("readability", "broken_words"): {"count"},
    _k("readability", "spaced_letters"): {"count"},
    _k("readability", "odd_characters"): {"count"},
    _k("readability", "fancy_bullets"): {"count"},
    _k("readability", "filename_generic"): {"name"},
    _k("readability", "filename_version"): {"name"},
    _k("readability", "filename_characters"): {"name"},
    _k("readability", "filename_ok"): {"name"},
    _k("career", "total_years"): {"years"},
    _k("career", "no_dates"): set(),
    _k("career", "undated_roles"): {"count"},
    _k("career", "not_reverse_chronological"): {"count"},
    _k("career", "reverse_chronological"): set(),
    _k("career", "gap"): {"months", "start", "end"},
    _k("career", "current_gap"): {"months", "since"},
    _k("career", "no_gaps"): set(),
    _k("career", "short_stints"): {"count", "months"},
    _k("career", "overlaps"): {"count", "months"},
    _k("career", "date_formats_mixed"): {"formats"},
    _k("career", "dates_year_only"): {"count"},
    _k("substance", "results_good"): {"share", "count", "total"},
    _k("substance", "results_few"): {"share", "count", "total"},
    _k("substance", "results_missing"): {"share", "count", "total"},
    _k("substance", "weak_openings"): {"count", "share"},
    _k("substance", "weak_openings_few"): {"count"},
    _k("substance", "action_verbs"): {"share"},
    _k("substance", "descriptions_missing"): set(),
    _k("substance", "descriptions_thin"): {"lines", "roles"},
    _k("substance", "buzzwords"): {"count", "terms"},
    _k("substance", "first_person"): {"count"},
    _k("substance", "first_person_few"): {"count"},
    _k("substance", "long_paragraphs"): {"count", "words"},
    _k("substance", "repeated_word"): {"word", "count"},
    _k("coverage", "good"): {"covered", "total", "share"},
    _k("coverage", "partial"): {"covered", "total", "share"},
    _k("coverage", "low"): {"covered", "total", "share"},
    _k("coverage", "covered_skills"): {"skills", "count"},
    _k("coverage", "missing_skills"): {"skills", "count"},
    _k("personal", "birth_date"): set(),
    _k("personal", "age"): set(),
    _k("personal", "marital_status"): set(),
    _k("personal", "nationality"): set(),
    _k("personal", "gender"): set(),
    _k("personal", "tax_code"): set(),
    _k("personal", "full_address"): set(),
    _k("personal", "photo"): {"count"},
    _k("personal", "gdpr_present"): set(),
    _k("personal", "gdpr_missing"): set(),
    _k("personal", "clean"): set(),
    _k("hidden", "text_invisible"): {"count", "words"},
    _k("hidden", "text_white"): {"count", "words"},
    _k("hidden", "text_tiny"): {"count", "words"},
    _k("hidden", "text_offpage"): {"count", "words"},
    _k("hidden", "repeated_term"): {"term", "count"},
    _k("hidden", "long_keyword_list"): {"count", "items"},
    _k("hidden", "duplicated_block"): {"count"},
    _k("hidden", "job_ad_text"): {"count"},
    _k("hidden", "clean"): {"file_checked"},
}

# Findings that never carry advice (positive results and plain facts).
_NO_ADVICE = {
    _k("readability", "headings_ok"), _k("readability", "contact_email"), _k("readability", "contact_phone"),
    _k("readability", "contact_linkedin"), _k("readability", "length_ok"), _k("readability", "filename_ok"),
    _k("career", "total_years"), _k("career", "reverse_chronological"), _k("career", "no_gaps"),
    _k("substance", "results_good"), _k("substance", "action_verbs"), _k("coverage", "good"),
    _k("coverage", "covered_skills"), _k("personal", "gdpr_present"), _k("personal", "clean"),
    _k("hidden", "clean"),
}

# Keys used by the GUI around the findings (titles, severities, areas, method names).
# Key -> placeholders the GUI must fill.
UI_KEYS: dict[str, set[str]] = {
    f"{_KEY_PREFIX}.title": set(),
    f"{_KEY_PREFIX}.intro": set(),
    f"{_KEY_PREFIX}.score": {"score"},
    f"{_KEY_PREFIX}.capped": {"cap"},
    f"{_KEY_PREFIX}.evidence": set(),
    f"{_KEY_PREFIX}.advice": set(),
    f"{_KEY_PREFIX}.not_assessed": set(),
    **{f"{_KEY_PREFIX}.area.{a}": set() for a in AREAS},
    **{f"{_KEY_PREFIX}.severity.{s}": set() for s in SEVERITIES},
    **{f"{_KEY_PREFIX}.method.{m}": set() for m in ("exact", "fuzzy", "tokens", "semantic")},
}
