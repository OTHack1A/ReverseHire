"""Presentation and history of a company, as sentences taken from its own website
(company card, section "Presentation and history").

No summarising model and no invented text: every sentence comes verbatim from a page
read by the identity scan (web/identity.py keeps the HTML of the home, about, history
pages...), trimmed at a sentence boundary, and keeps its source URL and where on the
page it was read. The original text is stored; the card shows it in the display
language through `display_text` (translation kept apart, original visible).

- Presentation: the lead paragraphs of the "about" page ("chi siamo", "azienda", "about
  us"), else the schema.org Organization `description`, else the `meta description`,
  else `og:description` of the home page (Italian home first when there is one).
- History: the lead paragraphs of the "history" page ("storia", "history", "heritage"),
  else the sentences of the about/home pages that carry a year or a founding verb
  ("fondata nel 1961", "dal 1985", "founded in").

Text that is not a presentation is never taken: cookie banners and consent dialogs
(elements whose id/class says cookie/consent/gdpr/modal/popup/newsletter, and sentences
about cookies, consent or JavaScript), navigation, header, footer, forms and the legal
data block (P.IVA, REA, capitale sociale).
"""

from __future__ import annotations

import re
from datetime import date

from .identity import _jsonld_organizations, _text_of
from .text import analyze_html, compact

SOURCE = "website"
PRESENTATION_CHARS = 700
HISTORY_CHARS = 1500
MIN_PARAGRAPH_CHARS = 60
MAX_HISTORY_SENTENCES = 8

_DROP_TAGS = ("script", "style", "noscript", "template", "svg", "iframe", "nav", "header", "footer", "form",
              "aside", "button", "select", "dialog")
_NOISE_MARKER = re.compile(r"cookie|consent|gdpr|iubenda|onetrust|didomi|cookiebot|modal|popup|newsletter|"
                           r"breadcrumb|\bshare\b|social|\bmenu\b|navbar|skip-link", re.I)
_NOISE_TEXT = re.compile(
    r"\bcookies?\b|\bconsenso\b|\bconsent\b|javascript|\bbrowser\b|accett[ao]|\baccept\b|preferenze|"
    r"preferences|privacy policy|informativa|trattamento dei dati|personal data|dati personali|"
    r"\biscriviti\b|subscribe|newsletter|tutti i diritti|all rights reserved|©", re.I)
_LEGAL_BLOCK = re.compile(r"P\.?\s*IVA|Partita IVA|R\.?\s*E\.?\s*A\.?\s|Capitale sociale|Share capital|"
                          r"C\.?\s*F\.?\s*\d|VAT\s*(?:No|number)", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÀ-Ý0-9“\"«(])")
_YEAR = re.compile(r"\b(1[89]\d{2}|20[0-2]\d)\b")
_FOUNDING = re.compile(r"\b(?:fondat[aoie]|nasce|nata|nato|costituit[aoie]|founded|established|since|dal|"
                       r"was born|origini|origins)\b", re.I)
_NUMBER_GROUP = re.compile(r"\b\d{4,6}\b")


def _meta(tree, *names: str) -> str:
    for element in tree.iter("meta"):
        key = (element.get("name") or element.get("property") or "").lower()
        if key in names:
            content = compact(element.get("content") or "")
            if content:
                return content
    return ""


def _clean_tree(html: str):
    tree = analyze_html(html)
    if tree is None:
        return None
    for element in list(tree.iter(*_DROP_TAGS)):
        if element.getparent() is not None:
            element.drop_tree()
    for element in list(tree.iter()):
        if not isinstance(element.tag, str) or element.getparent() is None:
            continue
        marker = f"{element.get('id') or ''} {element.get('class') or ''} {element.get('role') or ''}"
        if marker.strip() and _NOISE_MARKER.search(marker) and element.tag not in ("html", "body", "main"):
            element.drop_tree()
    return tree


def acceptable(text: str) -> bool:
    """A paragraph worth showing: long enough, prose, not a banner or the legal block."""
    text = compact(text)
    if len(text) < MIN_PARAGRAPH_CHARS or len(text.split()) < 8:
        return False
    if _NOISE_TEXT.search(text) or _LEGAL_BLOCK.search(text):
        return False
    if re.match(r"(?:[A-ZÀ-Ý]{2,}[\s|,/&-]+){2,}", text):
        return False  # "AMMINISTRATORE DELEGATO | PARTNER Con oltre 40 anni...": a person's bio
    letters = sum(c.isalpha() for c in text)
    if letters < 0.6 * len(text):
        return False
    # an address list ("Tower B, Ebene, Mauritius, 72201 Nex Tower, ...") is not prose
    if len(_NUMBER_GROUP.findall(text)) >= 2 and not re.search(r"[.!?…]\s", text + " "):
        return False
    if text.count(",") >= 6 and not re.search(r"[.!?…](?:\s|$)", text):
        return False
    return True


def lead_paragraphs(html: str, limit: int = 3) -> list[str]:
    """The first `limit` prose paragraphs of the page's main content (`<p>` elements)."""
    tree = _clean_tree(html)
    if tree is None:
        return []
    main = None
    for tag in ("main", "article"):
        main = next(iter(tree.iter(tag)), None)
        if main is not None:
            break
    root = main if main is not None else (tree.find("body") if tree.find("body") is not None else tree)
    out: list[str] = []
    for element in root.iter("p"):
        text = compact(" ".join(element.itertext()))
        if acceptable(text) and text not in out:
            out.append(text)
            if len(out) >= limit:
                break
    return out


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(compact(text)) if s.strip()]


def trim(text: str, limit: int) -> str:
    """At most `limit` characters, cut after the last whole sentence that fits (never in
    the middle of a sentence unless the first one alone is too long)."""
    text = compact(text)
    if len(text) <= limit:
        return text
    kept = ""
    for sentence in sentences(text):
        candidate = f"{kept} {sentence}".strip()
        if len(candidate) > limit:
            break
        kept = candidate
    if kept:
        return kept
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.6 else cut).rstrip(" ,;:") + "…"


def _entry(text: str, url: str, where: str, when: str) -> dict:
    return {"text": text, "url": url, "where": where, "source": SOURCE, "date": when}


_ABOUT_PATH = re.compile(r"chi-?siamo|about|azienda|company|who-?we-?are|profil|gruppo|group|storia|history|"
                         r"mission|corporate/?$|il-?gruppo|la-?societa", re.I)
# "/corporate/certificazioni--azienda", "/azienda/news": a page ABOUT something else
_NOT_ABOUT_PATH = re.compile(r"certific|qualit|sostenib|sustainab|\besg\b|news|press|blog|event|award|premi|"
                             r"brochure|download|catalog", re.I)
_ROLE_LABEL = re.compile(r"\b(?:AMMINISTRATORE DELEGATO|DIRETTORE|DIRETTRICE|RESPONSABILE|PARTNER|CEO|CFO|CTO|"
                         r"PRESIDENTE|MANAGER|FOUNDER|FONDATORE)\b")


def _looks_like_team_page(html: str) -> bool:
    """An "about" page that is a gallery of people (bios with role labels in capitals)."""
    tree = analyze_html(html)
    text = " ".join(tree.itertext()) if tree is not None else ""
    return len(_ROLE_LABEL.findall(text)) >= 3


def presentation_of(pages: list[dict], *, when: str | None = None) -> dict:
    """{"text", "url", "where", "source", "date"} or {} from the scanned pages."""
    from urllib.parse import urlsplit

    when = when or date.today().isoformat()
    about = [p for p in pages if p.get("kind") == "about" and _ABOUT_PATH.search(urlsplit(p.get("url", "")).path)
             and not _NOT_ABOUT_PATH.search(urlsplit(p.get("url", "")).path)
             and not _looks_like_team_page(p.get("html", ""))]
    for page in about:
        paragraphs = lead_paragraphs(page.get("html", ""), limit=3)
        text = trim(" ".join(paragraphs), PRESENTATION_CHARS)
        if len(text) >= 120:
            return _entry(text, page["url"], "about page, first paragraphs", when)
    homes = [p for p in pages if p.get("kind") == "home_it"] + [p for p in pages if p.get("kind") == "home"]
    for page in homes:
        html = page.get("html", "")
        for org in _jsonld_organizations(html):
            text = _text_of(org.get("description"))
            if acceptable(text):
                return _entry(trim(text, PRESENTATION_CHARS), page["url"], "JSON-LD description", when)
        tree = analyze_html(html)
        if tree is None:
            continue
        for names, where in ((("description",), "meta description"), (("og:description",), "og:description")):
            text = _meta(tree, *names)
            if len(text) >= 40 and not _NOISE_TEXT.search(text):
                return _entry(trim(text, PRESENTATION_CHARS), page["url"], where, when)
    for page in about:  # a short about text is better than nothing
        paragraphs = lead_paragraphs(page.get("html", ""), limit=2)
        if paragraphs:
            return _entry(trim(" ".join(paragraphs), PRESENTATION_CHARS), page["url"],
                          "about page, first paragraphs", when)
    return {}


def history_of(pages: list[dict], *, when: str | None = None) -> dict:
    """{"text", "url", "where", "source", "date"} or {} from the scanned pages."""
    when = when or date.today().isoformat()
    for page in [p for p in pages if p.get("kind") == "history"]:
        paragraphs = lead_paragraphs(page.get("html", ""), limit=6)
        text = trim(" ".join(paragraphs), HISTORY_CHARS)
        if len(text) >= 80:
            return _entry(text, page["url"], "history page", when)
    order = ("about", "home_it", "home")
    for kind in order:
        for page in [p for p in pages if p.get("kind") == kind]:
            picked: list[str] = []
            for paragraph in lead_paragraphs(page.get("html", ""), limit=12):
                for sentence in sentences(paragraph):
                    years = [int(y) for y in _YEAR.findall(sentence)]
                    # a recent year is news ("... inizia il 2026 ..."), not history
                    if years and min(years) < date.today().year - 1 and _FOUNDING.search(sentence) \
                            and sentence not in picked and 30 <= len(sentence) <= 400:
                        picked.append(sentence)
                if len(picked) >= MAX_HISTORY_SENTENCES:
                    break
            if picked:
                return _entry(trim(" ".join(picked), HISTORY_CHARS), page["url"],
                              f"{'about' if kind == 'about' else 'home'} page, sentences with dates", when)
    return {}


def profile_of(pages: list[dict], *, when: str | None = None) -> dict:
    """The card's "profile" section: {"presentation": {...}, "history": {...}, "date"}."""
    when = when or date.today().isoformat()
    return {"presentation": presentation_of(pages, when=when), "history": history_of(pages, when=when),
            "date": when}
