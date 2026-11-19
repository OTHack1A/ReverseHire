"""PDF output (QPrinter in PdfFormat + pypdf merge) and printing.

Application: letter pages, then the CV PDF pages as they are, then the private appendix.
Printing never talks to a printer by itself in tests: `print_pdf_file` takes a QPrinter,
so a test can hand it one that writes a PDF file.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import tempfile
from pathlib import Path

from .html_render import render_html
from .model import ExportDocument, ExportError, NoPrinterError

log = logging.getLogger("reversehire.export")

MARGIN_MM = 18.0


def _render_text_pdf(doc: ExportDocument, path: Path) -> None:
    """Sections of `doc` as paginated PDF through QTextDocument/QPrinter."""
    from PySide6.QtCore import QMarginsF
    from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter, QTextDocument

    writer = QPdfWriter(str(path))  # no printer subsystem involved
    writer.setResolution(300)
    layout = QPageLayout(QPageSize(QPageSize.PageSizeId.A4), QPageLayout.Orientation.Portrait,
                         QMarginsF(MARGIN_MM, MARGIN_MM, MARGIN_MM, MARGIN_MM), QPageLayout.Unit.Millimeter)
    writer.setPageLayout(layout)
    text = QTextDocument()
    text.setHtml(render_html(doc))
    text.print_(writer)


def typeset_parts(doc: ExportDocument, folder: Path) -> list[Path]:
    """Typeset the document into PDF parts inside `folder`, the CV PDF (as is) between the
    letter and the appendix. MUST run on the GUI thread (Qt text layout is not thread-safe
    on Windows: it crashes in a worker)."""
    try:
        if doc.kind == "application":
            before, after = doc.split_around_cv()
            to_render = [before] + ([after] if after.sections else [])
            cv = doc.cv_pdf_path
        else:
            to_render = [dataclasses.replace(doc, sections=[s for s in doc.sections if s.kind != "cv"])]
            cv = None
        order: list[Path] = []
        for i, part in enumerate(to_render):
            target = Path(folder) / f"part{i}.pdf"
            _render_text_pdf(part, target)
            order.append(target)
        if cv is not None:
            order.insert(1, Path(cv))
        return order
    except Exception as exc:
        raise ExportError(f"{type(exc).__name__}: {exc}") from exc


def merge_pdfs(sources: list[Path], path: str | Path) -> Path:
    """Concatenate PDF files into `path` (pypdf, safe in a worker thread)."""
    path = Path(path)
    try:
        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter()
        for source in sources:
            reader = PdfReader(str(source))
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception:
                    pass
            for page in reader.pages:
                writer.add_page(page)
        target = path.with_name(path.name + ".part")
        with open(target, "wb") as handle:
            writer.write(handle)
        os.replace(target, path)
    except Exception as exc:
        raise ExportError(f"{type(exc).__name__}: {exc}") from exc
    log.info("exported format=pdf file=%s", path.name)
    return path


def render_pdf(doc: ExportDocument, path: str | Path) -> Path:
    """Write the document as a PDF (call on the GUI thread; the dialog splits the work)."""
    with tempfile.TemporaryDirectory(prefix="reversehire-export-") as tmp:
        return merge_pdfs(typeset_parts(doc, Path(tmp)), path)


def print_pdf_file(pdf_path: str | Path, printer=None) -> int:
    """Send a PDF file to `printer` (default printer when None). Returns the page count.

    Pages are rasterised with QtPdf at the printer resolution and fitted into the page.
    """
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QPainter
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtPrintSupport import QPrinter, QPrinterInfo

    if printer is None:
        if not QPrinterInfo.defaultPrinterName():
            raise NoPrinterError("no default printer")
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    pdf = QPdfDocument()
    status = pdf.load(str(pdf_path))
    if status != QPdfDocument.Error.None_ or pdf.pageCount() < 1:
        raise ExportError(f"cannot read {Path(pdf_path).name}")
    painter = QPainter()
    if not painter.begin(printer):
        raise ExportError("the printer cannot be opened")
    try:
        area = printer.pageLayout().paintRectPixels(printer.resolution())
        for index in range(pdf.pageCount()):
            if index:
                printer.newPage()
            size = pdf.pagePointSize(index)
            scale = min(area.width() / size.width(), area.height() / size.height())
            width, height = max(1, int(size.width() * scale)), max(1, int(size.height() * scale))
            image = pdf.render(index, QSize(width, height))
            painter.drawImage(0, 0, image)
    finally:
        painter.end()
    log.info("printed pages=%d", pdf.pageCount())
    return pdf.pageCount()
