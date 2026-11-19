"""The company's links on the web: website, social networks, company pages on job sites,
Wikipedia/Wikidata and a map search, each with where it was found.

Pure functions (no network, no Qt):
- `classify(url)` -> (network, canonical url) for a company PROFILE link, None for anything
  else: share/intent buttons, posts, personal profiles, the network's own pages;
- `links_for_card(card)` / `links_for_company(company)` -> the ordered `SocialLink` list of
  a company card or of a search result, with provenance.

A profile URL is never guessed: a link is listed as a page only when it was found (on the
company's site, in Wikidata, in OpenStreetMap or in the card data). When the LinkedIn page
is unknown a LinkedIn company SEARCH is offered instead, and the map is always a search by
name + town; both are marked `kind="search"` (the app only opens them in the browser).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

# Display order of the networks.
NETWORKS = ("website", "linkedin", "facebook", "instagram", "x", "youtube", "tiktok", "github", "xing",
            "glassdoor", "indeed", "wikipedia", "wikidata", "maps")
PAGE, SEARCH = "page", "search"
# Short glyph drawn in each chip (monochrome; the full name is in the label and tooltip).
GLYPHS = {"website": "W", "linkedin": "in", "facebook": "f", "instagram": "IG", "x": "X", "youtube": "YT",
          "tiktok": "TT", "github": "GH", "xing": "XI", "glassdoor": "GD", "indeed": "ID", "wikipedia": "Wp",
          "wikidata": "WD", "maps": "M"}
# English names (the GUI uses the locale keys "social.network.<name>").
NAMES = {"website": "Website", "linkedin": "LinkedIn", "facebook": "Facebook", "instagram": "Instagram",
         "x": "X (Twitter)", "youtube": "YouTube", "tiktok": "TikTok", "github": "GitHub", "xing": "XING",
         "glassdoor": "Glassdoor", "indeed": "Indeed", "wikipedia": "Wikipedia", "wikidata": "Wikidata",
         "maps": "Google Maps"}

LINKEDIN_COMPANY_SEARCH = "https://www.linkedin.com/search/results/companies/?keywords="
MAPS_SEARCH = "https://www.google.com/maps/search/?api=1&query="

_HOSTS = {
    "linkedin.com": "linkedin", "facebook.com": "facebook", "fb.com": "facebook", "instagram.com": "instagram",
    "youtube.com": "youtube", "x.com": "x", "twitter.com": "x", "tiktok.com": "tiktok", "github.com": "github",
    "xing.com": "xing", "glassdoor.com": "glassdoor", "glassdoor.it": "glassdoor", "glassdoor.co.uk": "glassdoor",
    "glassdoor.de": "glassdoor", "glassdoor.fr": "glassdoor", "indeed.com": "indeed", "wikipedia.org": "wikipedia",
    "wikidata.org": "wikidata",
}
_SHARE = re.compile(r"share|sharer|intent|/dialog/|/plugins/|/watch|/p/|/posts?/|/status/|/reel/|/video/|/events?/|"
                    r"/hashtag/|/search|/explore/|/stories/|/photo|/groups?/|/embed", re.I)
_OWN_PAGES = re.compile(r"/(?:about|privacy|policy|policies|legal|terms|help|settings|login|signup|home|"
                        r"features|pricing|enterprise|marketplace|topics|trending|business|ads|careers|jobs|"
                        r"developers|i|tr)(?:/|$)", re.I)
_LINKEDIN = re.compile(r"^/(?:company|showcase)/([^/?#]+)", re.I)
_YOUTUBE = re.compile(r"^/(?:(?:channel|c|user)/[^/]+|@[^/]+|[A-Za-z0-9_.-]+)$", re.I)
_TIKTOK = re.compile(r"^/@[^/]+$")
_XING = re.compile(r"^/(?:pages|companies)/[^/]+$", re.I)
_GLASSDOOR = re.compile(r"^/(?:Overview|Panoramica|Reviews|Recensioni)/[^/]*-E(?:I_IE)?\d+[^/]*\.htm$", re.I)
_INDEED = re.compile(r"^/cmp/[^/]+$", re.I)
_WIKIPEDIA = re.compile(r"^/wiki/[^/:]+$")
_WIKIDATA = re.compile(r"^/wiki/(Q[1-9]\d*)$")


@dataclass(frozen=True)
class SocialLink:
    network: str          # one of NETWORKS
    url: str
    kind: str = PAGE      # PAGE (found) or SEARCH (a search opened in the browser)
    source: str = ""      # where it was found: "website", "wikidata", "openstreetmap", "card", "search"
    source_url: str = ""  # the page/item it was read from
    where: str = ""       # e.g. "link", "JSON-LD", "Wikidata item"

    @property
    def is_search(self) -> bool:
        return self.kind == SEARCH

    def to_dict(self) -> dict:
        return {"network": self.network, "url": self.url, "kind": self.kind, "source": self.source,
                "source_url": self.source_url, "where": self.where}


def _host_network(host: str) -> tuple[str, str]:
    host = host.lower()
    for known, name in _HOSTS.items():
        if host == known or host.endswith("." + known):
            return known, name
    return "", ""


def classify(url: str) -> tuple[str, str] | None:
    """(network, canonical url) of a company profile link; None otherwise."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    known, name = _host_network(host)
    if not name:
        return None
    path = parts.path.rstrip("/")
    if name == "linkedin":
        match = _LINKEDIN.match(path)
        return ("linkedin", f"https://www.linkedin.com/company/{match.group(1)}") if match else None
    if name == "wikipedia":
        if not _WIKIPEDIA.match(path) or not re.fullmatch(r"[a-z]{2,3}(?:\.m)?\.wikipedia\.org", host):
            return None
        return "wikipedia", f"https://{host.replace('.m.', '.')}{path}"
    if name == "wikidata":
        match = _WIKIDATA.match(path)
        return ("wikidata", f"https://www.wikidata.org/wiki/{match.group(1)}") if match else None
    if not path or _SHARE.search(path) or parts.query and re.search(r"(?:^|&)(?:u|url|text)=", parts.query):
        return None
    if name == "youtube":
        ok = _YOUTUBE.match(path) and not _OWN_PAGES.match(path)
        return ("youtube", f"https://www.youtube.com{path}") if ok else None
    if name == "tiktok":
        return ("tiktok", f"https://www.tiktok.com{path}") if _TIKTOK.match(path) else None
    if name == "xing":
        return ("xing", f"https://www.xing.com{path}") if _XING.match(path) else None
    if name == "glassdoor":
        return ("glassdoor", f"https://www.{known}{path}") if _GLASSDOOR.match(path) else None
    if name == "indeed":
        return ("indeed", f"https://{host}{path}") if _INDEED.match(path) else None
    if path.count("/") > 1 or _OWN_PAGES.match(path):
        return None  # a deeper page (a post, a photo) or the network's own page
    if name == "github" and not re.fullmatch(r"/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", path):
        return None
    if name == "facebook" and path.lower() in ("/profile.php", "/pages", "/watch"):
        return None
    base = {"x": "https://x.com", "facebook": "https://www.facebook.com", "github": "https://github.com"}.get(name, f"https://www.{known}")
    return name, base + path


def strip_legal_form(name: str) -> str:
    try:
        from ..companies import strip_legal_form as strip
    except Exception:  # noqa: BLE001 - only used for nicer searches
        return name
    return strip(name) or name


def linkedin_search_url(name: str) -> str:
    return LINKEDIN_COMPANY_SEARCH + quote(strip_legal_form(name), safe="")


def maps_search_url(name: str, town: str = "") -> str:
    query = " ".join(p for p in (strip_legal_form(name or "").strip(), (town or "").strip()) if p)
    return MAPS_SEARCH + quote(query, safe="")


class _Builder:
    def __init__(self):
        self.links: list[SocialLink] = []

    def add(self, network: str, url: str, source: str, source_url: str = "", where: str = "",
            kind: str = PAGE) -> None:
        if not url or network not in NETWORKS:
            return
        if any(link.url.rstrip("/").lower() == url.rstrip("/").lower() for link in self.links):
            return
        self.links.append(SocialLink(network, url, kind, source, source_url, where))

    def add_found(self, url: str, source: str, source_url: str = "", where: str = "") -> None:
        found = classify(url)
        if found:
            self.add(found[0], found[1], source, source_url, where)

    def finish(self, name: str, town: str) -> list[SocialLink]:
        if name.strip() and not any(link.network == "linkedin" for link in self.links):
            self.add("linkedin", linkedin_search_url(name), "search", kind=SEARCH)
        if name.strip():
            self.add("maps", maps_search_url(name, town), "search", kind=SEARCH)
        order = {n: i for i, n in enumerate(NETWORKS)}
        return sorted(self.links, key=lambda link: (order[link.network], link.kind != PAGE))


def _site(url: str | None) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    return url if "://" in url else "https://" + url


def _osm_links(builder: _Builder, tags: dict) -> None:
    for key, value in (tags or {}).items():
        key = str(key).lower()
        if not (key.startswith("contact:") or key in ("facebook", "instagram", "twitter", "linkedin", "youtube")):
            continue
        value = str(value or "").strip()
        network = key.split(":", 1)[-1]
        if value and "://" not in value and network in ("facebook", "instagram", "twitter", "linkedin", "youtube",
                                                          "tiktok", "github", "xing"):
            # a bare handle: OSM states it, the URL is the network's standard form
            host = {"twitter": "x.com", "linkedin": "www.linkedin.com/company"}.get(network, f"www.{network}.com")
            value = f"https://{host}/{value.lstrip('@') if network not in ('tiktok',) else '@' + value.lstrip('@')}"
        builder.add_found(value, "openstreetmap", where=key)


def links_for_card(card) -> list[SocialLink]:
    """The links of a company card (companies.CompanyCard), with provenance."""
    builder = _Builder()
    website = _site(getattr(card, "website", "") or getattr(card, "site", ""))
    if website:
        builder.add("website", website, "card")
    registry = getattr(card, "registry", None) or {}
    linkedin = registry.get("linkedin")
    for item in ([linkedin] if isinstance(linkedin, dict) else []) + list(registry.get("social") or []):
        if isinstance(item, dict):
            builder.add_found(str(item.get("value") or ""), str(item.get("source") or ""),
                              str(item.get("url") or ""), str(item.get("where") or ""))
    _osm_links(builder, getattr(card, "osm_tags", None) or {})
    wikidata = registry.get("wikidata")
    qid = str((wikidata or {}).get("value") or "") if isinstance(wikidata, dict) else ""
    if re.fullmatch(r"Q[1-9]\d*", qid):
        builder.add("wikidata", f"https://www.wikidata.org/wiki/{qid}", str(wikidata.get("source") or "wikidata"),
                    str(wikidata.get("url") or ""), str(wikidata.get("where") or ""))
    town = ""
    try:
        town = card.registry_value("town")
    except Exception:  # noqa: BLE001
        town = ""
    if not town:
        town = _town_of_address(getattr(card, "address", "") or "")
    return builder.finish(getattr(card, "name", "") or "", town)


def links_for_company(company) -> list[SocialLink]:
    """The links of a search result (models.Company): its site, OpenStreetMap tags, the
    social links recorded in its provenance and the Wikidata item."""
    builder = _Builder()
    website = _site(getattr(company, "site", None))
    if website:
        builder.add("website", website, "openstreetmap" if getattr(company, "origin", "") == "osm" else "card")
    for fact in getattr(company, "sources", None) or []:
        if isinstance(fact, dict) and fact.get("field") in ("linkedin", "social"):
            builder.add_found(str(fact.get("value") or ""), str(fact.get("source") or ""),
                              str(fact.get("url") or ""), str(fact.get("where") or ""))
    _osm_links(builder, getattr(company, "osm_tags", None) or {})
    qid = str((getattr(company, "ids", None) or {}).get("wikidata") or "")
    if re.fullmatch(r"Q[1-9]\d*", qid):
        builder.add("wikidata", f"https://www.wikidata.org/wiki/{qid}", "wikidata")
    fields = getattr(company, "fields", None) or {}
    town = str(fields.get("town") or "") or _town_of_address(getattr(company, "address", "") or "")
    return builder.finish(getattr(company, "name", "") or "", town)


def _town_of_address(address: str) -> str:
    """The town of an Italian address ("Via Roma 1, 40100 Bologna (BO)" -> "Bologna")."""
    match = re.search(r"\b\d{5}\s+([^,(]+)", address or "")
    if match:
        return match.group(1).strip()
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    return parts[-1] if len(parts) > 1 else ""


def provenance_text(link: SocialLink, translate=None) -> str:
    """Where the link comes from, for tooltips and exports ("found on the website (link)")."""
    tr = translate or (lambda key, default, **kw: default.format(**kw))
    if link.is_search:
        return tr("social.tip.search", "Search (opens your browser; nothing is read by the app)")
    source = {"website": tr("social.source.website", "the company website"),
              "website_jsonld": tr("social.source.website", "the company website"),
              "wikidata": "Wikidata", "openstreetmap": "OpenStreetMap",
              "card": tr("social.source.card", "the card data")}.get(link.source, link.source or "?")
    text = tr("social.tip.found", "Found on {source}", source=source)
    if link.where:
        text += f" ({link.where})"
    if link.source_url:
        text += f": {link.source_url}"
    return text
