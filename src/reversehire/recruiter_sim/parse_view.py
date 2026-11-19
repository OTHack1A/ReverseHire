"""Check 1, the ATS parse view: the text a parser would hand to the recruiter, with
what it loses, under the parser settings of a profile.

Rules (study §1.3 "what breaks parsing", Textkernel Resume Quality codes):
reading order with columns (433), text in headers/footers, text in images,
tables and text boxes, undecodable glyphs (icons, fonts without a Unicode map),
non-standard or letter-spaced headings (412), contacts not at the top (311),
vertical date ranges (418), page count.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..cv import skills as cv_skills
from ..cv.extraction import find_period
from ..cv.sections import detect_heading, has_section, rows_of, split_sections
from .layout import LayoutFacts
from .model import (CHECK_PARSE, COMPONENT_PARSE, FAIL, OK, SOURCE_CV, SOURCE_FILE, SOURCE_PROFILE, WARN,
                    CvDocument, Edit, EvidenceRef, Finding, msg)

MIN_TEXT_CHARS = 30
MAX_EVIDENCE = 4
EVIDENCE_CHARS = 140
LOST_TEXT_FAIL_CHARS = 50
GLYPHS_MIN = 3
CONTACT_TOP_LINES = 10
CONTACT_TOP_SHARE = 0.25
HEADING_MAX_WORDS = 5
# Points of the parse-quality score removed per finding status.
PENALTY = {FAIL: 35, WARN: 10, OK: 0}
# The parse component of the score: parse quality and the record round trip.
PARSE_SHARE, RECORD_SHARE = 0.6, 0.4
STANDARD_SECTIONS = ("experience", "education", "skills")

RE_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
RE_PHONE = re.compile(r"(?<![\w/.])(?:\+|00)?\d[\d\s./\-()]{6,}\d(?![\w/])")
RE_GARBLED = re.compile(r"[�-]|\(cid:\d+\)|Ã[\x80-\xbf]|â€")
RE_SPACED = re.compile(r"(?<!\w)(?:[^\W\d_] ){4,}[^\W\d_](?!\w)")
_DECORATION = re.compile(r"^[\s/\\#>•■□▪▫▸▹▶►◆◇●○◦|—–\-*~=+_:.»›]+")
_DATE_POINT_ONLY = re.compile(
    r"^\s*(?:dal|da|from|since)?\s*(?:\d{1,2}\s?[/.\-]\s?)?(?:[^\W\d_]{3,9}\.?\s*)?(?:19|20)\d{2}\s*[-–—]?\s*$",
    re.IGNORECASE)
_PRESENT_ONLY = re.compile(r"^\s*(?:[-–—]\s*)?(?:oggi|ad oggi|in corso|present|current|now|today|attuale)\s*$",
                           re.IGNORECASE)


@dataclass
class LostText:
    kind: str  # "image_page" | "header_footer" | "table" | "text_box" | "column_order"
    text: str


@dataclass
class ParsedView:
    """The text a parser with these settings would extract, and what it loses."""

    text: str
    lines: list[str]
    lost: list[LostText] = field(default_factory=list)
    merged_lines: list[str] = field(default_factory=list)  # lines mixing two columns (linear reading)
    layout: LayoutFacts | None = None

    @property
    def useful_chars(self) -> int:
        return len(re.sub(r"\W", "", self.text))


def short(text: str, n: int = EVIDENCE_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _key(line: str) -> str:
    return " ".join(line.lower().split())


def _remove(lines: list[str], unwanted: list[str]) -> tuple[list[str], list[str]]:
    """Lines without the unwanted ones (each removed once); and the removed lines."""
    pending = [_key(u) for u in unwanted if u.strip()]
    kept, removed = [], []
    for line in lines:
        k = _key(line)
        if k in pending:
            pending.remove(k)
            removed.append(line)
        else:
            kept.append(line)
    return kept, removed


def _clean(lines: list[str]) -> list[str]:
    return [" ".join(x.split()) for x in lines if x and x.strip()]


def build_parsed_view(cv: CvDocument, parser, layout: LayoutFacts) -> ParsedView:
    """Apply the parser settings to the file: reading order, header/footer, images,
    tables, text boxes. Without a readable file the app's text is used as is."""
    app_lines = _clean(cv.text.split("\n"))
    lost: list[LostText] = []
    merged: list[str] = []
    lines = list(app_lines)
    if layout.kind == "image":
        if parser.images == "lost":
            lost.extend(LostText("image_page", x) for x in app_lines)
            lines = []
    elif layout.kind == "pdf" and layout.readable:
        text_layer = _clean(layout.text_layer().split("\n"))
        if parser.images == "lost" and layout.image_pages:
            ocr_only = [x for x in app_lines if _key(x) not in {_key(t) for t in text_layer}]
            lost.extend(LostText("image_page", x) for x in ocr_only)
            lines = [x for x in app_lines if x not in ocr_only]
        if parser.reading_order == "linear" and layout.column_pages:
            linear = _clean(layout.linear_text().split("\n"))
            merged = [x for x in linear if "  " in x or _key(x) not in {_key(t) for t in text_layer}]
            if parser.images != "lost" and layout.image_pages:
                linear += [x for x in lines if _key(x) not in {_key(t) for t in text_layer}]
            lines = _clean(linear)
            lost.extend(LostText("column_order", x) for x in merged)
        elif parser.reading_order == "layout" and layout.column_pages:
            ordered = _clean(layout.layout_text().split("\n"))
            if parser.images != "lost" and layout.image_pages:
                ordered += [x for x in lines if _key(x) not in {_key(t) for t in text_layer}]
            lines = ordered
        if parser.header_footer == "drop" and layout.margin_lines:
            lines, removed = _remove(lines, [x for x in lines if _key(x) in {_key(m) for m in layout.margin_lines}])
            lost.extend(LostText("header_footer", x) for x in removed)
    elif layout.kind == "docx" and layout.readable:
        if layout.header_footer_text:
            if parser.header_footer == "keep":
                lines = _clean(layout.header_footer_text) + lines
            else:
                lost.extend(LostText("header_footer", x) for x in _clean(layout.header_footer_text))
        if parser.tables == "lost" and layout.table_text:
            lines, removed = _remove(lines, _clean(layout.table_text))
            lost.extend(LostText("table", x) for x in removed)
        if parser.text_boxes == "lost" and layout.text_boxes:
            lines, removed = _remove(lines, _clean(layout.text_boxes))
            lost.extend(LostText("text_box", x) for x in removed)
    lines = [" ".join(x.split()) for x in lines]
    return ParsedView("\n".join(lines), lines, lost, merged, layout)


# --- findings ------------------------------------------------------------------------


def _do():
    return msg("rsim.parse.do")


def _ev(texts, source: str = SOURCE_CV, lines: list[str] | None = None) -> list[EvidenceRef]:
    result: list[EvidenceRef] = []
    for t in texts:
        s = short(t)
        if not s or any(e.text == s for e in result):
            continue
        number = None
        if lines is not None:
            number = next((i + 1 for i, x in enumerate(lines) if _key(x) == _key(t)), None)
        result.append(EvidenceRef(source, s, number))
        if len(result) >= MAX_EVIDENCE:
            break
    return result


def _finding(code: str, status: str, found, evidence=None, fix=None, gain: float | None = None,
             edit_code: str | None = None, edit_message=None, details=None) -> Finding:
    edits = []
    message = edit_message or fix
    if message is not None and status in (WARN, FAIL):
        points = PENALTY[status] if gain is None else gain
        edits.append(Edit(edit_code or code, message, {COMPONENT_PARSE: points * PARSE_SHARE},
                          list(evidence or [])))
    return Finding(CHECK_PARSE, status, _do(), found, list(evidence or []), fix, code, edits, details or {})


SPACED_SHARE = 0.6  # share of one-letter tokens that makes a line letter-spaced ("E S P E R I E N Z E")


def _letter_spaced(line: str) -> bool:
    """A heading written with spaced letters. A sentence the parser cut into fragments
    ("Reali zz a z ione soft w are di ...": glyphs placed one by one in the PDF) is not
    one: there most tokens are longer than one letter."""
    if not RE_SPACED.search(line):
        return False
    tokens = [t for t in _DECORATION.sub("", line).split() if t.strip()]
    single = sum(1 for t in tokens if len(t) == 1 and t.isalpha())
    return bool(tokens) and single >= SPACED_SHARE * len(tokens)


def heading_candidates(lines: list[str]) -> list[str]:
    """Lines that look like section headings but are not standard ones."""
    result = []
    for i, line in enumerate(lines):
        if i < 3:
            continue  # name and headline
        stripped = _DECORATION.sub("", line).strip(" :")
        words = stripped.split()
        if not (1 <= len(words) <= HEADING_MAX_WORDS) or len(stripped) > 40:
            continue
        if re.search(r"[\d@/·|,;]", stripped) or line.rstrip().endswith((".", ",")):
            continue
        letters = [c for c in stripped if c.isalpha()]
        if len(letters) < 3:
            continue
        decorated = bool(_DECORATION.match(line)) and line.strip()[:1] in "/#■□▪▸►◆●>|"
        upper = all(c.isupper() for c in letters)
        colon = line.rstrip().endswith(":")
        if not (upper or decorated or colon):
            continue
        if detect_heading(line) is not None:
            continue
        if cv_skills.dictionary().lookup(stripped) is not None:
            continue
        result.append(line)
    return result


def vertical_dates(lines: list[str]) -> list[str]:
    """Date ranges split over two lines: a lone date followed by a lone date/"present"."""
    result = []
    for a, b in zip(lines, lines[1:]):
        if _DATE_POINT_ONLY.match(a) and not find_period(a) and (
                (_DATE_POINT_ONLY.match(b) and not find_period(b)) or _PRESENT_ONLY.match(b)):
            result.append(f"{a} / {b}")
    return result


def parse_findings(cv: CvDocument, view: ParsedView, profile) -> list[Finding]:
    parser = profile.parser
    layout = view.layout or LayoutFacts()
    findings: list[Finding] = []
    lines = view.lines
    pages = layout.pages or getattr(cv.read, "pages", None) or 1

    if view.useful_chars < MIN_TEXT_CHARS:
        findings.append(_finding("parse.no_text", FAIL, msg("rsim.parse.no_text", method=cv.method),
                                 [EvidenceRef(SOURCE_FILE, short(cv.file_path or cv.method))],
                                 msg("rsim.parse.no_text.fix"), gain=100))
        return findings

    image_pages = layout.image_pages
    if image_pages or cv.method in ("ocr", "mixed"):
        count = len(image_pages) or 1
        if parser.images == "lost":
            status = FAIL if layout.kind == "image" or count >= (layout.pages or 1) else WARN
            lost = [x.text for x in view.lost if x.kind == "image_page"]
            findings.append(_finding("parse.images", status, msg("rsim.parse.ocr_lost", pages=count),
                                     _ev(lost) or [EvidenceRef(SOURCE_FILE, f"pages {image_pages}")],
                                     msg("rsim.parse.ocr.fix")))
        else:
            findings.append(_finding("parse.images", WARN, msg("rsim.parse.ocr_kept", pages=count),
                                     [EvidenceRef(SOURCE_FILE, f"pages {image_pages or [1]}")],
                                     msg("rsim.parse.ocr.fix")))

    column_pages = sorted(layout.column_pages)
    if column_pages or layout.section_columns > 1:
        where = ", ".join(str(p) for p in column_pages) or "1"
        columns = max(list(layout.column_pages.values()) + [layout.section_columns])
        if parser.reading_order == "linear":
            evidence = _ev(view.merged_lines) or [EvidenceRef(SOURCE_FILE, f"page {where}")]
            findings.append(_finding("parse.columns", FAIL,
                                     msg("rsim.parse.columns", pages=where, columns=columns), evidence,
                                     msg("rsim.parse.columns.fix"), edit_message=msg("rsim.whatif.columns")))
        else:
            findings.append(_finding("parse.columns", WARN,
                                     msg("rsim.parse.columns_layout", pages=where, columns=columns),
                                     [EvidenceRef(SOURCE_FILE, f"page {where}")], msg("rsim.parse.columns.fix"),
                                     edit_message=msg("rsim.whatif.columns")))

    margin = list(layout.margin_lines) + list(layout.header_footer_text)
    if margin:
        text = short(" | ".join(margin), 100)
        contact_only = any(RE_EMAIL.search(m) or RE_PHONE.search(m) for m in margin) and not (
            RE_EMAIL.search(view.text if parser.header_footer == "drop" else cv.text))
        if parser.header_footer == "drop":
            status = FAIL if contact_only else WARN
            key = "rsim.parse.header_footer_contacts" if contact_only else "rsim.parse.header_footer_lost"
            findings.append(_finding("parse.header_footer", status, msg(key, text=text),
                                     _ev(margin, SOURCE_FILE), msg("rsim.parse.header_footer.fix"),
                                     edit_message=msg("rsim.whatif.header_footer", text=text)))
        else:
            findings.append(_finding("parse.header_footer", OK, msg("rsim.parse.header_footer_kept", text=text),
                                     _ev(margin, SOURCE_FILE)))

    if layout.tables:
        chars = len(re.sub(r"\W", "", " ".join(layout.table_text)))
        if parser.tables == "lost":
            status = FAIL if chars >= LOST_TEXT_FAIL_CHARS else WARN
            findings.append(_finding("parse.tables", status,
                                     msg("rsim.parse.tables_lost", count=layout.tables, chars=chars),
                                     _ev(layout.table_text, SOURCE_FILE), msg("rsim.parse.tables.fix")))
        else:
            findings.append(_finding("parse.tables", WARN, msg("rsim.parse.tables_flat", count=layout.tables),
                                     _ev(layout.table_text, SOURCE_FILE), msg("rsim.parse.tables.fix")))

    if layout.text_boxes and parser.text_boxes == "lost":
        chars = len(re.sub(r"\W", "", " ".join(layout.text_boxes)))
        status = FAIL if chars >= LOST_TEXT_FAIL_CHARS else WARN
        findings.append(_finding("parse.text_boxes", status,
                                 msg("rsim.parse.text_boxes_lost", count=len(layout.text_boxes),
                                     text=short(" | ".join(layout.text_boxes), 100)),
                                 _ev(layout.text_boxes, SOURCE_FILE), msg("rsim.parse.text_boxes.fix")))

    garbled = [x for x in lines if RE_GARBLED.search(x)]
    count = sum(len(RE_GARBLED.findall(x)) for x in garbled)
    if count >= GLYPHS_MIN:
        findings.append(_finding("parse.glyphs", WARN,
                                 msg("rsim.parse.glyphs", count=count, text=short(garbled[0], 80)),
                                 _ev(garbled, SOURCE_CV, lines), msg("rsim.parse.glyphs.fix")))
    if layout.fonts_without_unicode:
        findings.append(_finding("parse.fonts", WARN,
                                 msg("rsim.parse.fonts", count=len(layout.fonts_without_unicode)),
                                 [EvidenceRef(SOURCE_FILE, ", ".join(layout.fonts_without_unicode[:4]))],
                                 msg("rsim.parse.fonts.fix")))

    findings.extend(_heading_findings(view, parser))

    contact_lines = [i for i, x in enumerate(lines) if RE_EMAIL.search(x) or (
        RE_PHONE.search(x) and len(re.sub(r"\D", "", RE_PHONE.search(x).group(0))) >= 8
        and not find_period(x))]
    limit = max(CONTACT_TOP_LINES, int(CONTACT_TOP_SHARE * len(lines)))
    if contact_lines and contact_lines[0] >= limit:
        i = contact_lines[0]
        findings.append(_finding("parse.contacts_not_top", WARN,
                                 msg("rsim.parse.contacts_not_top", line=i + 1, lines=len(lines)),
                                 [EvidenceRef(SOURCE_CV, short(lines[i]), i + 1)],
                                 msg("rsim.parse.contacts_not_top.fix"),
                                 edit_message=msg("rsim.whatif.contacts_top")))

    split = vertical_dates(lines)
    if split:
        findings.append(_finding("parse.vertical_dates", WARN,
                                 msg("rsim.parse.vertical_dates", count=len(split), text=short(split[0], 60)),
                                 _ev(split), msg("rsim.parse.vertical_dates.fix")))

    if parser.max_pages and pages and pages > parser.max_pages:
        findings.append(_finding("parse.pages", WARN, msg("rsim.parse.pages", pages=pages, maximum=parser.max_pages),
                                 [EvidenceRef(SOURCE_PROFILE, f"max_pages={parser.max_pages}")],
                                 msg("rsim.parse.pages.fix", maximum=parser.max_pages)))

    if not any(f.status in (WARN, FAIL) for f in findings):
        findings.insert(0, _finding("parse.ok", OK, msg("rsim.parse.ok", lines=len(lines), pages=pages)))
    elif view.lost:
        kinds = ", ".join(dict.fromkeys(x.kind for x in view.lost))
        findings.insert(0, _finding("parse.lost", WARN, msg("rsim.parse.lost_summary", count=len(view.lost),
                                                             kinds=kinds), _ev([x.text for x in view.lost]),
                                    gain=0))
    return findings


def _heading_findings(view: ParsedView, parser) -> list[Finding]:
    findings: list[Finding] = []
    lines = view.lines
    sections = split_sections(view.text)
    standard = [k for k in STANDARD_SECTIONS if has_section(sections, k)]
    # A line in capitals inside a list section ("CCNA CISCO" under Certifications) is an
    # item of that list, not a heading.
    listed = {" ".join(r.split()) for key in ("certifications", "skills", "languages", "education")
              for r in rows_of(sections, key)}
    odd = [x for x in heading_candidates(lines) if " ".join(x.split()) not in listed]
    spaced = [x for x in lines if _letter_spaced(x)]
    if not standard:
        findings.append(_finding("parse.headings_missing", FAIL, msg("rsim.parse.headings_missing"),
                                 _ev(odd[:MAX_EVIDENCE], SOURCE_CV, lines), msg("rsim.parse.headings.fix")))
    elif odd:
        strict = parser.headings == "strict"
        key = "rsim.parse.headings_strict" if strict else "rsim.parse.headings_fuzzy"
        findings.append(_finding("parse.headings", WARN if strict else OK,
                                 msg(key, count=len(odd), text=short(", ".join(odd), 100)),
                                 _ev(odd, SOURCE_CV, lines), msg("rsim.parse.headings.fix"),
                                 edit_message=msg("rsim.whatif.headings", heading=short(odd[0], 40))))
    if spaced:
        findings.append(_finding("parse.headings_spaced", WARN,
                                 msg("rsim.parse.headings_spaced", count=len(spaced), text=short(spaced[0], 60)),
                                 _ev(spaced, SOURCE_CV, lines), msg("rsim.parse.headings_spaced.fix")))
    return findings


def parse_quality(findings: list[Finding]) -> float:
    """0..100: 100 minus the penalties of the parse findings."""
    if any(f.code == "parse.no_text" for f in findings):
        return 0.0
    penalty = sum(PENALTY.get(f.status, 0) for f in findings if f.code != "parse.lost")
    return float(max(0, 100 - penalty))
