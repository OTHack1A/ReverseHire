"""Neutral document model shared by every renderer (HTML, PDF, DOCX, XLSX)."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Row:
    """One field of a key/value table. `value` stays "" when unknown (renderers show a dash)."""

    label: str
    value: str = ""
    source: str = ""         # human name of the source ("VIES")
    url: str = ""
    note: int | None = None  # number of the footnote that names the source


@dataclass
class Rows:
    rows: list[Row] = field(default_factory=list)


@dataclass
class Paragraph:
    text: str
    style: str = "normal"  # normal | muted | right | bold | subject | note


@dataclass
class Bullets:
    items: list[str] = field(default_factory=list)


@dataclass
class Image:
    png: bytes
    caption: str = ""
    width_mm: float = 120.0


Block = Rows | Paragraph | Bullets | Image


@dataclass
class Section:
    key: str                      # "letter", "cv", "header", "registry", ...
    title: str = ""               # "" = no heading (the letter)
    blocks: list[Block] = field(default_factory=list)
    kind: str = "card"            # letter | cv | card
    banner: str = ""              # strong notice printed above the title
    page_break_before: bool = False
    checked: str = ""             # "Checked on 2026-11-17"

    def rows(self) -> list[Row]:
        return [r for b in self.blocks if isinstance(b, Rows) for r in b.rows]


@dataclass
class Footnote:
    number: int
    text: str
    url: str = ""


@dataclass
class ExportDocument:
    title: str
    kind: str = "card"            # card | application
    sections: list[Section] = field(default_factory=list)
    footnotes: list[Footnote] = field(default_factory=list)
    cv_pdf_path: Path | None = None
    file_stem: str = "export"
    created: str = ""             # ISO date shown in the footer
    labels: dict[str, str] = field(default_factory=dict)  # translated headings for the renderers

    def section(self, key: str) -> Section | None:
        return next((s for s in self.sections if s.key == key), None)

    def split_around_cv(self) -> tuple["ExportDocument", "ExportDocument"]:
        """(part before the CV, part after it). Without a CV section the second part is empty."""
        idx = next((i for i, s in enumerate(self.sections) if s.kind == "cv"), None)
        if idx is None:
            return self, dataclasses.replace(self, sections=[], footnotes=[])
        before = dataclasses.replace(self, sections=self.sections[:idx], footnotes=[])
        after = dataclasses.replace(self, sections=self.sections[idx + 1:])
        return before, after


def safe_stem(text: str, fallback: str = "export") -> str:
    """A file-name stem: letters, digits, dash and underscore only."""
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in text.strip())
    while "--" in stem:
        stem = stem.replace("--", "-")
    return stem.strip("-_")[:60] or fallback


class ExportError(Exception):
    """An export or print step failed; the message is meant to be shown to the user."""


class NoPrinterError(ExportError):
    """There is no default printer."""
