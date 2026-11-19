"""Wikidata as a free identity source of a company card (CC0, read through the polite gateway).

The item is found ONLY by an identifier, never by name (names are ambiguous): the QID the
company's own website links in its schema.org `sameAs`, else `haswbstatement:` searches by
LEI (P1278) or EU VAT number (P3608, "IT" + 11 digits), accepted when exactly one item
matches. The study found items for big groups only (Reply, Engineering, Accenture global);
most Italian SMEs have none, which is normal.

Read from the item (statements of rank "deprecated" are ignored):
P3608 VAT -> "vat", P1278 LEI -> "lei", P571 inception -> "founded" (year), P856 official
website -> "website", P1128 employees (latest P585 year) -> "employees"; P169 chief
executive officer -> a "management" contact (name from the item's label, only statements
without an end date P582). Facts carry source "wikidata" and the item URL.

Requests: at most 1 search + 1 item + 1 label request; the item request uses the same
parameters as financials/wikidata.py so the financials step reads it from the cache.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

log = logging.getLogger(__name__)

API = "https://www.wikidata.org/w/api.php"
SOURCE = "wikidata"
TTL_S = 7 * 24 * 3600
P_VAT, P_LEI, P_INCEPTION, P_WEBSITE, P_EMPLOYEES, P_CEO = "P3608", "P1278", "P571", "P856", "P1128", "P169"
P_POINT_IN_TIME, P_END = "P585", "P582"
P_LINKEDIN = "P4264"
# Profile identifiers on the item -> the profile URL (web/social.py classifies them again).
SOCIAL_PROPERTIES = {
    P_LINKEDIN: "https://www.linkedin.com/company/{}",   # LinkedIn company ID
    "P2013": "https://www.facebook.com/{}",              # Facebook username
    "P2003": "https://www.instagram.com/{}",             # Instagram username
    "P2002": "https://x.com/{}",                         # X username
    "P2397": "https://www.youtube.com/channel/{}",       # YouTube channel ID
    "P7085": "https://www.tiktok.com/@{}",               # TikTok username
    "P2037": "https://github.com/{}",                    # GitHub username
}
_QID = re.compile(r"^Q[1-9][0-9]*$")
_YEAR = re.compile(r"^[+-]?(\d{4})")


@dataclass
class WikidataIdentity:
    state: str  # "found" | "not_found" | "ambiguous" | "unavailable" | "skipped"
    qid: str = ""
    facts: list[dict] = field(default_factory=list)
    people: list[dict] = field(default_factory=list)
    detail: str = ""
    requests: int = 0

    @property
    def url(self) -> str:
        return f"https://www.wikidata.org/wiki/{self.qid}" if self.qid else ""


def _statements(claims: dict, prop: str) -> list[dict]:
    items = [s for s in claims.get(prop, []) or [] if isinstance(s, dict) and s.get("rank") != "deprecated"]
    preferred = [s for s in items if s.get("rank") == "preferred"]
    return preferred or items


def _value(statement: dict):
    try:
        return statement["mainsnak"]["datavalue"]["value"]
    except (KeyError, TypeError):
        return None


def _string_values(claims: dict, prop: str) -> list[str]:
    return [str(v) for v in (_value(s) for s in _statements(claims, prop)) if isinstance(v, str) and v.strip()]


def _year_of(value) -> str:
    if isinstance(value, dict) and int(value.get("precision", 0) or 0) >= 9:
        match = _YEAR.match(str(value.get("time") or ""))
        if match:
            return match.group(1)
    return ""


def parse_entity(claims: dict, qid: str, *, when: str | None = None) -> tuple[list[dict], list[str]]:
    """(facts, CEO item ids) of an item's claims (pure function)."""
    url = f"https://www.wikidata.org/wiki/{qid}"
    when = when or date.today().isoformat()
    facts: list[dict] = []

    def add(name: str, value: str) -> None:
        value = " ".join(str(value or "").split())
        if value and not any(f["field"] == name and f["value"] == value for f in facts):
            facts.append({"field": name, "value": value, "source": SOURCE, "url": url, "date": when,
                          "where": "Wikidata item"})

    for value in _string_values(claims, P_VAT):
        digits = re.sub(r"\s", "", value).upper()
        if digits.startswith("IT") and re.fullmatch(r"IT\d{11}", digits):
            add("vat", digits[2:])
    for value in _string_values(claims, P_LEI):
        if re.fullmatch(r"[A-Z0-9]{18}\d{2}", value.strip().upper()):
            add("lei", value.strip().upper())
    years = sorted({y for y in (_year_of(_value(s)) for s in _statements(claims, P_INCEPTION)) if y})
    if len(years) == 1:
        add("founded", years[0])
    for value in _string_values(claims, P_WEBSITE)[:1]:
        add("website", value)
    latest: tuple[int, float] | None = None
    for statement in _statements(claims, P_EMPLOYEES):
        value = _value(statement)
        try:
            amount = float(value["amount"]) if isinstance(value, dict) else None
        except (KeyError, TypeError, ValueError):
            amount = None
        year = ""
        for snak in (statement.get("qualifiers") or {}).get(P_POINT_IN_TIME, []):
            year = _year_of((snak.get("datavalue") or {}).get("value")) or year
        if amount is not None and year and (latest is None or int(year) > latest[0]):
            latest = (int(year), amount)
    if latest is not None:
        add("employees", f"{int(latest[1])} ({latest[0]})")
    # the company's profiles as stated on the item (identifiers, not guesses)
    for prop, template in SOCIAL_PROPERTIES.items():
        for value in _string_values(claims, prop)[:1]:
            handle = value.strip().strip("/")
            if re.fullmatch(r"[A-Za-z0-9_.@-]{1,100}", handle):
                add("linkedin" if prop == P_LINKEDIN else "social", template.format(handle))
    ceos = []
    for statement in _statements(claims, P_CEO):
        if (statement.get("qualifiers") or {}).get(P_END):
            continue  # a former CEO
        value = _value(statement)
        if isinstance(value, dict) and _QID.match(str(value.get("id") or "")):
            ceos.append(str(value["id"]))
    return facts, ceos[:2]


def _get(gateway, params: dict, cancel=None):
    kwargs = {"params": params, "cache_ttl_s": TTL_S}
    if cancel is not None:
        kwargs["cancel"] = cancel
    reply = gateway.get(API, **kwargs)
    if getattr(reply, "status", 0) != 200:
        raise ValueError(f"HTTP {getattr(reply, 'status', 0)}")
    return reply.json()


def find_qid(gateway, *, vat: str = "", lei: str = "", cancel=None) -> tuple[str, str, int]:
    """(qid, state, requests) from a VAT number or an LEI (exactly one item must match)."""
    queries = []
    if re.fullmatch(r"[A-Z0-9]{18}\d{2}", (lei or "").upper()):
        queries.append(f"haswbstatement:{P_LEI}={lei.upper()}")
    if re.fullmatch(r"\d{11}", vat or ""):
        queries.append(f"haswbstatement:{P_VAT}=IT{vat}")
    requests = 0
    state = "not_found"
    for query in queries:
        data = _get(gateway, {"action": "query", "list": "search", "srsearch": query, "format": "json"}, cancel)
        requests += 1
        hits = (data or {}).get("query", {}).get("search", []) if isinstance(data, dict) else []
        titles = [h.get("title") for h in hits if isinstance(h, dict)]
        if len(titles) == 1 and _QID.match(str(titles[0])):
            return str(titles[0]), "found", requests
        if len(titles) > 1:
            state = "ambiguous"
            log.info("Wikidata: %d items for %s; ambiguous, not used", len(titles), query)
    return "", state, requests


def lookup(gateway, *, qid: str = "", vat: str = "", lei: str = "", cancel=None) -> WikidataIdentity:
    """Facts and CEO contact of the company's Wikidata item. Never raises, except the
    gateway's Cancelled."""
    from ..discovery.gateway import Cancelled

    if gateway is None:
        return WikidataIdentity("skipped")
    requests = 0
    try:
        qid = (qid or "").strip().upper()
        if not _QID.match(qid):
            qid, state, requests = find_qid(gateway, vat=vat, lei=lei, cancel=cancel)
            if not qid:
                return WikidataIdentity(state if (vat or lei) else "skipped", requests=requests)
        data = _get(gateway, {"action": "wbgetentities", "ids": qid, "props": "claims", "format": "json"}, cancel)
        requests += 1
        entity = ((data or {}).get("entities") or {}).get(qid) if isinstance(data, dict) else None
        claims = entity.get("claims") if isinstance(entity, dict) else None
        if not isinstance(claims, dict):
            return WikidataIdentity("not_found", qid=qid, requests=requests)
        result = WikidataIdentity("found", qid=qid, requests=requests)
        result.facts, ceo_ids = parse_entity(claims, qid)
        result.facts.append({"field": "wikidata", "value": qid, "source": SOURCE, "url": result.url,
                             "date": date.today().isoformat(), "where": "Wikidata item"})
        if ceo_ids:
            labels = _get(gateway, {"action": "wbgetentities", "ids": "|".join(ceo_ids), "props": "labels",
                                    "languages": "it|en", "format": "json"}, cancel)
            result.requests += 1
            for ceo in ceo_ids:
                item = ((labels or {}).get("entities") or {}).get(ceo) or {}
                names = item.get("labels") or {}
                label = (names.get("it") or names.get("en") or {}).get("value", "")
                if label:
                    result.people.append({"name": label, "role": "CEO", "kind": "management", "email": "",
                                          "phone": "", "source_url": result.url, "origin": SOURCE})
        return result
    except Cancelled:
        raise
    except Exception as error:  # noqa: BLE001 - a free source must never break the card
        log.info("Wikidata identity lookup failed: %s", type(error).__name__)
        return WikidataIdentity("unavailable", qid=qid, detail=type(error).__name__, requests=requests)
