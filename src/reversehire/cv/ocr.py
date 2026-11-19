"""Local OCR for CVs supplied as images or scanned PDFs.

The engine is RapidOCR (PP-OCR models on onnxruntime, works offline). It is
imported lazily: without it the rest of the app works and only image/scanned
CVs fail with a clear message.

Two engines share one design (detect once, recognise every line with the Latin
model, then a 12-line probe with the Cyrillic model decides whether the page is
Cyrillic; if it is, every line is read again with the Cyrillic model and words
mixing look-alike letters are put back in one script):

- normal: the PP-OCRv6 small models of the rapidocr wheel plus the bundled 8 MB
  PP-OCRv5 Cyrillic recogniser (the Latin models cannot read Ukrainian at all);
- advanced: PP-OCRv6 medium detector and Latin recogniser plus the Cyrillic one,
  downloaded on request (see `ocr_models.py`), used when the normal reading looks
  poor (`ocr_quality.py`); on DirectML when available, else on the CPU.

Not included, on purpose: Indra OCR (its model weights are under a modified
OpenRAIL-M licence, not compatible with an MIT app) and VLM-based OCR (minutes
per page on the CPU and no working GPU path on the target hardware).

Reading order: OCR returns text boxes; `assemble_text` rebuilds the rows
top-to-bottom, left-to-right, and reads a two-column layout (sidebar CV)
column by column when it finds a clear vertical gutter.
"""

from __future__ import annotations

import logging
import re
import statistics
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

OCR_PACKAGE = "rapidocr"
IMAGE_FORMATS = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp")
# Limits: pages OCR-ed per document, render resolution, largest page side.
MAX_OCR_PAGES = 10
RENDER_DPI = 200
MAX_SIDE_PX = 3500
# Images smaller than this (long side) are upscaled: text would be too small.
MIN_SIDE_PX = 1200


class OcrUnavailable(Exception):
    """The OCR package is not installed or cannot start."""


@dataclass
class OcrBox:
    """One text box found by the OCR (axis-aligned, in image pixels)."""

    text: str
    score: float
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def height(self) -> float:
        return max(1.0, self.y1 - self.y0)

    @property
    def y_center(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class OcrPage:
    """Text of one image, with the mean confidence (None if no text).

    script: "latin", "cyrillic" (read with the Cyrillic model) or "cyrillic_unread"
    (Cyrillic found but not read with the Cyrillic model); "" when unknown."""

    text: str
    confidence: float | None
    boxes: int
    script: str = ""


class OcrBoxes(list):
    """The boxes of one page, with the script the engine found (`script`)."""

    script: str = ""


# An engine takes a PIL image and returns the boxes found (a list, or OcrBoxes).
OcrEngine = Callable[[object], list[OcrBox]]

_engine: OcrEngine | None = None
_engine_lock = threading.Lock()
_advanced: OcrEngine | None = None
_advanced_key: str | None = None
_advanced_lock = threading.Lock()

# Cyrillic routing (values from the study on invented CVs): lines probed, share of
# Cyrillic letters that makes a page Cyrillic, lowest line score kept.
PROBE_LINES = 12
CYRILLIC_PAGE_SHARE = 0.25
TEXT_SCORE = 0.5
CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_LETTER = re.compile(r"[^\W\d_]")
_LATIN = re.compile(r"[A-Za-z]")
# Look-alike letters: Cyrillic -> Latin and Latin -> Cyrillic.
_C2L = str.maketrans("АВЕКМНОРСТХаеорсухіІјЈтп", "ABEKMHOPCTXaeopcyxiIjJmn")
_L2C = str.maketrans("ABEKMHOPCTXaeopcyxiI", "АВЕКМНОРСТХаеорсухіІ")


def missing_engine_message() -> str:
    return (f"OCR engine not available: install the '{OCR_PACKAGE}' package "
            f"(pip install {OCR_PACKAGE}) to read image CVs and scanned PDFs.")


def get_engine() -> OcrEngine:
    """The shared normal engine (created on first use); OcrUnavailable if missing."""
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = _rapidocr_engine()
        return _engine


def get_advanced_engine() -> OcrEngine:
    """The shared advanced engine; OcrUnavailable when its models are not installed
    (or damaged) or the engine cannot start."""
    global _advanced, _advanced_key
    from . import ocr_models

    paths = ocr_models.advanced_paths()
    if paths is None:
        raise OcrUnavailable("The advanced OCR models are not installed.")
    with _advanced_lock:
        key = str(paths.det)
        if _advanced is None or _advanced_key != key:
            _advanced = _routed_engine(det=paths.det, rec=paths.rec_latin, keys=paths.keys_latin,
                                       cyr=paths.rec_cyrillic, cyr_keys=paths.keys_cyrillic,
                                       provider=ocr_models.best_provider(), name="advanced")
            _advanced_key = key
        return _advanced


def reset_engines() -> None:
    """Forget the loaded engines (tests, models installed or removed)."""
    global _engine, _advanced, _advanced_key
    with _engine_lock:
        _engine = None
    with _advanced_lock:
        _advanced, _advanced_key = None, None


def engine_provider(engine) -> str:
    """"dml" or "cpu" for an engine built here; "cpu" for any other callable."""
    provider = getattr(engine, "provider", None)
    try:
        return provider() if callable(provider) else "cpu"
    except Exception:  # noqa: BLE001
        return "cpu"


def cyrillic_share(text: str) -> float:
    """Share of the letters of `text` that are Cyrillic (0 without letters)."""
    letters = _LETTER.findall(text)
    return len(CYRILLIC.findall(text)) / len(letters) if letters else 0.0


def fix_homoglyphs(text: str) -> str:
    """A word mixing scripts ("Siеmеns" with a Cyrillic е, "Тeл" with a Latin e) is put
    in its majority script; words in one script are left alone."""
    def word(m: re.Match) -> str:
        w = m.group(0)
        latin, cyrillic = len(_LATIN.findall(w)), len(CYRILLIC.findall(w))
        if not (latin and cyrillic):
            return w
        return w.translate(_C2L) if latin >= cyrillic else w.translate(_L2C)

    return re.sub(r"[^\W\d_]+", word, text)


def _rapid_params(det: Path | None, rec: Path | None, keys: Path | None, provider: str) -> dict:
    # The angle classifier is off: it flipped correct horizontal rows by 180 degrees
    # in tests, and CV pages are upright.
    params = {"Global.log_level": "error", "Global.use_cls": False}
    if det is not None:
        params["Det.model_path"] = str(det)
    if rec is not None:
        params["Rec.model_path"] = str(rec)
    if keys is not None:
        params["Rec.rec_keys_path"] = str(keys)
    if provider == "dml":
        params["EngineConfig.onnxruntime.use_dml"] = True
    return params


def _rapidocr_engine() -> OcrEngine:
    """The normal engine: wheel models, plus the bundled Cyrillic recogniser when present."""
    from . import ocr_models

    if ocr_models.bundled_cyrillic_ok():
        return _routed_engine(cyr=ocr_models.CYRILLIC_MODEL, cyr_keys=ocr_models.CYRILLIC_KEYS, name="normal")
    log.warning("The bundled Cyrillic OCR model is missing: Cyrillic scans cannot be read")
    return _routed_engine(name="normal")


def _routed_engine(*, det: Path | None = None, rec: Path | None = None, keys: Path | None = None,
                   cyr: Path | None = None, cyr_keys: Path | None = None, provider: str = "cpu",
                   name: str = "normal") -> OcrEngine:
    """Detect once, read with the Latin model, re-read Cyrillic pages with the Cyrillic one.

    On a Latin page the output is exactly RapidOCR's own (same detection, recognition
    and score filter). Explicit model paths are always given for every non-wheel
    model: RapidOCR would otherwise download missing models from modelscope.cn."""
    try:
        from rapidocr import RapidOCR
        from rapidocr.utils.process_img import map_boxes_to_original
    except Exception as e:  # noqa: BLE001 - ImportError or a broken native dependency
        raise OcrUnavailable(missing_engine_message()) from e
    try:
        from rapidocr.main import RapidOCRError
    except Exception:  # noqa: BLE001 - name moved in another version
        RapidOCRError = Exception  # noqa: N806
    state = {"provider": provider}

    def build() -> list:
        latin = RapidOCR(params=_rapid_params(det, rec, keys, state["provider"]))
        cyrillic = (RapidOCR(params=_rapid_params(det, cyr, cyr_keys, state["provider"]))
                    if cyr is not None else None)
        return [latin, cyrillic]

    try:
        engines = build()
    except Exception as e:  # noqa: BLE001 - model files missing or damaged
        raise OcrUnavailable(f"OCR engine cannot start: {e}") from e
    lock = threading.Lock()

    def read(image) -> OcrBoxes:
        latin, cyrillic = engines
        original = latin.load_img(image)
        img, record = latin.preprocess_img(original)
        try:
            crops, detected = latin.detect_and_crop(img, record)
        except RapidOCRError:
            return OcrBoxes()  # no text found
        boxes = map_boxes_to_original(detected.boxes, record, original.shape[0], original.shape[1])
        result = latin.recognize_txt(crops)
        texts, scores = list(result.txts), [float(s) for s in result.scores]
        script = "latin"
        if cyrillic is not None and crops:
            widest = sorted(range(len(crops)), key=lambda i: -crops[i].shape[1])[:PROBE_LINES]
            probe = cyrillic.recognize_txt([crops[i] for i in widest])
            if cyrillic_share(" ".join(probe.txts)) >= CYRILLIC_PAGE_SHARE:
                script = "cyrillic"
                again = cyrillic.recognize_txt(crops)
                for i, (text, score) in enumerate(zip(again.txts, again.scores)):
                    if CYRILLIC.search(text):
                        texts[i], scores[i] = fix_homoglyphs(text), float(score)
        found = OcrBoxes()
        found.script = script
        for points, text, score in zip(boxes, texts, scores):
            if score < TEXT_SCORE or not text or not text.strip():
                continue
            xs = [float(p[0]) for p in points]
            ys = [float(p[1]) for p in points]
            found.append(OcrBox(text.strip(), score, min(xs), min(ys), max(xs), max(ys)))
        return found

    def run(image) -> list[OcrBox]:
        with lock:
            try:
                return read(image)
            except Exception:
                if state["provider"] == "cpu":
                    raise
                # DirectML failed on a real page: one log line, then the CPU for good.
                log.warning("The %s OCR engine failed on DirectML: switching to the CPU", name,
                            exc_info=True)
                state["provider"] = "cpu"
                engines[:] = build()
                return read(image)

    run.provider = lambda: state["provider"]  # type: ignore[attr-defined]
    run.has_cyrillic = cyr is not None  # type: ignore[attr-defined]
    run.engine_name = name  # type: ignore[attr-defined]
    return run


def ocr_image(image, engine: OcrEngine | None = None) -> OcrPage:
    """OCR of one PIL image; text assembled in reading order."""
    if is_blank(image):
        return OcrPage("", None, 0)
    boxes = (engine or get_engine())(image)
    script = getattr(boxes, "script", "")
    if not boxes:
        return OcrPage("", None, 0, script)
    return OcrPage(assemble_text(boxes), mean_confidence(boxes), len(boxes), script)


def mean_confidence(boxes: list[OcrBox]) -> float | None:
    """Mean score weighted by text length."""
    total = sum(len(b.text) for b in boxes)
    if not total:
        return None
    return sum(b.score * len(b.text) for b in boxes) / total


# --- reading order ------------------------------------------------------------

def assemble_text(boxes: list[OcrBox]) -> str:
    """Rows of text in reading order; a two-column layout is read column by column."""
    if not boxes:
        return ""
    gutter = find_gutter(boxes)
    if gutter is None:
        return "\n".join(_lines_text(boxes))
    rows: list[str] = []
    # Boxes crossing the gutter (e.g. a full-width title) split the page into
    # bands; inside each band the left column comes before the right one.
    band: list[OcrBox] = []
    for box in sorted(boxes, key=lambda b: (b.y0, b.x0)):
        if box.x0 < gutter < box.x1:
            rows += _band_text(band, gutter)
            band = []
            rows += _lines_text([box])
        else:
            band.append(box)
    rows += _band_text(band, gutter)
    return "\n".join(rows)


def _band_text(band: list[OcrBox], gutter: float) -> list[str]:
    left = [b for b in band if b.x1 <= gutter]
    right = [b for b in band if b.x0 >= gutter]
    return _lines_text(left) + _lines_text(right)


def find_gutter(boxes: list[OcrBox]) -> float | None:
    """x of a clear vertical gap between two text columns, or None.

    Conditions: an empty (or almost empty) vertical band in the middle of the
    text area, wide at least ~1.5 line heights; both sides with enough boxes;
    and the two sides must NOT be row-aligned (a "date | description" table is
    better read row by row) unless each side has its own section headings: a
    sidebar CV written with the same line pitch in both columns lines up row by
    row too, but its columns are independent texts ("CONTACTS", "SKILLS" on the
    left, "EXPERIENCE", "EDUCATION" on the right), while in a table or a Europass
    label | value layout the headings are only on one side.
    """
    if len(boxes) < 6:
        return None
    left_edge = min(b.x0 for b in boxes)
    right_edge = max(b.x1 for b in boxes)
    width = right_edge - left_edge
    if width <= 0:
        return None
    line_height = statistics.median(b.height for b in boxes)
    allowed_crossing = max(2, len(boxes) // 10)
    # Coverage of each x slot (slots of a fraction of the line height).
    step = max(1.0, line_height / 4)
    slots = int(width / step) + 1
    coverage = [0] * slots
    for b in boxes:
        for i in range(int((b.x0 - left_edge) / step), min(slots, int((b.x1 - left_edge) / step) + 1)):
            coverage[i] += 1
    # Longest run of (nearly) empty slots between 15% and 85% of the width.
    lo, hi = int(slots * 0.15), int(slots * 0.85)
    best: tuple[int, int] | None = None
    start = None
    for i in range(lo, hi + 1):
        empty = i < hi and coverage[i] <= allowed_crossing
        if empty and start is None:
            start = i
        elif not empty and start is not None:
            if best is None or i - start > best[1] - best[0]:
                best = (start, i)
            start = None
    if best is None or (best[1] - best[0]) * step < 1.5 * line_height:
        return None
    gutter = left_edge + (best[0] + best[1]) / 2 * step
    left = [b for b in boxes if b.x1 <= gutter]
    right = [b for b in boxes if b.x0 >= gutter]
    minimum = max(3, len(boxes) // 7)
    if len(left) < minimum or len(right) < minimum:
        return None
    if _aligned_ratio(left, right, line_height) >= 0.6 and not _independent_columns(left, right):
        return None
    return gutter


def _heading_count(boxes: list[OcrBox]) -> int:
    """Boxes whose whole text is a CV section heading ("SKILLS", "Esperienza lavorativa")."""
    from .sections import detect_heading

    count = 0
    for box in boxes:
        if len(box.text) > 60:
            continue
        match = detect_heading(box.text)
        if match is not None and not match[1]:  # a heading alone, no content after it
            count += 1
    return count


def _independent_columns(left: list[OcrBox], right: list[OcrBox]) -> bool:
    """Both sides carry section headings, at least three in all: two separate texts."""
    left_headings, right_headings = _heading_count(left), _heading_count(right)
    return left_headings >= 1 and right_headings >= 1 and left_headings + right_headings >= 3


def _aligned_ratio(left: list[OcrBox], right: list[OcrBox], line_height: float) -> float:
    """Share of boxes of the smaller side that have a box on the same row on the other side."""
    small, big = (left, right) if len(left) <= len(right) else (right, left)
    tolerance = line_height * 0.3
    aligned = sum(1 for a in small if any(abs(a.y_center - b.y_center) <= tolerance for b in big))
    return aligned / len(small)


def _lines_text(boxes: list[OcrBox]) -> list[str]:
    """Group boxes into rows (same vertical band), each row left to right.

    Two boxes on the same row far apart (more than 2 line heights) stay on
    separate rows, like the cells of a table.
    """
    rows: list[list[OcrBox]] = []
    for box in sorted(boxes, key=lambda b: b.y_center):
        if rows:
            row = rows[-1]
            center = sum(b.y_center for b in row) / len(row)
            height = statistics.median(b.height for b in row)
            if abs(box.y_center - center) <= 0.5 * min(height, box.height):
                row.append(box)
                continue
        rows.append([box])
    text: list[str] = []
    for row in rows:
        row.sort(key=lambda b: b.x0)
        parts = [row[0].text]
        for prev, box in zip(row, row[1:]):
            gap = box.x0 - prev.x1
            if gap > 2 * max(prev.height, box.height):
                text.append(" ".join(parts))
                parts = [box.text]
            else:
                parts.append(box.text)
        text.append(" ".join(parts))
    return text


# --- images -------------------------------------------------------------------

def load_images(path: Path, max_pages: int = MAX_OCR_PAGES) -> tuple[list, int]:
    """RGB PIL images of an image file (TIFF may have several frames) and the frame count.

    Raises ValueError/OSError if the file is not a readable image.
    """
    from PIL import Image, ImageOps

    images = []
    with Image.open(path) as img:
        frames = getattr(img, "n_frames", 1) or 1
        for index in range(min(frames, max_pages)):
            img.seek(index)
            frame = ImageOps.exif_transpose(img.copy()) if index == 0 else img.copy()
            images.append(prepare_image(frame))
    return images, frames


def prepare_image(img):
    """RGB on white, long side between MIN_SIDE_PX and MAX_SIDE_PX."""
    from PIL import Image

    if img.mode in ("RGBA", "LA", "P", "PA") or "transparency" in img.info:
        img = img.convert("RGBA")
        background = Image.new("RGBA", img.size, "white")
        background.alpha_composite(img)
        img = background
    img = img.convert("RGB")
    long_side = max(img.size)
    if long_side and long_side < MIN_SIDE_PX:
        factor = min(3.0, MIN_SIDE_PX / long_side)
        img = img.resize((round(img.width * factor), round(img.height * factor)), Image.Resampling.LANCZOS)
    elif long_side > MAX_SIDE_PX:
        img.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX), Image.Resampling.LANCZOS)
    return img


def is_blank(img) -> bool:
    """True for a (nearly) uniform image: nothing to read, skip the OCR."""
    try:
        low, high = img.convert("L").getextrema()
    except Exception:  # noqa: BLE001 - not a PIL image (e.g. a test double)
        return False
    return high - low < 16


# --- PDF pages ----------------------------------------------------------------

def render_pdf_pages(path: Path, page_indexes: list[int], dpi: int = RENDER_DPI) -> dict[int, object]:
    """PIL images of the given PDF pages (0-based); pages that cannot be rendered are missing.

    Uses Qt's PDF module (PDFium); falls back to the images embedded in the
    page (via pypdf) when QtPdf is not available.
    """
    try:
        return _render_qtpdf(path, page_indexes, dpi)
    except ImportError:
        log.info("QtPdf not available, using the embedded page images")
        return _embedded_images(path, page_indexes)


def _render_qtpdf(path: Path, page_indexes: list[int], dpi: int) -> dict[int, object]:
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QImage
    from PySide6.QtPdf import QPdfDocument

    document = QPdfDocument(None)
    try:
        document.load(str(path))
        if document.status() != QPdfDocument.Status.Ready:
            return _embedded_images(path, page_indexes)
        images = {}
        for index in page_indexes:
            if index >= document.pageCount():
                continue
            size_pt = document.pagePointSize(index)
            scale = dpi / 72
            w, h = size_pt.width() * scale, size_pt.height() * scale
            if max(w, h) > MAX_SIDE_PX:
                ratio = MAX_SIDE_PX / max(w, h)
                w, h = w * ratio, h * ratio
            qimage = document.render(index, QSize(max(1, round(w)), max(1, round(h))))
            if qimage.isNull():
                continue
            images[index] = prepare_image(_qimage_to_pil(qimage.convertToFormat(QImage.Format.Format_RGBA8888)))
        return images
    finally:
        document.close()


def _qimage_to_pil(qimage):
    from PIL import Image

    data = bytes(qimage.constBits())
    return Image.frombuffer("RGBA", (qimage.width(), qimage.height()), data, "raw", "RGBA",
                            qimage.bytesPerLine(), 1)


def _embedded_images(path: Path, page_indexes: list[int]) -> dict[int, object]:
    """Largest image embedded in each page (typical of scanners)."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    images = {}
    for index in page_indexes:
        if index >= len(reader.pages):
            continue
        try:
            candidates = [i.image for i in reader.pages[index].images if i.image is not None]
        except Exception:  # noqa: BLE001 - unsupported image filter
            log.warning("Page %d: embedded images cannot be decoded", index + 1)
            continue
        if candidates:
            largest = max(candidates, key=lambda im: im.width * im.height)
            images[index] = prepare_image(largest)
    return images
