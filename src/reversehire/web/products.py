"""Products and services a company offers to the public, from its own home page
(company card, section 4): a short summary (meta/og description, JSON-LD description or
the first sentences of the main text) and the menu entries that lead to product,
solution or service pages, each with its URL. No extra request: the home page comes
from the Fetcher cache.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .text import analyze_html, compact, extract_links, extract_text, normalize, same_site, truncate, url_key

MAX_ITEMS = 15
SUMMARY_CHARS = 400

_WORDS = (r"prodott\w*|products?|soluzion\w*|solutions?|serviz\w*|services?|macchin\w*|machines?|"
          r"machinery|linee|lines?|tecnologi\w*|technolog\w*|settor\w*|industr(?:y|ies)|applicazion\w*|"
          r"applications?|brands?|marchi|gamma|range|catalog\w*")
_SECTION_PATH = re.compile(rf"(?:^|/)({_WORDS})(?:/|$)", re.I)
_SECTION_WORD = re.compile(rf"(?:{_WORDS})", re.I)
# Menu words that name the section itself, not an item.
_GENERIC = {
    "prodotti", "prodotto", "products", "product", "soluzioni", "solutions", "servizi", "services",
    "macchine", "machines", "tecnologie", "technologies", "settori", "industries", "applicazioni",
    "applications", "brand", "brands", "marchi", "gamma", "catalogo", "catalogue", "catalog",
    "scopri", "scopri di piu", "discover", "discover more", "read more", "leggi di piu", "tutti",
    "all", "vedi tutti", "see all", "home", "news", "contatti", "contacts", "chi siamo", "about us",
}


def _meta(tree, *names: str) -> str:
    for element in tree.iter("meta"):
        key = (element.get("name") or element.get("property") or "").lower()
        if key in names:
            content = compact(element.get("content") or "")
            if content:
                return content
    return ""


def extract_products(html: str, url: str, main_text: str = "") -> dict:
    """{"summary", "summary_url", "items": [{"name", "url"}]} from a home page."""
    tree = analyze_html(html)
    if tree is None:
        return {"summary": "", "summary_url": url, "items": []}
    summary = _meta(tree, "description", "og:description", "twitter:description")
    if not summary:
        text = main_text or extract_text(html, url)
        sentences = re.split(r"(?<=[.!?])\s+", compact(text))
        summary = " ".join(sentences[:3])
    items: list[dict] = []
    seen: set[str] = set()
    for link, text in extract_links(html, url, tree):
        if not same_site(link, url):
            continue
        name = compact(re.sub(r"[​-‏⁠﻿]", "", text))
        folded = normalize(name)
        if not (3 <= len(name) <= 60) or folded in _GENERIC or len(name.split()) > 7:
            continue
        path = urlsplit(link).path
        if not _SECTION_PATH.search(path):
            continue
        # the section page itself ("/en/products/") is not an item
        if _SECTION_WORD.fullmatch(path.rstrip("/").rsplit("/", 1)[-1]):
            continue
        key = url_key(link)
        if key in seen or folded in {normalize(i["name"]) for i in items}:
            continue
        seen.add(key)
        items.append({"name": name, "url": link})
        if len(items) >= MAX_ITEMS:
            break
    return {"summary": truncate(summary, SUMMARY_CHARS) if summary else "", "summary_url": url, "items": items}
