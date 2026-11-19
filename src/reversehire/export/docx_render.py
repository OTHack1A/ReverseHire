"""ExportDocument -> .docx with python-docx."""

from __future__ import annotations

import io
import logging
import os
import re
from pathlib import Path

from .model import Bullets, ExportDocument, ExportError, Image, Paragraph, Rows

log = logging.getLogger("reversehire.export")
BLANK = "—"
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean(text) -> str:
    return _XML_BAD.sub("", str(text or ""))


def _add_text(paragraph, text: str, *, bold: bool = False, italic: bool = False, grey: bool = False,
              red: bool = False) -> None:
    from docx.shared import RGBColor

    lines = _clean(text).split("\n")
    for i, line in enumerate(lines):
        run = paragraph.add_run(line)
        run.bold = bold
        run.italic = italic
        if grey:
            run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
        if red:
            run.font.color.rgb = RGBColor(0xA0, 0x00, 0x00)
        if i < len(lines) - 1:
            run.add_break()


def build_docx(doc: ExportDocument):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Mm

    out = Document()
    out.core_properties.title = _clean(doc.title)[:200]
    for section in out.sections:
        section.page_height, section.page_width = Mm(297), Mm(210)
        section.left_margin = section.right_margin = Mm(20)
        section.top_margin = section.bottom_margin = Mm(18)
    if doc.kind == "card":
        out.add_heading(_clean(doc.title), level=1)
    first = True
    for section in doc.sections:
        if section.page_break_before and not first:
            out.add_page_break()
        first = False
        if section.banner:
            p = out.add_paragraph()
            _add_text(p, section.banner, bold=True, red=True)
        if section.title:
            out.add_heading(_clean(section.title), level=2)
            if section.checked:
                _add_text(out.add_paragraph(), section.checked, grey=True)
        for block in section.blocks:
            if isinstance(block, Rows):
                table = out.add_table(rows=0, cols=3)
                table.style = "Table Grid"
                for row in block.rows:
                    cells = table.add_row().cells
                    _add_text(cells[0].paragraphs[0], row.label, bold=True)
                    value = row.value or BLANK
                    _add_text(cells[1].paragraphs[0], value)
                    if row.source or row.url:
                        ref = f"[{row.note}] " if row.note else ""
                        _add_text(cells[2].paragraphs[0], ref + (row.source or ""), grey=True)
                for row_cells in table.rows:
                    for cell, width in zip(row_cells.cells, (Mm(48), Mm(80), Mm(42))):
                        cell.width = width
                out.add_paragraph()
            elif isinstance(block, Paragraph):
                p = out.add_paragraph()
                _add_text(p, block.text, bold=block.style in ("bold", "subject"), grey=block.style == "muted",
                          red=block.style == "note")
                if block.style == "right":
                    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            elif isinstance(block, Bullets):
                for item in block.items:
                    _add_text(out.add_paragraph(style="List Bullet"), item)
            elif isinstance(block, Image):
                out.add_picture(io.BytesIO(block.png), width=Mm(min(block.width_mm, 170)))
                if block.caption:
                    _add_text(out.add_paragraph(), block.caption, grey=True)
    if doc.footnotes:
        out.add_heading(_clean(doc.labels.get("sources", "Sources")), level=2)
        for note in doc.footnotes:
            p = out.add_paragraph()
            _add_text(p, f"[{note.number}] {note.text}" + (f"  {note.url}" if note.url else ""), grey=True)
    if doc.created:
        _add_text(out.add_paragraph(), doc.labels.get("footer") or doc.created, grey=True)
    return out


def render_docx(doc: ExportDocument, path: str | Path) -> Path:
    path = Path(path)
    try:
        target = path.with_name(path.name + ".part")
        build_docx(doc).save(str(target))
        os.replace(target, path)
    except Exception as exc:
        raise ExportError(f"{type(exc).__name__}: {exc}") from exc
    log.info("exported format=docx file=%s", path.name)
    return path
