"""Business sectors: OpenStreetMap tags and IT/EN keywords.

The table links the professions in the profile to the company types that can
be searched on OpenStreetMap. It is the most delicate point for result
quality: it should be reviewed with real use.
"""

import re
import unicodedata
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

from ..models import Company, CVProfile


@dataclass(frozen=True)
class Sector:
    key: str
    name_it: str
    name_en: str
    # OSM (key, value) pairs; the value "*" means any value.
    osm_tags: list[tuple[str, str]] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


def normalize_text(text: str) -> str:
    """Lowercase, no accents or punctuation, collapsed spaces."""
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold())
    unaccented = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^0-9a-z]+", " ", unaccented).split())


def _s(key, name_it, name_en, tag, words) -> Sector:
    return Sector(key, name_it, name_en, list(tag), list(words))


_LISTING: list[Sector] = [
    _s(
        "administration_accounting", "Amministrazione e contabilità",
        "Administration and accounting",
        [("office", "accountant"), ("office", "tax_advisor"), ("office", "financial_advisor")],
        ["contabile", "contabilita", "addetto amministrazione", "impiegato amministrativo",
         "impiegata amministrativa", "segreteria", "segretaria", "fatturazione", "bilancio",
         "paghe e contributi", "back office", "ragioniere", "commercialista", "prima nota",
         "accountant", "bookkeeper", "bookkeeping", "accounting", "administrative assistant",
         "office clerk", "payroll", "accounts payable", "accounts receivable", "auditing"],
    ),
    _s(
        "it_software", "Informatica e software", "IT and software",
        [("office", "it"), ("office", "telecommunication"), ("shop", "computer")],
        ["sviluppatore", "sviluppatrice", "programmatore", "software", "informatico",
         "sistemista", "web developer", "sviluppo web", "python", "java", "javascript",
         "devops", "data scientist", "data analyst", "cybersecurity", "database", "cloud",
         "backend", "frontend", "full stack", "software engineer", "developer",
         "programmer", "web designer", "analista programmatore", "system administrator",
         "machine learning", "informatica"],
    ),
    _s(
        "engineering_design", "Ingegneria e progettazione", "Engineering and design",
        [("office", "engineer"), ("office", "consulting"), ("office", "research")],
        ["ingegnere", "ingegneria", "progettista", "progettazione", "cad", "autocad",
         "solidworks", "ufficio tecnico", "disegnatore tecnico", "ricerca e sviluppo",
         "engineer", "design engineer", "mechanical engineer", "civil engineer",
         "r&d", "research and development", "technical drawing", "cad designer",
         "project engineer", "perito industriale"],
    ),
    _s(
        "manufacturing_metalworking", "Manifattura e metalmeccanica",
        "Manufacturing and metalworking",
        [("industrial", "machine_shop"), ("industrial", "factory"),
         ("industrial", "engineering"), ("craft", "metal_construction"),
         ("craft", "blacksmith"), ("craft", "tinsmith"), ("craft", "toolmaker"),
         ("man_made", "works")],
        ["operaio", "operaio specializzato", "saldatore", "tornitore", "fresatore", "cnc",
         "metalmeccanico", "macchine utensili", "assemblaggio", "attrezzista",
         "manutentore meccanico", "carpenteria metallica", "addetto alla produzione",
         "caposquadra", "welder", "machinist", "cnc operator", "assembler",
         "production operator", "production worker", "metalworker", "fabricator",
         "sheet metal", "lathe", "factory worker", "manufacturing"],
    ),
    _s(
        "construction", "Edilizia e costruzioni", "Construction",
        [("craft", "builder"), ("craft", "roofer"), ("craft", "plasterer"),
         ("craft", "tiler"), ("craft", "painter"), ("craft", "stonemason"),
         ("office", "construction_company")],
        ["muratore", "geometra", "edile", "capocantiere", "cantiere", "carpentiere edile",
         "imbianchino", "piastrellista", "ponteggiatore", "cartongessista",
         "construction worker", "site manager", "bricklayer", "foreman", "plasterer",
         "tiler", "roofer", "construction", "building contractor", "site supervisor"],
    ),
    _s(
        "wood_furniture", "Lavorazione del legno e arredo", "Woodworking and furniture",
        [("craft", "carpenter"), ("craft", "joiner"), ("craft", "sawmill"),
         ("craft", "upholsterer"), ("shop", "furniture"), ("shop", "kitchen")],
        ["falegname", "falegnameria", "ebanista", "carpentiere", "mobili", "arredamento",
         "lavorazione del legno", "serramenti", "infissi", "tappezziere", "restauratore ligneo",
         "woodworker", "woodworking", "carpenter", "cabinet maker", "cabinetmaker",
         "furniture maker", "joiner", "upholsterer", "wood", "furniture"],
    ),
    _s(
        "electrical_plumbing", "Impianti elettrici e termoidraulici",
        "Electrical and plumbing installation",
        [("craft", "electrician"), ("craft", "plumber"), ("craft", "hvac"),
         ("shop", "electrical")],
        ["elettricista", "idraulico", "termoidraulico", "impiantista", "frigorista",
         "installatore", "caldaie", "climatizzazione", "impianti elettrici",
         "quadri elettrici", "impianti civili", "manutentore impianti",
         "electrician", "plumber", "hvac technician", "hvac", "heating engineer",
         "installer", "pipefitter", "electrical installation", "air conditioning"],
    ),
    _s(
        "healthcare", "Sanità e cura della persona", "Healthcare",
        [("amenity", "clinic"), ("amenity", "doctors"), ("amenity", "dentist"),
         ("amenity", "hospital"), ("healthcare", "*")],
        ["infermiere", "infermiera", "medico", "operatore sociosanitario", "oss",
         "fisioterapista", "dentista", "odontoiatra", "igienista dentale",
         "tecnico di laboratorio", "tecnico di radiologia", "psicologo", "logopedista",
         "nurse", "physician", "doctor", "physiotherapist", "physical therapist",
         "healthcare assistant", "dental hygienist", "radiographer", "medical",
         "clinical", "nursing", "paramedic"],
    ),
    _s(
        "pharmacy", "Farmacia e parafarmacia", "Pharmacy",
        [("amenity", "pharmacy"), ("shop", "chemist"), ("shop", "medical_supply")],
        ["farmacista", "pharmacy", "parafarmacia", "banconista di farmacia", "galenica",
         "preparatore in farmacia", "tecnico di farmacia", "pharmacist", "pharmacy",
         "pharmacy technician", "pharmacy assistant", "dispensing", "drugstore"],
    ),
    _s(
        "education_training", "Istruzione e formazione", "Education and training",
        [("amenity", "school"), ("amenity", "kindergarten"), ("amenity", "college"),
         ("amenity", "university"), ("amenity", "language_school"),
         ("amenity", "driving_school"), ("amenity", "music_school")],
        ["insegnante", "docente", "maestra", "maestro", "educatore", "educatrice",
         "formatore", "professore", "professoressa", "tutor", "asilo nido",
         "scuola dell'infanzia", "istruttore", "lezioni private", "ricercatore",
         "teacher", "lecturer", "trainer", "educator", "instructor", "tutor",
         "teaching assistant", "professor", "school", "curriculum development"],
    ),
    _s(
        "retail", "Commercio al dettaglio", "Retail",
        [("shop", "*")],
        ["commesso", "commessa", "addetto vendite", "addetta vendite", "cassiere",
         "cassiera", "negozio", "responsabile punto vendita", "banconiere",
         "visual merchandiser", "scaffalista", "vendita al dettaglio",
         "retail", "shop assistant", "sales assistant", "cashier", "store manager",
         "sales associate", "merchandiser", "retail sales", "shop floor"],
    ),
    _s(
        "food_hospitality", "Ristorazione e ospitalità", "Food service and hospitality",
        [("amenity", "restaurant"), ("amenity", "cafe"), ("amenity", "bar"),
         ("amenity", "fast_food"), ("amenity", "pub"), ("tourism", "hotel"),
         ("tourism", "guest_house"), ("tourism", "hostel")],
        ["cuoco", "cuoca", "cameriere", "cameriera", "barista", "chef", "pizzaiolo",
         "aiuto cuoco", "lavapiatti", "receptionist", "addetto sala", "sommelier",
         "governante", "portiere d'albergo", "banchetti", "ristorazione",
         "cook", "waiter", "waitress", "bartender", "hotel", "front desk", "concierge",
         "housekeeping", "kitchen assistant", "line cook", "sous chef", "hospitality"],
    ),
    _s(
        "logistics_transport", "Logistica e trasporti", "Logistics and transport",
        [("office", "logistics"), ("industrial", "warehouse"), ("industrial", "logistics"),
         ("office", "moving_company")],
        ["magazziniere", "magazziniera", "autista", "corriere", "carrellista",
         "logistica", "spedizioniere", "trasporti", "camionista", "trasportatore",
         "addetto al picking", "responsabile magazzino", "patente c", "patente ce",
         "driver", "delivery driver", "forklift", "warehouse", "logistics",
         "supply chain", "dispatcher", "freight", "picking", "courier", "lorry driver"],
    ),
    _s(
        "automotive_repair", "Automotive e officine", "Automotive and repair shops",
        [("shop", "car_repair"), ("shop", "car"), ("shop", "tyres"),
         ("shop", "motorcycle"), ("shop", "car_parts")],
        ["meccanico", "carrozziere", "gommista", "elettrauto", "autofficina", "motorista",
         "meccanico auto", "diagnosi veicoli", "riparazione auto", "verniciatore auto",
         "mechanic", "car mechanic", "auto mechanic", "auto body", "body shop",
         "tire fitter", "vehicle technician", "automotive", "motorcycle mechanic"],
    ),
    _s(
        "banking_insurance", "Banche e assicurazioni", "Banking and insurance",
        [("amenity", "bank"), ("office", "insurance"), ("office", "financial")],
        ["bancario", "impiegato di banca", "consulente finanziario", "assicurativo",
         "assicurazioni", "promotore finanziario", "agente assicurativo", "credito",
         "gestore clienti", "finanziamenti", "sportello bancario",
         "banker", "bank teller", "insurance", "insurance agent", "financial advisor",
         "loan officer", "credit analyst", "risk analyst", "broker", "underwriter",
         "banking", "wealth management"],
    ),
    _s(
        "law_firms", "Studi legali e notarili", "Law firms",
        [("office", "lawyer"), ("office", "notary")],
        ["avvocato", "avvocata", "praticante avvocato", "paralegale", "notaio",
         "legale", "diritto", "giurista", "consulente legale", "contrattualistica",
         "lawyer", "attorney", "paralegal", "legal counsel", "solicitor",
         "legal assistant", "legal secretary", "law", "notary", "legal"],
    ),
    _s(
        "architecture_design", "Architettura e design d'interni", "Architecture and interior design",
        [("office", "architect"), ("shop", "interior_decoration"), ("craft", "interior_designer")],
        ["architetto", "architettura", "progettista d'interni", "interior designer",
         "urbanista", "paesaggista", "rendering", "bim", "restauro",
         "architect", "architectural", "interior design", "landscape architect",
         "urban planner", "3d visualization", "revit", "sketchup"],
    ),
    _s(
        "marketing_communication", "Marketing e comunicazione", "Marketing and communication",
        [("office", "advertising_agency"), ("office", "marketing"), ("office", "newspaper"),
         ("craft", "photographer"), ("shop", "copyshop")],
        ["marketing", "comunicazione", "social media", "copywriter", "grafico",
         "graphic designer", "seo", "ufficio stampa", "giornalista", "addetto stampa",
         "community manager", "videomaker", "fotografo", "pubblicita", "web marketing",
         "advertising", "public relations", "brand manager", "content creator",
         "content marketing", "photographer", "journalist", "digital marketing",
         "graphic design", "video editor"],
    ),
    _s(
        "agriculture_food", "Agricoltura e industria alimentare",
        "Agriculture and food production",
        [("craft", "winery"), ("craft", "brewery"), ("craft", "confectionery"),
         ("craft", "beekeeper"), ("craft", "distillery"), ("craft", "oil_mill"),
         ("shop", "farm"), ("shop", "bakery"), ("shop", "butcher"),
         ("shop", "greengrocer"), ("landuse", "farmyard"), ("office", "forestry")],
        ["agricoltore", "enologo", "panettiere", "panificatore", "pasticciere",
         "pasticcera", "casaro", "macellaio", "produzione alimentare",
         "azienda agricola", "vivaista", "giardiniere", "agronomo", "haccp",
         "addetto produzione alimentare", "salumiere", "frantoio", "cantina",
         "farmer", "agronomist", "baker", "butcher", "food production",
         "winemaker", "brewer", "food technologist", "farm worker", "gardener",
         "viticulture", "food processing"],
    ),
    _s(
        "chemicals_materials", "Chimica, plastica e materiali", "Chemicals and materials",
        [("industrial", "chemical"), ("man_made", "works")],
        ["chimico", "analista di laboratorio", "materie plastiche", "plastica",
         "vernici", "controllo qualita", "farmaceutico", "cosmetica", "tecnico di laboratorio chimico",
         "ingegnere chimico", "gomma", "ceramica", "laboratorio chimico",
         "chemist", "chemical engineer", "chemical", "lab technician",
         "laboratory analyst", "quality control", "process engineer", "plastics",
         "polymer", "coatings", "pharmaceutical", "materials scientist"],
    ),
    _s(
        "energy_environment", "Energia e ambiente", "Energy and environment",
        [("power", "plant"), ("office", "energy_supplier"), ("industrial", "oil"),
         ("industrial", "gas")],
        ["fotovoltaico", "energia", "rinnovabili", "energy manager", "efficienza energetica",
         "impianti eolici", "gestione rifiuti", "ambiente", "smaltimento rifiuti",
         "termotecnico", "idroelettrico", "biomassa", "certificatore energetico",
         "energy", "solar", "photovoltaic", "wind turbine", "renewable", "waste management",
         "environmental", "energy efficiency", "power plant", "sustainability"],
    ),
    _s(
        "real_estate", "Immobiliare", "Real estate",
        [("office", "estate_agent"), ("office", "property_management")],
        ["agente immobiliare", "real_estate", "amministratore di condominio",
         "valutatore immobiliare", "estimatore", "mediatore immobiliare",
         "gestione immobili", "locazioni", "compravendita immobili",
         "real estate", "property manager", "property management", "leasing agent",
         "real estate agent", "estate agent", "realtor", "letting"],
    ),
    _s(
        "beauty_wellness", "Estetica, benessere e sport", "Beauty, wellness and fitness",
        [("shop", "hairdresser"), ("shop", "beauty"), ("shop", "massage"),
         ("leisure", "fitness_centre"), ("leisure", "sports_centre")],
        ["parrucchiere", "parrucchiera", "estetista", "acconciatore", "acconciatrice",
         "massaggiatore", "massaggiatrice", "personal trainer", "istruttore fitness",
         "centro benessere", "barbiere", "onicotecnica", "istruttore di nuoto",
         "hairdresser", "hair stylist", "beautician", "esthetician", "massage therapist",
         "fitness instructor", "barber", "nail technician", "day spa", "wellness",
         "gym instructor", "cosmetologist"],
    ),
    _s(
        "public_administration", "Pubblica amministrazione", "Public administration",
        [("amenity", "townhall"), ("amenity", "courthouse"), ("office", "government"),
         ("amenity", "post_office")],
        ["pubblica amministrazione", "impiegato comunale", "ente pubblico",
         "concorso pubblico", "funzionario", "istruttore amministrativo", "anagrafe",
         "protocollo", "polizia locale", "vigile urbano", "sportello al cittadino",
         "civil servant", "public administration", "public sector", "municipal",
         "government", "local authority", "council officer", "public official"],
    ),
    _s(
        "non_profit", "Associazioni e terzo settore", "Non-profit and social sector",
        [("office", "association"), ("office", "ngo"), ("office", "foundation"),
         ("office", "charity"), ("amenity", "social_facility"),
         ("amenity", "community_centre")],
        ["volontario", "volontaria", "associazione", "onlus", "assistente sociale",
         "operatore sociale", "cooperativa sociale", "mediatore culturale",
         "terzo settore", "educatore professionale", "raccolta fondi", "servizio civile",
         "volunteer", "non profit", "nonprofit", "social worker", "ngo", "charity",
         "fundraising", "case manager", "community worker", "youth worker"],
    ),
    _s(
        "hr_consulting", "Risorse umane e consulenza", "HR and consulting",
        [("office", "employment_agency"), ("office", "consulting"), ("office", "company")],
        ["risorse umane", "selezione del personale", "consulente aziendale", "recruiter",
         "formazione aziendale", "agenzia per il lavoro", "ricerca e selezione",
         "responsabile hr", "consulente del lavoro", "business analyst",
         "human resources", "talent acquisition", "business consultant", "hr specialist",
         "hr manager", "recruitment", "staffing", "management consultant", "consulting"],
    ),
    _s(
        "electronics_automation", "Elettronica e automazione", "Electronics and automation",
        [("industrial", "electrical"), ("craft", "electronics_repair"),
         ("shop", "electronics"), ("man_made", "works")],
        ["elettronico", "automazione", "plc", "robotica", "tecnico elettronico",
         "manutentore elettrico", "strumentazione", "embedded", "firmware",
         "microcontrollori", "meccatronico", "schede elettroniche", "collaudatore",
         "electronics", "electronic technician", "automation", "robotics",
         "mechatronics", "embedded systems", "microcontroller", "pcb", "test technician",
         "maintenance technician", "industrial automation",
         # PLC / SCADA / system-integrator vocabulary (the skills of an automation engineer)
         "scada", "hmi", "dcs", "tia portal", "wincc", "step 7", "codesys", "twincat", "grafcet",
         "system integrator", "integratore di sistemi", "commissioning", "messa in servizio",
         "robot", "isole robotizzate", "robotic cells", "profinet", "profibus", "modbus", "opc ua",
         "motion control", "industrial networks", "reti industriali", "ot security"],
    ),
    _s(
        "textile_fashion", "Tessile, moda e calzature", "Textile, fashion and footwear",
        [("craft", "tailor"), ("craft", "shoemaker"), ("shop", "fabric"), ("shop", "tailor")],
        ["sarto", "sarta", "modellista", "tessile", "abbigliamento", "calzaturiero",
         "pellettiere", "cucitrice", "stilista", "maglieria", "confezioni",
         "tailor", "seamstress", "fashion designer", "pattern maker", "textile",
         "garment", "knitting", "shoemaker", "leatherworker", "sewing machine operator"],
    ),
    _s(
        "tourism_travel", "Turismo e viaggi", "Tourism and travel",
        [("shop", "travel_agency"), ("office", "travel_agent"), ("office", "tour_operator")],
        ["agente di viaggio", "tour operator", "guida turistica", "turismo",
         "accompagnatore turistico", "agenzia di viaggi", "biglietteria", "incoming",
         "travel agent", "tour guide", "tourism", "travel consultant", "booking",
         "ticketing", "travel agency", "tour manager"],
    ),
    _s(
        "veterinary_animals", "Veterinaria e animali", "Veterinary and animal care",
        [("amenity", "veterinary"), ("shop", "pet"), ("shop", "pet_grooming")],
        ["veterinario", "veterinaria", "assistente veterinario", "toelettatore",
         "toelettatura", "allevatore", "addetto animali", "pensione per animali",
         "veterinarian", "vet", "vet nurse", "veterinary nurse", "pet groomer",
         "animal care", "animal health", "dog groomer", "kennel"],
    ),
]

SECTORS: dict[str, Sector] = {s.key: s for s in _LISTING}

# --- Matching against the profile -------------------------------------------------

SECTOR_THRESHOLD = 0.5        # minimum score to include a sector
MIN_RATIO_TO_BEST = 0.4  # a sector must score at least 40% of the best one
MAX_PROFILE_SECTORS = 5
FUZZY_MIN_LENGTH = 7      # shorter keywords: exact match only
FUZZY_THRESHOLD = 87
SKILL_WEIGHT = 0.5
DEGREE_WEIGHT = 0.4
MAX_PHRASE_WORDS = 4


def _phrases(normalized_text: str) -> dict[int, set[str]]:
    """All sequences of 1..4 consecutive words, grouped by word count."""
    words = normalized_text.split()
    result: dict[int, set[str]] = {}
    for n in range(1, MAX_PHRASE_WORDS + 1):
        for i in range(len(words) - n + 1):
            result.setdefault(n, set()).add(" ".join(words[i : i + n]))
    return result


def _normalized_keywords(sector: Sector) -> list[tuple[str, int]]:
    seen_ids = set()
    result = []
    for p in sector.keywords:
        n = normalize_text(p)
        if n and n not in seen_ids:
            seen_ids.add(n)
            result.append((n, len(n.split())))
    return result


_NORMALIZED_KEYS: dict[str, list[tuple[str, int]]] = {
    k: _normalized_keywords(s) for k, s in SECTORS.items()
}


def _match_score(sentences: dict[int, set[str]], word: str, n_words: int) -> float:
    """1.0 if the keyword appears among the phrases; otherwise similarity (0 if low)."""
    candidates = sentences.get(n_words)
    if not candidates:
        return 0.0
    if word in candidates:
        return 1.0
    if len(word) < FUZZY_MIN_LENGTH:
        return 0.0
    best = process.extractOne(
        word, candidates, scorer=fuzz.ratio, score_cutoff=FUZZY_THRESHOLD
    )
    if best and _derived_word(word, best[0]):
        return 0.0
    return best[1] / 100.0 if best else 0.0


# Endings that keep the meaning of a word (plurals, gender): "tecnico" ~ "tecnici".
_INFLECTIONS = {"", "s", "es", "i", "e", "a", "o", "he", "hi"}


def _derived_word(keyword: str, candidate: str) -> bool:
    """True when one word only extends the other into a different word (fuzzy matching
    would confuse them): "mechanic" -> "mechanical", "logistic" -> "logistical"."""
    for short, long in ((keyword, candidate), (candidate, keyword)):
        if long.startswith(short) and long[len(short):] not in _INFLECTIONS:
            return True
    return False


def sector_scores(profile: CVProfile) -> dict[str, float]:
    """Score of each sector for the profile (only those with score > 0)."""
    elements: list[tuple[dict[int, set[str]], float]] = []
    for i, item in enumerate(profile.roles):
        elements.append((_phrases(normalize_text(item.value)), 1.0 / (1.0 + 0.1 * i)))
    for item in profile.skills:
        elements.append((_phrases(normalize_text(item.value)), SKILL_WEIGHT))
    for item in profile.degrees:
        elements.append((_phrases(normalize_text(item.value)), DEGREE_WEIGHT))

    scores: dict[str, float] = {}
    for key, words in _NORMALIZED_KEYS.items():
        total = 0.0
        for sentences, weight in elements:
            if not sentences:
                continue
            best = 0.0
            for word, n in words:
                best = max(best, _match_score(sentences, word, n))
                if best >= 1.0:
                    break
            total += best * weight
        if total > 0:
            scores[key] = total
    return scores


def sectors_for_profile(profile: CVProfile) -> list[str]:
    """SECTORS keys inferred from roles and skills, most relevant first.

    Empty list if no sector passes the threshold.
    """
    scores = sector_scores(profile)
    order = {k: i for i, k in enumerate(SECTORS)}
    candidates = sorted(
        ((k, p) for k, p in scores.items() if p >= SECTOR_THRESHOLD),
        key=lambda kp: (-kp[1], order[kp[0]]),
    )
    if not candidates:
        return []
    best = candidates[0][1]
    chosen = [k for k, p in candidates if p >= best * MIN_RATIO_TO_BEST]
    return chosen[:MAX_PROFILE_SECTORS]


# --- Widening: companies of unknown sector -----------------------------------------
#
# Many OSM companies carry only a generic tag ("office=company", "office=yes",
# "industrial=*"): a sector-filtered search never returns them, although almost
# any company can hire e.g. an accountant, a clerk or a technician. With
# ``include_generic`` they are searched too and ranked below the sector matches
# (``UNKNOWN_SECTOR_SCORE``) instead of being excluded.

GENERIC_COMPANY_TAGS: list[tuple[str, str]] = [
    ("office", "company"), ("office", "yes"), ("industrial", "*"), ("man_made", "works"),
    ("craft", "yes"),
]
# Tags that say nothing about the sector even when a sector lists them.
UNINFORMATIVE_TAGS: list[tuple[str, str]] = [("office", "company"), ("office", "yes"), ("craft", "yes")]
UNKNOWN_SECTOR_SCORE = 0.3  # below a name-only match (NAME_SCORE)


def search_tags(sectors: list[str] | None, include_generic: bool = False) -> list[tuple[str, str]]:
    """OSM (key, value) pairs to search for the sectors; with ``include_generic`` also the
    generic company tags. Empty list when no known sector is given (the caller then uses
    its own generic set, e.g. ``overpass.GENERIC_TAGS``)."""
    pairs: list[tuple[str, str]] = []
    for key in sectors or []:
        if key in SECTORS:
            for pair in SECTORS[key].osm_tags:
                if pair not in pairs:
                    pairs.append(pair)
    if pairs and include_generic:
        pairs += [p for p in GENERIC_COMPANY_TAGS if p not in pairs]
    return pairs


def has_generic_tag(company: Company) -> bool:
    """True when the company has one of GENERIC_COMPANY_TAGS (sector unknown)."""
    tags = company.osm_tags or {}
    return any(k in tags and (v == "*" or tags[k] == v) for k, v in GENERIC_COMPANY_TAGS)


# --- Preliminary relevance of a company ---------------------------------------

EXACT_TAG_SCORE = 1.0
GENERIC_TAG_SCORE = 0.7   # match with value "*"
# Tags a sector lists although they are mostly shops or repair counters, not the
# employers its profiles look for (live, Bologna 15 km: electronics shops ranked above
# automation companies). They still match, with a lower score.
WEAK_TAG_SCORE = 0.45
WEAK_TAGS: dict[str, set[tuple[str, str]]] = {
    "electronics_automation": {("shop", "electronics"), ("craft", "electronics_repair")},
    "it_software": {("shop", "computer")},
}
NAME_SCORE = 0.6
BOTH_BONUS = 0.2


def preliminary_relevance(company: Company, sectors: list[str],
                          unknown_sector_score: float = UNKNOWN_SECTOR_SCORE) -> float:
    """Estimate 0..1 of how relevant the company is to the sectors, from OSM tags and name.

    With no sectors requested there is no information: returns 0.5 (neutral).
    A company that matches no sector but has only a generic company tag (see
    GENERIC_COMPANY_TAGS) gets ``unknown_sector_score`` (0 to rank it with the
    unrelated ones, as before); a generic tag listed by a sector (e.g.
    ``office=company`` for HR) is worth the same, never an exact match.
    """
    valid_items = [SECTORS[k] for k in sectors if k in SECTORS]
    if not valid_items:
        return 0.5 if not sectors else 0.0
    generic_pairs = set(UNINFORMATIVE_TAGS)

    name_phrases = _phrases(normalize_text(company.name))
    best = 0.0
    for sector in valid_items:
        tag_score = 0.0
        for key, value in sector.osm_tags:
            present = company.osm_tags.get(key)
            if present is None:
                continue
            if (key, value) in generic_pairs or (key, present) in generic_pairs:
                if value == "*" or present == value:
                    tag_score = max(tag_score, unknown_sector_score)
            elif value == "*":
                tag_score = max(tag_score, GENERIC_TAG_SCORE)
            elif present == value:
                weak = (key, value) in WEAK_TAGS.get(sector.key, ())
                tag_score = max(tag_score, WEAK_TAG_SCORE if weak else EXACT_TAG_SCORE)
        name_score = 0.0
        if name_phrases:
            for word, n in _NORMALIZED_KEYS[sector.key]:
                if word in name_phrases.get(n, ()):
                    name_score = NAME_SCORE
                    break
        total = max(tag_score, name_score) + BOTH_BONUS * min(
            tag_score, name_score
        )
        best = max(best, min(1.0, total))
    if best < unknown_sector_score and has_generic_tag(company):
        best = unknown_sector_score
    return round(best, 4)
