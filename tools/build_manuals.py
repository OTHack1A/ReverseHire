"""Turn the Markdown manuals in docs/ into PDF files (for the installation ISO).

Usage:  python tools/build_manuals.py <output_folder>

Needs the `markdown-it-py` package and Microsoft Edge (installed with Windows),
which prints the intermediate HTML page to PDF in headless mode.
"""

import pathlib
import re
import subprocess
import sys

for _stream in (sys.stdout, sys.stderr):  # a cp1252 console: never UnicodeEncodeError
    getattr(_stream, "reconfigure", lambda **_k: None)(errors="replace")
import tempfile

from markdown_it import MarkdownIt

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from reversehire import REPOSITORY_URL  # noqa: E402 - the single place of the address

DOCS = ROOT / "docs"
MANUALS = {
    "INSTALLATION.md": "Installation_Manual.pdf",
    "USER_MANUAL.md": "User_Manual.pdf",
}
REPOSITORY_DOCS = REPOSITORY_URL + "/blob/main/docs/"
EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
CSS = """
@page { size: A4; margin: 18mm 16mm; }
body { font-family: "Segoe UI", Arial, sans-serif; font-size: 10.5pt; line-height: 1.45; color: #1b1b1b; }
h1 { font-size: 22pt; border-bottom: 2px solid #0a7d4f; padding-bottom: 6px; }
h2 { font-size: 15pt; margin-top: 22px; color: #0a5d3c; border-bottom: 1px solid #c9d6cf; padding-bottom: 3px;
     break-after: avoid; }
h3 { font-size: 12pt; margin-top: 16px; break-after: avoid; }
code { font-family: Consolas, monospace; font-size: 9.5pt; background: #f0f3f1; padding: 1px 3px;
       border-radius: 3px; overflow-wrap: anywhere; }
pre { background: #f0f3f1; padding: 8px 10px; border-radius: 4px; white-space: pre-wrap; break-inside: avoid; }
pre code { padding: 0; background: none; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 9.5pt; }
th, td { border: 1px solid #c2ccc6; padding: 4px 7px; text-align: left; vertical-align: top; }
th { background: #e4efe9; }
tr { break-inside: avoid; }
img { max-width: 100%; border: 1px solid #c2ccc6; margin: 6px 0; break-inside: avoid; }
a { color: #0a5d3c; }
"""


def link_target(match: re.Match) -> str:
    """Links between manuals point to the PDF next to this one; other docs to the repository."""
    name, anchor = match.group(1), match.group(2) or ""
    if name in MANUALS:
        return f'href="{MANUALS[name]}{anchor}"'
    return f'href="{REPOSITORY_DOCS}{name}{anchor}"'


def to_html(markdown_path: pathlib.Path) -> str:
    text = markdown_path.read_text(encoding="utf-8")
    body = MarkdownIt("commonmark").enable("table").render(text)
    body = re.sub(r'href="([A-Za-z_]+\.md)(#[^"]*)?"', link_target, body)
    title = text.splitlines()[0].lstrip("# ").strip()
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{title}</title>'
        f'<base href="{DOCS.as_uri()}/"><style>{CSS}</style></head><body>{body}</body></html>'
    )


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    output = pathlib.Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    edge = next((p for p in EDGE_PATHS if pathlib.Path(p).exists()), None)
    if edge is None:
        sys.exit("Microsoft Edge not found.")
    with tempfile.TemporaryDirectory() as work:
        for source, pdf_name in MANUALS.items():
            page = pathlib.Path(work) / (source + ".html")
            page.write_text(to_html(DOCS / source), encoding="utf-8")
            target = output / pdf_name
            target.unlink(missing_ok=True)
            subprocess.run(
                [edge, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                 f"--user-data-dir={pathlib.Path(work) / 'profile'}",
                 f"--print-to-pdf={target}", page.as_uri()],
                check=True, capture_output=True, timeout=180,
            )
            if not target.exists():
                sys.exit(f"{pdf_name} was not created.")
            print(f"{target} ({target.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
