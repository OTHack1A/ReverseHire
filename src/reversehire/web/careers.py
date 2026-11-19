"""Search for the careers page, the "about us" page and hiring signals.

All keywords are compared after `normalize` (lowercase, no accents).
"""

import html as html_module
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from ..models import Posting
from .ats import detect_ats, is_unsupported_ats
from .posting_attrs import complete_posting
from .text import analyze_html, url_key, compact, extract_links, normalize, same_site

# --- keywords --------------------------------------------------------------

# Link text: (phrase, weight). Strong phrases almost certainly indicate the careers page.
CAREERS_LINK_WORDS: list[tuple[str, int]] = [
    ("lavora con noi", 5), ("lavorare con noi", 5), ("lavora con me", 3),
    ("carriere", 5), ("carriera", 3), ("posizioni aperte", 5), ("posizioni disponibili", 5),
    ("offerte di lavoro", 5), ("annunci di lavoro", 5), ("opportunita di lavoro", 5),
    ("opportunita professionali", 5), ("opportunita", 2), ("candidature", 4),
    ("candidatura spontanea", 4), ("candidati", 3), ("unisciti a noi", 5),
    ("unisciti al team", 5), ("unisciti", 3), ("entra nel team", 5), ("entra nel nostro team", 5),
    ("entra a far parte", 4), ("cerchiamo personale", 4), ("ricerca personale", 4),
    ("selezione del personale", 3), ("lavoro", 1), ("stage", 1),
    ("careers", 5), ("career", 4), ("jobs", 5), ("job openings", 5), ("job offers", 5),
    ("join us", 5), ("join our team", 5), ("join the team", 5), ("work with us", 5),
    ("work for us", 5), ("vacancies", 5), ("open positions", 5), ("open roles", 5),
    ("current openings", 5), ("we're hiring", 5), ("we are hiring", 5), ("hiring", 2),
    ("jobs & careers", 5), ("karriere", 4), ("emplois", 3), ("empleo", 3),
    ("lavora da noi", 5), ("lavorare da noi", 5), ("lavora in", 2), ("posizioni lavorative", 5),
    ("offerte lavoro", 5), ("cerchiamo te", 4), ("siamo alla ricerca", 3), ("ricerchiamo", 3),
    ("job opportunities", 5), ("career opportunities", 5), ("careers at", 5), ("work at", 3),
    ("recruiting", 3), ("talent", 2),
]

# Path segments typical of careers pages.
_CAREERS_URL = re.compile(
    r"(?:^|[/_.-])(lavora-?con-?noi|lavorare-?con-?noi|lavora-?da-?noi|carriere?|careers?|jobs?|"
    r"join-?us|join-?our-?team|work-?with-?us|vacanc(?:y|ies)|posizioni-?aperte|"
    r"offerte-?(?:di-?)?lavoro|opportunita(?:-?di-?lavoro)?|candidature|candidati|"
    r"unisciti(?:-?a-?noi)?|recruiting|recruitment|hiring|karriere|stellenangebote|"
    r"posizioni-?lavorative|job-?opportunities|work-?at-?[a-z0-9-]+)(?:$|[/_.-])",
    re.I,
)

COMMON_PATHS_IT = ["/lavora-con-noi", "/carriere", "/careers", "/jobs"]
COMMON_PATHS_EN = ["/careers", "/jobs", "/lavora-con-noi", "/carriere"]

ABOUT_LINK_WORDS: list[tuple[str, int]] = [
    ("chi siamo", 5), ("chi sono", 3), ("about us", 5), ("about", 3), ("azienda", 3),
    ("la nostra azienda", 5), ("l'azienda", 4), ("la nostra storia", 4), ("storia", 2),
    ("profilo aziendale", 5), ("who we are", 5), ("our company", 4), ("company", 2),
    ("our story", 4), ("mission", 1),
]
_ABOUT_US_URL = re.compile(
    r"(?:^|[/_.-])(chi-?siamo|about(?:-?us)?|azienda|company|who-?we-?are|la-?nostra-?storia|"
    r"storia|profilo(?:-?aziendale)?|our-?story)(?:$|[/_.-])",
    re.I,
)
_NOT_ABOUT_URL = re.compile(
    r"(?:^|[/_.-])(locations?|offices?|sedi|dove-?siamo|contacts?|contatti|press|news|investors?)"
    r"(?:$|[/_.-])",
    re.I,
)

# External sites that do not count as the company's careers page.
_EXCLUDED_HOSTS = (
    "linkedin.com", "indeed.", "glassdoor.", "facebook.com", "instagram.com", "twitter.com",
    "x.com", "youtube.com", "tiktok.com", "infojobs.", "monster.", "google.", "wa.me",
)
_NON_PAGE_EXTENSIONS = (".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".svg", ".mp4")

OPEN_APPLICATION_SIGNALS = [
    "candidatura spontanea", "candidature spontanee", "candidati spontaneamente",
    "invia il tuo cv", "invia il tuo curriculum", "inviaci il tuo cv", "inviaci il tuo curriculum",
    "manda il tuo cv", "mandaci il tuo cv", "manda il tuo curriculum", "invia la tua candidatura",
    "inviare il proprio curriculum", "inviare il curriculum", "carica il tuo cv", "lavora con noi",
    "spontaneous application", "unsolicited application", "open application",
    "send us your cv", "send us your resume", "send your cv", "send your resume",
    "submit your cv", "submit your resume", "upload your cv", "general application",
]


@dataclass
class CandidateLink:
    url: str
    text: str
    score: int
    ats: bool = False


def _host_excluded(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(excluded_host in host for excluded_host in _EXCLUDED_HOSTS)


def _not_a_page(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(_NON_PAGE_EXTENSIONS)


def _text_score(text: str, words: list[tuple[str, int]]) -> int:
    norm = normalize(text)
    if not norm or len(norm) > 80:  # long texts: probably not menu entries
        return 0
    best = 0
    for sentence, weight in words:
        if re.search(r"(?:^|\W)" + re.escape(sentence) + r"(?:$|\W)", norm):
            best = max(best, weight)
    return best


def _path(url: str) -> str:
    parts = urlsplit(url)
    return normalize(parts.path.replace("_", "-") + ("?" + parts.query if parts.query else ""))


def find_careers_link(html: str, base_url: str, tree=None) -> list[CandidateLink]:
    """Links to the careers page (same site or external ATS), most likely first."""
    candidates: dict[str, CandidateLink] = {}
    current_page = url_key(base_url)
    for url, text in extract_links(html, base_url, tree):
        if _not_a_page(url) or url_key(url) == current_page:
            continue
        # Links to an ATS count even off-site: readable ones (API) and the ones
        # we can only show as a link (UNSUPPORTED_ATS_HOSTS).
        is_ats = bool(detect_ats(url)) or is_unsupported_ats(url)
        if _host_excluded(url) and not is_ats:
            continue
        score = _text_score(text, CAREERS_LINK_WORDS)
        careers_path = bool(_CAREERS_URL.search(_path(url)))
        if not is_ats and not same_site(url, base_url):
            # The group's careers site ("Careers" on brand.example -> group.example/en/careers):
            # followed only when both the link text and the address say careers.
            if not (score >= 4 and careers_path):
                continue
            score -= 1
        if careers_path:
            score += 3
        if is_ats:
            score += 4
        if score < 3:
            continue
        key = url_key(url)
        existing = candidates.get(key)
        if existing is None or score > existing.score:
            candidates[key] = CandidateLink(url, text, score, is_ats)
    return sorted(candidates.values(), key=lambda c: (-c.score, len(c.url)))


def find_about_us_link(html: str, base_url: str, tree=None) -> list[str]:
    """Links to the "chi siamo"/"about" page of the same site, most likely first."""
    candidates: dict[str, tuple[int, str]] = {}
    current_page = url_key(base_url)
    for url, text in extract_links(html, base_url, tree):
        if _not_a_page(url) or not same_site(url, base_url) or url_key(url) == current_page:
            continue
        if _NOT_ABOUT_URL.search(_path(url)):
            continue  # "/about/locations": a list of addresses, not a description
        score = _text_score(text, ABOUT_LINK_WORDS)
        if _ABOUT_US_URL.search(_path(url)):
            score += 3
        if score < 3:
            continue
        key = url_key(url)
        if key not in candidates or score > candidates[key][0]:
            candidates[key] = (score, url)
    ranked = sorted(candidates.values(), key=lambda c: (-c[0], len(c[1])))
    return [url for _, url in ranked]


# Link phrases that lead to the actual list of positions.
_LISTING_PHRASES = [
    "posizioni aperte", "posizioni disponibili", "offerte di lavoro", "annunci", "cerca tra le posizioni",
    "cerca lavoro", "vedi le posizioni", "tutte le posizioni", "open positions", "open roles",
    "all jobs", "job openings", "current openings", "vacancies", "search jobs", "view jobs",
    "see jobs", "browse jobs", "job search",
]


def sort_by_listing(candidates: list[CandidateLink]) -> list[CandidateLink]:
    """Puts first the links that seem to lead to the list of positions."""
    def listing(c: CandidateLink) -> bool:
        norm = normalize(c.text)
        return any(sentence in norm for sentence in _LISTING_PHRASES) or "search" in urlsplit(c.url).path.lower()
    return sorted(candidates, key=lambda c: (not listing(c), -c.score, len(c.url)))


def url_looks_like_careers(url: str) -> bool:
    """True if the URL path is typical of a careers page."""
    return bool(_CAREERS_URL.search(_path(url)))


def text_looks_like_careers(text: str) -> bool:
    """True if the text contains strong careers-page phrases (for paths tried blindly)."""
    norm = normalize(text[:20000])
    return any(
        weight >= 4 and re.search(r"(?:^|\W)" + re.escape(sentence) + r"(?:$|\W)", norm)
        for sentence, weight in CAREERS_LINK_WORDS
    )


_LANGUAGE_PREFIX = re.compile(r"^/([a-z]{2})(?:[-_][a-z]{2})?(?:/|$)", re.I)


def common_paths(base_url: str, language: str = "") -> list[str]:
    """URLs of the most common careers paths, to try if the links are not enough.

    On sites whose pages live under a language folder ("/it/", "/en-gb/") the
    paths are tried inside that folder first.
    """
    parts = urlsplit(base_url)
    root = f"{parts.scheme}://{parts.netloc}"
    folder = _LANGUAGE_PREFIX.match(parts.path or "")
    language = language or (folder.group(1) if folder else "")
    paths = COMMON_PATHS_EN if language.lower().startswith("en") else COMMON_PATHS_IT
    if folder:
        prefix = folder.group(0).rstrip("/")
        return [root + prefix + p for p in paths] + [root + p for p in paths]
    return [root + p for p in paths]


_SITEMAP_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
MAX_SITEMAP_URLS = 3


def careers_urls_from_sitemap(xml: str, base_url: str, maximum: int = MAX_SITEMAP_URLS) -> list[str]:
    """Same-site page URLs of a sitemap whose path looks like a careers page.

    Shortest paths first (the careers page rather than each posting); nested
    sitemap indexes and non-page files are ignored.
    """
    found: list[str] = []
    for match in _SITEMAP_LOC.finditer(xml or ""):
        url = html_module.unescape(match.group(1)).strip()
        if not url.startswith(("http://", "https://")) or not same_site(url, base_url):
            continue
        if _not_a_page(url) or url.lower().endswith((".xml", ".xml.gz", ".gz")):
            continue
        if url_looks_like_careers(url) and url_key(url) not in {url_key(u) for u in found}:
            found.append(url)
    found.sort(key=lambda u: (urlsplit(u).path.count("/"), len(u)))
    return found[:maximum]


def page_language(html: str, tree=None) -> str:
    if tree is None:
        tree = analyze_html(html)
    if tree is None:
        return ""
    return (tree.get("lang") or "").strip().lower()


def open_application_signal(text: str) -> bool:
    """True if the text invites to apply spontaneously or to send the CV."""
    norm = normalize(text)
    return any(sentence in norm for sentence in OPEN_APPLICATION_SIGNALS)


# --- heuristics: job titles on the careers page ----------------------------

_ROLE_WORDS = [
    # Italian
    "sviluppatore", "sviluppatrice", "programmatore", "programmatrice", "ingegnere", "tecnico",
    "tecnica", "operaio", "operaia", "impiegato", "impiegata", "addetto", "addetta",
    "responsabile", "analista", "consulente", "commerciale", "magazziniere", "autista",
    "contabile", "amministrativo", "amministrativa", "segretario", "segretaria", "receptionist",
    "cuoco", "cuoca", "cameriere", "cameriera", "barista", "infermiere", "infermiera", "medico",
    "progettista", "disegnatore", "specialista", "assistente", "stagista", "tirocinio",
    "tirocinante", "apprendista", "coordinatore", "coordinatrice", "direttore", "direttrice",
    "architetto", "saldatore", "elettricista", "meccanico", "manutentore", "operatore",
    "operatrice", "geometra", "perito", "educatore", "educatrice", "insegnante", "docente",
    "commesso", "commessa", "cassiere", "cassiera", "farmacista", "fisioterapista",
    "venditore", "venditrice", "carrellista", "mulettista", "montatore", "installatore",
    "manovale", "muratore", "idraulico", "estetista", "parrucchiere", "pasticcere", "pizzaiolo",
    "fornaio", "macellaio", "ragioniere", "capoturno",
    "tornitore", "fresatore", "carpentiere", "verniciatore", "falegname", "giardiniere",
    "collaboratore", "collaboratrice", "agente di commercio", "rappresentante", "autotrasportatore",
    "capocantiere", "buyer", "ricercatore", "ricercatrice", "redattore", "grafico", "grafica",
    # English
    "developer", "engineer", "technician", "manager", "analyst", "consultant", "sales",
    "accountant", "driver", "designer", "specialist", "assistant", "intern", "internship",
    "trainee", "apprentice", "coordinator", "director", "architect", "operator", "officer",
    "administrator", "representative", "associate", "executive", "scientist", "researcher",
    "writer", "editor", "nurse", "teacher", "cashier", "clerk", "recruiter", "head of",
    "devops", "full stack", "fullstack", "frontend", "front-end", "backend",
    "back-end", "product owner", "scrum master", "it support", "helpdesk",
    "warehouse", "mechanic", "electrician", "welder", "chef", "cook", "waiter", "bartender",
    "programmer", "tester", "worker", "planner",
]
_STRONG_MARKERS = re.compile(
    r"\((?:m|f|w|d|x|h|u|all genders?)(?:\s*/\s*(?:m|f|w|d|x|h|u|all genders?))+\)|"
    r"\b(?:m/f|f/m|m/w|w/m|h/f)(?:/[a-z])?\b|full[- ]time|part[- ]time|tempo (?:pieno|parziale|determinato|indeterminato)",
    re.I,
)
_POSTING_PATH = re.compile(
    r"(?:^|/)(?:jobs?|careers?|carriere|lavora-?con-?noi|posizion[ei]|offert[ae]|vacanc(?:y|ies)|"
    r"annunci|opportunita|positions?|openings?|ricerca|candidatura)/[^/]+",
    re.I,
)
_NAVIGATION_TEXTS = {
    normalize(t)
    for t in [
        "home", "contatti", "contattaci", "contact", "contact us", "chi siamo", "about", "about us",
        "privacy", "privacy policy", "cookie policy", "cookie", "blog", "news", "servizi", "services",
        "prodotti", "products", "login", "accedi", "registrati", "cerca", "search", "menu",
        "leggi di piu", "scopri di piu", "read more", "learn more", "candidati", "apply", "apply now",
        "invia candidatura", "torna su", "indietro", "back",
    ]
}


# Calls to action that often end up in the text of the same link as the title.
_CLOSING_INVITES = re.compile(
    r"[\s\-–|>»→:]*\b(?:discover more|find out more|read more|learn more|more info|see more|view job|"
    r"view details|details|apply now|apply|scopri di pi[uù]|scopri|leggi di pi[uù]|leggi tutto|"
    r"vedi dettagli|dettagli|candidati ora|candidati|invia candidatura)\b[\s>»→]*$",
    re.I,
)


# The same calls to action in front of the title ("Scopri di più → Warehouse Coordinator").
_OPENING_INVITES = re.compile(
    r"^\s*(?:discover more|find out more|read more|learn more|more info|see more|view job|view details|"
    r"apply now|scopri di pi[uù]|scopri|leggi di pi[uù]|leggi tutto|vedi dettagli|candidati ora)"
    r"\s*[-–|>»→:]+\s*",
    re.I,
)
_CAREERS_PAGE_PATH = re.compile(
    r"(?:^|[/_.-])(?:lavora-?con-?noi|lavora-?da-?noi|carriere?|careers?|jobs?|posizioni-?aperte|"
    r"offerte-?(?:di-?)?lavoro|work-?with-?us|join-?us)(?:$|[/_.-])", re.I)
# Links of a careers page that lead to other parts of the site (manuals, support, contacts...).
_NOT_A_POSTING_PATH = re.compile(
    r"(?:^|[/_.-])(?:download|downloads|manual[ei]?|manuals?|support[oi]?|assistenza|contatt[io]|contacts?|"
    r"privacy|cookie|news|blog|press|eventi|events|prodott[io]|products?|catalog(?:o|ue)?|login|"
    r"area-riservata|shop|store)(?:$|[/_.?=-])",
    re.I,
)


_SENIORITY_WORDS = {"junior", "senior", "stage", "stagista", "tirocinio", "tirocinante", "internship",
                    "intern", "apprendista", "lead", "head", "chief"}


def _starts_with_role(text: str) -> bool:
    """"Operatore CNC esperto", "Senior Developer": the title starts with the role (not
    "Il nostro responsabile qualità", a sentence about a person)."""
    words = normalize(text).split()
    if not words:
        return False
    if words[0] in _SENIORITY_WORDS:
        return True
    return any(normalize(role).split()[0] == words[0] for role in _ROLE_WORDS if role.strip())


_PEOPLE_BLOCK = re.compile(
    r"testimonial|interview|intervist|persona|people|person|staff|team-?member|our-?team|storie|stories|"
    r"story|quote|review|recension|dicono-?di-?noi|employee-?voice", re.I)


def _in_people_block(element, levels: int = 5) -> bool:
    """True inside a block of people (employee interviews, team, testimonials): their
    names with their job are not open positions."""
    current = element
    for _ in range(levels):
        if current is None:
            return False
        marks = f"{current.get('class') or ''} {current.get('id') or ''}"
        if marks.strip() and _PEOPLE_BLOCK.search(marks):
            return True
        current = current.getparent()
    return False


def _strip_invitations(text: str) -> str:
    cleaned = _CLOSING_INVITES.sub("", _OPENING_INVITES.sub("", text)).strip()
    return cleaned if len(cleaned) >= 4 else text


def _looks_like_title(text: str) -> bool:
    norm = normalize(text)
    if not 4 <= len(norm) <= 90 or norm in _NAVIGATION_TEXTS:
        return False
    words = norm.split()
    if not 1 <= len(words) <= 12:
        return False
    if _text_score(text, CAREERS_LINK_WORDS) >= 4 or open_application_signal(text):
        return False  # "Lavora con noi", "Posizioni aperte" etc. are not postings
    if norm.endswith((".", "?", "!")) and len(words) > 6:
        return False  # descriptive sentences, not titles
    stripped = text.strip()
    first = next((c for c in stripped if c.isalpha()), "")
    if first and first.islower():
        return False  # a requirement bullet ("interpretare il disegno tecnico"), not a title
    if stripped.endswith((".", ";")) and len(words) >= 3:
        return False  # "Diploma o qualifica ad indirizzo tecnico." is a requirement line
    return any(re.search(r"(?:^|\W)" + re.escape(p) + r"(?:$|\W)", norm) for p in _ROLE_WORDS)


def extract_heuristic_postings(html: str, page_url: str, tree=None) -> list[Posting]:
    """Titles that look like open positions (links and headings of the careers page).

    To limit false positives a single candidate is accepted only if it has a
    strong marker ("(m/f)", "full time", detail URL); otherwise two are needed.
    """
    if tree is None:
        tree = analyze_html(html)
    if tree is None:
        return []
    for element in list(tree.iter("nav", "header", "footer", "script", "style", "noscript", "form")):
        if element.getparent() is not None:
            element.drop_tree()

    candidates: list[tuple[Posting, bool]] = []
    seen: set[str] = set()
    page = url_key(page_url)
    for element in tree.iter("a", "h2", "h3", "h4", "h5", "li", "dt", "strong"):
        text = _strip_invitations(compact(" ".join(element.itertext())))
        if not _looks_like_title(text):
            continue
        if _in_people_block(element):
            continue  # "Arianna Rossi – Production engineer": an interview card, not a posting
        if element.tag == "li" and element.find(".//a") is not None:
            continue  # the inner link is already considered on its own
        url = page_url
        strong = bool(_STRONG_MARKERS.search(text))
        # a heading with a role name on the careers page itself ("OPERATORE CNC ESPERTO"
        # above its requirements) is a posting even when it is the only one
        if (element.tag in ("h2", "h3", "h4", "h5", "strong") and _CAREERS_PAGE_PATH.search(urlsplit(page_url).path)
                and _starts_with_role(text)):
            strong = True
        link = element if element.tag == "a" else element.find(".//a")
        if link is None:
            ancestor = element.getparent()
            link = ancestor if ancestor is not None and ancestor.tag == "a" else None
        if link is not None and link.get("href"):
            absolute = urljoin(page_url, html_module.unescape(link.get("href")).strip())
            if absolute.startswith(("http://", "https://")) and (same_site(absolute, page_url) or detect_ats(absolute)):
                url = absolute.split("#")[0]
                target = urlsplit(url)
                if url_key(url) != page and _NOT_A_POSTING_PATH.search(target.path + "?" + target.query) \
                        and not _POSTING_PATH.search(target.path):
                    continue  # "Supporto Tecnico" -> /contatti/..., manuals -> /download...
                if url_key(url) != page and _POSTING_PATH.search(target.path):
                    strong = True
        norm = normalize(text)
        key = norm + "|" + (url_key(url) if url_key(url) != page else "")
        twin = next((i for i, (p, _s) in enumerate(candidates)
                     if normalize(p.title) == norm or (min(len(norm), len(normalize(p.title))) >= 15 and (
                         normalize(p.title).startswith(norm) or norm.startswith(normalize(p.title))))), None)
        if twin is not None:
            # the same posting as a heading and as a link: keep the longer title and the
            # posting's own page
            old, old_strong = candidates[twin]
            if url_key(old.url) == page and url_key(url) != page:
                old.url = url
            if len(text) > len(old.title):
                old.title = text
            candidates[twin] = (old, old_strong or strong)
            continue
        if norm in seen or key in seen:
            continue
        seen.add(norm)
        seen.add(key)
        # Only the title is known here: "Stage ...", "(tempo indeterminato)", "- Remote".
        posting = complete_posting(Posting(title=text, url=url, source="heuristic"))
        candidates.append((posting, strong))

    if not candidates:
        return []
    if len(candidates) == 1 and not candidates[0][1]:
        return []
    return [posting for posting, _ in candidates]
