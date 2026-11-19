"""Verification of the assistant's suggestions: nothing it says is shown as a fact
until the app has checked it against a named source.

Steps of `verify_suggestion` (every network access goes through the app's polite
helpers and counts against a request budget):

1. Website. A suggested website is read with the app's Fetcher (public hosts only,
   robots.txt, per-host delay, redirects checked hop by hop, page cache). It must be a
   real company site that carries the company's name: in the title, the og:site_name,
   the schema.org data or the legal block (similarity >= 0.85, legal-form words
   ignored), or in the page text together with the domain name. Parked / for-sale
   domains are rejected. When no website is given, or it fails, the website is looked
   for with the existing discovery data only: the local OpenStreetMap index (a company
   of the same name near the suggested municipality, `find_place` + `radius`) and
   Wikidata (`wbsearchentities` by name, accepted only for exactly one item of the same
   name; its official website must then pass the same check). Never a search engine.
2. Identity. The site's identity pages are scanned with `web.identity.scan_identity`
   (the card builder's scanner); a VAT number printed there is checked in VIES
   (`registry.vies.lookup`), a company tax code in GLEIF (`registry.gleif.by_tax_code`),
   and the facts are judged with `registry.confirm.evaluate` (the card's rule).
3. Geography. The registered office (VIES/GLEIF), the map position or the suggested
   municipality (resolved with the place index) is compared with the requested area.
4. Status:
   VERIFIED   site reachable + name on it + (register identity, OR the same company
              on the map, OR the Wikidata item with the same website);
   PARTIAL    site reachable + name on it, no register/map/Wikidata confirmation;
   UNVERIFIED nothing confirmed (no reachable site with the name): shown greyed, never
              added automatically;
   REJECTED   contradicted: the site belongs to another company, or the company is
              outside the requested area.
   Absence of evidence is never "does not exist": that stays UNVERIFIED.

Only VERIFIED and PARTIAL give a company seed (`to_company_seed`, the keyword
arguments of ``companies.add_manual`` plus "extra"), with the provenance line
"suggested by the assistant on <date>, verified by: ...". The assistant's "why" is
stored as its OPINION, labelled, never as a fact.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Callable
from urllib.parse import urlsplit

from .brief import AREA_COUNTRY, AREA_PROVINCE, AREA_RADIUS, AREA_REGION, SearchArea
from .prompt import Suggestion, host_key
from .texts import text

log = logging.getLogger("reversehire.suggest")

VERIFIED, PARTIAL, UNVERIFIED, REJECTED = "verified", "partial", "unverified", "rejected"
STATUSES = (VERIFIED, PARTIAL, UNVERIFIED, REJECTED)

NAME_MATCH = 0.85
OTHER_COMPANY = 0.5
MAP_NAME_MATCH = 0.85
_LEGAL_WORDS = {"srl", "spa", "srls", "snc", "sas", "societa", "group", "gruppo", "italia", "the"}
WIKIDATA_NAME_MATCH = 0.9
MAP_RADIUS_KM = 12.0
MAX_EXTRA_PAGES = 2          # identity pages read after the home page
MAX_VIES_CALLS = 2
DEFAULT_REQUESTS = 8         # per suggestion
RADIUS_TOLERANCE = 1.15      # a company just outside the radius is not "outside"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"

_PARKED_WORDS = (
    "domain is for sale", "domain for sale", "this domain may be for sale", "buy this domain",
    "questo dominio e in vendita", "dominio in vendita", "il dominio e in vendita", "dominio acquistabile",
    "parked free", "parked domain", "domain parking", "this domain is parked", "sedo domain parking",
    "is available for purchase", "make an offer on this domain", "inquire about this domain",
    "this site is under construction", "website coming soon", "sito in costruzione",
    "default web site page", "welcome to nginx", "apache2 default page", "it works!",
    "account suspended", "this account has been suspended",
)
_PARKING_HOSTS = ("sedo.com", "sedoparking.com", "dan.com", "afternic.com", "hugedomains.com", "bodis.com",
                  "parkingcrew.net", "above.com", "undeveloped.com", "godaddy.com", "domainmarket.com",
                  "uniregistry.com", "parklogic.com", "buydomains.com")


class _Budget:
    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self.used = 0
        self.hit = False

    def take(self) -> bool:
        if self.used >= self.limit:
            self.hit = True
            return False
        self.used += 1
        return True


class _BudgetFetcher:
    """The Fetcher with a request budget: over budget, a page is "not readable" (None)."""

    def __init__(self, fetcher, budget: _Budget, cancel: threading.Event | None):
        self._fetcher = fetcher
        self._budget = budget
        self._cancel = cancel

    def _sent(self) -> int | None:
        counter = getattr(self._fetcher, "requests_per_domain", None)
        try:
            return int(sum(counter.values())) if counter is not None else None
        except (TypeError, ValueError, AttributeError):
            return None

    def fetch_page(self, url: str, *args, **kwargs):
        if (self._cancel is not None and self._cancel.is_set()) or not self._budget.take():
            return None
        before = self._sent()
        page = self._fetcher.fetch_page(url, *args, **kwargs)
        after = self._sent()
        if before is not None and after is not None:
            # the real HTTP requests: robots.txt, redirect hops (a cached page: none)
            self._budget.used += (after - before) - 1
        return page

    def safe_url(self, url: str) -> bool:
        safe = getattr(self._fetcher, "safe_url", None)
        return bool(safe(url)) if callable(safe) else True

    def __getattr__(self, name):
        return getattr(self._fetcher, name)


class _BudgetRegistry:
    def __init__(self, client, budget: _Budget, cancel: threading.Event | None):
        self._client = client
        self._budget = budget
        self._cancel = cancel

    def get_json(self, *args, **kwargs):
        from ..registry.http import RegistryError

        if self._cancel is not None and self._cancel.is_set():
            raise RegistryError("unavailable", "cancelled by the verification")
        if not self._budget.take():
            raise RegistryError("unavailable", "request budget reached")
        return self._client.get_json(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._client, name)


class _BudgetGateway:
    def __init__(self, gateway, budget: _Budget, cancel: threading.Event | None):
        self._gateway = gateway
        self._budget = budget
        self._cancel = cancel

    def get(self, url: str, **kwargs):
        from ..discovery.gateway import GatewayError

        if not self._budget.take():
            raise GatewayError("request budget reached")
        if self._cancel is not None:
            kwargs.setdefault("cancel", self._cancel)
        return self._gateway.get(url, **kwargs)

    def __getattr__(self, name):
        return getattr(self._gateway, name)


@dataclass
class Reason:
    key: str
    params: dict = field(default_factory=dict)
    ok: bool | None = None     # True = confirms, False = contradicts / failed, None = information

    @property
    def message(self) -> str:
        return text(self.key, **self.params)


@dataclass
class VerifiedSuggestion:
    suggestion: Suggestion
    status: str = UNVERIFIED
    reasons: list[Reason] = field(default_factory=list)
    site: str | None = None          # verified website (after redirects)
    site_source: str = ""            # "assistant" | "osm" | "wikidata"
    name_on_site: str = ""
    legal_name: str = ""             # from VIES / GLEIF
    vat: str = ""
    identity_status: str = ""        # registry.confirm status
    town: str = ""
    province: str = ""
    lat: float | None = None
    lon: float | None = None
    osm_id: str = ""
    wikidata: str = ""
    sources: list[str] = field(default_factory=list)   # named sources that confirmed something
    requests: int = 0
    checked_on: str = field(default_factory=lambda: date.today().isoformat())
    cli: str = ""

    def reason_lines(self) -> list[str]:
        return [r.message for r in self.reasons]

    @property
    def addable(self) -> bool:
        return self.status in (VERIFIED, PARTIAL)

    def verified_by(self) -> list[str]:
        """Human list of the checks that succeeded (for the provenance line)."""
        out = []
        if self.site and self.name_on_site:
            out.append(f"website {host_key(self.site)} (name on the page)")
        if "vies" in self.sources and self.vat:
            out.append(f"VIES (VAT {self.vat})")
        if "gleif" in self.sources:
            out.append("GLEIF")
        if "osm" in self.sources:
            out.append("OpenStreetMap" + (f" {self.osm_id}" if self.osm_id else ""))
        if "wikidata" in self.sources and self.wikidata:
            out.append(f"Wikidata {self.wikidata}")
        if "place" in self.sources:
            out.append("place index (area)")
        return out

    def provenance(self) -> str:
        from ..chat.messages import CLI_NAMES

        cli = CLI_NAMES.get(self.cli, self.cli or "assistant")
        checks = ", ".join(self.verified_by()) or "nothing"
        return text("suggest.provenance", cli=cli, date=self.checked_on, checks=checks)

    def opinion(self) -> str:
        return text("suggest.opinion", why=self.suggestion.why) if self.suggestion.why else ""

    def to_company_seed(self) -> dict | None:
        """Keyword arguments for ``companies.add_manual`` (pop "extra" first), or None
        when the suggestion is not VERIFIED/PARTIAL."""
        if not self.addable:
            return None
        name = self.legal_name if (self.legal_name and "vies" in self.sources) else self.suggestion.name
        address = ", ".join(x for x in (self.town, self.province) if x)
        notes = "\n".join(x for x in (self.provenance(), self.opinion()) if x)
        return {
            "name": name,
            "site": self.site,
            "address": address,
            "lat": self.lat,
            "lon": self.lon,
            "sectors": None,
            "description": "",
            "notes": notes,
            "tags": [text("suggest.tag")],
            "extra": {"origin": "assistant", "status": self.status, "cli": self.cli,
                      "suggested_name": self.suggestion.name, "company_type": self.suggestion.company_type,
                      "direction": self.suggestion.direction, "assistant_confidence": self.suggestion.confidence,
                      "assistant_opinion": self.suggestion.why, "verified_by": self.verified_by(),
                      "sources": list(self.sources), "vat": self.vat, "identity_status": self.identity_status,
                      "wikidata": self.wikidata, "osm_id": self.osm_id, "checked_on": self.checked_on},
        }

    def to_dict(self) -> dict:
        data = asdict(self)
        data["suggestion"] = self.suggestion.to_dict()
        return data

    @classmethod
    def from_dict(cls, d: dict) -> "VerifiedSuggestion":
        d = dict(d or {})
        result = cls(suggestion=Suggestion.from_dict(d.get("suggestion") or {"name": "?"}))
        for key in cls.__dataclass_fields__:
            if key in ("suggestion", "reasons") or key not in d:
                continue
            setattr(result, key, d[key])
        result.reasons = [Reason(**r) for r in d.get("reasons") or [] if isinstance(r, dict)]
        return result


# --- website ---------------------------------------------------------------------------------


@dataclass
class SiteCheck:
    state: str                      # ok | unreachable | not_allowed | parked | other_company | name_missing
    url: str = ""
    final_url: str = ""
    page: object = None
    matched: str = ""               # the name seen on the site
    where: str = ""                 # "title", "site name", "legal name", "page text and domain"
    strong: bool = False
    other: str = ""                 # another company's legal name (state other_company)


def _fold(value: str) -> str:
    from ..web.text import normalize

    return normalize(value)


def strict_similarity(a: str, b: str) -> float:
    """Name similarity without the token-SUBSET shortcut of `confirm.name_similarity`
    ("Gamma" is not "Gamma Visione Industriale"; a sentence that contains the name is not
    the name): the larger of the token Jaccard index and the ratio of the sorted tokens.
    Legal-form words are ignored, as in the card's rule."""
    from difflib import SequenceMatcher

    from ..registry.confirm import name_tokens

    ta, tb = set(name_tokens(a)), set(name_tokens(b))
    if not ta or not tb:
        return 0.0
    jaccard = len(ta & tb) / len(ta | tb)
    ratio = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return max(jaccard, ratio)


def name_variants(name: str) -> list[str]:
    """The suggested name and its core: without "(...)", without " - qualifier" / ", ...".
    "Soilmec (Gruppo Trevi)" -> ["Soilmec (Gruppo Trevi)", "Soilmec"];
    "Ferretti Group - cantiere di Forlì" -> [..., "Ferretti Group"]."""
    from ..registry.confirm import name_tokens

    out: list[str] = []
    base = " ".join(str(name or "").split())
    for candidate in (base, re.sub(r"\s*\([^)]*\)", "", base),
                      re.split(r"\s+[-–—|]\s+|,\s+", re.sub(r"\s*\([^)]*\)", "", base))[0]):
        candidate = candidate.strip(" -–,")
        if candidate and name_tokens(candidate) and candidate not in out:
            out.append(candidate)
    return out or [base]


def best_similarity(name: str, other: str) -> float:
    return max(strict_similarity(v, other) for v in name_variants(name))


def _contained(name: str, register_name: str) -> bool:
    """Every token of a core variant appears in the register's legal name ("Orogel" in
    "OROGEL SOCIETA' COOPERATIVA AGRICOLA"). Used only after a strong website match."""
    from ..registry.confirm import name_tokens

    register = set(name_tokens(register_name))
    return any(set(name_tokens(v)) and set(name_tokens(v)) <= register for v in name_variants(name))


def _last_segment(name: str) -> str:
    """The text right before an identifier is often glued to the previous sentence: keep
    the last segment ("... in tutta Italia. Example S.r.l." -> "Example S.r.l.")."""
    parts = [p.strip() for p in re.split(r"(?<![A-Z])[.!?]\s+(?=[A-Z0-9])|\s+[|–—-]\s+", name) if p.strip()]
    return parts[-1] if parts else name.strip()


def _title_names(html: str) -> list[tuple[str, str]]:
    from ..web.text import analyze_html

    tree = analyze_html(html)
    if tree is None:
        return []
    names: list[tuple[str, str]] = []
    for element in tree.iter("title"):
        title = " ".join((element.text_content() or "").split())
        for part in re.split(r"\s+[|\-–—:·»]\s+|\s*\|\s*", title):
            if part.strip():
                names.append((part.strip(), "title"))
        break
    for meta in tree.iter("meta"):
        prop = (meta.get("property") or meta.get("name") or "").lower()
        if prop in ("og:site_name", "application-name", "og:title"):
            value = " ".join((meta.get("content") or "").split())
            if value:
                names.append((value, "site name"))
    return names


def _legal_names(html: str, url: str, visible: str) -> list[str]:
    from ..web.identity import extract_from_jsonld, extract_identifiers

    names = []
    try:
        for item in extract_from_jsonld(html, url) + extract_identifiers(html, url, visible):
            value = _last_segment(item.value) if item.field == "name" else ""
            if value and value not in names:
                names.append(value)
    except Exception:  # noqa: BLE001 - extraction must never break the check
        log.debug("Suggest: legal names not extracted", exc_info=True)
    return names


def _host_label(url: str) -> str:
    host = host_key(url)
    parts = host.split(".")
    return parts[-2] if len(parts) >= 2 else host


def _host_matches(url: str, name: str) -> bool:
    from ..registry.confirm import name_tokens

    tokens = name_tokens(name)
    label = re.sub(r"[^a-z0-9]", "", _host_label(url))
    joined = "".join(tokens)
    if not tokens or len(label) < 3:
        return False
    return label == joined or (joined.startswith(label) and label == tokens[0]) or (
        len(label) >= 5 and label in joined) or (len(joined) >= 5 and joined in label)


def _parked(page, visible: str) -> bool:
    host = host_key(getattr(page, "final_url", "") or "")
    if any(host == p or host.endswith("." + p) for p in _PARKING_HOSTS):
        return True
    folded = _fold(visible)[:4000]
    return any(word in folded for word in _PARKED_WORDS)


def check_site(url: str, name: str, fetcher) -> SiteCheck:
    """Reads `url` and decides whether it is the website of the company `name`."""
    from ..registry.confirm import name_tokens
    from ..web.identity import legal_form_of
    from ..web.text import visible_text

    check = SiteCheck("unreachable", url=url)
    safe = getattr(fetcher, "safe_url", None)
    if callable(safe) and not safe(url):
        check.state = "not_allowed"
        return check
    page = fetcher.fetch_page(url)
    if page is None:
        return check
    check.page = page
    check.final_url = getattr(page, "final_url", "") or url
    html = getattr(page, "html", "") or ""
    visible = visible_text(html) or (getattr(page, "text", "") or "")
    if _parked(page, visible):
        check.state = "parked"
        return check
    best, best_name, where = 0.0, "", ""
    titles = _title_names(html)
    for candidate, kind in titles:
        score = best_similarity(name, candidate)
        if score > best:
            best, best_name, where = score, candidate, kind
    legal = _legal_names(html, check.final_url, visible)
    for candidate in legal:
        score = best_similarity(name, candidate)
        if score > best:
            best, best_name, where = score, candidate, "legal name"
    page_words = set(re.findall(r"[a-z0-9]+", _fold(visible)))
    variants = name_variants(name)
    in_text = any(name_tokens(v) and all(t in page_words for t in name_tokens(v)) for v in variants)
    host_ok = any(_host_matches(check.final_url, v) or _host_matches(url, v) for v in variants)
    if best >= NAME_MATCH:
        check.state, check.matched, check.where, check.strong = "ok", best_name, where, True
        return check
    if in_text and host_ok:
        check.state, check.matched, check.where = "ok", name, "page text and domain"
        return check
    if best >= 0.6 and host_ok:
        check.state, check.matched, check.where = "ok", best_name, f"{where} and domain"
        return check
    # another company's legal name (with a legal form) and no mention of this name at all:
    # the site belongs to someone else, even when the domain was built from the name
    others = [n for n in legal + [t for t, _k in titles] if legal_form_of(n)
              and best_similarity(name, n) < OTHER_COMPANY]
    if others and not in_text:
        check.state, check.other = "other_company", others[0]
        return check
    check.state = "name_missing"
    return check


# --- alternatives: map and Wikidata -----------------------------------------------------------


@dataclass
class MapMatch:
    name: str
    site: str | None
    lat: float | None
    lon: float | None
    province: str = ""
    town: str = ""
    osm_id: str = ""


def map_lookup(place_index, suggestion: Suggestion) -> MapMatch | None:
    """The company of the same name near the suggested municipality in the local map index."""
    name_similarity = best_similarity

    if place_index is None or not suggestion.municipality:
        return None
    try:
        place = place_index.find_place(suggestion.municipality, suggestion.province or "")
        if not place:
            return None
        lat, lon, _province = place
        best, best_score = None, 0.0
        for company in place_index.radius(lat, lon, MAP_RADIUS_KM):
            extra = getattr(company, "fields", None) or {}
            names = [company.name, extra.get("brand", ""), extra.get("operator", "")]
            score = max(name_similarity(suggestion.name, n) for n in names if n) if any(names) else 0.0
            if score > best_score:
                best, best_score = company, score
        if best is None or best_score < MAP_NAME_MATCH:
            return None
        extra = getattr(best, "fields", None) or {}
        return MapMatch(name=best.name, site=best.site, lat=best.lat, lon=best.lon,
                        province=extra.get("province", ""), town=extra.get("municipality") or extra.get("town", ""),
                        osm_id=str((best.osm_tags or {}).get("osm_id", "")))
    except Exception:  # noqa: BLE001 - the map is optional
        log.info("Suggest: map lookup failed", exc_info=True)
        return None


@dataclass
class RegisterMatch:
    name: str
    cf: str
    town: str
    province: str


def register_lookup(register_db, suggestion: Suggestion, area: SearchArea | None = None) -> RegisterMatch | None:
    """The company of the same name in the regional register (local, no network): in the
    suggested province (or the area's), preferring the suggested municipality."""
    if register_db is None or not suggestion.name:
        return None
    try:
        province = (suggestion.province or (area.province if area is not None else "") or "").strip().upper()
        words = [w for w in _fold(suggestion.name).split() if len(w) >= 3 and w not in _LEGAL_WORDS][:2]
        if not words:
            return None
        found = register_db.find(text=" ".join(words), province=province or None, only_active=True, limit=40)
        best, best_score = None, 0.0
        for record in found:
            score = best_similarity(suggestion.name, record.name)
            town = _fold(str(record.get("municipality") or ""))
            if suggestion.municipality and town and _fold(suggestion.municipality) == town:
                score += 0.05
            if score > best_score:
                best, best_score = record, score
        if best is None or best_score < MAP_NAME_MATCH:
            return None
        return RegisterMatch(name=best.name, cf=str(best.cf or best.key), town=str(best.get("municipality") or ""),
                             province=str(best.get("province") or province))
    except Exception:  # noqa: BLE001 - the register is optional
        log.info("Suggest: register lookup failed", exc_info=True)
        return None


@dataclass
class WikidataMatch:
    qid: str
    label: str
    website: str | None
    vat: str = ""


def wikidata_lookup(gateway, suggestion: Suggestion, language: str = "it", cancel=None) -> WikidataMatch | None:
    """Exactly one Wikidata item labelled like the company; its official website and VAT
    come from `registry.wikidata.lookup` (same request and cache as the card)."""
    from ..registry import wikidata
    from .prompt import normalize_website

    name_similarity = best_similarity

    if gateway is None:
        return None
    try:
        kwargs = {"params": {"action": "wbsearchentities", "search": suggestion.name, "language": language,
                             "uselang": language, "type": "item", "limit": "7", "format": "json"},
                  "cache_ttl_s": wikidata.TTL_S}
        if cancel is not None:
            kwargs["cancel"] = cancel
        reply = gateway.get(WIKIDATA_API, **kwargs)
        if getattr(reply, "status", 0) != 200:
            return None
        hits = (reply.json() or {}).get("search") or []
        same = [h for h in hits if isinstance(h, dict)
                and name_similarity(suggestion.name, str(h.get("label") or "")) >= WIKIDATA_NAME_MATCH]
        if len(same) != 1:
            return None
        qid = str(same[0].get("id") or "")
        identity = wikidata.lookup(gateway, qid=qid, cancel=cancel)
        if identity.state != "found":
            return None
        website = next((f["value"] for f in identity.facts if f["field"] == "website"), None)
        vat = next((f["value"] for f in identity.facts if f["field"] == "vat"), "")
        return WikidataMatch(qid=qid, label=str(same[0].get("label") or ""), website=normalize_website(website),
                             vat=vat)
    except Exception as error:  # noqa: BLE001 - Wikidata is optional (Cancelled included)
        log.info("Suggest: Wikidata lookup failed (%s)", type(error).__name__)
        return None


# --- area ---------------------------------------------------------------------------------------


def area_contains(area: SearchArea | None, *, province: str = "", lat: float | None = None,
                  lon: float | None = None, country: str = "") -> bool | None:
    """True / False when the place can be judged against the area, None when unknown."""
    if area is None:
        return None
    if area.kind == AREA_COUNTRY:
        if country:
            return country.upper() == (area.country or "IT").upper()
        return True if province and len(province) == 2 and (area.country or "IT").upper() == "IT" else None
    if area.kind == AREA_PROVINCE:
        return province.upper() == area.province.upper() if province and area.province else None
    if area.kind == AREA_REGION:
        if province and area.provinces:
            return province.upper() in {p.upper() for p in area.provinces}
        return None
    if area.kind == AREA_RADIUS:
        if lat is None or lon is None or area.lat is None or area.lon is None or not area.radius_km:
            return None
        from ..geo.distance import haversine_km

        return haversine_km(area.lat, area.lon, lat, lon) <= float(area.radius_km) * RADIUS_TOLERANCE
    return None


# --- the whole check ------------------------------------------------------------------------


def verify_suggestion(s: Suggestion, *, fetcher, gateway=None, registry=None, place_index=None,
                      register_db=None, area: SearchArea | None = None, cancel: threading.Event | None = None,
                      max_requests: int = DEFAULT_REQUESTS, cli: str = "", today: str | None = None,
                      language: str = "it") -> VerifiedSuggestion:
    """Checks one suggestion (see the module docstring). Never raises.

    `fetcher`: ``web.fetch.Fetcher`` (or anything with ``fetch_page``); `gateway`:
    ``discovery.PoliteGateway`` (Wikidata); `registry`: ``registry.http.RegistryClient``
    (VIES, GLEIF); `place_index`: ``discovery.osm_extract.OsmIndex`` (``find_place``,
    ``radius``); all optional except the fetcher. `max_requests` caps the requests of
    this suggestion (robots.txt and cached answers not counted)."""
    result = VerifiedSuggestion(suggestion=s, cli=cli, checked_on=today or date.today().isoformat())
    budget = _Budget(max_requests)
    try:
        _verify(result, s, _BudgetFetcher(fetcher, budget, cancel),
                _BudgetGateway(gateway, budget, cancel) if gateway is not None else None,
                _BudgetRegistry(registry, budget, cancel) if registry is not None else None,
                place_index, area, cancel, language, register_db)
    except Exception:  # noqa: BLE001 - never into the caller
        log.exception("Suggest: verification of a suggestion failed")
        if result.status not in (REJECTED,):
            result.status = UNVERIFIED
    if budget.hit:
        result.reasons.append(Reason("suggest.reason.budget", {}, None))
    if cancel is not None and cancel.is_set() and result.status != REJECTED:
        result.reasons.append(Reason("suggest.reason.cancelled", {}, None))
    result.requests = budget.used
    log.info("Suggestion checked: status=%s sources=%s requests=%d", result.status, ",".join(result.sources),
             budget.used)
    return result


def _cancelled(cancel) -> bool:
    return cancel is not None and cancel.is_set()


def _verify(result: VerifiedSuggestion, s: Suggestion, fetcher, gateway, registry, place_index, area,
            cancel, language, register_db=None) -> None:
    reasons = result.reasons
    site_check: SiteCheck | None = None
    contradicted = False
    # 1. website given by the assistant
    if s.website:
        site_check = check_site(s.website, s.name, fetcher)
        _site_reason(reasons, site_check, s.website)
        if site_check.state == "ok":
            result.site_source = "assistant"
        elif site_check.state == "other_company":
            contradicted = True
    # 2. the local map index (no network): the same company near the suggested town
    map_match = map_lookup(place_index, s) if not _cancelled(cancel) else None
    if map_match is not None:
        result.sources.append("osm")
        result.osm_id = map_match.osm_id
        result.lat, result.lon = map_match.lat, map_match.lon
        result.town = map_match.town or s.municipality
        result.province = map_match.province or result.province
        reasons.append(Reason("suggest.reason.map_match", {"name": map_match.name,
                                                           "town": result.town or s.municipality}, True))
        if (site_check is None or site_check.state != "ok") and map_match.site and not _cancelled(cancel) \
                and host_key(map_match.site) != host_key(s.website):
            candidate = check_site(map_match.site, s.name, fetcher)
            if candidate.state == "ok":
                site_check, result.site_source, contradicted = candidate, "osm", False
                reasons.append(Reason("suggest.reason.site_from_map", {"site": map_match.site}, None))
                _site_reason(reasons, candidate, map_match.site)
    # 2b. the regional register (local): the same company in the province, its tax code
    register_match = register_lookup(register_db, s, area) if not _cancelled(cancel) else None
    if register_match is not None:
        result.sources.append("register")
        result.town = result.town or register_match.town
        result.province = result.province or register_match.province
        if not result.vat and register_match.cf.isdigit() and len(register_match.cf) == 11:
            result.vat = register_match.cf
        result.legal_name = result.legal_name or register_match.name
        reasons.append(Reason("suggest.reason.register_match", {"name": register_match.name,
                                                                "town": register_match.town or s.municipality},
                              True))
    # 3. identity from the site's own pages and the free registers
    identity_ok = False
    other = ""
    if site_check is not None and site_check.state == "ok" and not _cancelled(cancel):
        identity_ok, other = _identity(result, s, site_check, fetcher, registry, cancel)
    # 4. Wikidata: an alternative website, or a confirmation of the one found
    needs_wiki = site_check is None or site_check.state != "ok" or not (identity_ok or map_match is not None)
    if gateway is not None and needs_wiki and not _cancelled(cancel):
        wiki = wikidata_lookup(gateway, s, language=language, cancel=cancel)
        if wiki is not None and wiki.website:
            if site_check is not None and site_check.state == "ok":
                if host_key(wiki.website) == host_key(site_check.final_url or site_check.url):
                    result.sources.append("wikidata")
                    result.wikidata = wiki.qid
                    reasons.append(Reason("suggest.reason.wikidata_match", {"qid": wiki.qid}, True))
            elif not _cancelled(cancel):
                candidate = check_site(wiki.website, s.name, fetcher)
                if candidate.state == "ok":
                    site_check, result.site_source, contradicted = candidate, "wikidata", False
                    result.sources.append("wikidata")
                    result.wikidata = wiki.qid
                    reasons.append(Reason("suggest.reason.site_from_wikidata",
                                          {"qid": wiki.qid, "site": wiki.website}, None))
                    _site_reason(reasons, candidate, wiki.website)
                    identity_ok, other = _identity(result, s, candidate, fetcher, registry, cancel)
    site_ok = site_check is not None and site_check.state == "ok"
    if site_check is None:
        reasons.append(Reason("suggest.reason.no_site", {}, False))
    if site_ok:
        result.site = _site_root(site_check.final_url or site_check.url)
        result.name_on_site = site_check.matched
        result.sources.insert(0, "website")
        if other and not site_check.strong and not identity_ok:
            # the site only mentions the name, and its VAT number belongs to someone else
            contradicted = True
            reasons.append(Reason("suggest.reason.site_other_company",
                                  {"site": host_key(result.site), "other": other}, False))
    # 5. geography
    if _geography(result, s, place_index, area) is False:
        contradicted = True
    # 6. status
    if contradicted:
        result.status = REJECTED
    elif site_ok and (identity_ok or "osm" in result.sources or "wikidata" in result.sources
                      or "register" in result.sources):
        result.status = VERIFIED
    elif site_ok or "register" in result.sources:
        # no readable site, but an official register lists it in the area: it exists there
        result.status = PARTIAL
    else:
        result.status = UNVERIFIED


def _site_root(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/" if parts.scheme and parts.netloc else url


def _site_reason(reasons: list[Reason], check: SiteCheck, url: str) -> None:
    site = host_key(check.final_url or url) or url
    key = {"ok": "suggest.reason.site_ok", "unreachable": "suggest.reason.site_unreachable",
           "not_allowed": "suggest.reason.site_not_allowed", "parked": "suggest.reason.site_parked",
           "other_company": "suggest.reason.site_other_company",
           "name_missing": "suggest.reason.site_name_missing"}[check.state]
    reasons.append(Reason(key, {"site": site, "where": check.where, "other": check.other}, check.state == "ok"))


def _identity(result: VerifiedSuggestion, s: Suggestion, site_check: SiteCheck, fetcher, registry,
              cancel) -> tuple[bool, str]:
    """(identity confirmed, name of another company the site's VAT belongs to)."""
    from ..registry import confirm, gleif, vies
    from ..registry.checksums import cf_ok, is_personal_cf, normalize_vat, piva_ok
    from ..web.identity import scan_identity

    # keep one request of the budget for VIES: the identity pages are read with what is left
    budget = getattr(fetcher, "_budget", None)
    left = (budget.limit - budget.used) if budget is not None else MAX_EXTRA_PAGES + 1
    extra = max(0, min(MAX_EXTRA_PAGES, left - 1 if registry is not None else left))
    site = _site_root(site_check.final_url or site_check.url)
    scan = scan_identity(site, fetcher, cancel=cancel, max_extra=extra, home=site_check.page)
    facts = scan.facts()
    towns = [f["value"] for f in facts if f.get("field") == "town"]
    provinces = [f["value"] for f in facts if f.get("field") == "province"]
    if towns and not result.town:
        result.town = towns[0]
    if provinces and not result.province:
        result.province = provinces[0]
    own_names = [s.name] + ([site_check.matched] if site_check.matched and site_check.strong else [])
    vats: list[str] = []
    for fact in facts:
        number = normalize_vat(fact.get("value", ""))
        if fact.get("field") in ("vat", "tax_code") and piva_ok(number) and number not in vats:
            vats.append(number)
    other = ""
    chosen = None
    if registry is not None:
        for number in vats[:MAX_VIES_CALLS]:
            if _cancelled(cancel):
                break
            try:
                answer = vies.lookup(registry, number)
            except Exception:  # noqa: BLE001 - cancellation or an unexpected client error
                break
            if not answer.valid:
                continue
            score = max(best_similarity(n, answer.name) for n in own_names)
            if score >= confirm.NAME_THRESHOLD or (site_check.strong and _contained(s.name, answer.name)):
                chosen = answer
                break
            other = other or answer.name
        if chosen is not None:
            other = ""
            facts += chosen.facts()
            result.vat, result.legal_name = chosen.vat, chosen.name
            result.sources.append("vies")
            result.reasons.append(Reason("suggest.reason.vies_match", {"vat": chosen.vat, "name": chosen.name}, True))
            if chosen.town:
                result.town = chosen.town
            if chosen.province:
                result.province = chosen.province
        elif other:
            result.reasons.append(Reason("suggest.reason.vies_other", {"vat": vats[0], "name": other}, False))
        codes = [f["value"].upper() for f in facts if f.get("field") == "tax_code" and cf_ok(f["value"].upper())
                 and not is_personal_cf(f["value"])]
        if chosen is None and codes and not _cancelled(cancel):
            try:
                answer = gleif.by_tax_code(registry, codes[0])
            except Exception:  # noqa: BLE001
                answer = None
            record = answer.records[0] if answer is not None and answer.records else None
            if record is not None and max(best_similarity(n, record.legal_name) for n in own_names) \
                    >= confirm.NAME_THRESHOLD:
                facts += record.facts()
                result.legal_name = result.legal_name or record.legal_name
                result.sources.append("gleif")
                if record.province:
                    result.province = record.province
                if record.town:
                    result.town = record.town
    identity = confirm.evaluate(facts, s.name)
    result.identity_status = identity.status
    result.reasons.append(Reason("suggest.reason.identity", {"status": identity.status}, None))
    confirmed = identity.status == confirm.CONFIRMED or (
        identity.status == confirm.PROBABLE and "id_matches_register" in identity.reasons)
    return confirmed or chosen is not None, other


def _geography(result: VerifiedSuggestion, s: Suggestion, place_index, area) -> bool | None:
    """True inside, False outside (contradiction), None unknown; adds the reason."""
    if area is None:
        return None
    # evidence: register / map / site first; the assistant's own municipality as a fallback
    evidence_province = result.province.upper() if len(result.province or "") == 2 else ""
    lat, lon = result.lat, result.lon
    town = result.town
    if (lat is None or not evidence_province) and town and place_index is not None:
        try:
            place = place_index.find_place(town, evidence_province)
        except Exception:  # noqa: BLE001
            place = None
        if place:
            lat = lat if lat is not None else place[0]
            lon = lon if lon is not None else place[1]
            evidence_province = evidence_province or (place[2] or "")
    has_evidence = bool(result.sources) and (evidence_province or lat is not None)
    inside = area_contains(area, province=evidence_province, lat=lat, lon=lon) if has_evidence else None
    claim_inside = None
    claim_place = None
    if s.municipality and place_index is not None:
        try:
            claim_place = place_index.find_place(s.municipality, s.province if len(s.province or "") == 2 else "")
        except Exception:  # noqa: BLE001
            claim_place = None
    if claim_place:
        claim_inside = area_contains(area, province=claim_place[2] or "", lat=claim_place[0], lon=claim_place[1])
    elif len(s.province or "") == 2:
        claim_inside = area_contains(area, province=s.province, country=s.country)
    if inside is True or (inside is None and claim_inside is True):
        shown_town = town or s.municipality
        shown_province = evidence_province or (claim_place[2] if claim_place else s.province)
        result.reasons.append(Reason("suggest.reason.place_ok", {"town": shown_town, "province": shown_province},
                                     True))
        if inside is None and claim_place:
            result.lat = result.lat if result.lat is not None else claim_place[0]
            result.lon = result.lon if result.lon is not None else claim_place[1]
        if "place" not in result.sources and (claim_place or inside):
            result.sources.append("place")
        if not result.town:
            result.town = s.municipality
        if not result.province:
            result.province = shown_province or ""
        return True
    if inside is False and claim_inside is not True:
        result.reasons.append(Reason("suggest.reason.place_outside", {"town": town or s.municipality,
                                                                      "province": evidence_province}, False))
        return False
    if inside is None and claim_inside is False:
        result.reasons.append(Reason("suggest.reason.place_outside", {"town": s.municipality,
                                                                      "province": claim_place[2] if claim_place
                                                                      else s.province}, False))
        return False
    result.reasons.append(Reason("suggest.reason.place_unknown", {}, None))
    return None


@dataclass
class BatchCallbacks:
    on_progress: Callable[[str], None] | None = None
    on_result: Callable[[VerifiedSuggestion], None] | None = None


def verify_batch(suggestions: list[Suggestion], *, fetcher, gateway=None, registry=None, place_index=None,
                 register_db=None, area: SearchArea | None = None, cancel: threading.Event | None = None,
                 max_total_requests: int = 40, max_requests_each: int = DEFAULT_REQUESTS, cli: str = "",
                 callbacks: BatchCallbacks | None = None, language: str = "it") -> list[VerifiedSuggestion]:
    """`verify_suggestion` for each suggestion, in order, within one total request budget.
    Never raises; each result is also delivered to `callbacks.on_result` at once."""
    callbacks = callbacks or BatchCallbacks()
    results: list[VerifiedSuggestion] = []
    left = max(0, int(max_total_requests))
    for index, suggestion in enumerate(suggestions, start=1):
        if _cancelled(cancel):
            break
        _call(callbacks.on_progress, text("suggest.progress.verifying", index=index, total=len(suggestions),
                                          name=suggestion.name))
        share = min(max_requests_each, left)
        result = verify_suggestion(suggestion, fetcher=fetcher, gateway=gateway, registry=registry,
                                   place_index=place_index, register_db=register_db, area=area, cancel=cancel, max_requests=share, cli=cli,
                                   language=language)
        left -= result.requests
        results.append(result)
        _call(callbacks.on_result, result)
    counts = {status: sum(1 for r in results if r.status == status) for status in STATUSES}
    _call(callbacks.on_progress, text("suggest.progress.done", **counts))
    return results


def _call(callback, value) -> None:
    if callback is None:
        return
    try:
        callback(value)
    except Exception:  # noqa: BLE001
        log.exception("Suggest: a verification callback raised an exception")
