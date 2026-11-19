"""Layout facts of a CV file that a parser's behaviour depends on.

PDF: text chunks with their position (pypdf's text visitor), pages without a
text layer, multi-column pages (a vertical gutter free of text over a large
span), lines repeated in the top/bottom margins of several pages, fonts without
a Unicode map. DOCX: tables, text boxes, section columns, header/footer text.

The column test (sweep a vertical gutter across the page; two columns when the
gutter is crossed by no text over a large vertical span and the text on its
right is not just a narrow right-aligned band of dates) and the repeated
margin-line test follow the approach of Resume-Matcher
`apps/backend/app/services/ats_parse/checks.py` (`has_columns`,
`_is_right_aligned_band`, `_repeated_margin_lines`), Copyright Resume-Matcher
contributors, Apache License 2.0; re-implemented for pypdf text chunks
(positions are estimated from the font size) and modified for ReverseHire.

Best effort and read-only: a file that cannot be read gives
`LayoutFacts(readable=False)`; nothing here raises on a bad file.
"""

from __future__ import annotations

import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

MIN_PAGE_CHARS = 20  # fewer useful characters: the page is an image (as cv/reading.py)
CHAR_WIDTH = 0.5  # average glyph width as a share of the font size (estimate)
COLUMN_MIN_BOXES = 3
COLUMN_MIN_COVERAGE = 0.05  # share of the page height both columns must cover
RIGHT_BAND_MAX_WIDTH = 0.3
RIGHT_BAND_MIN_FLUSH = 0.8
RIGHT_BAND_EDGE_TOLERANCE = 4.0
MARGIN_SHARE = 0.08  # top/bottom band of the page treated as header/footer
LINE_TOLERANCE = 0.5  # boxes on the same line: |dy| below this share of the font size
IMAGE_EXTENSIONS = ("png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp", "gif")


@dataclass
class Box:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    size: float = 10.0


@dataclass
class PageLayout:
    number: int  # 1-based
    width: float
    height: float
    boxes: list[Box] = field(default_factory=list)
    text: str = ""  # pypdf text in content-stream order

    @property
    def useful_chars(self) -> int:
        return len(re.sub(r"\W", "", self.text))


@dataclass
class LayoutFacts:
    kind: str = ""  # "pdf" | "docx" | "image" | other extension | ""
    extension: str = ""
    pages: int | None = None
    readable: bool = True
    pdf_pages: list[PageLayout] = field(default_factory=list)
    image_pages: list[int] = field(default_factory=list)  # 1-based pages without a text layer
    column_pages: dict[int, int] = field(default_factory=dict)  # page -> columns found
    gutters: dict[int, float] = field(default_factory=dict)  # page -> x of the gutter
    margin_lines: list[str] = field(default_factory=list)  # repeated top/bottom lines (PDF)
    header_footer_text: list[str] = field(default_factory=list)  # DOCX header/footer paragraphs
    tables: int = 0
    table_text: list[str] = field(default_factory=list)
    text_boxes: list[str] = field(default_factory=list)
    section_columns: int = 1  # DOCX w:cols
    fonts_without_unicode: list[str] = field(default_factory=list)
    images: int = 0

    def text_layer(self) -> str:
        """Text the file's own text layer gives (no OCR), pages joined."""
        return "\n".join(p.text for p in self.pdf_pages if p.number not in self.image_pages)

    def layout_text(self) -> str:
        """Text read column by column (left column, then right) on multi-column pages,
        as a layout-aware parser does; other pages as the text layer gives them."""
        rows: list[str] = []
        for page in self.pdf_pages:
            if page.number in self.image_pages:
                continue
            gutter = self.gutters.get(page.number)
            if gutter is None:
                rows.extend(page.text.split("\n"))
                continue
            # By start x: widths are estimated, the left edge of a chunk is exact.
            for side in ([b for b in page.boxes if b.x0 < gutter], [b for b in page.boxes if b.x0 >= gutter]):
                rows.extend(_linear_rows(PageLayout(page.number, page.width, page.height, side)))
        return "\n".join(rows)

    def linear_text(self) -> str:
        """Text read top-to-bottom across the full page width, as a parser without
        layout analysis does: lines of side-by-side columns are interleaved."""
        rows: list[str] = []
        for page in self.pdf_pages:
            if page.number in self.image_pages:
                continue
            rows.extend(_linear_rows(page))
        return "\n".join(rows)


def _linear_rows(page: PageLayout) -> list[str]:
    boxes = sorted(page.boxes, key=lambda b: (-b.y0, b.x0))
    rows: list[list[Box]] = []
    for b in boxes:
        if rows and abs(rows[-1][0].y0 - b.y0) <= LINE_TOLERANCE * max(b.size, 1.0):
            rows[-1].append(b)
        else:
            rows.append([b])
    return ["  ".join(x.text for x in sorted(r, key=lambda b: b.x0)).strip() for r in rows]


# --- PDF -------------------------------------------------------------------------------


def _pdf_page(page, number: int) -> PageLayout:
    box = page.mediabox
    width, height = float(box.width), float(box.height)
    left, bottom = float(box.left), float(box.bottom)
    boxes: list[Box] = []

    def visitor(text, cm, tm, font_dict, font_size):
        if not text or not text.strip():
            return
        try:
            x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4] - left
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5] - bottom
            scale = abs(tm[3] * cm[3]) or abs(tm[0] * cm[0]) or 1.0
            size = abs(float(font_size or 0) * scale) or 10.0
        except (TypeError, IndexError, ValueError):
            return
        for i, line in enumerate(t for t in text.split("\n") if t.strip()):
            line = line.strip()
            w = len(line) * size * CHAR_WIDTH
            ly = y - i * size * 1.2
            boxes.append(Box(x, ly, x + w, ly + size, line, size))

    try:
        text = page.extract_text(visitor_text=visitor) or ""
    except Exception:  # noqa: BLE001 - a bad page gives no boxes
        text = ""
    return PageLayout(number, width, height, boxes, text)


def _union_length(intervals: list[tuple[float, float]]) -> float:
    total, end = 0.0, float("-inf")
    for lo, hi in sorted(intervals):
        if hi > end:
            total += hi - max(lo, end)
            end = hi
    return total


def _free_bands(blocked: list[tuple[float, float]], height: float) -> list[tuple[float, float]]:
    bands: list[tuple[float, float]] = []
    cursor = 0.0
    for lo, hi in sorted(blocked):
        if lo > cursor:
            bands.append((cursor, lo))
        cursor = max(cursor, hi)
    if cursor < height:
        bands.append((cursor, height))
    return bands


def _right_aligned_band(boxes: list[Box], width: float) -> bool:
    edge = max(b.x1 for b in boxes)
    flush = sum(1 for b in boxes if edge - b.x1 <= RIGHT_BAND_EDGE_TOLERANCE)
    widest = max(b.x1 - b.x0 for b in boxes)
    return flush >= RIGHT_BAND_MIN_FLUSH * len(boxes) and widest < RIGHT_BAND_MAX_WIDTH * width


def has_columns(page: PageLayout) -> bool:
    """True when a vertical gutter separates two text columns over a large span."""
    return column_gutter(page) is not None


def column_gutter(page: PageLayout) -> float | None:
    """x of a vertical gutter separating two text columns over a large span, or None."""
    if page.height <= 0 or len(page.boxes) < 2 * COLUMN_MIN_BOXES:
        return None
    for step in range(25, 76, 2):
        gutter = page.width * step / 100
        free = _free_bands([(b.y0, b.y1) for b in page.boxes if b.x0 < gutter < b.x1], page.height)
        for lo, hi in free:
            if hi - lo <= 0:
                continue
            left = [(max(b.y0, lo), min(b.y1, hi)) for b in page.boxes
                    if b.x1 <= gutter and b.y1 > lo and b.y0 < hi]
            right_boxes = [b for b in page.boxes if b.x0 >= gutter and b.y1 > lo and b.y0 < hi]
            if min(len(left), len(right_boxes)) < COLUMN_MIN_BOXES:
                continue
            if _right_aligned_band(right_boxes, page.width):
                continue
            right = [(max(b.y0, lo), min(b.y1, hi)) for b in right_boxes]
            if min(_union_length(left), _union_length(right)) / page.height >= COLUMN_MIN_COVERAGE:
                return gutter
    return None


def _margin_key(text: str) -> str:
    return re.sub(r"\d+", "#", " ".join(text.lower().split()))


def repeated_margin_lines(pages: list[PageLayout]) -> list[str]:
    """Lines in the top/bottom margin band repeated on two or more pages."""
    if len(pages) < 2:
        return []
    counts: Counter[str] = Counter()
    first: dict[str, str] = {}
    for page in pages:
        seen = set()
        for b in page.boxes:
            if b.y0 > page.height * (1 - MARGIN_SHARE) or b.y1 < page.height * MARGIN_SHARE:
                key = _margin_key(b.text)
                if key and key not in seen:
                    seen.add(key)
                    first.setdefault(key, b.text)
        counts.update(seen)
    return [first[k] for k, n in counts.items() if n >= 2]


def _fonts_without_unicode(page) -> list[str]:
    names: list[str] = []
    try:
        resources = page.get("/Resources")
        fonts = resources.get_object().get("/Font") if resources is not None else None
        if fonts is None:
            return names
        for font in fonts.get_object().values():
            f = font.get_object()
            if f.get("/Subtype") in ("/Type0", "/Type3") and "/ToUnicode" not in f:
                names.append(str(f.get("/BaseFont", f.get("/Subtype"))).lstrip("/"))
    except Exception:  # noqa: BLE001 - font inspection is best effort
        return names
    return names


def _pdf_layout(path: Path) -> LayoutFacts:
    from pypdf import PdfReader

    facts = LayoutFacts(kind="pdf", extension="pdf")
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            reader.decrypt("")
        pages = list(reader.pages)
    except Exception:  # noqa: BLE001 - unreadable PDF
        facts.readable = False
        return facts
    facts.pages = len(pages)
    fonts: list[str] = []
    for number, page in enumerate(pages, start=1):
        layout = _pdf_page(page, number)
        facts.pdf_pages.append(layout)
        if layout.useful_chars < MIN_PAGE_CHARS:
            facts.image_pages.append(number)
        else:
            gutter = column_gutter(layout)
            if gutter is not None:
                facts.column_pages[number] = 2
                facts.gutters[number] = gutter
        try:
            xobjects = page["/Resources"].get_object().get("/XObject")
            if xobjects:
                facts.images += sum(1 for x in xobjects.get_object().values()
                                    if x.get_object().get("/Subtype") == "/Image")
        except Exception:  # noqa: BLE001
            pass
        for name in _fonts_without_unicode(page):
            if name not in fonts:
                fonts.append(name)
    facts.fonts_without_unicode = fonts
    facts.margin_lines = repeated_margin_lines(
        [p for p in facts.pdf_pages if p.number not in facts.image_pages])
    return facts


# --- DOCX ------------------------------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _paragraphs(element) -> list[str]:
    rows = []
    for p in element.iter(f"{_W}p"):
        text = "".join(t.text or "" for t in p.iter(f"{_W}t")).strip()
        if text:
            rows.append(text)
    return rows


def _docx_layout(path: Path) -> LayoutFacts:
    from lxml import etree

    facts = LayoutFacts(kind="docx", extension="docx", pages=None)
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            document = archive.read("word/document.xml")
            margins = [archive.read(n) for n in sorted(names)
                       if re.fullmatch(r"word/(?:header|footer)\d*\.xml", n)]
            app = archive.read("docProps/app.xml") if "docProps/app.xml" in names else b""
        root = etree.fromstring(document)
    except (zipfile.BadZipFile, KeyError, OSError, etree.XMLSyntaxError):
        facts.readable = False
        return facts
    m = re.search(rb"<Pages>(\d+)</Pages>", app)
    if m and int(m.group(1)) > 0:
        facts.pages = int(m.group(1))
    body = root.find(f"{_W}body")
    if body is None:
        return facts
    top_tables = [t for t in body.iter(f"{_W}tbl") if not any(
        a.tag == f"{_W}tbl" for a in t.iterancestors())]
    facts.tables = len(top_tables)
    for table in top_tables:
        facts.table_text.extend(_paragraphs(table))
    for box in body.iter(f"{_W}txbxContent"):
        facts.text_boxes.extend(_paragraphs(box))
    facts.images = sum(1 for _ in body.iter(f"{_W}drawing"))
    columns = [int(c.get(f"{_W}num") or 1) for c in root.iter(f"{_W}cols")]
    facts.section_columns = max(columns, default=1)
    for part in margins:
        try:
            facts.header_footer_text.extend(_paragraphs(etree.fromstring(part)))
        except etree.XMLSyntaxError:
            continue
    return facts


def analyse_layout(path: str | Path | None) -> LayoutFacts:
    """Layout facts of a CV file (PDF, DOCX, image); never raises on a bad file."""
    if not path:
        return LayoutFacts(readable=False)
    p = Path(path)
    extension = p.suffix.lower().lstrip(".")
    if not p.is_file():
        return LayoutFacts(extension=extension, readable=False)
    if extension == "pdf":
        return _pdf_layout(p)
    if extension == "docx":
        return _docx_layout(p)
    if extension in IMAGE_EXTENSIONS:
        return LayoutFacts(kind="image", extension=extension, pages=1, image_pages=[1])
    return LayoutFacts(kind=extension, extension=extension)
