"""Reading the text of a CV (PDF, DOCX, images) and language detection.

Text PDFs and DOCX are read directly; images and PDF pages without a text
layer go through the local OCR (`ocr.py`, whose engine is imported only when
needed).

Advanced reader (`advanced` argument of `read_document`): after the normal OCR
pass, "auto" checks the trigger rule of `ocr_quality.py` and, when it fires and
the advanced models are installed, reads the same OCR pages again with the
advanced engine and keeps the better of the two readings (`ocr_quality.measure`);
"force" always reads them again (the better reading is still kept, and a note
says so); "off" (the default) never does. The advanced pass has its own deadline
and stops at `cancel`: the normal reading is then kept. It never raises because
of the advanced engine.
"""

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .ocr import IMAGE_FORMATS, MAX_OCR_PAGES

log = logging.getLogger(__name__)

SUPPORTED_FORMATS = (".pdf", ".docx") + IMAGE_FORMATS
# Below this threshold of "useful" characters a document has no usable text.
_MIN_CHARS = 30
# A PDF page with fewer useful characters than this is treated as scanned (OCR).
_MIN_PAGE_CHARS = 20
# Longest time the advanced pass may take on one document (about 6 s/page on a CPU).
ADVANCED_MAX_S = 240.0
ADVANCED_MODES = ("off", "auto", "force")

# Called with (pages done, pages to OCR) while the OCR runs.
Progress = Callable[[int, int], None]

# ReadResult.advanced: what happened with the advanced reader.
ADVANCED_NOT_NEEDED = "not_needed"   # checked: the normal reading looked fine
ADVANCED_MISSING = "missing"         # needed (or forced) but the models are not installed
# Warnings of the advanced pass (English, as stored in ReadResult.warnings); the
# interface shows them translated through WARNING_KEYS.
WARNING_ADVANCED_STOPPED = "The advanced reader was stopped: the normal reading is shown."
WARNING_ADVANCED_FAILED = "The advanced reader failed: the normal reading is shown."
WARNING_ADVANCED_NOT_BETTER = "The advanced reader did not improve the reading: the normal reading is shown."
WARNING_KEYS = {WARNING_ADVANCED_STOPPED: "reading.warning.advanced_stopped",
                WARNING_ADVANCED_FAILED: "reading.warning.advanced_failed",
                WARNING_ADVANCED_NOT_BETTER: "reading.warning.advanced_not_better"}
ADVANCED_USED = "used"               # the advanced reading was better and is the one kept
ADVANCED_KEPT_NORMAL = "kept_normal"  # the advanced reading was not better
ADVANCED_FAILED = "failed"           # the advanced engine failed
ADVANCED_CANCELLED = "cancelled"     # cancelled or out of time: the normal reading is kept


@dataclass
class ReadResult:
    """Text of a CV and how it was obtained.

    method: "text" (text layer / DOCX), "ocr" (everything from OCR) or "mixed"
    (PDF with text pages and scanned pages). pages: pages (or image frames)
    in the file. ocr_confidence: mean OCR score 0-1 (None without OCR).
    warnings: short English notes (skipped pages, missing OCR engine...).
    engine: "normal" or "advanced" (the OCR engine of the kept text; "" without
    OCR); provider: "cpu" or "dml" for that engine. advanced: one of the
    ADVANCED_* values ("" when not considered); signals: the trigger signals that
    fired on the normal reading ("A".."E", see ocr_quality.py).
    """

    text: str
    method: str
    pages: int
    ocr_confidence: float | None = None
    warnings: list[str] = field(default_factory=list)
    engine: str = ""
    provider: str = ""
    advanced: str = ""
    signals: list[str] = field(default_factory=list)


@dataclass
class _OcrJob:
    """The OCR part of a reading, kept to read the same pages again.

    kind "image": `base` holds one text per frame; kind "pdf": the text of every page
    (the text layer, replaced by the OCR text of the scanned pages)."""

    kind: str
    base: list[str]
    targets: list[int]
    images: list
    normal: "_Pass | None" = None  # the normal pass, once run


@dataclass
class _Pass:
    pages: list[str]
    confidence: float | None
    page_chars: list[int]
    scripts: list[str]
    ocr_pages: int
    complete: bool = True
    failed_pages: int = 0


# The GUI picks the text to show by matching keywords in these messages (see gui/common.py).
class CVReadError(Exception):
    """The CV cannot be read (missing file, unsupported format, no text)."""


def read_text(path: str | Path) -> str:
    """CV text, one row per paragraph/cell; CVReadError if unreadable."""
    return read_document(path).text


def read_document(path: str | Path, progress: Progress | None = None, *, advanced: str = "off",
                  advanced_progress: Progress | None = None,
                  cancel: threading.Event | None = None) -> ReadResult:
    """Text of a CV with the reading method; CVReadError if unreadable.

    Blocking call: the OCR of a page takes a few seconds (run it in a worker
    thread). `progress(done, total)` is called before each OCR page and at the end;
    `advanced_progress(done, total)` the same for the advanced pass, if it runs.
    `advanced`: "off", "auto" or "force" (see the module docstring).
    """
    p = Path(path)
    if not p.is_file():
        raise CVReadError(f"File not found: {p}")
    extension = p.suffix.lower()
    job: _OcrJob | None = None
    if extension == ".pdf":
        result, job = _read_pdf(p, progress)
    elif extension == ".docx":
        result = ReadResult(_read_docx(p), "text", 1)
    elif extension in IMAGE_FORMATS:
        result, job = _read_image(p, progress)
    else:
        raise CVReadError(
            f"Unsupported format: '{p.suffix or '(no extension)'}'. "
            "Use a PDF, DOCX or image file (JPG, PNG, BMP, TIFF, WEBP)."
        )
    result.text = _normalize_text(result.text)
    if job is not None and result.method != "text" and advanced in ("auto", "force"):
        try:
            _advanced_pass(result, job, advanced, advanced_progress, cancel)
        except Exception:  # noqa: BLE001 - the advanced reader never stops the reading
            log.warning("Advanced OCR pass failed", exc_info=True)
            result.advanced = ADVANCED_FAILED
    if _useful_chars(result.text) < _MIN_CHARS:
        if result.method != "text":
            raise CVReadError(
                "No readable text found, not even with OCR: the scanned image is empty, "
                "too small or too blurred. Use a sharper scan, a text PDF or a DOCX."
            )
        if extension == ".pdf":
            raise CVReadError(
                "The PDF has no extractable text: it is probably a scanned image. "
                "Export the CV as a text PDF or as DOCX."
            )
        raise CVReadError("The document contains no text.")
    return result


def _useful_chars(text: str) -> int:
    return len(re.sub(r"\W", "", text))


def _ocr_engine():
    """The OCR engine; CVReadError with an install hint if it is missing."""
    from . import ocr

    try:
        return ocr.get_engine()
    except ocr.OcrUnavailable as e:
        raise CVReadError(str(e)) from e


def _ocr_pass(job: _OcrJob, engine, progress: Progress | None, *, cancel: threading.Event | None = None,
              deadline: float | None = None, keep: list[str] | None = None) -> _Pass:
    """OCR of the job's images with `engine`. With `keep` (the pages of an earlier
    pass) a page the engine fails on keeps its earlier text; without it errors propagate.
    Stops (complete=False) at `cancel` or after `deadline` (time.monotonic())."""
    from . import ocr

    pages = list(keep if keep is not None else job.base)
    scores: list[tuple[float, int]] = []
    page_chars: list[int] = []
    scripts: list[str] = []
    ocr_pages = failed = 0
    complete = True
    total = len(job.images)
    for done, (index, image) in enumerate(zip(job.targets, job.images)):
        if (cancel is not None and cancel.is_set()) or (deadline is not None and time.monotonic() > deadline):
            complete = False
            break
        _notify(progress, done, total)
        try:
            page = ocr.ocr_image(image, engine)
        except Exception:
            if keep is None:
                raise
            log.warning("The advanced OCR engine failed on a page: the normal text is kept", exc_info=True)
            failed += 1
            pages[index] = keep[index]
            continue
        scripts.append(page.script)
        if job.kind == "image":
            if keep is None or _useful_chars(page.text):
                pages[index] = page.text
            page_chars.append(_useful_chars(page.text))
            if page.confidence is not None:
                scores.append((page.confidence, max(1, len(page.text))))
                ocr_pages += 1
        elif _useful_chars(page.text):
            pages[index] = page.text
            ocr_pages += 1
            page_chars.append(_useful_chars(page.text))
            scores.append((page.confidence or 0.0, max(1, len(page.text))))
        else:
            page_chars.append(0)
    if complete:
        _notify(progress, total, total)
    return _Pass(pages, _weighted(scores), page_chars, scripts, ocr_pages, complete, failed)


def _read_image(p: Path, progress: Progress | None) -> tuple[ReadResult, _OcrJob]:
    from . import ocr

    try:
        images, frames = ocr.load_images(p)
    except Exception as e:  # noqa: BLE001 - Pillow raises many types for bad files
        raise CVReadError(f"Unreadable or damaged image: {e}") from e
    engine = _ocr_engine()
    job = _OcrJob("image", [""] * len(images), list(range(len(images))), images)
    run = _ocr_pass(job, engine, progress)
    warnings = []
    if frames > len(images):
        warnings.append(f"Only the first {len(images)} of {frames} image pages were read.")
    result = ReadResult("\n".join(run.pages), "ocr", frames, run.confidence, warnings, "normal",
                        _provider(engine))
    job.normal = run
    return result, job


def _provider(engine) -> str:
    from . import ocr

    return ocr.engine_provider(engine)


def _read_pdf(p: Path, progress: Progress | None) -> tuple[ReadResult, _OcrJob | None]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        pdf_reader = PdfReader(str(p))
        if pdf_reader.is_encrypted:
            try:
                pdf_reader.decrypt("")
            except Exception as e:  # noqa: BLE001 - password required
                raise CVReadError("The PDF is password-protected.") from e
        pages = [page.extract_text() or "" for page in pdf_reader.pages]
    except CVReadError:
        raise
    except (PdfReadError, OSError, ValueError, KeyError) as e:
        raise CVReadError(f"Unreadable or damaged PDF: {e}") from e
    scanned = [i for i, text in enumerate(pages) if _useful_chars(text) < _MIN_PAGE_CHARS]
    if not scanned:
        return ReadResult("\n".join(pages), "text", len(pages)), None
    from . import ocr

    has_text = _useful_chars("".join(pages)) >= _MIN_CHARS
    warnings: list[str] = []
    to_ocr = scanned[:MAX_OCR_PAGES]
    if len(scanned) > len(to_ocr):
        warnings.append(f"Only the first {len(to_ocr)} of {len(scanned)} scanned pages were read.")
    try:
        images = ocr.render_pdf_pages(p, to_ocr)
    except Exception as e:  # noqa: BLE001 - rendering must not stop the text pages
        log.warning("PDF pages cannot be rendered", exc_info=True)
        images = {}
        warnings.append(f"Scanned pages cannot be rendered: {e}")
    # Blank pages (e.g. an empty last page) need no OCR, and no engine.
    to_ocr = [i for i in to_ocr if i in images and not ocr.is_blank(images[i])]
    if not to_ocr:
        method = "text" if has_text else "ocr"
        return ReadResult("\n".join(pages), method, len(pages), None, warnings), None
    try:
        engine = _ocr_engine()
    except CVReadError as e:
        if has_text:
            # The text pages are enough to go on; the scanned ones are skipped.
            warnings.append(f"{len(to_ocr)} scanned page(s) were skipped. {e}")
            return ReadResult("\n".join(pages), "text", len(pages), None, warnings), None
        raise CVReadError(
            f"The PDF has no extractable text: it is probably a scanned image. {e}"
        ) from e
    job = _OcrJob("pdf", pages, to_ocr, [images[i] for i in to_ocr])
    run = _ocr_pass(job, engine, progress)
    job.normal = run
    if not run.ocr_pages:
        # Nothing found by the OCR: blank pages of a text PDF, or an empty scan.
        method = "text" if has_text else "ocr"
    else:
        method = "mixed" if len(scanned) < len(pages) else "ocr"
    result = ReadResult("\n".join(run.pages), method, len(pages), run.confidence, warnings, "normal",
                        _provider(engine))
    return result, job


def _advanced_pass(result: ReadResult, job: _OcrJob, mode: str, progress: Progress | None,
                   cancel: threading.Event | None) -> None:
    """Reads the OCR pages again with the advanced engine when needed (see the module
    docstring) and keeps the better reading; updates `result` in place."""
    from . import ocr, ocr_quality

    normal = job.normal
    if normal is None:
        return
    found = ocr_quality.signals(result.text, result.ocr_confidence, normal.page_chars, normal.scripts)
    result.signals = list(found.fired)
    if mode == "auto" and not found.triggered:
        result.advanced = ADVANCED_NOT_NEEDED
        return
    log.info("Advanced OCR %s (signals: %s)", "forced" if mode == "force" else "needed",
             ", ".join(found.fired) or "none")
    try:
        engine = ocr.get_advanced_engine()
    except ocr.OcrUnavailable as error:
        result.advanced = ADVANCED_MISSING
        log.info("Advanced OCR not available: %s", error)
        return
    except Exception:  # noqa: BLE001 - a broken model must not stop the reading
        log.warning("The advanced OCR engine cannot start", exc_info=True)
        result.advanced = ADVANCED_FAILED
        return
    started = time.monotonic()
    run = _ocr_pass(job, engine, progress, cancel=cancel, deadline=started + ADVANCED_MAX_S,
                    keep=normal.pages)
    seconds = time.monotonic() - started
    if not run.complete:
        log.info("Advanced OCR stopped after %.1f s (cancelled or out of time): normal reading kept",
                 seconds)
        result.advanced = ADVANCED_CANCELLED
        result.warnings.append(WARNING_ADVANCED_STOPPED)
        return
    if run.failed_pages == len(job.images):
        result.advanced = ADVANCED_FAILED
        result.warnings.append(WARNING_ADVANCED_FAILED)
        return
    text = _normalize_text("\n".join(run.pages))
    before = ocr_quality.measure(result.text, result.ocr_confidence)
    after = ocr_quality.measure(text, run.confidence)
    provider = ocr.engine_provider(engine)
    better = ocr_quality.advanced_is_better(before, after)
    log.info("Advanced OCR: %d page(s) in %.1f s on %s; normal %d skills / %d experiences / %d headings / "
             "confidence %.3f; advanced %d / %d / %d / %.3f: %s kept", len(job.images), seconds, provider,
             before.skills, before.experiences, before.headings, before.confidence, after.skills,
             after.experiences, after.headings, after.confidence, "advanced" if better else "normal")
    if better:
        result.text = text
        result.ocr_confidence = run.confidence
        result.engine, result.provider, result.advanced = "advanced", provider, ADVANCED_USED
        if job.kind == "pdf" and run.ocr_pages and result.method == "text":
            result.method = "mixed"
    else:
        result.advanced = ADVANCED_KEPT_NORMAL
        if mode == "force":
            result.warnings.append(WARNING_ADVANCED_NOT_BETTER)


def _weighted(scores: list[tuple[float, int]]) -> float | None:
    total = sum(w for _, w in scores)
    return sum(s * w for s, w in scores) / total if total else None


def _notify(progress: Progress | None, done: int, total: int) -> None:
    if progress is None:
        return
    try:
        progress(done, total)
    except Exception:  # noqa: BLE001 - a broken callback must not stop the reading
        log.warning("Progress callback failed", exc_info=True)


def _read_docx(p: Path) -> str:
    import zipfile

    from docx import Document
    from docx.opc.exceptions import PackageNotFoundError
    from lxml.etree import XMLSyntaxError

    try:
        document = Document(str(p))
    except (PackageNotFoundError, XMLSyntaxError, zipfile.BadZipFile, KeyError, ValueError, OSError) as e:
        raise CVReadError(f"Unreadable or damaged DOCX: {e}") from e
    rows: list[str] = []
    _docx_blocks(document.element.body, rows)
    return "\n".join(rows)


def _docx_blocks(container, rows: list[str]) -> None:
    """Paragraphs and tables in document order (nested tables too).

    Merged cells appear only once; each cell paragraph is a row, so a Europass
    table (date | content) becomes "date" followed by the content.
    """
    from docx.oxml.ns import qn

    for child in container.iterchildren():
        if child.tag == qn("w:p"):
            text = _paragraph_text(child)
            if text.strip():
                rows.append(text)
        elif child.tag == qn("w:tbl"):
            for row in child.iterchildren(qn("w:tr")):
                seen_ids: set[int] = set()
                for cell in row.iterchildren(qn("w:tc")):
                    if id(cell) in seen_ids:
                        continue
                    seen_ids.add(id(cell))
                    _docx_blocks(cell, rows)
        elif child.tag == qn("w:sdt"):  # content controls
            content = child.find(qn("w:sdtContent"))
            if content is not None:
                _docx_blocks(content, rows)


def _paragraph_text(p) -> str:
    from docx.oxml.ns import qn

    parts = []
    for node in p.iter():
        if node.tag == qn("w:t") and node.text:
            parts.append(node.text)
        elif node.tag == qn("w:tab"):
            parts.append("\t")
        elif node.tag in (qn("w:br"), qn("w:cr")):
            parts.append("\n")
    return "".join(parts)


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = text.replace("​", "").replace("﻿", "")
    rows = [re.sub(r"[ \t]+", " ", r).strip() for r in text.split("\n")]
    # At most one consecutive blank row.
    result: list[str] = []
    for r in rows:
        if r or (result and result[-1]):
            result.append(r)
    return "\n".join(result).strip()


# --- language ---------------------------------------------------------------

_detector = None


CV_LANGUAGES = ("it", "en", "uk")
# Share of Cyrillic letters above which a CV is treated as Ukrainian.
_CYRILLIC_SHARE = 0.3


def detect_language(text: str) -> str:
    """'it', 'en' or 'uk' (a CV written mostly in Cyrillic); 'it' if uncertain."""
    global _detector
    if not text or not text.strip():
        return "it"
    letters = re.findall(r"[^\W\d_]", text[:20000])
    if letters:
        cyrillic = sum(1 for c in letters if "Ѐ" <= c <= "ӿ")
        if cyrillic / len(letters) >= _CYRILLIC_SHARE:
            return "uk"
        # Latin text only from here on (a Ukrainian CV with long English lists stays above).
        text = re.sub(r"[Ѐ-ӿ]+", " ", text)
    try:
        from lingua import Language, LanguageDetectorBuilder

        if _detector is None:
            _detector = LanguageDetectorBuilder.from_languages(
                Language.ITALIAN, Language.ENGLISH
            ).build()
        language = _detector.detect_language_of(text[:5000])
    except Exception:  # noqa: BLE001 - detection must never block
        log.warning("Language detection failed", exc_info=True)
        return "it"
    if language is None:
        return "it"
    return "en" if language.name == "ENGLISH" else "it"
