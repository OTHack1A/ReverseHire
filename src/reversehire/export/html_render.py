"""ExportDocument -> HTML for the preview and for QTextDocument printing.

Light, print-friendly style (black on white) whatever the application theme. The HTML uses
only the subset QTextDocument understands (tables, lists, inline styles, data: images).
"""

from __future__ import annotations

import base64
import html

from .model import Bullets, ExportDocument, Image, Paragraph, Rows, Section

BLANK = "—"
BASE_CSS = (
    "body { font-family: 'Segoe UI', Arial, sans-serif; font-size: 10.5pt; color: #000000; "
    "background-color: #ffffff; }"
    "h1 { font-size: 17pt; margin-bottom: 4px; }"
    "h2 { font-size: 13pt; margin-top: 14px; margin-bottom: 4px; }"
    "td { vertical-align: top; padding-right: 10px; padding-bottom: 2px; }"
    "a { color: #0b4f8a; }"
    ".muted { color: #555555; }"
    ".banner { color: #a00000; font-weight: bold; }"
)


def _e(text) -> str:
    return html.escape(str(text or ""))


def _multiline(text: str) -> str:
    return "<br>".join(_e(line) for line in str(text or "").split("\n"))


def _link(url: str, text: str | None = None) -> str:
    if url.startswith(("http://", "https://", "mailto:")):
        return f'<a href="{_e(url)}">{_e(text or url)}</a>'
    return _e(text or url)


def _paragraph(p: Paragraph) -> str:
    body = _multiline(p.text)
    if p.style == "right":
        return f'<p align="right">{body}</p>'
    if p.style == "bold":
        return f"<p><b>{body}</b></p>"
    if p.style == "subject":
        return f"<p><b>{body}</b></p>"
    if p.style == "muted":
        return f'<p style="color:#555555">{body}</p>'
    if p.style == "note":
        return f'<p style="color:#a00000">{body}</p>'
    return f"<p>{body}</p>"


def _rows(block: Rows) -> str:
    out = ['<table cellspacing="0" cellpadding="1">']
    for row in block.rows:
        if row.value:
            value = _link(row.value) if row.value.startswith(("http://", "https://")) else _e(row.value)
            if row.note:
                value += f' <span style="color:#555555">[{row.note}]</span>'
        else:
            value = BLANK
        out.append(f'<tr><td style="color:#555555">{_e(row.label)}</td><td>{value}</td></tr>')
    out.append("</table>")
    return "".join(out)


def _image(block: Image) -> str:
    data = base64.b64encode(block.png).decode("ascii")
    caption = f'<br><span class="muted">{_e(block.caption)}</span>' if block.caption else ""
    return f'<p><img src="data:image/png;base64,{data}">{caption}</p>'


def _section(section: Section, first: bool) -> str:
    out: list[str] = []
    style = ' style="page-break-before: always"' if section.page_break_before and not first else ""
    if section.banner:
        out.append(f'<p class="banner"{style}><span style="color:#a00000"><b>{_e(section.banner)}</b></span></p>')
        style = ""
    if section.title:
        extra = f' <span style="font-size:9pt; color:#555555">{_e(section.checked)}</span>' if section.checked else ""
        out.append(f"<h2{style}>{_e(section.title)}{extra}</h2>")
    elif style:
        out.append(f'<p{style}></p>')
    for block in section.blocks:
        if isinstance(block, Rows):
            out.append(_rows(block))
        elif isinstance(block, Paragraph):
            out.append(_paragraph(block))
        elif isinstance(block, Bullets):
            out.append("<ul>" + "".join(f"<li>{_multiline(i)}</li>" for i in block.items) + "</ul>")
        elif isinstance(block, Image):
            out.append(_image(block))
    return "\n".join(out)


def render_html(doc: ExportDocument, *, heading: bool = True, footer: bool = True) -> str:
    """A complete HTML document. `heading=False` leaves the title out (letter parts)."""
    parts = [f"<html><head><meta charset='utf-8'><title>{_e(doc.title)}</title>"
             f"<style>{BASE_CSS}</style></head><body bgcolor='#ffffff' text='#000000'>"]
    if heading and doc.kind == "card":
        parts.append(f"<h1>{_e(doc.title)}</h1>")
    for i, section in enumerate(doc.sections):
        parts.append(_section(section, first=(i == 0)))
    if doc.footnotes:
        title = doc.labels.get("sources", "Sources")
        parts.append(f"<h2>{_e(title)}</h2><table cellspacing='0' cellpadding='1'>")
        for note in doc.footnotes:
            parts.append(f"<tr><td class='muted'>[{note.number}]</td><td>{_e(note.text)}</td>"
                         f"<td>{_link(note.url) if note.url else ''}</td></tr>")
        parts.append("</table>")
    if footer and doc.created:
        parts.append(f"<p class='muted' style='font-size:8pt'>{_e(doc.labels.get('footer') or doc.created)}</p>")
    parts.append("</body></html>")
    return "\n".join(parts)
