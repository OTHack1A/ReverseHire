"""Minimal reader for .xlsx workbooks with the standard library only.

Used for the ISTAT ATECO table and for company lists the user imports (Registro
Imprese exports, the innovative start-up register). It reads cell values (shared
strings, inline strings, numbers, booleans) of one sheet; formulas give their
cached value; styles, dates and merged cells are ignored (dates stay numbers).
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_CELL_REF = re.compile(r"([A-Z]+)(\d+)")
MAX_ROWS = 200_000  # a safety limit for very large files


def _column_index(letters: str) -> int:
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _text(element) -> str:
    """Concatenated text of an <si> or <is> element (rich text runs included)."""
    return "".join(node.text or "" for node in element.iter(f"{{{_NS['m']}}}t"))


def sheet_names(path: str | Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    return [s.get("name", "") for s in workbook.iterfind("m:sheets/m:sheet", _NS)]


def _sheet_member(archive: zipfile.ZipFile, sheet: str | int | None) -> str:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    sheets = list(workbook.iterfind("m:sheets/m:sheet", _NS))
    if not sheets:
        raise ValueError("The workbook has no sheet")
    chosen = sheets[0]
    if isinstance(sheet, int):
        chosen = sheets[sheet]
    elif isinstance(sheet, str):
        matches = [s for s in sheets if s.get("name", "").strip().lower() == sheet.strip().lower()]
        if not matches:
            raise ValueError(f"Sheet not found: {sheet!r}")
        chosen = matches[0]
    rel_id = chosen.get(f"{{{_NS['r']}}}id")
    relations = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    for relation in relations.iter(f"{_REL_NS}Relationship"):
        if relation.get("Id") == rel_id:
            target = relation.get("Target", "")
            target = target.lstrip("/")
            return target if target.startswith("xl/") else f"xl/{target}"
    return "xl/worksheets/sheet1.xml"


def read_rows(path: str | Path, sheet: str | int | None = None,
              max_rows: int = MAX_ROWS) -> list[list[str]]:
    """Rows of a sheet (the first one by default) as lists of strings; empty cells are ""."""
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = [_text(si) for si in root.iterfind("m:si", _NS)]
        member = _sheet_member(archive, sheet)
        root = ElementTree.fromstring(archive.read(member))
    rows: list[list[str]] = []
    for row in root.iterfind("m:sheetData/m:row", _NS):
        values: dict[int, str] = {}
        for position, cell in enumerate(row.iterfind("m:c", _NS)):
            match = _CELL_REF.match(cell.get("r", ""))
            column = _column_index(match.group(1)) if match else position
            kind = cell.get("t", "")
            if kind == "inlineStr":
                inline = cell.find("m:is", _NS)
                values[column] = _text(inline) if inline is not None else ""
                continue
            raw = cell.find("m:v", _NS)
            text = raw.text if raw is not None and raw.text is not None else ""
            if kind == "s" and text:
                try:
                    text = shared[int(text)]
                except (ValueError, IndexError):
                    text = ""
            elif kind == "b":
                text = "TRUE" if text == "1" else "FALSE"
            elif text and kind in ("", "n"):
                # Whole numbers without ".0" (codes such as a VAT number stored as a number).
                try:
                    number = float(text)
                    if number.is_integer() and abs(number) < 1e15:
                        text = str(int(number))
                except ValueError:
                    pass
            values[column] = text
        if values:
            width = max(values) + 1
            rows.append([values.get(i, "") for i in range(width)])
        else:
            rows.append([])
        if len(rows) >= max_rows:
            break
    return rows
