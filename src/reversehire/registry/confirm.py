"""Identity-confirmation rule of a company card (sources study, section F.2).

Independent sources: (W) the company's own website (text and schema.org JSON-LD),
(V) VIES, (G) GLEIF, (K) Wikidata, (R) Registro Imprese (imported CSV row, manual
visura, paid register), (O) OSM / Overture / other discovery data and the card name.

- CONFIRMED: a P.IVA or CF printed on (W), checksum-valid, equals an identifier from
  (R), (V) or (G); AND the legal name from (W) or (O) fuzzy-matches (token set >= 0.85
  after removing legal-form words) the name from (V/G/R); AND the town/postcode of the
  registered office agrees between (V/G/R) and (W) when (W) states it. If the REA is on
  both (W) and (R) it must be equal, otherwise PROBABLE. ATECO only from (R).
- PROBABLE: (W) identifier checksum-valid but no independent register hit (VIES
  "not registered" is unknown, not negative); or a register hit by name (+ town) while
  the website has no identifier; or a CONFIRMED candidate that failed a check above.
- UNVERIFIED: no identifiers.
- CONFLICT: the same kind of identifier (VAT vs VAT, CF vs CF) differs between
  sources. A CF and a P.IVA that differ are not a conflict (older companies).

Input: provenance facts {"field", "value", "source", "url", "date"}; output: an
IdentityResult with the agreement table (field x source) shown by the "why" view.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher

from .checksums import cf_ok, normalize_cf, normalize_rea, normalize_vat, piva_ok, rea_equal

CONFIRMED = "confirmed"
PROBABLE = "probable"
UNVERIFIED = "unverified"
CONFLICT = "conflict"
STATUSES = (CONFIRMED, PROBABLE, UNVERIFIED, CONFLICT)

NAME_THRESHOLD = 0.85

# Source names (provenance "source") grouped by the letters of the rule.
WEBSITE_SOURCES = ("website", "website_jsonld")
REGISTER_SOURCES = ("register",)  # import CSV, manual visura, paid register
SOURCE_CLASS = {"website": "W", "website_jsonld": "W", "vies": "V", "gleif": "G",
                "wikidata": "K", "register": "R"}
INDEPENDENT = ("R", "V", "G")
COMPARED_FIELDS = ("vat", "tax_code", "rea", "name", "postcode", "town", "province", "lei", "ateco")

_LEGAL_FORM_WORDS = {
    "spa", "s.p.a", "s.p.a.", "srl", "s.r.l", "s.r.l.", "srls", "snc", "sas", "sapa", "scarl",
    "scrl", "societa", "per", "azioni", "a", "responsabilita", "limitata", "semplificata",
    "in", "nome", "collettivo", "accomandita", "semplice", "cooperativa", "coop", "soc",
    "con", "socio", "unico", "su", "e", "c", "ltd", "limited", "gmbh", "ag", "inc", "llc",
    "corp", "co", "plc", "sa", "bv", "nv", "the", "di", "&",
    # brand suffixes, not part of the legal name ("IMA Group" on the site)
    "group", "gruppo",
}


def source_class(source: str) -> str:
    return SOURCE_CLASS.get((source or "").lower(), "O")


def name_tokens(name: str) -> list[str]:
    """Lower-case tokens without accents, punctuation and legal-form words."""
    decomposed = unicodedata.normalize("NFKD", (name or "").casefold())
    text = "".join(c for c in decomposed if not unicodedata.combining(c))
    # "s.p.a." -> "spa", "i.m.a." -> "ima": drop dots between single letters first.
    text = re.sub(r"\b(?:[a-z]\.){2,}[a-z]?\.?", lambda m: m.group(0).replace(".", ""), text)
    tokens = re.findall(r"[a-z0-9]+", text)
    return [t for t in tokens if t not in _LEGAL_FORM_WORDS]


def name_similarity(a: str, b: str) -> float:
    """Token-set ratio (0..1) of two company names, legal-form words removed."""
    ta, tb = set(name_tokens(a)), set(name_tokens(b))
    if not ta or not tb:
        return 0.0
    common = " ".join(sorted(ta & tb))
    rest_a = " ".join(sorted(ta - tb))
    rest_b = " ".join(sorted(tb - ta))
    first = (common + " " + rest_a).strip()
    second = (common + " " + rest_b).strip()

    def ratio(x: str, y: str) -> float:
        return SequenceMatcher(None, x, y).ratio() if x and y else 0.0

    scores = [ratio(first, second)]
    if common:
        scores += [ratio(common, first), ratio(common, second)]
    return max(scores)


def _normal(field_name: str, value: str) -> str:
    value = " ".join(str(value or "").split())
    if field_name == "vat":
        return normalize_vat(value)
    if field_name in ("tax_code", "lei"):
        return normalize_cf(value)
    if field_name == "rea":
        return normalize_rea(value)
    if field_name in ("town", "province"):
        return unicodedata.normalize("NFKD", value.casefold()).encode("ascii", "ignore").decode()
    return value


@dataclass
class IdentityResult:
    status: str = UNVERIFIED
    reasons: list[str] = field(default_factory=list)  # keys "identity.reason.*" (English defaults below)
    agreements: list[dict] = field(default_factory=list)  # {"field", "value", "sources": [...]}
    conflicts: list[dict] = field(default_factory=list)  # {"field", "values": {source: value}}
    table: dict[str, dict[str, str]] = field(default_factory=dict)  # field -> source -> value
    matched_identifier: str = ""
    name_score: float | None = None
    website_verified: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# English text of the reason keys (the GUI translates them when the locale has the key).
REASON_TEXTS = {
    "id_matches_register": "An identifier printed on the website equals the one in an independent register.",
    "name_matches": "The legal name agrees with the register name.",
    "name_differs": "The register name does not match the company name on the website.",
    "town_matches": "The registered office town/postcode agrees.",
    "town_differs": "The registered office town/postcode differs between website and register.",
    "rea_matches": "The REA agrees between website and register.",
    "rea_differs": "The REA on the website differs from the register.",
    "no_register_hit": "The website identifier is valid but no independent register confirmed it yet.",
    "vies_not_registered": "VIES does not know this VAT number (only intra-EU traders are listed): not a negative.",
    "register_by_name": "A register record matches by name, but the website shows no identifier.",
    "no_identifiers": "No legal identifier found yet.",
    "same_kind_differs": "The same kind of identifier differs between sources.",
    "invalid_website_id": "The website shows an identifier with a wrong check digit.",
    "website_ids_differ": "The website shows several different VAT numbers and no register says which one "
                          "belongs to this company.",
}


def evaluate(facts: list[dict], card_name: str = "", *, vies_not_registered: bool = False) -> IdentityResult:
    """Identity status of a card from its provenance facts (see module docstring)."""
    result = IdentityResult()
    by_class: dict[str, dict[str, set[str]]] = {}
    for fact in facts or []:
        name = fact.get("field")
        if name not in COMPARED_FIELDS:
            continue
        value = _normal(name, fact.get("value", ""))
        if not value:
            continue
        source = str(fact.get("source") or "")
        result.table.setdefault(name, {})
        shown = result.table[name].get(source)
        if shown is None:
            result.table[name][source] = str(fact.get("value"))
        elif _normal(name, shown) != value and str(fact.get("value")) not in shown.split(" | "):
            result.table[name][source] = f"{shown} | {fact.get('value')}"
        by_class.setdefault(source_class(source), {}).setdefault(name, set()).add(value)
    if card_name:
        result.table.setdefault("name", {}).setdefault("card", card_name)
        by_class.setdefault("O", {}).setdefault("name", set()).add(card_name)

    def values(cls: str, name: str) -> set[str]:
        return by_class.get(cls, {}).get(name, set())

    # Website identifiers, checksum-valid only.
    w_vat = {v for v in values("W", "vat") if piva_ok(v)}
    w_cf = {v for v in values("W", "tax_code") if cf_ok(v)}
    if (values("W", "vat") - w_vat) or (values("W", "tax_code") - w_cf):
        result.reasons.append("invalid_website_id")
    w_ids = w_vat | w_cf

    # Agreements: a value seen by two or more sources.
    for name, per_source in result.table.items():
        groups: dict[str, list[str]] = {}
        for source, shown in per_source.items():
            for part in shown.split(" | "):
                key = _normal(name, part) if name != "name" else " ".join(sorted(name_tokens(part)))
                if key:
                    groups.setdefault(key, []).append(source)
        for key, sources in groups.items():
            if len(set(sources)) >= 2:
                result.agreements.append({"field": name, "value": key, "sources": sorted(set(sources))})

    # Conflicts: same kind of identifier with different values.
    for name in ("vat", "tax_code"):
        independent = {cls: values(cls, name) for cls in ("R", "V", "G", "K") if values(cls, name)}
        all_independent = set().union(*independent.values()) if independent else set()
        website = w_vat if name == "vat" else w_cf
        clash = False
        if len(all_independent) > 1:
            clash = True
        elif all_independent and website and not (all_independent & website):
            # the website may print several group companies: conflict only when the
            # website states exactly one value of this kind
            clash = len(website) == 1
        if clash:
            result.conflicts.append({"field": name, "values": dict(result.table.get(name, {}))})
    if result.conflicts:
        result.status = CONFLICT
        result.reasons.append("same_kind_differs")
        return result

    independent_ids = set()
    for cls in INDEPENDENT:
        independent_ids |= values(cls, "vat") | values(cls, "tax_code")
    matched = w_ids & independent_ids
    register_names = set().union(*(values(cls, "name") for cls in INDEPENDENT))
    own_names = values("W", "name") | values("O", "name")
    best = 0.0
    for a in own_names:
        for b in register_names:
            best = max(best, name_similarity(a, b))
    result.name_score = round(best, 2) if register_names and own_names else None
    name_ok = best >= NAME_THRESHOLD

    w_places = values("W", "postcode") | values("W", "town")
    register_places = set()
    for cls in INDEPENDENT:
        register_places |= values(cls, "postcode") | values(cls, "town")
    town_ok = not w_places or not register_places or bool(w_places & register_places)

    w_rea, r_rea = values("W", "rea"), values("R", "rea")
    rea_ok = True
    if w_rea and r_rea:
        rea_ok = any(rea_equal(a, b) for a in w_rea for b in r_rea)
        result.reasons.append("rea_matches" if rea_ok else "rea_differs")

    if not matched and len(w_vat) > 1:
        # several VAT numbers on the website and no register says which one is this company's
        result.conflicts.append({"field": "vat", "values": dict(result.table.get("vat", {}))})
        result.status = CONFLICT
        result.reasons.append("website_ids_differ")
        return result
    if matched:
        result.matched_identifier = sorted(matched)[0]
        result.website_verified = True
        result.reasons.append("id_matches_register")
        result.reasons.append("name_matches" if name_ok else "name_differs")
        if w_places and register_places:
            result.reasons.append("town_matches" if town_ok else "town_differs")
        result.status = CONFIRMED if (name_ok and town_ok and rea_ok) else PROBABLE
        return result
    if w_ids:
        result.status = PROBABLE
        result.reasons.append("no_register_hit")
        if vies_not_registered:
            result.reasons.append("vies_not_registered")
        return result
    if register_names and name_ok:
        result.status = PROBABLE
        result.reasons.append("register_by_name")
        return result
    result.status = UNVERIFIED
    result.reasons.append("no_identifiers")
    return result
