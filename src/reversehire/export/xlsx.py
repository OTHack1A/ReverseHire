"""Minimal .xlsx writer with the standard library only (zipfile + SpreadsheetML).

One worksheet per document section (columns Field, Value, Source, Link) plus a "Sources"
sheet with the numbered footnotes. Text goes in as inline strings, so a value that starts
with "=" is text, never a formula. Control characters XML cannot hold are dropped.
"""

from __future__ import annotations

import logging
import os
import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from .model import Bullets, ExportDocument, ExportError, Image, Paragraph, Rows

log = logging.getLogger("reversehire.export")

MAX_CELL = 32000
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_SHEET_BAD = re.compile(r"[\[\]:*?/\\]")
COLUMN_WIDTHS = (30, 70, 22, 45)

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _text(value) -> str:
    return _XML_BAD.sub("", str(value or ""))[:MAX_CELL]


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def sheet_name(title: str, used: set[str]) -> str:
    base = _SHEET_BAD.sub(" ", _text(title)).strip().strip("'") or "Sheet"
    base = base[:31]
    name, n = base, 2
    while name.lower() in used:
        suffix = f" ({n})"
        name = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(name.lower())
    return name


def _cell(col: int, row: int, value: str, style: int = 0) -> str:
    if value == "":
        return ""
    ref = f"{column_letter(col)}{row}"
    s = f' s="{style}"' if style else ""
    return f'<c r="{ref}"{s} t="inlineStr"><is><t xml:space="preserve">{escape(_text(value))}</t></is></c>'


def _sheet_xml(rows: list[tuple[list[str], int]]) -> str:
    """rows: ([col values], style) with style 1 = bold header, 2 = wrapped text."""
    body = []
    for number, (values, style) in enumerate(rows, 1):
        cells = "".join(_cell(c, number, v, style or (2 if c else 0)) for c, v in enumerate(values))
        body.append(f'<row r="{number}">{cells}</row>' if cells else f'<row r="{number}"/>')
    cols = "".join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>'
                   for i, w in enumerate(COLUMN_WIDTHS, 1))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{_NS}"><sheetViews><sheetView workbookViewId="0"/></sheetViews>'
            f'<cols>{cols}</cols><sheetData>{"".join(body)}</sheetData></worksheet>')


_STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<styleSheet xmlns="{_NS}">'
           '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
           '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
           '<fills count="2"><fill><patternFill patternType="none"/></fill>'
           '<fill><patternFill patternType="gray125"/></fill></fills>'
           '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
           '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
           '<cellXfs count="3">'
           '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
           '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
           '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">'
           '<alignment vertical="top" wrapText="1"/></xf></cellXfs>'
           '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def sheets_of(doc: ExportDocument) -> list[tuple[str, list[tuple[list[str], int]]]]:
    """(sheet title, rows) pairs: one per section, then the sources."""
    labels = doc.labels
    header = [labels.get("field", "Field"), labels.get("value", "Value"), labels.get("source", "Source"),
              labels.get("link", "Link")]
    out: list[tuple[str, list]] = []
    for section in doc.sections:
        title = section.title or labels.get(f"sheet_{section.key}", section.key)
        rows: list[tuple[list[str], int]] = []
        if section.banner:
            rows.append(([section.banner], 1))
        if section.kind == "card" or section.key == "cv":
            rows.append((header, 1))
        if section.checked:
            rows.append((["", section.checked], 0))
        for block in section.blocks:
            if isinstance(block, Rows):
                for r in block.rows:
                    ref = f"[{r.note}] " if r.note else ""
                    rows.append(([r.label, r.value, (ref + r.source) if (r.source or ref) else "", r.url], 0))
            elif isinstance(block, Paragraph):
                rows.append((["", block.text], 1 if block.style in ("bold", "subject") else 0))
            elif isinstance(block, Bullets):
                rows.extend((["", f"- {item}"], 0) for item in block.items)
            elif isinstance(block, Image):
                rows.append((["", block.caption], 0))
        out.append((title, rows))
    if doc.footnotes:
        rows = [(["#", labels.get("source", "Source"), labels.get("link", "Link")], 1)]
        rows += [([str(n.number), n.text, n.url], 0) for n in doc.footnotes]
        out.append((labels.get("sheet_sources", "Sources"), rows))
    return out


def build_xlsx_bytes(doc: ExportDocument) -> bytes:
    import io

    sheets = sheets_of(doc) or [("Sheet", [])]
    used: set[str] = set()
    names = [sheet_name(title, used) for title, _ in sheets]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        overrides = "".join(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in range(1, len(sheets) + 1))
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" '
                   'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" '
                   'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   f'{overrides}</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   f'<Relationship Id="rId1" Type="{_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        sheet_tags = "".join(f'<sheet name="{escape(n, {chr(34): "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>'
                             for i, n in enumerate(names, 1))
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   f'<workbook xmlns="{_NS}" xmlns:r="{_REL}"><sheets>{sheet_tags}</sheets></workbook>')
        rels = "".join(f'<Relationship Id="rId{i}" Type="{_REL}/worksheet" Target="worksheets/sheet{i}.xml"/>'
                       for i in range(1, len(sheets) + 1))
        rels += f'<Relationship Id="rId{len(sheets) + 1}" Type="{_REL}/styles" Target="styles.xml"/>'
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        z.writestr("xl/styles.xml", _STYLES)
        for i, (_, rows) in enumerate(sheets, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(rows))
    return buffer.getvalue()


def render_xlsx(doc: ExportDocument, path: str | Path) -> Path:
    path = Path(path)
    try:
        target = path.with_name(path.name + ".part")
        target.write_bytes(build_xlsx_bytes(doc))
        os.replace(target, path)
    except Exception as exc:
        raise ExportError(f"{type(exc).__name__}: {exc}") from exc
    log.info("exported format=xlsx file=%s", path.name)
    return path
