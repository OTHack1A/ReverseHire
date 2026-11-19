"""HTML utilities: cleaned text, links, text and URL normalization."""

import html as html_module
import logging
import re
import unicodedata
from urllib.parse import urldefrag, urljoin, urlsplit

from lxml import html as lxml_html

log = logging.getLogger(__name__)

_SPACES = re.compile(r"\s+")
_TAGS_TO_DROP = ("script", "style", "noscript", "template", "svg", "iframe", "head")


def compact(text: str) -> str:
    """Reduces every run of whitespace to a single space."""
    return _SPACES.sub(" ", text or "").strip()


def normalize(text: str) -> str:
    """Lowercase, without accents and with collapsed spaces (for comparisons)."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    unaccented = "".join(c for c in decomposed if not unicodedata.combining(c))
    return compact(unaccented.lower().replace("’", "'"))


def truncate(text: str, maximum: int) -> str:
    """Truncates to `maximum` characters, preferably at a space."""
    text = compact(text)
    if len(text) <= maximum:
        return text
    cut = text[:maximum]
    space = cut.rfind(" ")
    if space > maximum * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:.") + "…"


def analyze_html(html: str):
    """lxml tree of the HTML, or None if it cannot be parsed."""
    if not html or not html.strip():
        return None
    try:
        return lxml_html.document_fromstring(html)
    except Exception:  # lxml raises different types for degenerate input
        return None


def _drop(tree, tag: tuple[str, ...]) -> None:
    """Removes from the tree the elements with the given tags (with their content)."""
    for element in list(tree.iter(*tag)):
        if element.getparent() is not None:
            element.drop_tree()


def _plain_text(html: str) -> str:
    """Fallback: visible text of the HTML without scripts, styles and the like."""
    tree = analyze_html(html)
    if tree is None:
        return ""
    _drop(tree, _TAGS_TO_DROP)
    body = tree.find("body")
    root = body if body is not None else tree
    # itertext joins nodes without separators: separate them with a space.
    return compact(" ".join(root.itertext()))


def visible_text(html: str) -> str:
    """All visible text of the page, footer and navigation included (entities decoded,
    whitespace collapsed). `extract_text` keeps only the main content and drops the
    footer, where Italian sites print P.IVA, REA and PEC."""
    return compact(html_module.unescape(_plain_text(html)))


def extract_text(html: str, url: str | None = None) -> str:
    """Main text of the page (trafilatura) with a simple fallback."""
    if not html or not html.strip():
        return ""
    text = ""
    try:
        import trafilatura

        text = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            include_links=False,
            favor_recall=True,
        ) or ""
    except Exception as error:  # trafilatura must never make fetching fail
        log.debug("trafilatura did not extract the text: %s", type(error).__name__)
        text = ""
    text = text.strip()
    if not text:
        text = _plain_text(html)
    return text


def text_from_fragment(fragment: str, maximum: int = 500) -> str:
    """Short text from an HTML fragment (also with double-encoded entities, e.g. Greenhouse)."""
    if not fragment:
        return ""
    text = fragment
    # Some APIs return HTML with already encoded entities (&lt;p&gt;).
    for _ in range(2):
        if "&lt;" in text or "&amp;" in text or "&gt;" in text:
            text = html_module.unescape(text)
    if "<" in text and ">" in text:
        tree = analyze_html(f"<div>{text}</div>")
        if tree is not None:
            _drop(tree, ("script", "style"))
            text = " ".join(tree.itertext())
    return truncate(html_module.unescape(text), maximum)


_BLOCK_END = re.compile(
    r"<br\s*/?>|</(?:p|div|li|h[1-6]|tr|ul|ol|section|article|blockquote|dd|dt|table)\s*>", re.I
)
_LIST_ITEM = re.compile(r"<li(?:\s[^>]*)?>", re.I)


def fragment_lines(fragment: str, maximum: int = 6000) -> str:
    """Plain text of an HTML (or plain) fragment that keeps one line per block.

    Paragraphs, headings and list items stay on separate lines (list items as
    "- item"), so later rules can split requirements and read headings. The
    result is cut at `maximum` characters, preferably at the end of a line.
    """
    if not fragment:
        return ""
    text = str(fragment)
    for _ in range(2):
        if "&lt;" in text or "&amp;" in text or "&gt;" in text:
            text = html_module.unescape(text)
    if "<" in text and ">" in text:
        text = _LIST_ITEM.sub("\n- ", _BLOCK_END.sub("\n", text))
        tree = analyze_html(f"<div>{text}</div>")
        if tree is not None:
            _drop(tree, ("script", "style"))
            text = "".join(tree.itertext())
    text = html_module.unescape(text).replace("\r", "\n")
    lines = [compact(line) for line in text.split("\n")]
    text = "\n".join(line for line in lines if line and line != "-")
    if len(text) <= maximum:
        return text
    cut = text[:maximum]
    newline = cut.rfind("\n")
    if newline > maximum * 0.8:
        return cut[:newline]
    space = cut.rfind(" ")
    if space > maximum * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:.") + "…"


def clean_url(url: str) -> str:
    """URL without fragment (#...) and without surrounding spaces."""
    return urldefrag((url or "").strip())[0]


def url_key(url: str) -> str:
    """Normalized form of a URL to recognise duplicates."""
    parts = urlsplit(clean_url(url))
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{host}{path}{query}"


def host_base(url: str) -> str:
    """Lowercase host without 'www.'."""
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def same_site(url: str, reference: str) -> bool:
    """True if `url` is on the same site as `reference` (subdomains included)."""
    a, b = host_base(url), host_base(reference)
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def extract_links(html: str, base_url: str, tree=None) -> list[tuple[str, str]]:
    """Pairs (absolute URL without fragment, link text) of the http(s) links.

    The text also includes title/aria-label, useful for icon-only links.
    """
    if tree is None:
        tree = analyze_html(html)
    if tree is None:
        return []
    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for a in tree.iter("a"):
        # lxml does not decode some entities (e.g. &apos;) in attributes.
        href = html_module.unescape(a.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        try:
            absolute = clean_url(urljoin(base_url, href))
        except ValueError:
            continue
        if urlsplit(absolute).scheme not in ("http", "https"):
            continue
        text = compact(" ".join(a.itertext()))
        extra = " ".join(filter(None, [a.get("title"), a.get("aria-label")]))
        if extra:
            text = compact(f"{text} {extra}")
        pair = (absolute, text)
        if pair not in seen:
            seen.add(pair)
            result.append(pair)
    return result
