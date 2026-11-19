"""Companies for a spontaneous application: the companies the app already knows whose
stored text matches the searched profile by meaning (no network).

    candidates, mode = rank_companies(query, cards=cards, register=register_companies,
                                      map_companies=osm_companies, embedder=embedder, store=store)

Where they come from: the saved cards ("My companies": every text of the card), the
regional register (name, ATECO code and description) and the local map index (name, OSM
tags and description). Each is ranked with the engine of the app's lists
(`jobsearch/semantic.py`, `scoring/semantic_index.py`): the words of the search and their
equivalents found in a passage rank above a similarity of meaning, which is an estimate.
A register company found through the sector of the search (ATECO divisions named by the
search words, `ateco_prefixes_for`) is kept with that reason even without a matching text.
Companies of the register or the map that are saved cards already are listed once, as
cards. `direction_for_query(query, prefs)` maps the search to one career direction of the
affinity preferences (None: judge it on every direction).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import lexicon
from ._util import CancelCheck, contains_phrase, fold, log

ORIGIN_CARD, ORIGIN_REGISTER, ORIGIN_MAP = "card", "register", "map"
MAX_MAP_CANDIDATES = 400      # map entries judged by meaning (after the word filter)
MEANING_MIN = 45.0            # meaning-only hits below this are not listed
SECTOR_RELEVANCE = 30.0       # a register company found by the sector of the search only
DEFAULT_LIMIT = 150

# Search words -> ATECO 2025 divisions, besides the sector phrases of the ads (lexicon).
EXTRA_SECTORS: list[tuple[str, str, tuple[str, ...]]] = [
    (r"cyber\w*|sicurezza informatica|information security|infosec|security operation\w*|\bsoc\b|"
     r"penetration test\w*|\bot\b|operational technology|scada|\bics\b", "ICT and cybersecurity", ("62", "63")),
    (r"software|developer|sviluppat\w*|programmat\w*|programmer|devops|cloud|data scien\w*|\bai\b",
     "software and ICT", ("62", "63")),
    (r"\bplc\b|automazione|automation|robotic\w*|mechatronic\w*|meccatronic\w*", "automation and machinery",
     ("28", "27", "26", "33")),
    (r"elettronic\w*|electronic\w*|firmware|embedded", "electronics", ("26", "27")),
    (r"telecomunicazion\w*|telecommunication\w*|networking|\brete\b|network", "telecommunications",
     ("61", "62")),
]


@dataclass
class CompanyCandidate:
    """One row of "Companies for a spontaneous application"."""

    key: str                       # "card:<id>", "register:<key>", "map:<name>|<lat>|<lon>"
    name: str
    origin: str                    # ORIGIN_CARD | ORIGIN_REGISTER | ORIGIN_MAP
    item: object                   # the CompanyCard, or the models.Company of the register / map
    place: str = ""
    card_id: int | None = None
    built: bool = False            # a card whose data were collected ("Build the card" done)
    relevance: float = 0.0         # 0..100
    exact: bool = False            # the words of the search (or an equivalent) were found
    terms: list[str] = field(default_factory=list)
    snippet: str = ""
    passage: object | None = None  # scoring.semantic_index.Passage of the hit (its source)
    sector: str = ""               # label of the sector that matched (register, by ATECO)

    @property
    def has_card(self) -> bool:
        return self.card_id is not None


def _search_text(query) -> str:
    parts = [getattr(query, "role", "") or ""] + list(getattr(query, "skills", []) or [])
    try:
        parts += [term for term, _kind in query.also_terms()]
    except Exception:  # noqa: BLE001
        pass
    return fold(" ; ".join(p for p in parts if p))


def ateco_prefixes_for(query) -> tuple[list[str], str]:
    """(ATECO division prefixes, sector label) named by the words of the search."""
    text = _search_text(query)
    prefixes: list[str] = []
    labels: list[str] = []
    for pattern, label, divisions in EXTRA_SECTORS:
        if re.search(pattern, text):
            labels.append(label)
            prefixes += [d for d in divisions if d not in prefixes]
    for label, divisions in lexicon.sector_hints(text):
        labels.append(label)
        prefixes += [f"{d:02d}" for d in divisions if f"{d:02d}" not in prefixes]
    return prefixes, ", ".join(dict.fromkeys(labels))


def direction_for_query(query, prefs) -> str | None:
    """The enabled career direction whose roles, skills or name the search names best."""
    text = _search_text(query)
    if not text or prefs is None:
        return None
    best, best_score = None, 0
    for direction in getattr(prefs, "directions", []) or []:
        if not getattr(direction, "enabled", True):
            continue
        score = 0
        for value in [direction.name] + list(direction.roles) + list(direction.skills):
            value = fold(value)
            if len(value) >= 2 and contains_phrase(text, value):
                score += 2 if " " in value else 1
        if score > best_score:
            best, best_score = direction.id, score
    return best


def _name_key(name: str) -> str:
    folded = fold(name)
    folded = re.sub(r"\b(s\.?r\.?l\.?s?|s\.?p\.?a\.?|s\.?n\.?c\.?|s\.?a\.?s\.?|spa|srl|unipersonale|"
                    r"societa|cooperativa|soc\.?|coop)\b", " ", folded)
    return " ".join(re.findall(r"[a-z0-9]+", folded))


def _place_of(company) -> str:
    try:
        from ..geo.places import location_of_company

        is_card = hasattr(company, "registry_value")   # a saved company: its card's rules
        found = location_of_company(card=company) if is_card else location_of_company(company)
        if found.known:
            return found.display()
    except Exception:  # noqa: BLE001 - the register's own fields below
        pass
    fields = getattr(company, "fields", None) or {}
    town = fields.get("town") if isinstance(fields, dict) else ""
    province = fields.get("province") if isinstance(fields, dict) else ""
    if town:
        return f"{town} ({province})" if province else str(town)
    address = getattr(company, "address", "") or ""
    return address.split(",")[-1].strip() if address else ""


def _register_document(company, key: str):
    from ..scoring.semantic_index import company_document

    document = company_document(company, key=key)
    fields = getattr(company, "fields", None) or {}
    text = fields.get("ateco_text") if isinstance(fields, dict) else ""
    if text:
        from ..scoring.semantic_index import _add

        _add(document.passages, str(text), "ateco")
    return document


def _map_document(company, key: str):
    from ..scoring.semantic_index import _add, company_document

    document = company_document(company, key=key)
    tags = getattr(company, "osm_tags", None) or {}
    if isinstance(tags, dict):
        values = [str(v) for k, v in tags.items() if k in ("description", "office", "industrial", "craft",
                                                           "shop", "product", "operator") and v]
        if values:
            _add(document.passages, ", ".join(values), "map")
    return document


def _map_prefilter(query, companies) -> list:
    """The map entries worth judging: a word of the search (or an equivalent) in the name or
    the tags (the region has thousands of entries)."""
    terms = [t for t in re.split(r"\s*;\s*", _search_text(query)) for t in [t.strip()] if len(t) >= 3]
    words_ = {w for t in terms for w in t.split() if len(w) >= 3 and w not in lexicon.GENERIC_WORDS}
    out = []
    for company in companies:
        tags = getattr(company, "osm_tags", None) or {}
        text = fold(" ".join([getattr(company, "name", "") or ""] + [str(v) for v in tags.values()]))
        if any(contains_phrase(text, t) for t in terms) or any(contains_phrase(text, w) for w in words_):
            out.append(company)
        if len(out) >= MAX_MAP_CANDIDATES:
            break
    return out


def rank_companies(query, *, cards=(), register=(), map_companies=(), register_by_sector=(), embedder=None,
                   store=None, cancel=None, limit: int = DEFAULT_LIMIT) -> tuple[list[CompanyCandidate], str]:
    """(candidates best first, mode "semantic" | "keyword"). See the module docstring.
    `register_by_sector`: register companies found through the ATECO divisions of the
    search (kept with the sector as reason). Never raises."""
    from ..scoring.semantic_index import card_document
    from .semantic import rank_by_meaning

    is_cancelled = CancelCheck(cancel)
    candidates: dict[str, CompanyCandidate] = {}
    documents = []
    seen_names: set[str] = set()
    _prefixes, sector_label = ateco_prefixes_for(query)
    try:
        from ..companies import card_key
    except Exception:  # noqa: BLE001
        card_key = None
    card_keys = set()
    for card in cards or []:
        key = f"card:{card.id}"
        candidates[key] = CompanyCandidate(key, card.name, ORIGIN_CARD, card, place=_card_place(card),
                                           card_id=card.id, built=bool(card.built))
        documents.append(card_document(card))
        documents[-1].key = key
        seen_names.add(_name_key(card.name))
        card_keys.add(getattr(card, "key", ""))

    def known(company) -> bool:
        if _name_key(getattr(company, "name", "")) in seen_names:
            return True
        if card_key is not None:
            try:
                return card_key(company) in card_keys
            except Exception:  # noqa: BLE001
                return False
        return False

    sector_keys: set[str] = set()
    for origin, items in ((ORIGIN_REGISTER, list(register or []) + list(register_by_sector or [])),
                          (ORIGIN_MAP, _map_prefilter(query, map_companies or []))):
        by_sector = {id(c) for c in register_by_sector or []} if origin == ORIGIN_REGISTER else set()
        for company in items:
            if is_cancelled():
                break
            name = getattr(company, "name", "") or ""
            if not name or known(company):
                continue
            ident = (getattr(company, "fields", None) or {}).get("register_key") or (
                getattr(company, "ids", None) or {}).get("tax_code") or \
                f"{_name_key(name)}|{getattr(company, 'lat', '')}|{getattr(company, 'lon', '')}"
            key = f"{origin}:{ident}"
            if key in candidates:
                if id(company) in by_sector:
                    sector_keys.add(key)
                continue
            seen_names.add(_name_key(name))
            candidates[key] = CompanyCandidate(key, name, origin, company, place=_place_of(company))
            documents.append(_register_document(company, key) if origin == ORIGIN_REGISTER
                             else _map_document(company, key))
            if id(company) in by_sector:
                sector_keys.add(key)
    if not candidates:
        return [], "keyword" if embedder is None else "semantic"
    hits, mode = rank_by_meaning(query, documents, embedder=embedder, store=store, cancel=cancel)
    out: list[CompanyCandidate] = []
    for key, candidate in candidates.items():
        hit = hits.get(key)
        if hit is not None:
            candidate.relevance = float(hit.relevance)
            candidate.exact = bool(hit.exact)
            candidate.terms = list(hit.terms or [])
            candidate.passage = hit.passage
            candidate.snippet = hit.snippet(160) if hit.passage is not None else ""
        listed = candidate.exact or (mode == "semantic" and candidate.relevance >= MEANING_MIN)
        if not listed and key in sector_keys:
            candidate.sector = sector_label
            candidate.relevance = max(candidate.relevance, SECTOR_RELEVANCE)
            candidate.terms, candidate.snippet, candidate.passage = [], "", None
            listed = True
        if listed:
            out.append(candidate)
    out.sort(key=lambda c: (not c.exact, -c.relevance, c.name.casefold()))
    log.info("Spontaneous companies: %d of %d listed (%s)", len(out), len(candidates), mode)
    return out[:max(1, int(limit))], mode


def _card_place(card) -> str:
    try:
        from ..geo.places import location_of_company

        found = location_of_company(card=card)
        if found.known:
            return found.display()
    except Exception:  # noqa: BLE001 - the address as saved is still right
        pass
    address = getattr(card, "address", "") or ""
    return address.split(",")[-1].strip() if address else ""
