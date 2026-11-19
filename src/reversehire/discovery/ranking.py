"""Pre-crawl ranking of the discovered companies, explainable.

Before any website is read, only free local data are known: the company NAME, its
OpenStreetMap tags, Wikidata's industry (when the company comes from Wikidata) and the
register ATECO code of an imported list. `max_companies` keeps the first companies of
this ranking, so it decides which sites are read: it must put the employers of the
profile first (machinery, automation, system integrators, industrial software... for an
automation engineer) and push down public offices, universities and research institutes
(unless the profile is research), shops, restaurants and associations.

Score (higher first), built from:
- `geo.sectors.preliminary_relevance` (OSM tags and name against the profile sectors);
- name keywords: sector terms (IT/EN) of the chosen sectors, terms from the profile's
  roles and skills, words of the chosen ATECO categories' titles;
- tags that mark an industrial site (man_made=works, industrial=*, craft=*,
  office=company|engineer, product=*);
- Wikidata industry matching the same terms;
- imported register ATECO: inside the chosen categories (strong), outside (demoted);
  companies with an unknown ATECO are kept and ranked by the other signals;
- a small bonus for a website (only sites can be read);
- a small bonus for a growing revenue (financials/trend.py `ranking_bonus`: +0.05 from a
  CAGR of +2 %/year, +0.10 from +10 %/year, computed only from the figures stored on the
  user's card of that company, at least 3 years; never a penalty, so companies without
  figures are neither hidden nor pushed down);
- demotion factors for public administration, education/research, retail and
  hospitality, associations and unrelated professional offices.

`rank(company, context)` returns the score and the short English reasons
("why selected"), stored by the pipeline in `company.fields["why_selected"]`.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field

from ..geo.sectors import SECTORS, normalize_text, preliminary_relevance
from ..models import Company, public_tags

# --- weights --------------------------------------------------------------------------------

SECTOR_NAME_BONUS = 0.40       # first sector term in the name
EXTRA_NAME_BONUS = 0.10        # each further distinct term (at most MAX_EXTRA_TERMS)
MAX_EXTRA_TERMS = 2
PROFILE_NAME_BONUS = 0.20      # a term of the CV (role/skill) in the name
ATECO_NAME_BONUS = 0.15        # a word of a chosen ATECO title in the name
INDUSTRIAL_TAG_BONUS = 0.15
WIKIDATA_INDUSTRY_BONUS = 0.20
ATECO_MATCH_SCORE = 0.95       # register ATECO inside the chosen categories
ATECO_MISMATCH_PENALTY = 0.25  # register ATECO known and outside them
WEBSITE_BONUS = 0.10
LARGE_EMPLOYER, LARGE_EMPLOYER_BONUS = 250, 0.25    # employees (Wikidata, register imports)
MEDIUM_EMPLOYER, MEDIUM_EMPLOYER_BONUS = 50, 0.10
WIKIDATA_BASE = 0.70         # a company found on Wikidata (head office in the region, website)
NAME_FLOOR = 0.6              # base score of a company whose name states a sector trade
WORKS_SCORE = 0.5             # a factory of unknown kind (man_made=works only)
PRODUCT_BONUS = 0.35           # product=* names something of the profile's field
CONSUMER_PRODUCT_FACTOR = 0.55  # product=* names consumer goods / food / clothes...
UNRELATED_INDUSTRY_FACTOR = 0.75  # Wikidata industry known and unrelated
SECTOR_ORDER_STEP = 0.10       # each later profile sector counts 10% less...
SECTOR_ORDER_FLOOR = 0.70      # ...down to this factor

PUBLIC_FACTOR = 0.25
EDUCATION_FACTOR = 0.35
RETAIL_FACTOR = 0.35
ASSOCIATION_FACTOR = 0.35
OFFICE_FACTOR = 0.5

# --- name terms per sector (normalised, IT and EN) -------------------------------------------

SECTOR_NAME_TERMS: dict[str, tuple[str, ...]] = {
    "electronics_automation": (
        "automazione", "automazioni", "automation", "automations", "automatic", "automatica",
        "automatiche", "automatismi", "robot", "robots", "robotica", "robotics", "robotic", "plc",
        "scada", "system integrator", "integrator", "integration", "quadri elettrici", "electrical panels",
        "elettronica", "elettronica industriale", "electronics", "electronic", "elettromeccanica",
        "mechatronics", "meccatronica", "controlli", "controls", "control systems", "motion", "drives",
        "sensori", "sensors", "strumentazione", "instruments", "software industriale", "industrial software",
        "industria 4 0", "industry 4 0", "packaging", "macchine automatiche", "machinery", "machines",
        "impianti", "impiantistica", "plants", "visione artificiale", "machine vision", "vision systems",
    ),
    "manufacturing_metalworking": (
        "meccanica", "meccaniche", "mechanical", "mechanics", "officina meccanica", "officine",
        "costruzioni meccaniche", "carpenteria", "lavorazioni meccaniche", "machining", "metal", "metalli",
        "acciaio", "steel", "macchine", "machinery", "machines", "industrie", "industries", "industriale",
        "industrial", "manufacturing", "oleodinamica", "hydraulics", "pneumatica", "pneumatics",
        "ingranaggi", "gears", "stampi", "moulds", "molds", "utensili", "tools", "fonderia", "foundry",
    ),
    "engineering_design": (
        "engineering", "ingegneria", "progettazione", "progetti", "design engineering", "technical office",
        "studio tecnico", "r d", "tech", "technologies", "tecnologie", "technology",
    ),
    # only words that really say "software house" (not "solutions", "systems", "digital")
    "it_software": (
        "software", "informatica", "informatics", "software house", "cloud", "cybersecurity", "computing",
    ),
    "electrical_plumbing": (
        "impianti", "impianti elettrici", "elettrica", "elettrici", "electrical", "elettroimpianti",
        "termoidraulica", "idraulica", "hvac", "quadri elettrici",
    ),
    "chemicals_materials": ("chimica", "chemical", "chemicals", "plastica", "plastics", "polimeri", "polymers",
                            "ceramica", "ceramics", "materiali", "materials", "gomma", "rubber"),
    "energy_environment": ("energia", "energy", "solare", "solar", "ambiente", "environment", "rinnovabili",
                           "renewables", "power"),
    "logistics_transport": ("logistica", "logistics", "trasporti", "transport", "spedizioni", "shipping"),
}

# OSM product=* / Wikidata industry words of industrial equipment makers (normalised, IT/EN).
INDUSTRIAL_PRODUCTS = {
    "machinery", "machine", "machines", "machine tools", "industrial automation", "automation",
    "automatic machines", "macchine", "macchinari", "robot", "robots", "robotics", "packaging",
    "packaging machinery", "labeling", "labelling", "industrial labeling system", "electrical equipment",
    "electrical panels", "control systems", "controls", "plc", "electronics", "electronic components",
    "sensors", "valves", "pumps", "compressors", "hydraulics", "pneumatics", "motors", "drives",
    "gears", "bearing", "bearings", "moulds", "molds", "dies", "tools", "machined parts", "automotive parts",
    "engines", "engine", "industrial equipment", "conveyors", "material handling", "cnc", "software",
    "machining", "metalworking", "mechanical engineering", "industrial machinery", "capsule", "capsules",
}
INDUSTRY_WORDS = {
    "manufacturing", "manifattura", "industria meccanica", "meccanica", "mechanical engineering",
    "automazione", "automation", "electrical engineering", "ingegneria meccanica", "ingegneria elettrica",
    "industria elettronica", "automotive",
    "industria automobilistica", "aerospace", "aeronautica", "machinery", "macchine", "packaging",
    "imballaggio", "information technology", "software", "motorcycle", "motorcycle industry", "motociclistica",
    "vehicle", "vehicles", "veicoli", "bus manufacturing", "electronics", "robotics", "robotica",
    "defence", "defense", "difesa", "railway", "ferroviaria", "industrial machinery",
    "engine tuning", "engines", "metallurgia", "metalworking", "industria del software",
    # words of Wikidata short descriptions of machine and component makers
    "riduttori", "gearmotors", "gearboxes", "motoriduttori", "macchine automatiche", "macchine per",
    "machines for", "manufacturer of machines", "produttrice di macchine", "costruttrice di macchine",
    "oleodinamica", "hydraulic", "pneumatic", "valvole", "pompe", "compressori", "utensili", "macchine utensili",
    "automation company", "azienda di automazione", "sistemi di automazione", "impiantistica",
    "componenti", "components", "meccatronica", "mechatronics", "elettromeccanica", "electromechanical",
    "gelato machines", "macchine per gelato", "packaging machinery", "macchine per l imballaggio",
}
# Wikidata industries that are not employers of an industrial/automation profile.
UNRELATED_INDUSTRIES = {
    "negozi", "catena di negozi", "retail", "grande distribuzione", "distribuzione organizzata", "commercio",
    "supermercati", "supermarket", "store", "stores", "industria alimentare", "food industry", "food",
    "industria tessile", "textile", "fashion", "moda", "abbigliamento", "editoria", "publishing",
    "casa editrice", "industria cinematografica", "film industry", "music", "musica", "banca", "banking",
    "assicurazioni", "insurance", "hospitality", "turismo", "tourism", "ristorazione", "facility management",
    "istruzione", "education", "sanita", "healthcare", "industria farmaceutica", "pharmaceutical industry",
    "cosmetica", "cosmetics", "real estate", "immobiliare", "sport", "calcio", "football",
}
# product=* of consumer goods, food and fashion: a factory, but not an automation employer
# for this profile (a profile in those sectors keeps them through the sector terms).
CONSUMER_PRODUCTS = {
    "milk", "dairy", "cheese", "food", "pasta", "bread", "wine", "beer", "meat", "coffee", "chocolate",
    "clothes", "clothing", "textile", "textiles", "shoes", "fashion", "lingerie", "pillow", "mattress",
    "furniture", "cosmetics", "perfume", "toys", "billiards", "table soccer", "medicine", "medicines",
    "pharmaceutical", "drugs", "paint", "paints", "petroleum", "fuel", "stickers", "sticker", "jewellery",
    "printing", "paper", "glass", "ceramics", "tiles", "bricks", "concrete", "asphalt", "plastic bags",
}

# Words too generic to say anything when they come from the CV or an ATECO title.
_STOP_WORDS = set("""
a ad al alla alle allo agli ai and anche as at by con da dal dalla dalle dei del della delle dello
di e ed for from gli i il in la le lo nei nel nella nelle o of on or per su sul sulla the to un una
uno with altri altre altro altra other others not elsewhere classified nca n c a non
fabbricazione manufacture manufacturing produzione production attivita activities activity servizi services
service commercio trade riparazione repair installazione installation lavori work works prodotti products
parti parts componenti components apparecchiature equipment generale general specializzati specialized
engineer ingegnere manager specialist technician tecnico tecnica senior junior lead head assistant operator
consultant consulente responsabile addetto addetta junior developer sviluppatore programmatore
english italian inglese italiano level livello years anni experience esperienza
next high low new smart pro plus one two first full stack the and group italia italy europe
solutions solution systems system data digital tech technology technologies project projects office
industria industriale industriali industry industrial industries studio design italiana italiano
""".split())
_MIN_TERM_LENGTH = 4
# Words of the ATECO titles that name what a machine is FOR, not the trade itself
# ("machinery for food, beverage and tobacco"): a coffee roaster is not a machine maker.
_ATECO_QUALIFIERS = set("""
beverage beverages tobacco food alimentari bevande tabacco paper carta cartone paperboard plastics plastiche
materie rubber gomma distribution distribuzione electricity elettricita energia metal metalli speciali special
purpose uses multiple other altre vari various processing lavorazione production prodotti products textile
tessili leather pelli agriculture agricoltura forestry mining estrazione printing stampa household domestico
""".split())

# --- demotion lists ----------------------------------------------------------------------------

_PUBLIC_TAGS = {("amenity", v) for v in ("townhall", "courthouse", "police", "fire_station", "post_office",
                                         "public_building", "prison", "embassy")} | \
    {("office", v) for v in ("government", "administrative", "public_administration", "tax", "diplomatic",
                             "quango")}
_PUBLIC_NAMES = ("comune di", "provincia di", "regione emilia", "regione ", "ministero", "prefettura",
                 "questura", "agenzia delle entrate", "agenzia delle dogane", "azienda usl", "ausl", "asl ",
                 "inps", "inail", "camera di commercio", "tribunale", "carabinieri", "polizia",
                 "vigili del fuoco", "ufficio postale", "poste italiane", "unione dei comuni",
                 "municipio", "city council", "municipality")
_EDUCATION_TAGS = {("amenity", v) for v in ("university", "college", "school", "kindergarten",
                                            "research_institute", "library", "language_school")} | \
    {("office", v) for v in ("research", "educational_institution", "university")}
_EDUCATION_NAMES = ("universita", "university", "politecnico", "dipartimento", "istituto", "institute",
                    "consiglio nazionale delle ricerche", "cnr ", "infn", "enea", "inaf", "ingv",
                    "scuola", "liceo", "accademia", "academy", "fondazione", "foundation", "centro ricerche",
                    "research centre", "research center", "laboratorio nazionale", "its ", "ifts", "irccs")
_RETAIL_KEYS = ("shop", "tourism")
_RETAIL_AMENITIES = {"restaurant", "cafe", "bar", "fast_food", "pub", "ice_cream", "fuel", "car_wash",
                     "pharmacy", "bank", "marketplace", "cinema", "theatre", "nightclub"}
_ASSOCIATION_TAGS = {("office", v) for v in ("association", "ngo", "foundation", "charity", "union",
                                             "political_party", "religion", "trade_union")} | \
    {("amenity", v) for v in ("social_facility", "community_centre", "place_of_worship")}
_ASSOCIATION_NAMES = ("associazione", "association", "confindustria", "confartigianato", "confcommercio",
                      "cna ", "sindacato", "cgil", "cisl", "uil ", "federazione", "cooperativa sociale",
                      "onlus", "pro loco", "circolo", "parrocchia", "club")
_OFFICE_VALUES = {"insurance", "estate_agent", "lawyer", "notary", "accountant", "tax_advisor", "financial",
                  "financial_advisor", "employment_agency", "travel_agent", "property_management",
                  "advertising_agency", "architect"}
# Sectors that make a demoted kind of company relevant again.
_KEEP_PUBLIC = {"public_administration"}
_KEEP_EDUCATION = {"education_training"}
_KEEP_RETAIL = {"retail", "food_hospitality", "pharmacy", "beauty_wellness", "automotive_repair",
                "tourism_travel", "veterinary_animals", "agriculture_food", "banking_insurance"}
_KEEP_ASSOCIATION = {"non_profit"}
_OFFICE_SECTORS = {"administration_accounting", "banking_insurance", "law_firms", "real_estate",
                   "hr_consulting", "architecture_design", "marketing_communication", "tourism_travel"}
_RESEARCH_WORDS = ("research", "researcher", "ricercatore", "ricercatrice", "ricerca", "phd", "dottorato",
                   "postdoc", "post doc", "assegnista", "scientist", "professor", "docente", "lecturer")


@dataclass
class RankContext:
    sectors: list[str] = field(default_factory=list)
    ateco: list[str] = field(default_factory=list)
    sector_terms: dict[str, str] = field(default_factory=dict)    # normalised term -> sector key
    profile_terms: set[str] = field(default_factory=set)
    ateco_terms: set[str] = field(default_factory=set)
    research_profile: bool = False


@dataclass
class Ranking:
    score: float
    reasons: list[str] = field(default_factory=list)

    @property
    def why(self) -> str:
        return "; ".join(self.reasons)


def _phrases(text: str, longest: int = 4) -> set[str]:
    words = normalize_text(text).split()
    found: set[str] = set()
    for n in range(1, longest + 1):
        for i in range(len(words) - n + 1):
            found.add(" ".join(words[i:i + n]))
    return found


def _terms_of(text: str) -> set[str]:
    """Useful single words and the whole (short) phrase of a CV value or a title."""
    norm = normalize_text(text)
    result = {w for w in norm.split() if len(w) >= _MIN_TERM_LENGTH and w not in _STOP_WORDS
              and not w.isdigit()}
    if 2 <= len(norm.split()) <= 3 and not all(w in _STOP_WORDS for w in norm.split()):
        result.add(norm)
    return result


def context_for(profile=None, sectors: list[str] | None = None, ateco: list[str] | None = None) -> RankContext:
    """The ranking context of a search: sector, profile and ATECO name terms."""
    chosen = [s for s in (sectors or []) if s in SECTORS]
    context = RankContext(sectors=chosen, ateco=list(ateco or []))
    for key in chosen:
        for term in SECTOR_NAME_TERMS.get(key, ()):
            context.sector_terms.setdefault(normalize_text(term), key)
    if profile is not None:
        texts: list[str] = []
        for attribute in ("roles", "skills"):
            for item in getattr(profile, attribute, None) or []:
                texts += [getattr(item, "value", "") or "", getattr(item, "original", "") or ""]
        for experience in getattr(profile, "experiences", None) or []:
            texts.append(getattr(experience, "title", "") or "")
        for text in texts:
            if text:
                context.profile_terms |= _terms_of(text)
        roles = " ".join(normalize_text(getattr(r, "value", "") or "") for r in getattr(profile, "roles", None) or [])
        roles += " " + " ".join(normalize_text(getattr(e, "title", "") or "")
                                for e in getattr(profile, "experiences", None) or [])
        words = set(roles.split())
        context.research_profile = any((w in words) if " " not in w else (w in roles) for w in _RESEARCH_WORDS)
    if "education_training" in chosen:
        context.research_profile = True
    if context.ateco:
        from .ateco import describe

        for code in context.ateco:
            described = describe(code)
            if described is not None:
                context.ateco_terms |= _terms_of(described.name_it) | _terms_of(described.name_en)
    # a term shared with the sector lists counts once, as a sector term
    context.profile_terms -= set(context.sector_terms)
    context.ateco_terms -= set(context.sector_terms) | context.profile_terms | _ATECO_QUALIFIERS
    context.ateco_terms = {t for t in context.ateco_terms if len(t) >= 6}
    return context


def _matches(phrases: set[str], terms) -> list[str]:
    return sorted({t for t in terms if t in phrases}, key=lambda t: (-len(t), t))


def _tag_pairs(company: Company) -> set[tuple[str, str]]:
    return {(str(k), str(v)) for k, v in public_tags(company.osm_tags).items()}


_NEEDLE_CACHE: dict[tuple[str, ...], tuple[tuple[str, str], ...]] = {}
_NEEDLE_CACHE_LOCK = threading.Lock()


def normalized_needles(needles) -> tuple[tuple[str, str], ...]:
    """((needle, normalised needle), ...) of a fixed word list, computed once per list
    (the lists are module constants: normalising them again for every company was most of
    the ranking's time). Thread-safe; a list given as another type is normalised as is."""
    key = needles if isinstance(needles, tuple) else tuple(needles)
    found = _NEEDLE_CACHE.get(key)
    if found is None:
        found = tuple((needle, normalize_text(needle)) for needle in key)
        with _NEEDLE_CACHE_LOCK:
            if len(_NEEDLE_CACHE) > 256:   # only fixed lists are expected: never grows unbounded
                _NEEDLE_CACHE.clear()
            _NEEDLE_CACHE[key] = found
    return found


def _name_has(norm_name: str, needles) -> str:
    padded = f" {norm_name} "
    for needle, n in normalized_needles(needles):
        if not n:
            continue
        if f" {n} " in padded or (needle.endswith(" ") and padded.startswith(f" {n} ")):
            return n
    return ""


def _industrial_tag(pairs: set[tuple[str, str]]) -> str:
    for key, value in sorted(pairs):
        if (key, value) == ("man_made", "works"):
            return "man_made=works"
        if key == "industrial" and value not in ("warehouse", "logistics", "depot", "scrap_yard"):
            return f"industrial={value}"
        if key == "product":
            return f"product={value}"
    for key, value in sorted(pairs):
        if key == "craft" and value not in ("yes",):
            return f"craft={value}"
        if (key, value) in (("office", "company"), ("office", "engineer")):
            return f"office={value}"
    return ""


def _kind(company: Company, pairs: set[tuple[str, str]], norm_name: str) -> tuple[str, str]:
    """(kind of demoted company, evidence) or ("", "")."""
    hit = next((f"{k}={v}" for k, v in sorted(pairs) if (k, v) in _PUBLIC_TAGS), "")
    if hit or (name := _name_has(norm_name, _PUBLIC_NAMES)):
        return "public", hit or f"name: {name}"
    hit = next((f"{k}={v}" for k, v in sorted(pairs) if (k, v) in _EDUCATION_TAGS), "")
    if hit or (name := _name_has(norm_name, _EDUCATION_NAMES)):
        return "education", hit or f"name: {name}"
    hit = next((f"{k}={v}" for k, v in sorted(pairs) if (k, v) in _ASSOCIATION_TAGS), "")
    if hit or (name := _name_has(norm_name, _ASSOCIATION_NAMES)):
        return "association", hit or f"name: {name}"
    hit = next((f"{k}={v}" for k, v in sorted(pairs)
                if k in _RETAIL_KEYS or (k == "amenity" and v in _RETAIL_AMENITIES)), "")
    if hit:
        return "retail", hit
    hit = next((f"office={v}" for k, v in sorted(pairs) if k == "office" and v in _OFFICE_VALUES), "")
    if hit:
        return "office", hit
    return "", ""


_KIND_TEXT = {"public": "public administration", "education": "school / university / research",
              "association": "association", "retail": "shop / hospitality", "office": "unrelated office"}


def rank(company: Company, context: RankContext) -> Ranking:
    """Score and reasons of one company (see the module docstring)."""
    reasons: list[str] = []
    tags = company.osm_tags or {}
    works = tags.get("man_made") == "works"
    # "man_made=works" is any factory (dairy, clothes, mattresses...): it says "industrial
    # site", not "this sector". The sector match is computed without it and the factory
    # gets WORKS_SCORE; what it makes (product=*) decides the rest.
    scored = company
    if works:
        scored = Company(name=company.name, lat=company.lat, lon=company.lon,
                         osm_tags={k: v for k, v in tags.items() if k != "man_made"})
    if context.sectors:
        # the profile's sectors in order of relevance: the first one counts fully
        per_sector = [(preliminary_relevance(scored, [key]) * max(SECTOR_ORDER_FLOOR,
                                                                    1.0 - SECTOR_ORDER_STEP * i), key)
                      for i, key in enumerate(context.sectors)]
        base = max(value for value, _ in per_sector)
        names = [SECTORS[key].name_en for value, key in per_sector if value >= 0.6]
        if names:
            reasons.append("map/name matches " + ", ".join(names[:2]))
    else:
        base = preliminary_relevance(scored, [])
    if works:
        base = max(base, WORKS_SCORE)
    extra_fields = getattr(company, "fields", None) or {}
    if "wikidata" in str(extra_fields.get("_discovered_by", "")).split(",") and base < WIKIDATA_BASE:
        # listed on Wikidata with an official website and the head office in the region:
        # an established company, usually a large employer
        base = WIKIDATA_BASE
        reasons.append("established company (Wikidata)")
    score = base
    products = [p for p in re.split(r"[;,]", str(tags.get("product", "") or "")) if p.strip()]
    if products:
        product_text = " ".join(p.replace("_", " ") for p in products)
        product_phrases = _phrases(product_text)
        wanted = set(context.sector_terms) | context.profile_terms | context.ateco_terms | INDUSTRIAL_PRODUCTS
        hits = _matches(product_phrases, wanted)
        if hits:
            score += PRODUCT_BONUS
            reasons.append(f"makes {product_text[:50]}")
        elif _matches(product_phrases, CONSUMER_PRODUCTS):
            score *= CONSUMER_PRODUCT_FACTOR
            reasons.append(f"demoted: makes {product_text[:50]} (not your field)")
    norm_name = normalize_text(company.name)
    phrases = _phrases(company.name)
    sector_hits = _matches(phrases, context.sector_terms)
    if sector_hits:
        # the name states the trade ("Macchine Automatiche", "Automazioni"): at least as
        # telling as a sector tag on the map, plus the bonus
        score = max(score, NAME_FLOOR) + SECTOR_NAME_BONUS + EXTRA_NAME_BONUS * min(
            MAX_EXTRA_TERMS, len(sector_hits) - 1)
        reasons.append("name: " + ", ".join(sector_hits[:3]))
    profile_hits = _matches(phrases, context.profile_terms)
    if profile_hits:
        score += PROFILE_NAME_BONUS
        reasons.append("name matches the CV: " + ", ".join(profile_hits[:2]))
    ateco_hits = _matches(phrases, context.ateco_terms)
    if ateco_hits and not sector_hits:
        score += ATECO_NAME_BONUS
        reasons.append("name matches an ATECO category: " + ", ".join(ateco_hits[:2]))
    pairs = _tag_pairs(company)
    industrial = _industrial_tag(pairs)
    if industrial:
        score += INDUSTRIAL_TAG_BONUS
        reasons.append(f"industrial site ({industrial})")
    extra = getattr(company, "fields", None) or {}
    # Wikidata industry and short description ("azienda italiana produttrice di riduttori")
    industry = "; ".join(str(extra.get(k, "") or "") for k in ("industry", "about") if extra.get(k))
    if industry:
        industry_phrases = _phrases(industry)
        unrelated = _matches(industry_phrases, UNRELATED_INDUSTRIES - set(context.sector_terms))
        hits = _matches(industry_phrases, set(context.sector_terms) | INDUSTRIAL_PRODUCTS | INDUSTRY_WORDS)
        if unrelated and not (set(context.sectors) & _KEEP_RETAIL):
            score *= UNRELATED_INDUSTRY_FACTOR
            reasons.append(f"demoted: Wikidata industry {industry[:50]}")
        elif hits:
            score += WIKIDATA_INDUSTRY_BONUS
            reasons.append(f"Wikidata industry: {industry[:60]}")
    try:
        employees = int(float(str(extra.get("employees", "") or 0).replace(",", "").strip() or 0))
    except ValueError:
        employees = 0
    if employees >= LARGE_EMPLOYER:
        score += LARGE_EMPLOYER_BONUS
        reasons.append(f"large employer ({employees} employees)")
    elif employees >= MEDIUM_EMPLOYER:
        score += MEDIUM_EMPLOYER_BONUS
        reasons.append(f"{employees} employees")
    trend = extra.get("revenue_trend")
    if isinstance(trend, dict) and trend.get("cagr") is not None:
        # a company whose STORED revenue figures grow (its card: >= 3 yearly values) is a
        # little more promising: financials/trend.py ranking_bonus (+0.05 from +2 %/year,
        # +0.10 from +10 %/year). Never negative: no figures or a decline never push down.
        from ..financials.trend import ranking_bonus

        bonus = ranking_bonus(trend)
        if bonus:
            score += bonus
            reasons.append(f"revenue growing {trend['cagr'] * 100:+.1f} %/year over "
                           f"{trend.get('first_year')}-{trend.get('last_year')} (+{bonus:.2f})")
    if context.ateco:
        from .ateco import matches as ateco_matches
        from .merge import ateco_of

        code = ateco_of(company)
        if code and ateco_matches(code, context.ateco):
            score = max(score, ATECO_MATCH_SCORE) + 0.3
            reasons.append(f"register ATECO {code} in your categories")
        elif code:
            score -= ATECO_MISMATCH_PENALTY
            reasons.append(f"register ATECO {code} outside your categories")
    if company.site:
        score += WEBSITE_BONUS
        reasons.append("has a website")
    kind, evidence = _kind(company, pairs, norm_name)
    keep = {"public": _KEEP_PUBLIC, "education": _KEEP_EDUCATION, "association": _KEEP_ASSOCIATION,
            "retail": _KEEP_RETAIL, "office": _OFFICE_SECTORS}.get(kind, set())
    if kind and not (set(context.sectors) & keep) and not (kind == "education" and context.research_profile):
        factor = {"public": PUBLIC_FACTOR, "education": EDUCATION_FACTOR, "association": ASSOCIATION_FACTOR,
                  "retail": RETAIL_FACTOR, "office": OFFICE_FACTOR}[kind]
        if kind == "retail" and industrial:
            factor = max(factor, 0.7)  # a factory shop
        score *= factor
        reasons.append(f"demoted: {_KIND_TEXT[kind]} ({evidence})")
    if not reasons:
        reasons.append("no specific signal (generic company)")
    return Ranking(round(max(0.0, score), 4), reasons)


def rank_all(companies: list[Company], context: RankContext) -> dict[int, Ranking]:
    """Ranking of every company by `id()` (also stored as fields["why_selected"])."""
    result: dict[int, Ranking] = {}
    for company in companies:
        ranking = rank(company, context)
        result[id(company)] = ranking
        try:
            company.fields["why_selected"] = ranking.why
        except (AttributeError, TypeError):
            pass
    return result


_SPLIT = re.compile(r"\s*;\s*")


def why_selected(company: Company) -> list[str]:
    """The stored reasons of a company (empty when it was never ranked)."""
    text = str((getattr(company, "fields", None) or {}).get("why_selected", "") or "")
    return [p for p in _SPLIT.split(text) if p]
