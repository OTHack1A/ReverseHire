"""Built-in word tables of the job search (Italian and English).

- a small gazetteer of towns (Emilia-Romagna and the main Italian cities) used when no
  place lookup (the local OSM index) is passed in; coordinates are approximate town
  centres, enough for a radius filter;
- groups of equivalent job words (IT <-> EN, masculine/feminine forms);
- contract, working-time and remote words of a query;
- sector, collective-contract (CCNL) and company-size hints of an ad, mapped to ATECO
  2025 divisions (two-digit codes, NACE Rev. 2.1 structure);
- staffing agency names;
- skill extraction through the CV skill dictionary (`cv/data/skills.json`) plus a few
  technologies it lacks.
"""

from __future__ import annotations

import re
from functools import lru_cache

from ._util import contains_phrase, fold, words

# --- places -------------------------------------------------------------------------------

# name -> (lat, lon, province code)
TOWNS: dict[str, tuple[float, float, str]] = {
    # Emilia-Romagna
    "bologna": (44.494, 11.343, "BO"), "modena": (44.647, 10.925, "MO"),
    "reggio emilia": (44.698, 10.631, "RE"), "parma": (44.801, 10.328, "PR"),
    "piacenza": (45.052, 9.693, "PC"), "ferrara": (44.836, 11.620, "FE"),
    "ravenna": (44.418, 12.204, "RA"), "forli": (44.222, 12.041, "FC"), "cesena": (44.139, 12.243, "FC"),
    "rimini": (44.060, 12.566, "RN"), "imola": (44.353, 11.715, "BO"), "carpi": (44.784, 10.885, "MO"),
    "sassuolo": (44.551, 10.784, "MO"), "formigine": (44.574, 10.847, "MO"),
    "fiorano modenese": (44.537, 10.817, "MO"), "maranello": (44.526, 10.866, "MO"),
    "mirandola": (44.887, 11.066, "MO"), "vignola": (44.481, 11.011, "MO"),
    "castelfranco emilia": (44.596, 11.050, "MO"), "spilamberto": (44.532, 11.017, "MO"),
    "castelnuovo rangone": (44.551, 10.938, "MO"), "nonantola": (44.678, 11.041, "MO"),
    "soliera": (44.738, 10.925, "MO"), "faenza": (44.285, 11.883, "RA"), "lugo": (44.420, 11.911, "RA"),
    "casalecchio di reno": (44.476, 11.275, "BO"), "san lazzaro di savena": (44.471, 11.405, "BO"),
    "castel san pietro terme": (44.398, 11.589, "BO"), "zola predosa": (44.488, 11.218, "BO"),
    "anzola dell'emilia": (44.547, 11.195, "BO"), "scandiano": (44.597, 10.692, "RE"),
    "correggio": (44.770, 10.782, "RE"), "guastalla": (44.920, 10.656, "RE"),
    "reggiolo": (44.917, 10.808, "RE"), "rubiera": (44.651, 10.782, "RE"),
    "casalgrande": (44.586, 10.734, "RE"), "fidenza": (44.866, 10.061, "PR"),
    "riccione": (43.999, 12.656, "RN"), "cento": (44.727, 11.288, "FE"),
    # main Italian cities
    "milano": (45.464, 9.190, "MI"), "torino": (45.070, 7.687, "TO"), "genova": (44.405, 8.946, "GE"),
    "venezia": (45.440, 12.316, "VE"), "verona": (45.438, 10.992, "VR"), "padova": (45.406, 11.877, "PD"),
    "vicenza": (45.548, 11.546, "VI"), "treviso": (45.666, 12.243, "TV"), "brescia": (45.541, 10.212, "BS"),
    "bergamo": (45.698, 9.677, "BG"), "mantova": (45.156, 10.791, "MN"), "cremona": (45.133, 10.023, "CR"),
    "firenze": (43.770, 11.256, "FI"), "prato": (43.880, 11.097, "PO"), "pisa": (43.716, 10.402, "PI"),
    "lucca": (43.843, 10.507, "LU"), "roma": (41.903, 12.496, "RM"), "napoli": (40.852, 14.268, "NA"),
    "bari": (41.117, 16.872, "BA"), "palermo": (38.116, 13.361, "PA"), "catania": (37.502, 15.087, "CT"),
    "cagliari": (39.224, 9.122, "CA"), "trento": (46.070, 11.121, "TN"), "bolzano": (46.498, 11.354, "BZ"),
    "trieste": (45.650, 13.777, "TS"), "udine": (46.062, 13.235, "UD"), "ancona": (43.616, 13.519, "AN"),
    "pesaro": (43.910, 12.913, "PU"), "perugia": (43.111, 12.389, "PG"), "pescara": (42.461, 14.216, "PE"),
}
TOWN_ALIASES = {
    "reggio nell'emilia": "reggio emilia", "reggio nell emilia": "reggio emilia", "forli'": "forli",
    "milan": "milano", "turin": "torino", "genoa": "genova", "venice": "venezia", "padua": "padova",
    "florence": "firenze", "rome": "roma", "naples": "napoli", "mantua": "mantova",
}
# province code -> capital town (for "provincia di X" and "(BO)")
PROVINCE_CAPITAL = {code: name for name, (_, _, code) in TOWNS.items()
                    if name in ("bologna", "modena", "reggio emilia", "parma", "piacenza", "ferrara", "ravenna",
                                "forli", "rimini", "milano", "torino", "genova", "venezia", "verona", "padova",
                                "vicenza", "treviso", "brescia", "bergamo", "mantova", "cremona", "firenze",
                                "prato", "pisa", "lucca", "roma", "napoli", "bari", "palermo", "catania",
                                "cagliari", "trento", "bolzano", "trieste", "udine", "ancona", "pesaro",
                                "perugia", "pescara")}
REGIONS = {"emilia-romagna", "emilia romagna", "lombardia", "veneto", "piemonte", "toscana", "lazio", "liguria",
           "marche", "umbria", "campania", "puglia", "sicilia", "sardegna", "trentino-alto adige", "friuli"}


def builtin_place(name: str) -> tuple[float, float, str] | None:
    """(lat, lon, province) of a town of the built-in table, by name or alias."""
    key = fold(name).strip(" .,;()")
    key = TOWN_ALIASES.get(key, key)
    found = TOWNS.get(key)
    if found:
        return found
    if len(key) == 2 and key.upper() in PROVINCE_CAPITAL:
        return TOWNS[PROVINCE_CAPITAL[key.upper()]]
    return None


# --- job words ----------------------------------------------------------------------------

# Each group lists words (or short phrases) that name the same thing in titles.
WORD_GROUPS: list[tuple[str, ...]] = [
    ("sviluppatore", "sviluppatrice", "developer", "programmatore", "programmatrice", "programmer",
     "software engineer", "dev"),
    ("ingegnere", "engineer"),
    ("tecnico", "tecnica", "technician"),
    ("analista", "analyst"),
    ("contabile", "accountant", "accounting", "contabilita"),
    ("commerciale", "sales", "venditore", "venditrice"),
    ("magazziniere", "magazziniera", "warehouse", "warehouse worker", "warehouseman"),
    ("carrellista", "forklift", "forklift driver", "mulettista"),
    ("manutentore", "manutentrice", "manutenzione", "maintenance"),
    ("elettricista", "electrician"),
    ("saldatore", "saldatrice", "welder"),
    ("autista", "driver"),
    ("operaio", "operaia", "operatore", "operatrice", "operator"),
    ("progettista", "designer", "disegnatore", "disegnatrice", "draftsman"),
    ("impiegato", "impiegata", "clerk", "office worker"),
    ("amministrativo", "amministrativa", "administrative", "administration", "amministrazione"),
    ("acquisti", "buyer", "purchasing"),
    ("qualita", "quality"),
    ("capo progetto", "project manager"),
    ("sistemista", "system administrator", "sysadmin"),
    ("infermiere", "infermiera", "nurse"),
    ("logistica", "logistics"),
    ("risorse umane", "human resources", "hr"),
    ("assistenza clienti", "customer service", "customer care"),
    ("segretario", "segretaria", "secretary"),
    ("cuoco", "cuoca", "cook", "chef"),
    ("cameriere", "cameriera", "waiter", "waitress"),
    ("commesso", "commessa", "shop assistant", "sales assistant"),
    ("addetto paghe", "payroll"),
    ("collaudatore", "collaudatrice", "tester", "collaudo", "testing"),
    ("montatore", "montatrice", "assembler", "fitter"),
    ("responsabile", "manager", "head"),
    ("stagista", "tirocinante", "intern", "internship", "stage"),
    ("automazione", "automation"),
    ("informatico", "informatica", "it"),
    ("dati", "data"),
    ("produzione", "production"),
    ("ufficio tecnico", "technical office"),
    ("ot", "operational technology", "tecnologia operativa", "tecnologie operative"),
]
# Words that never make a match alone (too generic).
GENERIC_WORDS = {
    "addetto", "addetta", "addetti", "addette", "junior", "senior", "jr", "sr", "lead", "figura", "risorsa",
    "personale", "cercasi", "cerchiamo", "ricerca", "offerta", "lavoro", "job", "jobs", "position", "role",
    "specialist", "specialista", "assistant", "assistente", "staff", "team", "the", "and", "per", "con", "di",
    "del", "della", "da", "in", "a", "e", "of", "for", "with", "to", "il", "la", "lo", "le", "gli", "un", "una",
    "uno", "su", "al", "alla", "nel", "nella", "presso", "or", "o",
}
# Words of the groups above that name a field, not a person's job: a skill variant typed
# in the role ("sicurezza informatica") covers them; a job noun ("programmatore") is kept.
FIELD_WORDS = {"informatico", "informatica", "it", "dati", "data", "automazione", "automation", "qualita",
               "quality", "logistica", "logistics", "produzione", "production", "manutenzione", "maintenance",
               "contabilita", "accounting", "amministrazione", "administration", "acquisti", "purchasing",
               "collaudo", "testing", "ot", "operational technology"}
ROLE_NOUNS = {fold(w) for group in WORD_GROUPS for w in group} - FIELD_WORDS
_GROUP_OF: dict[str, int] = {}
for _index, _group in enumerate(WORD_GROUPS):
    for _word in _group:
        _GROUP_OF.setdefault(fold(_word), _index)


def synonyms(word: str) -> list[str]:
    """The word and its equivalents (folded), the word first."""
    key = fold(word)
    index = _GROUP_OF.get(key)
    if index is None:
        return [key]
    return [key] + [fold(w) for w in WORD_GROUPS[index] if fold(w) != key]


def group_id(word: str) -> str:
    key = fold(word)
    index = _GROUP_OF.get(key)
    return f"g{index}" if index is not None else key


# --- query filters ------------------------------------------------------------------------

CONTRACT_WORDS: list[tuple[str, str]] = [
    ("tempo indeterminato", "permanent"), ("indeterminato", "permanent"), ("permanent", "permanent"),
    ("contratto stabile", "permanent"),
    ("tempo determinato", "fixed_term"), ("determinato", "fixed_term"), ("fixed term", "fixed_term"),
    ("fixed-term", "fixed_term"),
    ("somministrazione", "temporary_agency"), ("interinale", "temporary_agency"),
    ("temporary", "temporary_agency"),
    ("apprendistato", "apprenticeship"), ("apprenticeship", "apprenticeship"),
    ("tirocinio", "internship"), ("stage", "internship"), ("internship", "internship"),
    ("partita iva", "freelance"), ("p.iva", "freelance"), ("freelance", "freelance"),
    ("libero professionista", "freelance"), ("contractor", "freelance"),
]
HOURS_WORDS: list[tuple[str, str]] = [
    ("part time", "part_time"), ("part-time", "part_time"), ("parttime", "part_time"),
    ("full time", "full_time"), ("full-time", "full_time"), ("fulltime", "full_time"),
    ("tempo pieno", "full_time"), ("tempo parziale", "part_time"),
]
REMOTE_WORDS: list[tuple[str, str]] = [
    ("full remote", "remote"), ("smart working", "remote"), ("da remoto", "remote"), ("in remoto", "remote"),
    ("telelavoro", "remote"), ("remote", "remote"), ("remoto", "remote"),
    ("ibrido", "hybrid"), ("hybrid", "hybrid"),
    ("in sede", "onsite"), ("on-site", "onsite"), ("onsite", "onsite"), ("in presenza", "onsite"),
]
SENIORITY_WORDS = {"junior": "junior", "jr": "junior", "neolaureato": "junior", "neolaureata": "junior",
                   "graduate": "junior", "senior": "senior", "sr": "senior", "esperto": "senior",
                   "esperta": "senior", "mid": "mid"}
# Contracts accepted by a contract filter (a "permanent" search also takes ads that may
# become permanent).
CONTRACT_ACCEPTS = {
    "permanent": {"permanent", "permanent_possible"},
    "fixed_term": {"fixed_term", "temporary_agency", "permanent_possible"},
    "temporary_agency": {"temporary_agency", "permanent_possible"},
    "apprenticeship": {"apprenticeship"},
    "internship": {"internship"},
    "freelance": {"freelance"},
}


# --- sector, CCNL and size hints (ATECO 2025 divisions) -----------------------------------

SECTOR_HINTS: list[tuple[str, str, tuple[int, ...]]] = [
    # (regex on folded text, label, divisions)
    (r"metalmeccanic\w*", "metalworking", (25, 28, 29, 30, 33)),
    (r"macchine automatiche|automatic machines", "automatic machines", (28,)),
    (r"oleodinamic\w*|idraulic\w*", "hydraulics", (28,)),
    (r"(?<!elettro)meccanic\w*|mechanical engineering", "mechanical", (28, 25)),
    (r"ceramic\w*|piastrell\w*", "ceramics", (23,)),
    (r"packaging|imballag\w*|confezionament\w*", "packaging", (28, 17, 22)),
    (r"alimentar\w*|food|agroalimentar\w*", "food", (10, 11)),
    (r"farmaceutic\w*|pharma\w*", "pharmaceutical", (21,)),
    (r"chimic\w*|chemical", "chemical", (20,)),
    (r"automotive|automobilistic\w*", "automotive", (29,)),
    (r"logistic\w*|logistics", "logistics", (52, 49)),
    (r"trasport\w*|spedizion\w*", "transport", (49, 52)),
    (r"elettromeccanic\w*", "electromechanical", (27, 28)),
    (r"elettronic\w*|electronics", "electronics", (26, 27)),
    (r"biomedical\w*|medicale|medical device\w*", "medical devices", (32, 26)),
    (r"tessil\w*|abbigliament\w*|moda\b|fashion", "textile and fashion", (13, 14, 15)),
    (r"plastic\w*|gomma", "plastics", (22,)),
    (r"siderurgic\w*|metallurgic\w*|fonderia", "metallurgy", (24,)),
    (r"edil\w*|costruzion\w*", "construction", (41, 42, 43)),
    (r"impiantistic\w*", "installation", (43,)),
    (r"\bict\b|informatic\w*|software house|system integrator|it consulting|consulenza it", "ICT", (62, 63)),
    (r"telecomunicazion\w*|telecommunication\w*", "telecommunications", (61,)),
    (r"\bgdo\b|grande distribuzione|retail", "retail", (47,)),
    (r"ingrosso|wholesale", "wholesale", (46,)),
    (r"legno|arredament\w*|arredo|mobili\b", "wood and furniture", (16, 31)),
    (r"cartotecnic\w*|cartari\w*", "paper", (17,)),
    (r"energ\w*", "energy", (35,)),
    (r"bancari\w*|\bbanca\b|banking", "banking", (64,)),
    (r"assicurativ\w*|insurance", "insurance", (65,)),
    (r"ristorazione|horeca", "food service", (56,)),
    (r"alberghier\w*|hotel", "hotels", (55,)),
    (r"aerospazial\w*|aerospace|nautic\w*", "aerospace and boats", (30,)),
    (r"agricol\w*", "agriculture", (1,)),
]
CCNL_HINTS: list[tuple[str, str, set[int], set[int]]] = [
    # (regex, label, consistent divisions, contradictory divisions)
    (r"metalmeccanic\w*|metalmeccanica", "Metalmeccanico", set(range(24, 31)) | {33, 43, 95},
     {10, 11, 47, 55, 56, 86, 87, 88}),
    (r"commercio|terziario", "Commercio", set(range(45, 48)) | {52} | set(range(58, 83)), set(range(24, 31))),
    (r"chimic\w*|farmaceutic\w*", "Chimico-farmaceutico", set(range(19, 23)), {10, 11, 47}),
    (r"alimentar\w*|industria alimentare", "Alimentare", {10, 11}, set(range(24, 31))),
    (r"tessil\w*|abbigliamento", "Tessile-abbigliamento", {13, 14, 15}, set()),
    (r"legno|arredo", "Legno-arredo", {16, 31}, set()),
    (r"edil\w*", "Edilizia", {41, 42, 43}, {62, 63}),
    (r"logistica|trasporto merci|spedizione", "Logistica e trasporti", set(range(49, 54)), set()),
    (r"gomma|plastica", "Gomma-plastica", {22}, set()),
    (r"ceramica|piastrelle", "Ceramica", {23}, set()),
    (r"telecomunicazioni", "Telecomunicazioni", {61}, set()),
    (r"turismo|pubblici esercizi", "Turismo", {55, 56, 79}, set(range(24, 31))),
    (r"credito|bancari\w*", "Credito", {64}, set()),
    (r"grafic\w*|cartari\w*", "Grafico-cartario", {17, 18}, set()),
    (r"elettric\w*|gas.acqua", "Elettrico / gas-acqua", {35, 36}, set()),
    (r"cooperative sociali|sanita privata", "Socio-sanitario", {86, 87, 88}, set(range(24, 31))),
]
_CCNL_CONTEXT = re.compile(r"\bccnl\b|contratto collettivo|c\.c\.n\.l")
SIZE_WORDS: list[tuple[str, str]] = [
    (r"multinazional\w*|multinational|gruppo internazionale|international group", "large"),
    (r"grande azienda|grande realta|large company|azienda leader|leader (?:mondiale|nel|di mercato)", "large"),
    (r"\bpmi\b|piccola (?:e media )?(?:azienda|impresa|realta)|small company|start-?up", "small"),
    (r"media (?:azienda|impresa|realta)|medium-sized", "medium"),
    (r"realta strutturata|azienda strutturata|solida realta|well.established", "medium_large"),
]
_EMPLOYEES = re.compile(r"(?:oltre|piu di|over|more than|circa|about)?\s*(\d{2,6})\s*(?:dipendenti|addetti|employees)")


def division(code) -> int | None:
    """Two-digit ATECO division of a code ("28.99.3" -> 28); None when not a code."""
    found = re.match(r"\s*(\d{1,2})", str(code or ""))
    return int(found.group(1)) if found else None


_SECTIONS = [(1, 3, "A"), (5, 9, "B"), (10, 33, "C"), (35, 35, "D"), (36, 39, "E"), (41, 43, "F"),
             (45, 47, "G"), (49, 53, "H"), (55, 56, "I"), (58, 60, "J"), (61, 63, "K"), (64, 66, "L"),
             (68, 68, "M"), (69, 75, "N"), (77, 82, "O"), (84, 84, "P"), (85, 85, "Q"), (86, 88, "R"),
             (90, 93, "S"), (94, 96, "T"), (97, 98, "U"), (99, 99, "V")]


def section(div: int | None) -> str | None:
    if div is None:
        return None
    return next((s for low, high, s in _SECTIONS if low <= div <= high), None)


def sector_hints(text: str) -> list[tuple[str, tuple[int, ...]]]:
    """(label, divisions) of the sector phrases in an ad."""
    folded = fold(text)
    found = []
    for pattern, label, divisions in SECTOR_HINTS:
        if re.search(pattern, folded) and all(label != f[0] for f in found):
            found.append((label, divisions))
    return found


def ccnl_hint(text: str):
    """(label, consistent, contradictory) of the collective contract named in an ad."""
    folded = fold(text)
    for context in _CCNL_CONTEXT.finditer(folded):
        window = folded[context.start(): context.start() + 80]
        for pattern, label, ok, bad in CCNL_HINTS:
            if re.search(pattern, window):
                return label, ok, bad
    return None


def size_hint(text: str) -> tuple[str | None, int | None]:
    """("small" | "medium" | "medium_large" | "large" | None, employees stated or None)."""
    folded = fold(text)
    employees = None
    found = _EMPLOYEES.search(folded)
    if found:
        employees = int(found.group(1))
    for pattern, band in SIZE_WORDS:
        if re.search(pattern, folded):
            return band, employees
    if employees is not None:
        return band_of(employees), employees
    return None, None


def band_of(employees: int | None) -> str | None:
    if employees is None:
        return None
    if employees < 50:
        return "small"
    if employees < 250:
        return "medium"
    return "large"


def size_agrees(hint: str, band: str) -> bool:
    if hint == band:
        return True
    if hint == "medium_large":
        return band in ("medium", "large")
    return False


# --- agencies -----------------------------------------------------------------------------

AGENCY_NAMES = (
    "randstad", "adecco", "manpower", "synergie", "gi group", "gigroup", "umana", "etjca", "openjobmetis",
    "orienta", "lavoropiu", "maw", "e-work", "ework", "staff spa", "tempor", "articolo1", "during",
    "men at work", "quanta", "sgb humangest", "humangest", "temporary spa", "ali spa", "kelly services",
    "hays", "michael page", "page personnel", "robert half", "experis", "spring professional",
    "agenzia per il lavoro",
)


def is_agency_name(name: str | None) -> bool:
    folded = fold(name)
    return bool(folded) and any(contains_phrase(folded, a) for a in AGENCY_NAMES)


# --- skills -------------------------------------------------------------------------------

EXTRA_TECH: dict[str, tuple[str, ...]] = {
    "Oracle Database": ("oracle", "oracle database", "oracle db", "pl/sql", "plsql"),
    "SAP Business One": ("sap b1", "sap business one"),
    "MES": ("mes",),
    "SCADA": ("scada",),
    "CODESYS": ("codesys",),
    "Rockwell Studio 5000": ("studio 5000", "rslogix", "rockwell"),
    "Microsoft Dynamics": ("dynamics 365", "navision", "dynamics nav", "business central"),
    "Zucchetti": ("zucchetti",),
    "TeamSystem": ("teamsystem",),
}


@lru_cache(maxsize=1)
def _dictionary():
    try:
        from ..cv.skills import dictionary
        return dictionary()
    except Exception:  # the dictionary is optional here
        return None


def extract_skills(text: str, limit: int = 40) -> list[str]:
    """Canonical (English) names of the skills and technologies named in a text."""
    if not text:
        return []
    found: list[str] = []
    dictionary = _dictionary()
    if dictionary is not None:
        try:
            for hit in dictionary.find(text):
                name = hit.entry.en
                if name not in found:
                    found.append(name)
        except Exception:
            pass
    folded = fold(text)
    for name, forms in EXTRA_TECH.items():
        if name not in found and any(contains_phrase(folded, f) for f in forms):
            found.append(name)
    return found[:limit]


@lru_cache(maxsize=1)
def _entry_forms() -> dict[int, tuple[str, ...]]:
    """Dictionary entry index -> every folded form of it (labels and variants)."""
    dictionary = _dictionary()
    out: dict[int, list[str]] = {}
    if dictionary is None:
        return {}
    for mapping in (getattr(dictionary, "_ci", {}) or {}, getattr(dictionary, "_cs", {}) or {}):
        for form, index in mapping.items():
            key = fold(form)
            if key and key not in out.setdefault(index, []):
                out[index].append(key)
    return {k: tuple(v) for k, v in out.items()}


def skill_labels(name: str) -> list[str]:
    """Folded English and Italian labels of a skill only (no variants)."""
    forms = {fold(name)}
    dictionary = _dictionary()
    if dictionary is not None:
        try:
            entry = dictionary.lookup(name)
            if entry is not None:
                forms.update(fold(x) for x in (entry.en, entry.it) if x)
        except Exception:
            pass
    return sorted(f for f in forms if f)


def skill_forms(name: str) -> list[str]:
    """Folded forms of a skill: its English and Italian labels and every variant of the
    dictionary ("Cybersecurity" -> "cyber security", "sicurezza informatica", ...)."""
    forms = {fold(name)}
    dictionary = _dictionary()
    if dictionary is not None:
        try:
            entry = dictionary.lookup(name)
            if entry is not None:
                forms.update(fold(x) for x in (entry.en, entry.it) if x)
                forms.update(f for f in _entry_forms().get(entry.index, ()) if len(f) >= 2)
        except Exception:
            pass
    for canonical, extra in EXTRA_TECH.items():
        if fold(canonical) in forms:
            forms.update(fold(e) for e in extra)
    return sorted(f for f in forms if f)


# --- language -----------------------------------------------------------------------------

_IT = {"il", "la", "di", "che", "per", "con", "una", "del", "della", "nel", "siamo", "ricerca", "azienda",
       "esperienza", "conoscenza", "sede", "lavoro", "dei", "delle", "gli", "sono", "nostro", "offriamo"}
_EN = {"the", "and", "of", "to", "with", "we", "our", "you", "for", "is", "are", "experience", "knowledge",
       "team", "will", "your", "job", "role", "skills"}


def detect_language(text: str) -> str | None:
    tokens = words(text[:4000])
    it = sum(1 for t in tokens if t in _IT)
    en = sum(1 for t in tokens if t in _EN)
    if it + en < 3:
        return None
    return "it" if it >= en else "en"
