# Interfaces between modules

Binding contract for whoever implements the modules in `src/reversehire/`. The types `CVProfile`, `ProfileItem`, `Evidence`, `WorkExperience`, `EmploymentGap`, `Mobility`, `Company`, `Posting`, `RequirementCheck`, `Assessment`, `HiringStatus` are in `models.py` (see "Data model" at the end); `Settings` is in `config.py`; `Store` is in `store.py` (cache: `cache_read(namespace, key, max_age_s)`, `cache_write(namespace, key, value)`; it can be used from several threads).

Note: this document describes the contract of the core modules. The Qt (PySide6) GUI layer is not detailed here, except for the small helper modules that hold its rules (see "GUI helpers"). Sections: cv, geo, web, discovery, registry, financials, scoring, pipeline, progress, jobsearch, affinity, background, suggest, registers, applications, recruiter_sim, mapview, version, companies (company cards), mail, chat, export, cv_library and display_text, config, store and applog, GUI helpers, sysinfo, diagnostics, and the data model.

Common rules:
- Identifiers, comments and documentation are in English. No LLM in the core (the optional assistant strip talks to the user's own installed CLI, see "chat"). Only the listed dependencies.
- Every class that uses the network receives `settings: Settings` and `store: Store | None` in its constructor and accepts an optional `client` parameter (httpx.Client) for tests.
- All HTTP requests use `settings.user_agent` and a timeout. Tests never touch the network.
- Long operations accept `cancel: threading.Event | None` and stop as soon as it is set.
- Expected errors (network, unreadable file) become module-specific exceptions or `None`/status values, never a crash.
- Code that runs in parallel threads (crawl of several companies at once) must not share mutable state without locking. The per-domain delay and `robots.txt` rules still apply to every site.

## cv
```python
# cv/reading.py
SUPPORTED_FORMATS = (".pdf", ".docx", ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp")
class CVReadError(Exception): ...
@dataclass
class ReadResult: text: str; method: str; pages: int; ocr_confidence: float | None = None; warnings: list[str]
    # method: "text" (text layer / DOCX), "ocr" (all pages by OCR) or "mixed"; ocr_confidence 0..1
def read_document(path: str | Path, progress: Callable[[int, int], None] | None = None, *, advanced: str = "off",
                  advanced_progress: Callable[[int, int], None] | None = None,
                  cancel: threading.Event | None = None) -> ReadResult
    # PDF, DOCX, images; scanned PDF pages and images go through cv/ocr.py; progress(done, total) per OCR page.
    # advanced: "off" (default) | "auto" (read the OCR pages again with the advanced reader when the trigger rule
    # of cv/ocr_quality.py fires and the models are installed, and keep the better of the two readings; a tie keeps
    # the normal one) | "force" (always read again, still keeping the better reading). advanced_progress is the
    # page callback of that second pass, which has its own deadline (ADVANCED_MAX_S = 240 s) and stops at `cancel`;
    # it never raises because of the advanced engine
# ReadResult also has: engine ("normal" | "advanced" | ""), provider ("cpu" | "dml"), advanced (ADVANCED_NOT_NEEDED |
#   ADVANCED_MISSING | ADVANCED_USED | ADVANCED_KEPT_NORMAL | ADVANCED_FAILED | ADVANCED_CANCELLED | ""), signals (the
#   trigger letters "A".."E" that fired on the normal reading)
def read_text(path: str | Path) -> str            # read_document(path).text; CVReadError if unreadable or without text
def detect_language(text: str) -> str             # "it", "en" or "uk" (mostly Cyrillic text)

# cv/sections.py
def split_sections(text) -> list[Section]          # decorated headings ("// SKILLS", "01 EXPERIENCE",
    # "X · continued"), page headers/footers removed, hyphenated words re-joined, a "continued"
    # section left empty receives the rows after the next page break; "references"/"privacy" ignored

# cv/skills.py  (dictionary data/skills.json: ~700 IT/EN/UK entries, English canonical name "en")
def extract_skills(sections, cv_language, display=None) -> list[ProfileItem]
def dictionary() -> SkillDictionary                # .find(text, list_context=False), .lookup(text)

# cv/translate.py  (local MT: OPUS-MT it->en, uk->en in ONNX int8, onnxruntime + tokenizers)
def translate(texts: list[str], source_lang: str, *, folder=None) -> list[str]   # unchanged if no model
def is_available(source_lang, folder=None) -> bool; def is_installed(source_lang, folder=None) -> bool
def models_folder(folder=None) -> Path             # REVERSEHIRE_TRANSLATION_MODELS or <data folder>/models/translation
def download_models(languages=("it", "uk"), folder=None, progress=None, client=None) -> list[Path]
    # explicit only (also `python -m reversehire.cv.translate download it uk`); SHA-256 pinned

# cv/english.py
def to_english(profile, *, folder=None, use_mt=True) -> CVProfile   # in place: tables, ESCO EN label, local MT

# cv/ocr.py  (RapidOCR imported lazily; OcrUnavailable when it is missing)
IMAGE_FORMATS = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"); MAX_OCR_PAGES = 10
class OcrUnavailable(Exception): ...
@dataclass
class OcrPage: text: str; confidence: float | None; boxes: int
def get_engine() -> OcrEngine                      # the shared NORMAL engine; OcrUnavailable without rapidocr
def get_advanced_engine() -> OcrEngine             # the advanced engine; needs the downloaded models (cv/ocr_models.py), on DirectML when available
def reset_engines() -> None                        # drops the loaded sessions (after the advanced models are installed or removed)
# Two engines, one design: detect once, read every line with the Latin model, then a 12-line probe with the Cyrillic
# model decides whether the page is Cyrillic (then every line is read again with it and look-alike letters are put
# back in one script). normal = PP-OCRv6 small models of the rapidocr wheel + the bundled PP-OCRv5 Cyrillic
# recogniser (cv/data/, 8 MB); advanced = PP-OCRv6 medium detector and Latin recogniser + the Cyrillic one.
# Not included, on purpose: Indra OCR (modified OpenRAIL-M weights, not compatible with an MIT app) and VLM OCR
def ocr_image(image, engine=None) -> OcrPage
def assemble_text(boxes: list[OcrBox]) -> str      # reading order; two-column layouts read column by column
def render_pdf_pages(path: Path, page_indexes: list[int], dpi: int = 200) -> dict[int, object]

# cv/ocr_models.py  (model files of the engines; the advanced reader is optional and downloaded on request)
def find_advanced(folders=None) -> Path | None             # first COMPLETE "<models folder>/ocr/advanced-v1" (all files, right sizes)
def advanced_status(folders=None, provider=None) -> AdvancedStatus   # installed, folder, size_bytes, provider ("cpu" | "dml"), download_bytes
def advanced_download_bytes(bundled=None) -> int           # about 147 MB; the Cyrillic recogniser is copied from the bundled file
def download_advanced(target=None, *, fetch=None, progress=None, cancel=None, settings=None) -> Path
    # pinned Hugging Face revisions through the polite gateway, resumable ("<file>.part"), cancellable, every file checked
    # against its SHA-256; DownloadCancelled / ModelError. remove_advanced(folder=None) deletes it; verify_folder(folder) -> bad files
def best_provider(*, refresh=False) -> str                 # "dml" when onnxruntime-directml is installed and one small session runs, else "cpu"
    # (REVERSEHIRE_OCR_PROVIDER=cpu|dml overrides). onnxruntime_distributions() lists the installed onnxruntime packages
    # (both onnxruntime and onnxruntime-directml installed = a Diagnostics warning). CUDA is not used.
# Explicit model paths are always passed to RapidOCR: without them it would download missing models from modelscope.cn

# cv/ocr_quality.py  (when the normal reading looks poor, and which of two readings is better)
def signals(text, confidence, page_chars, scripts) -> Signals       # fired: "A" mean confidence < 0.97, "B" an OCR page with < 400
    # useful characters, "C" no experience heading, "D" fewer than 3 section headings, "E" Cyrillic not read as Cyrillic; any one is enough
def measure(text, confidence) -> ReadingQuality; def advanced_is_better(normal, advanced) -> bool   # skills + experiences, then headings, then confidence

# cv/mobility.py
WORKING_DAYS_PER_YEAR = 220
def find_mobility(all_rows: list[str], experience_rows: list[str]) -> Mobility
def travel_days(norm: str) -> int | None           # days a year from "40 giorni" or "20%" (of 220 days)
def find_home(rows: list[str]) -> str | None       # town from the contact block, usable for geocoding

# cv/photo.py  (face DETECTION only, to locate the portrait; no recognition; nothing is written to disk)
def extract_photo(path, *, detector=None, max_pages: int = 2) -> Photo | None     # PNG bytes in memory, longest side <= 512 px

# cv/esco.py
def essential_skills(occupation_uri: str | None, language: str = "it", store=None, settings=None,
                     client=None) -> list[str]   # one request per occupation (cached); [] on any failure
def occupation_skills(...) -> dict[str, list[str]]  # same arguments; essential and optional skills
def profile_occupation(profile) -> str | None      # ESCO URI of the first role that has one

# cv/recruiter.py
def review(profile: CVProfile, path: str | None = None, *, esco_skills: list[str] | None = None,
           today: date | None = None, embedder=None) -> RecruiterReview
@dataclass
class RecruiterReview: score: int; area_scores: dict[str, int | None]; findings: list[Finding];
    coverage: list[SkillCoverage]; language: str; words: int; pages: int | None; capped: bool
@dataclass
class Finding: area: str; key: str; severity: str; params: dict; evidence: list[str];
    advice_key: str | None; penalty: int
    # areas: readability, career, substance, coverage, personal, hidden; severity: good|info|warning|problem
    # key/advice_key are i18n keys ("recruiter.*"), never display text
def inspect_file(path) -> FileFacts               # pages, image-only pages, hidden text, producer

# cv/ai_signals.py
def analyse(text: str, language: str, path=None, embedder=None) -> AISignals
@dataclass
class AISignals: level: str; score: int; signals: list[Signal]; words: int; reliable: bool; notice_key: str
    # level "low" | "medium" | "high"; score is internal and never shown; never a verdict
@dataclass
class Signal: key: str; strength: float; weight: float; params: dict; evidence: list[str]; advice_key: str | None

# cv/ai_estimate.py  (ESTIMATE FROM WRITING SIGNALS, NOT PROOF)
def estimate_ai_probability(text: str, language: str, *, path=None, embedder=None, signals: AISignals | None = None) -> AiEstimate
@dataclass
class AiEstimate: probability: float; low: float; high: float; signals: list[DrivingSignal]   # 0..1, low <= probability <= high
@dataclass
class DrivingSignal: key: str; share: float; params: dict; sentences: list[SentenceAdvice]; ...   # the top TOP_SIGNALS = 3, each with up to 3
    # sentences of the CV and one rewrite suggestion per sentence (i18n key + parameters)
# Default backend "writing-signals-v1": the ai_signals score through a logistic, PROBABILITY_FLOOR 0.03 .. PROBABILITY_CEILING 0.85;
# the band is wider for short text, few computable signals and non-English text. Not calibrated on CVs of known authorship.
# set_backend(backend) / reset_backend(): a backend is any object with `name` and
#   estimate(text, language, *, path=None, embedder=None, signals=None) -> AiEstimate (probabilities in 0..1). The interface always shows the
#   band and the fixed caption ("ai.estimate.caption")

# cv/extraction.py
def build_profile(path: str | Path, store: Store | None = None,
                  settings: Settings | None = None, use_esco: bool = True) -> CVProfile
def profile_from_text(text: str, path: str = "", store=None, settings=None,
                      use_esco: bool = True) -> CVProfile
```
`build_profile` and `profile_from_text` also accept the keyword-only parameters `esco` (a ready `EscoNormalizer`) and `today` (reference date), meant for tests, plus `english=True` (values shown in English; False keeps the CV language), `translation_models` (model folder) and `use_mt=True`.

Profile values are in English whatever the CV language (it/en/uk): skills -> English dictionary name, languages -> English name + CEFR/level, degrees -> table of degree types and subjects, roles -> ESCO English label or job-title table, the rest -> local translation model; without the model the text stays as it is and is listed in `CVProfile.untranslated`. `CVProfile.language` stays the CV language; `CVProfile.translated` / `translation` ("tables" | "local") say how. Each translated `ProfileItem` keeps the CV wording in `original` (`WorkExperience.original_title`). Scoring matches both (`models.item_variants(item) -> [value, original]`, `scoring.relevance.profile_terms`), so Italian postings still match the Italian wording.

`profile_from_text` also fills `experiences`, `gaps` (pauses longer than 6 months), `mobility`, `home`, `seniority` and `seniority_evidence`.

The GUI runs the whole CV analysis through `gui/cv_analysis.py`: `analyse_cv(path, store=None, settings=None, *, use_esco=True, progress=None, embedder=None) -> CVAnalysis` with `CVAnalysis(profile, reading, review, signals)`; `essential_skills_for(profile, store, settings)` returns the ESCO essential skills or `None` (the "Role coverage" area is then not assessed).

`CVProfile.roles` is ordered from the most relevant (most recent/longest). `sectors` holds keys of `geo/sectors.py` (see below); the cv module does NOT compute them: the pipeline adds them by calling `sectors_for_profile`. Every `ProfileItem` has at least one `Evidence(source="cv")`.

## geo
```python
# geo/distance.py
def haversine_km(lat1, lon1, lat2, lon2) -> float
def point_from_bearing(lat, lon, azimuth_deg, distance_km) -> tuple[float, float]
    # start point from latitude/longitude + azimuth (0 = north, clockwise) + distance; ValueError on invalid input

# geo/geocoding.py
@dataclass
class Place: lat: float; lon: float; name: str
def parse_coordinates(text: str) -> tuple[float, float] | None     # "44.49, 11.34" etc.
class Geocoder:
    def __init__(self, settings, store=None, client=None, limiter=None)
    def geocode(self, text: str) -> Place | None                    # direct coordinates or Nominatim (1 req/s, cache)

# geo/sectors.py
SECTORS: dict[str, Sector]          # key -> Sector(key, name_it, name_en, osm_tags: list[tuple[str, str]], keywords: list[str])
def sectors_for_profile(profile: CVProfile) -> list[str]            # keys of SECTORS inferred from roles/skills
def preliminary_relevance(company: Company, sectors: list[str]) -> float   # 0..1, from OSM tags and name only

# geo/regions.py  (the 20 Italian regions; OpenStreetMap extract URLs, Wikidata ids, provinces)
@dataclass(frozen=True)
class Region: key: str; name: str; slug: str; wikidata: str; iso: str; provinces: tuple[Province, ...]   # .url, .file_name
REGIONS: dict[str, Region]
def region_for(value: str | None) -> Region | None        # a key, "custom:<url>" or a bare http(s) URL of an .osm.pbf
def provinces_of(region: Region | None) -> list[Province]

# geo/italy.py  (static tables, no I/O): province(code) -> ItalianProvince | None (107 provinces with sigla, names, region key, capital's coordinates), province_by_name(name), provinces_of_region(key),
#   region_key(name), region_name(key), country_code(name), country_name(code, language="it"), foreign_town(name), centroid(key), in_san_marino(lat, lon), in_italy_box(lat, lon); San Marino is a country of its own
# geo/places.py  (a messy place text -> country, region, province, municipality; only the app's own data and the local OSM place index: no network; results cached)
@dataclass(frozen=True)
class Location: country, country_code, region_key, region_name, province_code, province_name, municipality, provenance   # .known, .italian, .display(language=None) ("Castel Maggiore (BO)", "Lugano, Svizzera")
def normalise_location(text, *, lat=None, lon=None) -> Location            # provenance says how it was found: sigla | province-name | town | osm-index | region | country | foreign-town | san-marino | coordinates | text | ""
def location_of_company(company=None, *, card=None, assessment=None) -> Location; location_of_posting(posting); location_of_search(search: dict | None); set_place_lookup(lookup); clear_cache(); geocode_query(name)
    # foreign places keep the country only (and the town as written); gui/geo_filter.py and the lists' place column use these functions

# geo/overpass.py
class OverpassError(Exception): ...
class OverpassClient:
    def __init__(self, settings, store=None, client=None)
    def count(self, lat, lon, radius_km, sectors: list[str] | None) -> int
    def find(self, lat, lon, radius_km, sectors: list[str] | None,
             max_companies: int = 0, cancel=None) -> list[Company]    # distance_km filled in, sorted by distance, no duplicates
```
`sectors=None` means all company types. `max_companies=0` means no limit.

## web
```python
# web/fetch.py
@dataclass
class Page: url: str; final_url: str; http_status: int; html: str; text: str; engine: str
class Fetcher:
    def __init__(self, settings, store=None, client=None)
    def fetch_page(self, url: str, force_engine: str | None = None) -> Page | None   # robots.txt, per-domain delay, cache
    def close(self) -> None

# web/analysis.py
@dataclass
class SiteOutcome:
    status: HiringStatus
    postings: list[Posting]
    company_text: str            # descriptive text (home, about), cleaned
    pages_read: int
    careers_url: str | None
    postings_source: str         # "ats:greenhouse", "jsonld", "heuristic", "unsolicited", ""
def analyze_site(url: str, fetcher: Fetcher, settings: Settings, cancel=None) -> SiteOutcome

# web/ats.py
@dataclass(frozen=True)
class ATSReference: platform: str; slug: str; domain: str = ""; site: str = ""
    # platforms: greenhouse, lever, ashby, workable, recruitee, smartrecruiters, personio, workday, bamboohr
def detect_ats(*texts: str) -> list[ATSReference]
def download_postings(ref: ATSReference, fetcher) -> list[Posting] | None
def is_unsupported_ats(url: str) -> bool        # known ATS host whose postings cannot be read (still a hiring signal)

# web/posting_attrs.py
def detect_attributes(text: str, *, title="", location="", employment_type=None, workplace=None,
                      salary=None) -> PostingAttributes      # contract, remote, salary, travel (None = not stated)
def complete_posting(posting: Posting) -> Posting             # fills the attributes still empty

# web/jobboards.py  (optional extra "jobboards": python-jobspy; off by default)
def availability() -> tuple[bool, str]                       # (True, "") when JobSpy can run
def search_terms(roles: list[str]) -> list[str]
def town_of(place) -> str | None
def search_job_boards(roles, place, radius_km, settings, *, cancel=None, limit=..., sites=("indeed", "linkedin"),
                      store=None, runner=None, sleep=time.sleep, failures=None) -> list[tuple[Company, list[Posting]]]
def merge_into(found, existing: list[Company], radius_km) -> tuple[list[tuple[Company, list[Posting]]], dict[int, list[Posting]]]
```
```python
# web/identity.py  (legal identifiers and pages of the company's OWN site; company card v3)
def scan_identity(site_url, fetcher, *, cancel=None, max_extra=MAX_EXTRA_PAGES, home=None) -> IdentityScan
    # home page (+ its Italian version) and at most max_extra internal pages chosen by link text: legal notice, contacts, about,
    # history, careers, team, privacy; legal/privacy pages are skipped once VAT, REA, PEC and registered office are known.
    # Found(field, value, url, source, context, where): VAT (checksum), tax code, REA, share capital, PEC, e-mail, phone, registered
    # office, operating sites, founding year, size hints, schema.org JSON-LD and microdata, <address>, "unico socio", "in liquidazione",
    # direction and coordination (art. 2497-bis), social links (links only). `where` = footer | header | page text | JSON-LD | ...
    # IdentityScan.pages = [{"url", "kind", "text", "html"}] reused by profile and people (no extra request)
# web/profile.py
def profile_of(pages, *, when=None) -> dict      # {"presentation": {...}, "history": {...}}: sentences taken verbatim from the pages, each with url/where/source/date; never rewritten
# web/social.py  (no network, no Qt)
def classify(url) -> (network, canonical url) | None      # a company PROFILE link only: share buttons, posts, personal profiles and the network's own pages give None
def links_for_card(card) / links_for_company(company) -> list[SocialLink]      # SocialLink(network, url, kind, source, source_url, where); kind = "page" (found) | "search" (opened in the browser)
    # a profile URL is NEVER guessed: it is listed as a page only when found on the company's site, in Wikidata, in OpenStreetMap or in the card data; else a LinkedIn company SEARCH, and the map is always a search by name and town
def linkedin_search_url(name) / maps_search_url(name, town="") / strip_legal_form(name) / provenance_text(link, translate=None)
# web/people.py
def people_of(pages, postings=(), extra=(), deleted=(), site="") -> list[dict]
    # contacts {id, name, role, email, phone, kind: hr|management|general|privacy, source_url, where, origin, date}; HR first;
    # privacy/DPO mailboxes are kept but marked "privacy" (never proposed as the recipient); e-mail addresses are never guessed;
    # only addresses of the company's own mail domains when `site` is given; `deleted` ids are not added again
```
Postings carry `contract`, `remote`, `salary` (as published, never estimated), `salary_min`, `salary_max`, `salary_period`, `salary_currency`, `travel` and `travel_note`; each field is `None` when the posting does not state it (frequent in practice). Descriptions are kept as plain text, one line per block, up to 6000 characters, so that the requirement rules can read headings and bullets.

## discovery
Company discovery from several independent sources, merged and deduplicated. Every source is optional and every failure is non-fatal. Every outgoing request goes through one `PoliteGateway`.
```python
# discovery/__init__.py
def source_names(settings) -> list[str]          # enabled sources; "own" is always on, "regional_register" is on unless the value says "-regional_register";
    # the old name "imports" is an alias of "regional_register" (ALIASES); unknown names are dropped
def sources_value(chosen) -> str                  # the settings value for the sources ticked in the panel ("-regional_register" written when it is off)
def build_sources(settings, store, names=None, region=None, *, register_manager=None) -> list[DiscoverySource]
    # priority order: osm_extract, regional_register, own, overpass, wikidata, overture; the register manager defaults to registers.shared_manager
# discovery/sources.py
KNOWN_SOURCES = ("osm_extract", "overpass", "wikidata", "overture", "regional_register", "imports", "own"); AREAS = ("radius", "province", "region")
    # REGIONAL_REGISTER = "regional_register" (the official lists AND the user's imported lists, see "registers"); IMPORTS = "imports" is only its old name
    # (ImportSource still exists but build_sources no longer builds it); REGISTER_OFF = "-regional_register"
@dataclass
class DiscoveryQuery: lat; lon; radius_km; sectors=None; area="radius"; province=""; region=None; ateco=[]; include_generic=True
class DiscoverySource:  name; network; def find(query, gateway, cancel, progress=None) -> list[Company]; def count(...) -> int | None
class SourceSkipped(Exception): reason; detail; state          # not an error ("not_downloaded", "outside_region", ...)
@dataclass
class SourceStatus: name; state; count; new; seconds; reason; detail     # state ok | empty | skipped | failed | unavailable | cancelled | disabled
def discover(query, sources, gateway, cancel=None, on_status=None, progress=None) -> DiscoveryResult   # (companies, statuses); .all_failed
    # local sources first; Overpass runs only when the local map data could not answer ("not_needed" otherwise);
    # at most 2 network sources at once; the results are merged by website domain, VAT number, name and town
# discovery/gateway.py
class PoliteGateway(settings, store=None):    # >= settings.request_pause_s (default 2 s) between two requests to the same host,
    # shared by the whole process; at most 2 hosts at once; identifying User-Agent with a contact URL; HTTP 429 (or 503 with
    # Retry-After): one back-off, then the host is given up for this gateway (SourceBlocked); optional Store cache; cancellable;
    # resumable, throttled downloads (default 3 MB/s)
    def get(url, **kw) -> Reply; def post(url, **kw) -> Reply; def download(url, target, *, cancel=None, ...); def close(); def stats() -> dict
# discovery/osm_extract.py  (Source A: local regional extract, filtered once into a SQLite index under <data folder>/osm/)
def status(region) -> ExtractStatus                          # .pbf_exists, .ready, .stale, index age
def download(region, gateway, *, cancel=None, progress=None) # explicit only (the "Download / refresh map data" button), never during a search
def build_index(source, target, *, cancel=None, progress=None, region_key=...)   # one read of the file with discovery/pbf.py
class OsmIndex: radius(lat, lon, km); province(code, lat=None, lon=None); region(lat=None, lon=None); count_*(...); find_place(name); provinces()
# discovery/ateco.py  (ISTAT ATECO 2025, discovery/data/ateco_2025.csv)
def ateco_for_profile(profile, sectors=None, limit=15) -> list[AtecoCode]   # CATEGORIES to look for, never the code of a company
def search(text, limit=30) -> list[AtecoCode]; def describe(code) -> AtecoCode | None
# discovery/imports.py  (the IMPORTER: CSV or XLSX lists, up to 50,000 rows; register export, start-up register, or generic)
def preview(path) -> ImportPreview; def records(preview, mapping=None) -> list[dict]; automap(header); detect_kind(...); dedup_key(record)
    # New imports are stored by registers.lists.import_list (see "registers"). save / lists / delete / refresh / register / update_row / add_row /
    # delete_row / read_list keep the lists that older versions held in the Store cache; registers.lists.migrate_imported_lists moves them once.
# discovery/ranking.py  (pre-crawl ranking, explainable)
def context_for(profile=None, sectors=None, ateco=None) -> RankContext
def rank(company, context) -> Ranking                       # .score, .reasons, .why ("; "-joined English reasons)
def rank_all(companies, context) -> dict[int, Ranking]      # also stores company.fields["why_selected"]
# discovery/prefs.py: DiscoveryPrefs(area, province, ateco, ateco_custom, sources, region), read(store) / write(store, prefs)   # kept in the Store cache
#   effective_sources(settings, store) / effective_region(settings, store): the choice made on tab 2 when there is one, else settings.discovery_sources / osm_region
```
`max_companies` keeps the first companies of the ranking, so the ranking decides which sites are read. It puts employers of the profile first and pushes down public offices, education and research (unless the profile is research), shops, hospitality and associations. A register ATECO from an imported list counts strongly; an unknown ATECO is kept and ranked by the other signals. Companies from Wikidata and the register lists carry identifiers in `Company.ids` and their provenance in `Company.sources`. The Wikidata query runs once per region (30-day cache); Overture (optional, needs `duckdb`) is off by default.

## registry (free identity sources)
```python
# registry/http.py
class RegistryClient(settings, store=None, client=None, ...)   # per-host pacing (settings.request_pause_s), 30-day Store cache,
    # on HTTP 429 one wait, then the host is paused for 5 minutes (RegistryError("rate_limited"))
# registry/vies.py:  lookup(client, vat, country="IT") -> ViesResult      # EU VAT number check; "not registered" is not a negative
# registry/gleif.py: by_tax_code(client, tax_code) / by_lei(client, lei) -> GleifAnswer
# registry/checksums.py: piva_ok / cf_ok / normalize_* / rea_equal       # check digits of the Italian identifiers
# registry/visura.py: parse_csv(text), clean_fields(raw), pick_row(...)    # register data typed or imported by the user; invalid identifiers are not saved
# registry/confirm.py
STATUSES = ("confirmed", "probable", "unverified", "conflict")
def evaluate(facts: list[dict], card_name="", *, vies_not_registered=False) -> IdentityResult
    # CONFIRMED only when an identifier printed on the company's own site equals the one in an independent register
    # (register import, manual Registro Imprese data, paid register, VIES or GLEIF) and the names agree; every value keeps its source, URL and date
# registry/wikidata.py: lookup(gateway, *, qid="", vat="", lei="", cancel=None) -> WikidataIdentity   # found by identifier only (never by name); VAT, LEI, founding year, website, employees, CEO as a contact
# registry/paid.py: ONE paid provider, Openapi (company.openapi.com, "IT-full"), UNTESTED against the live service (UNTESTED_AGAINST_LIVE_SERVICE = True):
#   written from the documented fields and tested with fabricated answers only. can_fetch(settings), estimate(settings), cost_note(settings),
#   fetch_company(settings, *, vat, tax_code, client=None) -> (fields, payload). A call happens only with the user's own key
#   (settings.paid_registry_provider / paid_registry_key), after the GUI showed the cost and the user confirmed, one company at a time;
#   never from build_card/build_cards, never in tests. The key goes only in the Authorization header and is never logged, cached or shown.
#   Other providers of the list are descriptions only (implemented=False); PaidRegistryUnavailable(kind) for every refusal
```

## financials (revenue, net profit, employees by year)
```python
# financials/__init__.py
FinancialYear(year, revenue, profit, employees, currency, scope, source, source_url, retrieved_at, period_end, note, filled_from, ranges)   # unknown = None, never 0;
    # ranges = {"revenue": [low, high], ...}: a figure known only as a range ("between 10 and 50 million") stays a range, the amount itself stays None
FinancialSeries   # keyed by (year, scope): add(), merge(), view(scope) -> SeriesView, scopes(), to_dict()/from_dict(); conflicts are kept, never overwritten
AUTHORITY = {"manual": 90, "esef": 80, "openapi": 70, "pasted": 60, "wikidata": 30}   # the more authoritative record wins; the other one becomes a Conflict
SCOPES = ("consolidated", "separate", "unknown")                         # never mixed in one chart view; no currency conversion
EsefSource(gateway=None, years=5)       # filings.xbrl.org API by LEI: ifrs-full Revenue (fallback RevenueFromContractsWithCustomers), ProfitLoss, AverageNumberOfEmployees when tagged; scope consolidated; listed companies only
WikidataSource(gateway=None)            # P2139 revenue, P2295 net profit, P1128 employees (qualifier P585); low authority, scope unknown; ambiguous statements stay blank
OpenapiSource(api_key, gateway=None)    # paid (paid = True): revenue and employees of the IT-full answer; UNTESTED against the live service; no profit field
ManualSource / parse_csv(text, *, source, default_scope, multiplier, note) / add_value(series, year, ...)   # typed or CSV figures (English or Italian headers); an ambiguous number such as "1.234" is rejected, not guessed
collect_financials(identity: FinancialsIdentity, sources, gateway=None, *, allow_paid=False, series=None, authority=None) -> CollectResult
    # never raises; paid sources are skipped unless allow_paid=True; CollectResult.reports / why_missing(tr) / notes(tr)
FinancialsIdentity.from_card(card)      # lei, wikidata qid, VAT digits
# financials/trend.py  (from the STORED figures only; nothing is estimated)
revenue_trend(series, scope=None, *, last=10) -> RevenueTrend     # CAGR of revenue between the first and last year of ONE scope and ONE currency; needs MIN_YEARS = 3 yearly revenue values
    # (both ends positive), else state "insufficient"; state growing (CAGR >= +2 %/year, STABLE_BAND) | stable | declining (<= -2 %) | insufficient
RevenueTrend(state, cagr, first_year, last_year, years, sources, currency, scope); .known, .percent, .to_dict()
trend_text(trend, tr=None) -> str; trend_cell(trend) -> str ("+6.3 %/year (2021-2024)" or "")
employees_by_year(series, scope=None) -> [(year, employees, source)]; latest_employees(series, scope=None) -> (employees, year, source) | None
ranking_bonus(trend) -> float     # +0.05 from +2 %/year, +0.10 from +10 %/year, never negative: a declining company or one without figures is never pushed down nor hidden (used by discovery/ranking.py)
# financials/paste.py  ("Paste figures...": the user copies the text of a company-information page; the app never reads those sites)
parse_pasted(text) -> PasteResult(years: list[PastedYear] newest first, currency, warnings)     # never raises; Italian and English labels (Fatturato/Ricavi/Revenue..., Utile/Perdita/Profit...,
    # Dipendenti/Addetti/Employees...), one figure per line, a year on an earlier line, or a table with the years in its header; "1.234.567", "1,2 mln", "850 mila", "(120)"; ranges kept as ranges; a number followed by %, "percent" or "per cento" is ignored (a rate or a share, never an amount); `format_figure(figure, whole=False, language=None)` gives the grid text with the interface language's thousands marks ("12,345,678" in English), `parse_figure` reads every style back
PastedYear(year, revenue, profit, employees: Figure | None, lines); Figure(low, high, text, unit) (.is_range, .value); parse_figure(text) -> Figure | None (one cell of the review grid)
    # NOTHING is saved by the parser: the review window (gui/financials_paste.py) saves only what the user confirms, via companies.add_pasted_years, with source "pasted"
# financials/info_sites.py  ("Open company-information sites")
INFO_SITES (five public sites); info_query(identity) -> str (VAT digits, else tax code, else the quoted name, plus the figures sought); info_search_url(identity) -> str ("" when nothing identifies the company)
    # only builds a web-search link opened in the user's browser; the app never fetches those sites (their terms forbid automated reading)
# financials/manual.py: parse_csv_file(path, **kwargs); ImportResult; parse_amount(text) -> float | None (AmountError for an ambiguous "1.234")
# financials/chart.py: FinancialChart (QPainter only, no chart library; follows the palette; a loss is hatched), render_png(width, height)
```
`companies.build_card` runs the free sources marked `automatic` (`collect.automatic_sources()`: today EsefSource and WikidataSource) only. Paid figures come from `companies.fetch_paid` (one confirmed call).

## scoring
```python
# scoring/embedding.py
class Embedder(Protocol):
    def embed(self, texts: list[str], kind: str = "passage") -> np.ndarray   # normalised rows; kind is "query" or "passage"
def create_embedder(store: Store | None = None, models_folder=None) -> Embedder   # local multilingual model, loaded on first call
def model_search_folders(data_models=None) -> list[Path]   # <data>/models, $REVERSEHIRE_MODELS, <repo>/models, next to the installed program, models of the data folders of older versions
def find_local_model(model: str, folders) -> LocalModel | None   # LocalModel(model, folder, snapshot); read only
class FastembedEmbedder:   # create_embedder's class; extra keyword-only: search_folders=, probe=, clock=
    source_folder: Path | None; offline_load: bool | None; last_error: ModelLoadError | None; reduced = False
class ModelLoadError(RuntimeError): cause: str; attempts: list[tuple[str, Exception]]; folders; def details() -> str
CAUSES = ("runtime", "blocked", "network", "certificate", "damaged", "memory", "not_writable", "unknown")
def classify_load_error(error) -> str        # one of CAUSES, from the exception chain
def failure_cause(error) -> str; def failure_details(error) -> str   # never CV text
class LexicalEmbedder:      # built-in fallback: hashed word/char n-grams, deterministic, reduced = True
class ResilientEmbedder(primary, fallback=None, retry_after_s=300, clock=..., on_change=None)   # falls back, retries
def use_system_certificates() -> bool        # truststore.inject_into_ssl(), once per process

# scoring/assessment.py
def assess(profile: CVProfile, company: Company, outcome, embedder: Embedder,
           ui_language: str = "it", *, settings: Settings | None = None,
           role_skills: list[str] | None = None) -> Assessment

# scoring/requirements.py
def extract_items(description: str, posting=None) -> list[RequirementItem]   # priority must | nice | context (+ duty)
def check_items(items, profile, embedder=None, cv=None, max_travel_days=None) -> list[RequirementCheck]
def coverage(checks: list[RequirementCheck]) -> float | None                  # weights must 5 / nice 3 / context 1
def must_summary(checks) -> tuple[int, int]

# scoring/preferences.py
def compute_desirability(company, posting, profile, settings=None, checks=None) -> Desirability
def compute_spontaneous(profile, company, outcome, r_rel) -> Spontaneous
def compute_match(*, relevance, fit, desirability, spontaneous, confidence: float, blocked: bool = False) -> float
def yearly_salary(posting) -> float | None

# scoring/semantic_index.py  (THE engine of every "search by meaning" box: My companies, Favourites, Results, Job search results, Previous searches; no Qt)
card_document(card) / company_document(company, evaluation=None, key=None) / posting_document(posting, key=None) / search_document(search: dict, companies=()) -> Document(key, title, passages: list[Passage(text, source, label)])
    # a document = short passages (sentences grouped up to PASSAGE_CHARS = 280, at most 60 per document), each with its source ("presentation", "products", "posting", "note", ...) shown with the hit;
    # a full card is indexed with everything it collected (presentation, history, products, ATECO title, reasons of the assessment, posting descriptions), not only name and tags
SemanticIndex(embedder=None, store=None): set_documents(docs); build(documents=None, *, progress=None, cancel=None) -> int (passages embedded now); search(query, *, keys=None) -> list[Hit];
    # hits_by_key(query, keys=None); .mode "semantic" | "keyword" (no embedder, or the lexical fallback = reduced); thread-safe for one builder and readers (builds on a worker)
Hit(key, relevance 0..100, tier, passage, semantic, terms): .exact, .matches (relevance >= MATCH_THRESHOLD = 35), .source, .snippet(limit), .spans(text), .snippet_html(tag="b")
    # tiers: TIER_PHRASE (the whole query phrase, or an IT <-> EN variant, found as words in a passage: 75..100) > TIER_ALL_WORDS (all the words in one passage: 60..85) > TIER_SEMANTIC (meaning only: 0..59):
    # an exact hit never ranks below an unrelated semantic neighbour. Vectors are cached in memory and in the Store (namespace "semantic_v1", key = sha256(model, kind, text)): a changed card
    # re-embeds only the passages whose text changed, a repeated search embeds only the query. expand_query(query) gives the IT <-> EN variants; mode_of(embedder)
# scoring/company_search.py  (older, card-only; the lists no longer use search_cards, "Similar companies" still uses similar_cards)
def search_cards(cards: list, query: str, embedder, *, limit: int = 50,
                 min_score: float | None = None) -> list[tuple[CompanyCard, float, str]]   # (card, score 0..100, matched text)
def similar_cards(cards: list, card, embedder, limit: int = 10) -> list[tuple[CompanyCard, float, str]]
```
`outcome` is a `SiteOutcome` or `None`; the scoring module treats it by attributes (duck typing) and does not import from `web`. `relevance` and `fit` are 0..100 (`fit=None` without postings), `confidence` is 0..1. `outcome=None` is for companies without a website: the assessment is based on OSM tags only, with low confidence. `assess` copies `status` and `careers_url` from the result, sorts `postings` from best fit and fills `reasons` and `gaps`. `ui_language` is the language code of the interface, used for the language of reasons and gaps; the default in the code is still `"it"`. `settings` carries the candidate's preferences (`prefer_permanent`, `prefer_remote`, `min_salary_year`, `max_travel_days_year`) and the radius; `role_skills` are the ESCO essential skills of the occupation, used as role coverage. The `Assessment` also carries `desirability`, `spontaneous` (only without postings), `match` (the sorting value) and, for the best posting, `requirements`, `blocked` and `blocked_reason`. A requirement is `blocking` only when it is a must-have of a hard kind (language, licence, car, travel) and the CV explicitly contradicts it; silence is "uncertain", never a failure. Formulas are in the module docstrings of `scoring/preferences.py` and `scoring/requirements.py`.

## pipeline
```python
# pipeline.py
class SearchExecutor:
    def __init__(self, settings, store, embedder=None, fetcher=None, overpass=None, *, analyze=None,
                 assessor=None, fetcher_factory=None, embedder_factory=None, job_board_search=None,
                 skills_lookup=None, sources=None, gateway=None)
    def estimate(self, place: Place, radius_km, sectors, n_manual_urls: int = 0) -> Estimate
    def run(self, profile, place: Place, radius_km, manual_urls: list[str], sectors: list[str] | None,
            on_progress: Callable[[Event], None], cancel: threading.Event, search_id: int | None = None,
            *, area: str | None = None, province: str | None = None, ateco: list[str] | None = None) -> int
    def resume(self, search_id: int, on_progress, cancel) -> int
    discovery_statuses: list[SourceStatus]        # of the last discovery
@dataclass
class Event: kind: str; message: str; current: int; total: int; company; assessment; search_id;
    key: str; parameters: dict; status: str      # key + parameters: the i18n text of the event
def estimate_from_count(settings, found_items: int, n_manual_urls: int = 0) -> Estimate   # no network
def search_parameters(store, search_id) -> dict   # manual_urls, sectors, and "reduced"/"model_cause" when run without the model
SearchExecutor.reduced -> dict | None             # {"cause", "details"} when the lexical fallback is in use
```
New event keys: `pipeline.reduced_mode` (INFO; parameters `cause`, `reason`, `details`: the model could not be loaded and the search goes on with `LexicalEmbedder`, confidences × `REDUCED_CONFIDENCE_FACTOR`), `pipeline.overpass_slow` (INFO, `seconds`), `pipeline.no_companies_map` / `pipeline.no_companies_no_site` (INFO, `radius`, `found`, `sectors`), `pipeline.scoring_without_site` (PROGRESS); `pipeline.analysing` now also has `position` and `total`; the END event has `reduced`. The model is loaded (or the fallback chosen) right after the discovery, before the first site. The Overpass search runs on a helper thread: a cancel returns at once. `error.model` is no longer raised by `run`.
The keyword-only arguments replace the network parts in tests. Unless only an Overpass client is injected (`overpass=`, the old single-source path), the companies come from the discovery sources above, ranked before any site is read; `area`, `province` and `ateco` default to the choices saved by the location tab (`discovery/prefs.py`) and to the ATECO categories proposed for the profile. The estimate counts from the local map data when it is there (or with an Overpass "out count"); an unknown count is estimated as 0. With sectors, companies that carry only a generic company tag are included, ranked below the sector matches. When `settings.job_boards_enabled` is on, `run` first asks the job boards (`web/jobboards.py`) for the top CV roles around the town of the starting point; their employers are merged into the companies already found or added with `origin="jobboard"`, and processed right after the manual URLs. Any job-board failure becomes an info event (`pipeline.jobboards_*` keys) and the search goes on.
`run` analyses the companies' sites in parallel: the number of worker threads is `Settings.parallel_companies` (clamped to 1..16, default 8; 1 means the sequential loop). Results keep following the priority order. The `on_progress` callback is not called from the GUI thread: the GUI must pass the events to the main thread (Qt signals or a queue) instead of touching widgets directly.

## progress (status-bar progress models; no Qt)
```python
# progress.py
CV_STEPS = (("read", .08), ("ocr", .29), ("ocr_advanced", .08), ("sections", .10), ("esco", .10), ("translate", .14),
            ("recruiter", .11), ("ai", .04), ("photo", .03), ("display", .03))   # step keys with their share of the bar
StepCallback = Callable[[str, int, int], None]          # (step key, pages done, pages total); 0/0 for the unpaged steps
def notify_step(callback, key, done=0, total=0)         # a failing callback never stops the work
class CVProgress: update(key, done=0, total=0) -> float; fraction; index; count; ocr_page   # "page 2 of 5" during "ocr" / "ocr_advanced"
class SearchProgress(clock=time.monotonic):             # companies of a search: "analysed / total", time left
    set_total(total, *, estimated: bool) -> Revision | None   # the estimate as soon as discovery has its list; the final number later
    update(done); fraction (None until the total is known); shown_total; remaining_seconds() -> float | None
    # remaining_seconds: None until ETA_MIN_DONE = 3 companies over ETA_MIN_SECONDS = 20 s; the bar never goes backwards
def remaining_minutes(seconds) -> int | None            # rounded up; 0 = "less than a minute"
```
`gui/progress_bar.py` has `BarState(fraction, text, tooltip)` and `StatusProgress(bar)`: one bar for every producer (owners such as "cv", "search", "analysis", "jobs", "ocr_models", "affinity", "suggest", "registers", "jobs_companies", "interest-fast", "interest-deep"): `show(owner, state)`, `finish(owner, final=None)`, `state(owner)`, `idle()`; the producer that started last is shown. States reach it from the workers through the main window's queue.

## jobsearch (job postings from sources that allow automated reading; errors are logged and returned, never raised)
```python
# jobsearch/__init__.py
parse_query(text, *, place_lookup=None, esco=None, default_radius_km=30.0) -> SearchQuery   # role, skills, place + radius (default 30 km, province 40, at most 300), contract, hours, remote,
    # seniority, "quoted phrases" (must appear), -excluded words; role variants IT <-> EN; understanding applied (below). place_level: town | province | region | country (+ region_key / country)
    # SearchQuery.alternatives ("OT/Cybersecurity" -> [["ot", "cybersecurity"]]), .expansions (the other terms searched for a word or skill), .corrections (typos corrected), .removed_terms (removed by the user);
    # .also_terms() -> [(term, kind)] = the "Searched also for" chips; .remove_term(term) -> bool; .word_groups() / .skill_groups() / .search_terms() / .relevance(title, text="")
build_sources(keys=None, *, cards=None, local_postings=None, max_pages=20, careerjet_user_ip="", careerjet_user_agent="") -> list[JobSource]
    # LocalPostingsSource, AtsCompaniesSource (nine ATS APIs + careers pages of the cards, max 60 companies, robots.txt), AgencySitemapSource
    # x 6 (Randstad, Manpower, Synergie enabled; Adecco, Gi Group, Umana: allowed_by_terms = False, never contacted), Adzuna / Jooble /
    # Careerjet (keyed, off by default, untested_live = True)
search_jobs(query, sources, *, gateway=None, cv_profile=None, scorer=None, enabled=None, cache=None, progress=None, cancel=None,
            source_timeout_s=120.0, place_lookup=None, geocode=None, esco=None, today=None, max_results=300, max_workers=3,
            keep_unknown_location=True, embedder=None, vector_store=None) -> SearchResult
    # progress(done, estimated_total, message); fan-out in worker threads; per-source timeout keeps partial results; de-duplication;
    # place/radius and contract filters; rank = 65 % fit with the CV (the scorer; the relevance of the ad without a CV), 10 % how well the ad answers the query
    # (words, synonyms, meaning), 15 % distance, 10 % recency. With `embedder` the postings are also ranked by meaning (jobsearch/semantic.py): a posting whose words do not
    # match but whose meaning is close enough (MEANING_KEEP = 62 of 100) is kept, with that reason; without the model the mode is "keyword". `vector_store` caches the vectors.
pipeline.rerank_postings(query, postings, *, cv_profile=None, scorer=None, embedder=None, vector_store=None, today=None, cancel=None) -> (postings, stats)
    # the postings already found, ranked again for a changed query (a "Searched also for" term removed): no source is asked again; never raises
pipeline.context_why(query, posting, today=None) -> list[str]; pipeline.enabled_ids(sources, enabled=None) -> set[str]
# jobsearch/understanding.py (local tables only, no network): split_token("ot/cybersecurity") -> (["ot", "cybersecurity"], True) ("-", "+", "&", "," and brackets split too, unless the whole token is
#   a known term: "pl/sql", "c++", "node.js"); correct(word) -> str | None (typo against the CV skill dictionary, the job-search words, the role tables and the IT <-> EN terms; words under 5
#   letters are never corrected; rapidfuzz ratio >= 86); equivalents(term, limit=10) -> list[str] ("sicurezza informatica" <-> "cybersecurity"); expansions_for(words, skills); ESCO labels only when the helper passed to parse_query returned them
# jobsearch/lexicon.py: built-in Italian/English tables (a small gazetteer of towns for when no place lookup is passed, groups of equivalent job words, contract / hours / remote / seniority words,
#   sector, CCNL and size hints mapped to ATECO 2025 divisions, staffing-agency names, skill extraction); data only, no I/O
# jobsearch/semantic.py: rank_by_meaning(query, documents, *, embedder=None, store=None, cancel=None) -> ({document key: Hit}, mode "semantic" | "keyword"); query_texts(query) (the role as typed, each alternative,
#   the first "Searched also for" terms; at most 8); never raises; the engine is scoring/semantic_index.py
# jobsearch/spontaneous.py  (companies for a spontaneous application: companies the app already knows, no network)
rank_companies(query, *, cards=(), register=(), map_companies=(), register_by_sector=(), embedder=None, store=None, cancel=None, limit=150) -> (list[CompanyCandidate], mode)
    # sources: the saved cards (every text of the card), the regional register (name, ATECO, description), the local map index (name, OSM tags); a company that is already a card is listed once, as a card;
    # words of the search and their equivalents rank above a similarity of meaning; meaning-only hits under MEANING_MIN = 45 are not listed; a register company found through the ATECO divisions
    # of the search words (ateco_prefixes_for(query)) is kept with that sector as the reason
CompanyCandidate(key ("card:<id>" | "register:<key>" | "map:<name>|<lat>|<lon>"), name, origin ("card" | "register" | "map"), item, place, card_id, built, relevance 0..100, exact, terms, snippet, passage, sector); .has_card
direction_for_query(query, prefs) -> str | None      # the enabled career direction (affinity preferences) the search names best; None = judge on every direction
parse_pasted(text, html=None, hint_url=None) -> PastedDraft          # LinkedIn job / company page, Indeed job, generic ad; nothing is fetched,
    # lines naming people are dropped (removed_people); fields carry confidence "high" | "medium" | "low"; to_posting(), to_company_fields()
company_seed_from_posting(posting) -> dict                            # keyword arguments of companies.add_manual (+ "extra")
possible_employers(posting, company_index, cards, agency_clients) -> list[EmployerHypothesis]
estimate_employers(...) -> EmployerEstimate                           # hypotheses (top 5, each >= 10 %) + the "not in our list" share
Posting, SearchQuery, SearchResult, SourceReport, JobSearchCache, JobSource, texts.text(key)
```
A `JobSource` has `id`, `label`, `enabled_by_default`, `allowed_by_terms` (False = can never be enabled and is never contacted whatever the settings say), `terms_verdict` (`allowed` | `not_addressed` | `harmful_automation_only` | `forbidden` | `api_terms`), `terms_note`, `terms_url`, `terms_checked` (ISO date), `needs_key`, `untested_live`, `describe()` and `run(ctx)`. The default `SourceReport.state` values: `ok`, `empty`, `disabled`, `no_key`, `forbidden_by_terms`, `failed`, `timeout`, `cancelled`, `partial`. Keys are sent only to their own service, never logged, never cached. The English texts of the package are in `jobsearch/texts.py` (and the locale files).

## affinity (is this company one to send the CV to? pure Python, no Qt, no network, no store)
```python
# affinity/__init__.py re-exports: affinity, cv_context, finalize, AffinityResult, AffinityPreferences, Direction, default_directions,
#   Weights, Thresholds, default_weights, sort_key, Feedback, CalibrationReport, calibrate, rejudge, CompanyDemand, build_demand,
#   demand_from_card, demand_from_result, AffinityCache, affinity_for_companies, cv_fingerprint, results_to_json, results_from_json,
#   Note, FALLBACK, text, and the verdicts APPLY | SPONTANEOUS | WATCH | NOT_A_FIT | UNKNOWN (VERDICTS)
# affinity/engine.py
def affinity(cv_profile, demand, preferences: AffinityPreferences | None = None, *, embedder=None,
             weights: Weights | None = None, today: date | None = None, context: CvContext | None = None) -> AffinityResult
    # demand: a CompanyDemand, a card or its row dict, a models.Company, or a (Company, Assessment[, outcome]) tuple (build_demand dispatches)
    # one scope for the whole profile ("general") plus one per ENABLED direction; the headline is the applicable direction with the highest affinity, else "general"; a direction whose best posting fits (and passes `apply_min`, no hard fail) gets the Apply bonus first ("postings first"). A posting known only by its title is scored by comparing the title with the CV roles (`_title_score`), not 0; an internship / apprenticeship title (`_entry_level`) never fits a CV with `ENTRY_LEVEL_MAX_YEARS` = 5 or more years (reason `affinity.reason.entry_level`); `ALGORITHM_VERSION` = "3"
def cv_context(cv_profile, embedder=None, today=None) -> CvContext      # computed once and passed as `context=` for many companies
def finalize(result, weights) -> AffinityResult                         # (re)scores the stored components and applies the verdict table; used by rejudge

# affinity/weights.py  (all the numbers of the decision; the module docstring holds the formulas)
COMPONENTS = ("skills", "role", "sector", "seniority", "constraints", "practical")   # default weights 0.35 / 0.20 / 0.15 / 0.10 / 0.10 / 0.10
Thresholds(min_coverage=0.45, not_fit=40, apply_min=50, spontaneous_min=55, posting_fit=55, hard_cap=25, confidence_high=0.75, confidence_medium=0.55, ...)
Weights(components, thresholds, judgements=0); .normalized(); .version; to_dict() / from_dict()   # Settings.affinity_weights holds to_dict()
def decide(*, affinity, confidence, hard_fail, posting_fits, plausible_need, type_excluded, outside_directions, directions_only, thresholds) -> (verdict, reason codes)
    # first rule that applies: UNKNOWN (affinity is None); NOT_A_FIT (company type excluded, a hard constraint failed, outside every direction with directions_only, or affinity < not_fit);
    # APPLY (an open posting fits and affinity >= apply_min); SPONTANEOUS (affinity >= spontaneous_min, a plausible need, confidence not low); else WATCH
def sort_key(result) -> tuple     # ascending: APPLY and SPONTANEOUS first (APPLY gets +15, times the company-type weight), then WATCH, NOT_A_FIT, UNKNOWN

# affinity/model.py
AffinityResult(company_id, company_name, status ("ok" | "not_enough_data"), affinity (0..100 | None), confidence ("low"|"medium"|"high"), direction, direction_name,
               verdict, reasons, hints, scopes: list[Scope], strengths, gaps, extras, talking_points, best_posting: PostingFit | None,
               opportunity: Opportunity, company_types, primary_type, type_weight, type_excluded, outside_directions, directions_only,
               distance_km, fingerprints {cv, data, weights, preferences}, algorithm)
    # .headline -> Scope; .components; to_dict() / from_dict() / to_json() / from_json(); every user-visible text is a Note (i18n key + parameters), never display text
Scope(id, name, applicable, affinity, raw_affinity, coverage, confidence, components: dict[str, Component], best_posting, plausible_need, hard_fail, cap_reason, demand_share)
Component(key, score | None, quality, evidence: list[Point], detail);  Point(note, link: Link, term, weight, score);  Link = the CV line and the company text (with its source) behind a point
Opportunity(score 0..100, level low|medium|high, hiring_now, hiring_for_you, signals)   # reported separately; only the verdict uses it

# affinity/preferences.py
Direction(id, name, roles, families, skills, enabled=True, custom=False)      # shipped ids: industrial_automation, software_development, cybersecurity_networks, technical_coordination
AffinityPreferences(directions, company_types: dict[str, float], max_distance_km, prefer_permanent, prefer_remote, min_salary_year, max_travel_days,
                    knockout_answers, directions_only)
    # company types (affinity/knowledge.py COMPANY_TYPES): machine_builder, system_integrator, manufacturer, software_house (+ "other"); weight 0 = excluded
    # enabled_directions(), type_weight(key), to_dict() / from_dict() / to_json() / from_json(), fingerprint(); from_settings(settings, base=None) takes the practical fields
# affinity/demand.py: CompanyDemand = what a company seeks and does, CV-independent, each item with origin, weight and quoted evidence; origins explicit_must (5),
#   explicit_nice (3), explicit_context (1), stated (2: a technology named on the site), inferred_product / inferred_type (rule weights below 1); built by
#   build_demand(source) / demand_from_card(card, *, extra_postings=None) / demand_from_result(company, assessment=None, outcome=None, *, extra_postings=None);
#   .fingerprint() changes when the data changes (it is the data part of the cache key)
# affinity/knowledge.py: classify_company(texts, ateco="", ...) -> list[CompanyType] (type from site words, ATECO and OSM tags; confidence low|medium|high); rules in affinity/data/*.json
# affinity/batch.py
affinity_for_companies(cv_profile, items, preferences=None, *, weights=None, embedder=None, cache: AffinityCache | None = None,
                       progress=None, cancel=None, today=None, sort=False) -> list[AffinityResult]
    # input order (sort=True: weights.sort_key); progress(done, total, company_name); cancel = Event or callable, checked between companies, partial results returned;
    # a company whose data cannot be read is skipped. AffinityCache(size=5000): thread-safe in-memory LRU (get / put / clear, hits, misses)
cv_fingerprint(cv_profile, today=None) -> str     # includes the month: affinity is recomputed when the month changes
# affinity/calibration.py
Feedback(company_id, agrees, user_verdict=None, when=0.0)         # the latest judgement per company wins
calibrate(feedback, results, weights=None) -> CalibrationReport    # MIN_JUDGEMENTS = 8; needs at least 20 % disagreements; each component moves at most 0.05;
    # thresholds not_fit / apply_min / spontaneous_min move by 2 points and never more than 10 from the default; report: before, after, used, disagreements, applied, changed, note
rejudge(results, weights) -> list[AffinityResult]                 # the verdicts the new weights would give
```
The result keeps its own explanation: `reasons`, `strengths`, `gaps`, `extras`, `talking_points` and every `Point` quote the CV line and the company source. A thin result carries `hints` (what to add to improve it). Nothing here reads the network: the data is whatever the card or the search result already holds (company types, ATECO, products, postings, the revenue trend). `Settings.affinity_preferences` / `affinity_weights` hold the user's directions and calibration. Persistence is done by the GUI service (`gui/affinity_service.py`; store tables `affinity_results` and `affinity_feedback`).

## background (fast pre-screen first, deep analysis when the PC is idle; no Qt)
```python
# background/screen.py
screen_companies(candidates, cv_profile=None, preferences=None, *, sectors=None, ateco=None, key_of=None) -> list[ScreenResult]
    # offline, only data already in memory (name, OSM tags, Wikidata industry, register ATECO and employees, earlier card data); it ORDERS, never hides a company;
    # a candidate that cannot be read keeps its place at the end with score 0; a company-type weight of 0 puts a company last, with the reason
ScreenResult(key, name, score, direction, direction_name, company_type, reason, index)   # score is an ordering value, not an affinity
# background/scheduler.py
IdleConfig(idle_threshold_s=300, app_inactive_fallback_s=600, require_ac=True, cpu_max=0.5, require_network=True, daily_request_budget=600, per_host_daily=60,
           estimated_requests_per_job=25, max_attempts=5, backoff_base_s=300, backoff_max_s=21600, poll_s=0.5, max_parallel=1)
IdleScheduler(run_job, *, config=None, idle_seconds=None, on_ac_power=None, cpu_load=None, network_available=None, app_inactive_seconds=None, clock=time.time,
              on_status=None, on_state=None, on_job_done=None, system_probes=None)
    # run_job(job, ctx) -> JobResult | bool | None does the work (exceptions are caught); ctx.cancel is the event to honour, ctx.charge(host, n) counts requests
    # enqueue(key, name="", priority=0, payload=None, ...) / enqueue_screened(results, payload_of=None, host_of=None) / update_priorities / remove / clear_finished / jobs()
    # set_paused(bool) (the user's switch) / set_foreground_busy(bool) (a search is running: running jobs stop at once) / notify_user_active()
    # tick() -> SchedulerStatus (one step; tests call it) / start() daemon thread / stop(timeout) / wait_jobs(timeout) / status()
    # to_state() -> JSON dict (queue, attempts, retry times, counters of the day) and from_state(state, run_job, **kwargs) / load_state(state): the integrator stores it (on_state is called after every change)
# A job runs only when ALL hold: no input for idle_threshold_s (unknown idle time: the app window inactive for app_inactive_fallback_s; both unknown: no), on AC power
# (unknown = allowed), CPU load <= cpu_max (unknown = allowed), network available (unknown = allowed), no foreground search, not paused, today's budget and the per-host limit not spent.
# When a condition fails the job's cancel event is set and the job goes back to the queue without penalty; a failing job is retried with exponential back-off and marked failed after max_attempts.
# States: Job.state QUEUED | DONE | FAILED; SchedulerStatus.state S_IDLE | S_RUNNING | S_PAUSED | S_STOPPED
# background/probes.py: SystemProbes().idle_seconds() / on_ac_power() / cpu_load() / network_available(): cheap, cached 2 s, never raise, None = cannot tell
#   (Windows: ctypes system calls; Linux: xprintidle, /sys, /proc; macOS: ioreg, pmset; network unknown on macOS)
```
`Settings.idle_*` configure it (see "config"). The scheduler holds no CV and no secrets; the main window (`gui/window_interest.py`) injects the job callable.

## suggest (company suggestions from the user's own assistant, verified by the app; no Qt)
```python
# suggest/brief.py
SearchArea(kind="province", country="IT", region="", province="", province_name="", provinces=[], town="", radius_km=None, lat=None, lon=None)
    # kind: AREA_RADIUS | AREA_PROVINCE | AREA_REGION | AREA_COUNTRY; a radius is around a TOWN, never an address; lat/lon are for the area check only and are never sent
build_profile_brief(cv_profile, preferences=None, area=None, *, employer_class=None, forbidden=None) -> ProfileBrief
    # only roles (at most 6), skill families with years, sectors, languages, seniority, wanted directions and company types, the area; never name, e-mail, phone, address, home town,
    # employer names (an employer becomes "undisclosed employer" or "<sector>, <size class>"), dates (durations only) or raw CV text; every line is scrubbed and a final leak check
    # drops any line that still holds a forbidden string (fail closed; ProfileBrief.dropped counts them)
ProfileBrief.preview_text() -> str      # EXACTLY the block put into the prompt: the GUI shows it before the first use
find_leaks(text, cv_profile, forbidden=None) -> list[str]
# suggest/engine.py
suggest_companies(brief, *, cli, n=10, exclude=(), offset_token=None, language="en", callbacks=None, timeout=240, data_dir=None, allow_web_search=False,
                  model=None, cancel=None, resume=True, popen=None) -> SuggestionBatch        # never raises; runs the user's own CLI (chat.CliInfo)
SuggestionBatch(suggestions, duplicates, dropped, more_available, offset_token, usage: Usage, notices, error, message, detail, cli, web_search, prompt_chars)   # .ok; offset_token goes back for "Expand"
    # error codes: ERR_NO_CLI, ERR_MALFORMED, ERR_EMPTY, ERR_CLI, ERR_CANCELLED, ERR_INTERNAL
Suggestion(name, municipality, province, country, website, company_type, direction, why, confidence, rank)    # claims only; `why` is the assistant's OPINION
# suggest/runner.py: build_suggest_claude_command / build_suggest_codex_command (same lock-down as chat; no tool at all by default; with allow_web_search only Claude's WebSearch
#   or Codex's web_search, everything that reads files or runs commands stays off and any other tool call stops the run); Usage = tokens, cost estimate, turns and web searches as the CLI reports them
# suggest/verify.py
verify_suggestion(s, *, fetcher, gateway=None, registry=None, place_index=None, register_db=None, area=None, cancel=None, max_requests=8, cli="", today=None, language="it") -> VerifiedSuggestion   # never raises
verify_batch(suggestions, *, fetcher, ..., max_total_requests=40, max_requests_each=8, callbacks: BatchCallbacks | None = None) -> list[VerifiedSuggestion]    # in order, one request budget
    # (the GUI uses REQUESTS_EACH = 8 and BATCH_REQUESTS = 80); every network access goes through the polite fetcher / gateway / registry client; never a search engine
    # STATUSES: VERIFIED (site reachable + name on it + register identity OR the same company on the map OR a Wikidata item with the same website), PARTIAL (site + name only; or no readable site but the regional register lists the same company in the area: `register_db` is the local register, `register_lookup`, source "register", reason `suggest.reason.register_match`),
    #   UNVERIFIED (nothing confirmed; shown greyed, never added automatically), REJECTED (the site belongs to another company, or the company is outside the area).
    #   Absence of evidence is never "does not exist". Parked or for-sale domains are rejected.
VerifiedSuggestion(suggestion, status, reasons, site, site_source ("assistant" | "osm" | "wikidata"), name_on_site, legal_name, vat, identity_status, town, province, lat, lon, osm_id, wikidata, sources, requests, checked_on, cli)
    # .addable (VERIFIED or PARTIAL); .to_company_seed() -> keyword arguments of companies.add_manual plus "extra" (origin "assistant", provenance line "suggested by the assistant on <date>,
    #   verified by: ...", the assistant's why stored as a labelled OPINION), None when not addable
check_site(url, name, fetcher) -> SiteCheck; area_contains(area, *, province="", lat=None, lon=None, ...)
```
What leaves the computer is the preview text, sent to the user's own CLI on the user's own plan; `Settings.suggest_allow_web_search` (default off) is the single opt-in that lets the assistant search the web.

## registers (the regional register: free official company lists in one local, editable SQLite file; no Qt)
```python
# registers/__init__.py (re-exports)
RegisterManager(folder=None, settings=None, *, gateway_factory=None, db=None)      # folder default <data folder>/registers (register.sqlite, raw/<source>/ downloads with a manifest.json)
    .sources_info() -> list[dict]            # per source: title, licence, attribution, terms_url, update frequency, note, enabled, last_refresh, raw_bytes, downloaded (+ the bought-list source)
    .attributions(used_only=True) -> list[str]
    .estimate(ids=None, cancel=None) / .check_updates(ids=None, cancel=None, progress=None) / .download(ids=None, progress=None, cancel=None, force=False)   # explicit only, polite gateway
    .refresh(sources=None, regions=None, progress=None, cancel=None, *, download=True, force=False) -> RefreshReport    # download what changed, parse for the regions, merge; never raises; state ok|partial|failed|cancelled
    .import_bought_list(path, mapping=None, source_date="", progress=None, cancel=None) -> dict      # a Registro Imprese list the user bought (CSV/XLSX), merged by tax code; earlier lists kept
    .reminder(today=None) / .disk_usage() / .purge_raw(ids=None) -> int / .find(**kwargs) / .stats() / .coverage_note(province, ateco_division, municipality=None) / .db
RegisterJob(target, *args, progress=None, **kwargs)     # a manager call on a daemon thread: cancel(), done, result, error, wait(timeout), last_progress (a GUI timer polls it)
shared_manager(settings=None, folder=None) -> RegisterManager / close_shared()     # ONE manager per data folder, shared by the window and the discovery source
discovery_source(manager, region=None, store=None)      # the discovery source "regional_register" (no network; at most 3000 candidates); with `store` the lists an older version kept there are moved once
SOURCE_CLASSES; DOWNLOADABLE = ("rna", "gleif", "opencoesione", "agcm"); RI_BOUGHT; RegisterProgress (done / estimated bytes and rows, .fraction); RefreshReport / SourceRefresh
# registers/db.py   RegisterDB (SCHEMA_VERSION = 2; one connection per thread; WAL where allowed)
find(region=None, province=None, municipality=None, ateco_prefixes=(), text=None, only_active=True, limit=None, *, include_hints=True, include_presumed_province=True,
     include_hidden=False, offset=0, include_unplaced=False, keep_uncoded=()) -> list[RegisterCompany]     # count(**same filters); an ATECO prefix matches the exact code and, with include_hints, the RNA sector hints
get(key) / provenance_of(key) / stats() / coverage_note(province, division, municipality=None) / possible_duplicates(key_or_name, municipality="", limit=10)
edit(key, {field: value}) -> bool          # the edited fields are LOCKED: no later refresh overwrites them; unlock(key, field)
add_company(fields, cf="") -> key | None   # a company without a tax code gets a "U-..." key
hide(key) / unhide(key) / delete(key) / undo() / can_undo()                      # undo log of the last UNDO_LIMIT = 200 actions
export_csv(path) -> int / import_csv(path) -> ImportCsvReport / remove_source(source) / load_source(source, rows, regions=None, ...) -> LoadReport
# registers/record.py  RegisterCompany(key, cf, values, provenance {field: (source, date)}, locked, sources, user_added, hidden, province_src, province_guess, hiring_dates)
#   .province (sometimes PRESUMED from the tax office: .province_approx, .province_guess), .ateco_kind, .badges(), .to_dict(), .to_candidate(query_province="") -> models.Company
# registers/lists.py   the user's imported CSV/XLSX lists, each one a register source "list:<id>"
import_list(db, name, kind, records, *, source="", source_date="", path="", list_id=None, created=None, progress=None, cancel=None) -> ListReport; refresh_list(db, source_id, records, ...);
delete_list(db, source_id) -> int; lists(db); migrate_imported_lists(store, db, cancel=None) -> MigrationReport    # idempotent, never deletes the Store's copy
```
Sources: RNA state aid (the last `register_rna_months` months, at most 120, 0 = the whole archive), the GLEIF Golden Copy, OpenCoesione and the AGCM legality rating; the bought Registro Imprese list and the user's imported lists come after them. Per-field priority (`registers/model.py PRIORITY`): the user's edit first, then the bought list, GLEIF, OpenCoesione, AGCM, RNA, imported lists, derived values; ATECO is taken only as an exact code from the bought list, OpenCoesione or an imported list (RNA gives sector HINTS only). Fields: name, vat, legal_form, address, postcode, municipality, province, region, ateco, ateco_text, nace_hints, size_class, employees_class, founded, lei, website, status, legality_*, lat/lon, pec, rea, aid_*, note, email, phone. Licences and attributions come from `sources_info()` / `attributions()`; the official counts behind the coverage note are in `db.OFFICIAL_COUNTS`.

## applications (the tracker of CVs already sent: "Applications sent")
```python
# applications/__init__.py: ApplicationService, default_folder, DEFAULT_FOLLOW_UP_DAYS = 10 (working days), DUPLICATE_WINDOW_DAYS = 60, the records
#   Application, Appointment, Attachment, HistoryEntry, FollowUp, Statistics, ImportReport; the constants STATUSES, KINDS, CHANNELS, SOURCES, APPOINTMENT_KINDS, OUTCOMES;
#   export_ics / to_ics. The Qt widgets (ApplicationsPanel, ApplicationDialog, UpcomingAppointments, PdfPreview) are imported lazily, so the logic works without Qt.
STATUSES = ("draft", "sent", "acknowledged", "interview", "offer", "rejected", "no_answer", "withdrawn")      # OPEN_STATUSES = sent | acknowledged | interview
KINDS = ("posting", "spontaneous"); CHANNELS = ("email", "site", "agency", "linkedin", "other"); SOURCES = ("manual", "mail_draft", "export")
APPOINTMENT_KINDS = ("phone", "video", "onsite", "test", "other"); OUTCOMES = ("pending", "positive", "neutral", "negative", "cancelled")
ApplicationService(folder=None, *, clock=time.time, tr=None)      # folder default <data folder>/applications: applications.sqlite (SCHEMA_VERSION 2; tables applications, status_history,
    # appointments, attachments) and files/ (the archive: every file stored once under its SHA-256, at most 100 MB each); thread-safe, one connection per thread; its own database, NOT the main Store
    .create(*, company_name, company_id=None, role="", posting_url="", kind=None, channel="email", recipient_email="", contact_name="", sent_date=None, status=None, cover_subject="",
            cover_letter="", cv_path=None, cv_language="", notes="", follow_up_date=None, source="manual", who="user") -> Application
        # company_name is required (ValueError); kind defaults to "posting" when there is a URL, else "spontaneous"; status to "sent" when a date is given, else "draft"; cv_path is COPIED into the archive (call from a worker)
    .get(id) / .list(status=None, kind=None, company_id=None, ...) / .count() / .applications_for_company(company_id, company_name=None)
    .update(id, *, who="user", cv_path=None, **fields) / .set_status(id, status) / .mark_sent(id, when=None) / .set_cv(id, path) / .delete(id) -> DeleteResult / .history(id)   # changes of status and of the key fields are logged in status_history
    .cv_file(app) / .read_cv(app) -> bytes | None / .materialize_cv(app, folder) / .add_attachment(id, path) / .attachments(id, attachment_id=None) / .remove_attachment(id) / .gc_archive() -> int
    .record_from_mail_draft(draft, outcome=None, company=None, *, role="", posting_url="", kind=None, cv_language="") -> Application | None   # a DRAFT record for a draft the program opened; None when the draft was not opened
    .record_from_export(*, company=None, role="", posting_url="", kind=None, cover_letter="", cover_subject="", cv_path=None, cv_language="", channel="other", recipient_email="") -> Application
        # both return the existing record instead of creating a second one when the same company and role were recorded within DUPLICATE_WINDOW_DAYS; the program NEVER marks anything as sent by itself
    .due_follow_ups(today=None, after_days=10, *, working_days=True) -> list[FollowUp]; .find_duplicates(...); .statistics(today=None) -> Statistics
    .add_appointment(application_id, *, start, kind="phone", duration_min=60, place="", people="", notes="", outcome="pending", reminder_min=None) / get_appointment / update_appointment / delete_appointment
    .appointments_for(application_id) / .upcoming_appointments(now=None, *, days=None, limit=10) -> [(Appointment, Application)] / .next_appointments(now=None) -> {application_id: Appointment}
    .export_ics(appointment_ids=None, *, application_id=None) -> str / .appointment_ics(id) -> str          # RFC 5545 text; PRODID "-//ReverseHire//Applications sent//EN"; UID domain reversehire.invalid
    .export_csv(path=None) -> str / .import_csv(source) -> ImportReport     # cells starting with = + - @ are written as text; rows without a company or identical to a record are skipped
    .export_application(application_id, destination) -> Path               # a new sub-folder: the letter (.txt), the CV PDF as sent, the notes and a .ics when there are appointments
```
No MCP tool reads the applications database. A follow-up is due after `after_days` working days without an answer, unless the user set a later follow-up date.

## recruiter_sim (simulators of the screening systems; every result is labelled an approximation built from public documentation)
```python
# recruiter_sim/__init__.py
CvDocument(profile, read=None, path=None, layout=None); Target(title, description, location, skills, kind, posting, distance_km)
    # Target.from_posting(posting) / Target.from_role(title, skills=())
KnockoutAnswers(right_to_work, needs_sponsorship, max_commute_km, willing_to_relocate, notice_weeks, min_salary_year, driving_licence, ...)  # None = not answered
simulate(cv, target, profile, *, answers=None, embedder=None, ai_estimate=None, today=None, progress=None, cancel=None) -> SimulationResult
simulate_all(cv, target, profiles=None, **kwargs) -> {profile key: SimulationResult}     # the file layout is read once
what_if(result, n=5) -> [Edit]; score_after(result, edits) -> int                        # the best CV edits; skills the CV does not show are conditional
match_posting(cv_profile, posting, *, mode="taxonomy", embedder=None, today=None, cv_text=None, index=None) -> MatchResult   # "Match with my CV"
load_profiles(), get_profile(key), profile_for_platform(key), profile_for_host(host), analyse_layout(path), texts.render(key, params)
```
The profiles are data (`recruiter_sim/data/profiles.json`, 20 profiles, `reviewed` 2026-11-18, each validated by `validate_profile`; a bad file raises `ProfileError`): kinds `ats`, `parser`, `agency`, `none`, `baseline`, and a `confidence` of `low` | `medium` | `high` (in the shipped file: 7 medium, 13 low, none high; the `none` kind, e-mail without an ATS, is medium). The eleven checks (`CHECK_IDS`: parse, record, knockout, findability, semantic, timeline, skill_recency, credentials, format, ai_text, score) each give `Finding`s with a status `ok` | `warn` | `fail` | `unknown`, a `Message` (i18n key + parameters) and `EvidenceRef`s (the CV line, the posting line, the file, the user's answer or the profile's documentation). The score has five components (parse, findability, match, timeline, credentials) with per-profile weights; a knock-out can cap it. The engine computes ONE simulated score 0..100 per system; each system then presents it on its own scale (`SystemProfile.score.kind`: percent, rating_points, grade_AD, high_med_low, criteria_met, none) and `recruiter_sim/percent.py` maps every scale back to a percentage 0-100 % (`to_percent(kind, value)`; grades A / B / C / D = 87.5 / 65 / 45 / 17.5, High / Medium / Low = 85 / 55 / 20, stars x 20): the interface always shows the percentage first and the system's own presentation as a secondary note. `match_posting`: each skill of the ad is an item weighted 5 must / 3 nice / 1 context, credited by the evidence level in the CV (4 in a dated job .. 0 not found) and its recency; score = 100 (0.8 ad coverage + 0.2 use of the CV's skills); a hard must-have contradicted by the CV caps it at 20. English texts: `recruiter_sim/texts.py`.

## mapview (the map widget; importing the package never imports Qt WebEngine)
```python
# mapview/__init__.py
MapView(parent=None, *, settings=None, theme=None, tr=None, force_fallback=False, open_external_links=True, actions=None)   # Leaflet in QWebEngineView, else a QPainter map
    # signals: pointActivated(str), externalUrlRequested(str), backendChanged(str), offlineRequested(bool) (the user switched backgrounds off = True / on = False), tilesFailed(str) (reason code);
    # backend "web" | "fallback" (REVERSEHIRE_MAP_FALLBACK=1 forces the fallback)
    # set_points([MapPoint(id, lat, lon, label="", kind="company", status="", tooltip="")]) (kind: company | posting | home | hypothesis; anything else is drawn as company; status picks a colour of the theme),
    # set_radius(lat, lon, km), clear_radius(), set_center(lat, lon, zoom=11), fit_to_points(include_radius=True), select(id), set_theme(MapTheme), set_settings(MapSettings),
    # set_translator(tr), retranslate(), retry()
    # on-map message: banner_state() -> "offline" | "tiles_failed" | "web_failed" | "" and banner_text(); the message is a PANEL CENTRED on the map (never above the corner toggle), with a button that runs `actions["online"]` ("Show the online map") or `actions["retry"]` ("Retry");
    # `actions` = QActions shared with the window's menu (without them the map makes its own and only emits offlineRequested / retries itself); a corner toggle ("Work offline") follows MapSettings.offline
MapSettings(tile_url="", offline=False, attribution_text=..., attribution_url=..., user_agent="", cache_dir=None, cache_max_mb=200, max_zoom=19)   # MapSettings.from_app_settings(settings, data_folder) reads
    # map_tile_url, map_offline and user_agent; the cache is <data folder>/cache/map; .resolved() replaces every empty or invalid field by a safe default (logged); .page_payload() = what the page gets (no paths, no User-Agent)
validate_tile_template(template) -> (normalised | None, reason)       # {z} {x} {y}; https, or http on a loopback host, or file://
```
When a message shows (decided in `MapView._update_banner`, in this order): backgrounds switched off (`map_offline`) -> "offline" (text from `mapview.offline_message`, button "Show the online map"); the Leaflet page failed and the plain map is in use (not forced) -> "web_failed" (button "Retry"); no background tile could be loaded -> "tiles_failed" (the reason comes from `probe_tiles(tile_url, user_agent)`, one test request for the world tile on a worker thread: "network:<error>", "status:<code>" or ""; logged once per cause). The plain map (`mapview/fallback.py`, `FallbackMap`) draws the points in Web-Mercator coordinates, the radius circle and a scale bar, with drag and wheel; it needs no network.

Tile policy (OpenStreetMap): a stable application User-Agent, the attribution "(c) OpenStreetMap contributors" always visible (not changeable for the OSM tile servers), normal HTTP caching (`<data folder>/cache/map`, 200 MB), only the tiles of the current view, no prefetching and no offline download. `mapview/policy.py` (`RequestPolicy(assets_dir)`, `.set_tile_template(template)`, `.decide(url) -> Decision(allow, is_tile, reason)`, no Qt) decides every request of the page: files inside the package's bundled `web` folder (and the folder of a `file://` tile template), the web-channel script (`qrc:/qtwebchannel/qwebchannel.js`), `data:` / `about:` / `blob:` URLs and the one configured tile host are allowed; everything else is blocked. `gui/lazy_map.py` (`LazyMap`) builds the view only the first time its page is shown: a "Loading the map…" placeholder (`mapview.loading`, painted first, `PLACEHOLDER_PAINT_MS` = 80 ms) stands in while the Qt WebEngine profile is created; `lazy_map.warm_up(settings)` starts that profile `WARM_UP_DELAY_MS` = 20 s after the main window opens, at a quiet moment (nothing typed or clicked for `WARM_UP_QUIET_S` = 3 s, when it is busy or a search runs it postpones, at most `WARM_UP_TRIES` = 6 times and then gives up for that session), without any network request.

## version
`src/reversehire/_version.py` holds `__version__` ("MAJOR.MINOR.PATCH") and `BUILD_DATE` ("" for a checkout never built) as plain string assignments: `pyproject.toml` reads them without importing the package (`[tool.setuptools.dynamic]`) and `tools/build.py` rewrites them. The third number is the **build number**: `python tools/build.py --out <folder>` adds one at every build, writes the date, builds the wheel and prints the new version (it writes the old one back when the wheel fails; `--dry-run`, `--no-wheel`, `--date`, `--root` exist). `reversehire.build_number(version=...) -> int` and `reversehire.version_text()` ("0.1.3 (2026-11-18)") serve the About window and Diagnostics. `reversehire.DEVELOPER` ("0THack1A") and `REPOSITORY_URL` are the other constants of `__init__.py`.

## companies (saved company cards)
```python
# companies.py
CARD_STATUSES = ("interesting", "applied", "interview", "offer", "closed")
SECTIONS = ("header", "registry", "hiring", "products", "ats", "screening", "profile", "people", "financials")   # the nine card sections, in order
REFRESH_SECTIONS = ("identity", "profile", "people", "financials", "hiring", "products", "ats", "screening")       # the units build_card can rebuild
@dataclass
class CompanyCard: id: int; key: str; name: str; site: str | None; address: str; lat; lon; distance_km;
    osm_tags: dict; sectors: list[str]; description: str; assessment: dict | None; status: str;
    favourite: bool; notes: str; contacts: str; tags: list[str]; source_search_id: int | None;
    origin: str; created: float; updated: float
    # company card: legal_name, vat, tax_code, rea, ateco, website, registry, products, hiring, ats, screening, profile
    # (presentation, history), people (contacts), financials ({"series", "reports", "scope", "date"}), identity_status, identity,
    # provenance (per-field source, URL and date), and the *_checked timestamps (identity, registry, hiring, products, ats, screening,
    # profile, people, financials)
def card_key(company: Company) -> str                # one card per company
def save_from_result(store, company, assessment, search_id=None) -> CompanyCard   # never overwrites the user's fields
def add_manual(store, name, site=None, *, address="", lat=None, lon=None, sectors=None, description="",
               status="interesting", favourite=False, notes="", contacts="", tags=None) -> CompanyCard
def list_cards(store, *, status=None, favourites_only=False, hiring_only=False, text=None) -> list[CompanyCard]
def update(store, card_id: int, **fields) -> CompanyCard
def set_favourite(store, target: int | str | Company, on: bool, *, assessment=None, search_id=None) -> CompanyCard | None
def favourite_keys(store) -> set[str]
def delete(store, card_id: int) -> None
def build_card(store, company_or_url, fetcher, settings, *, cancel=None, progress=None, outcome=None, registry=None, gateway=None,
               sections=None, roles=None) -> CompanyCard
    # slow and polite, on demand only ("Create full card"): steps site, identity (home + legal, contacts, about, history, careers, team,
    # privacy pages), registers (VIES, GLEIF, Wikidata), profile (presentation and history), people, financials (the free sources marked `automatic`: ESEF, Wikidata),
    # ats, products; every completed step is saved at once (progress(key, done, total)); a cancel keeps what was done.
    # The paid provider is NEVER called here.
def build_cards(store, card_ids, fetcher, settings, *, cancel=None, progress=None, registry=None, gateway=None, sections=None) -> list[CompanyCard]
    # "Full cards for selected / all favourites": one card after the other, never in parallel; a failing card is logged and skipped
def fetch_paid(store, card_id, settings, *, client=None) -> (CompanyCard, errors)   # ONE paid lookup, only after the user confirmed the cost
def registro_imprese_query(card) -> str; apply_registro_imprese(store, card_id, raw, *, when=None)   # Registro Imprese by hand: ATECO (checked against the ATECO 2025 table), PEC, REA, tax code, legal form, registered office; source "Registro Imprese (entered by hand)"
def add_financial_year(store, card_id, year, *, revenue=None, profit=None, employees=None, scope="separate", currency="EUR", note=""); import_financials_csv(store, card_id, text, *, scope="separate", multiplier=1.0, filename="") -> (card, messages)
def add_pasted_years(store, card_id, rows: list[dict], *, source: str, currency="EUR", scope="separate") -> CompanyCard
    # the figures the user reviewed and confirmed in "Paste figures..." (rows: {"year", "revenue" | "profit" | "employees": financials.paste.Figure | None}); stored with source "pasted"
    # and the source the user stated in the note ("source: ..."); a range stays a range (FinancialYear.ranges, amount None); a new paste of the same year replaces the old one
def revenue_trends(store) -> {card key: RevenueTrend.to_dict()}; annotate_revenue_trends(store, found: list[Company]) -> int   # puts the trend into company.fields["revenue_trend"] (read by discovery/ranking.py and the results table)
CompanyCard.revenue_trend() -> financials.trend.RevenueTrend; CompanyCard.latest_employees() -> (employees, year, source) | None; CompanyCard.financial_series()
get(store, card_id) / get_by_key(store, key) / find(store, company) / is_favourite(store, company) / saved_keys(store) / cards_to_enrich(store)
def delete_contact(store, card_id, contact_id) -> CompanyCard   # a deleted contact is not added again
def workplace(card=None, *, company=None, assessment=None) -> str      # location of the best posting, else the town (and province); "" when unknown
def linkedin_search_url(name) -> str; strip_legal_form(name) -> str    # the app only OPENS this URL in the browser
def refresh_card(store, card_id, fetcher, settings, section, *, ...) -> CompanyCard
def apply_register_data(store, card_id, raw: dict, *, url="", when=None) -> tuple[CompanyCard, dict[str, str]]   # typed by the user; invalid identifiers come back as errors
def import_register_csv(store, card_id, text, *, filename="") -> ...
def card_text(card, tr=None, *, markdown=True) -> str        # "Copy as text" and "Export card (Markdown)"
def card_to_dict(card, *, personal=False) -> dict            # for the MCP server: company data only, never notes, contacts, tags or status
```
ATECO and REA come only from a register source (an imported list, register data entered by the user, the Registro Imprese form, the paid register) or, for the REA, from the company's own site. Fields that were not found stay blank (`missing_paid` lists them, each with a hint on where to find the value for free by hand); the only paid query is the confirmed `fetch_paid`. Identity status, evidence and rules: `registry/confirm.py`. Site analysis for the card: `web/identity.py` (legal identifiers and pages), `web/profile.py` (presentation and history), `web/people.py` (contacts), `web/screening.py` (selection systems and statements about automated processing) and `web/products.py` (products and services).

The cards live in the `companies` table of the `Store` (`card_insert`, `card_update`, `card_read`, `card_by_key`, `card_rows`, `card_delete`, `favourite_card_keys`). Every function takes the store first and is thread-safe.

## mail (application e-mail as a draft; nothing is ever sent)
```python
# mail/__init__.py
MailSettings(subject_template, body_template, attachment_path, my_name, my_email, my_phone, method)   # to_dict() / from_dict(); stored in Settings.mail
build_draft(settings, *, company, role, contact_name, to, extra=None) -> Draft      # {company} {role} {contact_name} {my_name} {my_email} {my_phone}; {x|fallback}; [[optional part]]; {{ }} literal
pick_recipient(contacts, company_email="", typed="") -> Recipient | None            # typed, else the first HR contact, else another contact, else the company e-mail; privacy mailboxes never
detect_environment() -> MailEnvironment        # read-only (registry on Windows, xdg-settings/which on Linux, application folders on macOS); starts nothing
recommended_methods(environment) / describe_methods / method_by_id / open_with_fallback(draft, methods, first="") -> list[Outcome]
    # METHOD_IDS = outlook_classic (COM through pywin32), thunderbird, apple_mail, xdg_email, outlook_new (an .eml draft opened with new Outlook itself), mailto, gmail_web,
    # eml_draft, assisted; "automatic" = the first recommended one (by the default mail program); the assisted way is always last and always works
Outcome(method, status, message, steps, client, attached, body_complete, clipboard_used, folder_revealed, error)
System(spawn, open_target, open_browser, reveal, clipboard, platform)   # every side effect is a replaceable callable; REVERSEHIRE_MAIL_DRY_RUN=1 makes the real ones refuse to launch anything
```
New Outlook needs a text/plain-only .eml (with an HTML alternative the attachment is dropped) opened with `olk.exe`; the draft is also kept in the account's Drafts folder. A mailto or Gmail link cannot carry an attachment, so those ways open the folder with the PDF. The log gets the method, the recipient's domain and the attachment's file name, never the text.

## chat (the assistant strip: the user's own Claude Code or Codex, restricted to ReverseHire's MCP tools; no GUI imports)
```python
# chat/__init__.py
detect_all(overrides) / check_cli(kind, override) -> CliInfo(kind, status, path, version, hint_key, hint_params, launcher)   # runs `--version` and the sign-in status, never a model; status NOT_INSTALLED | BROKEN | NOT_LOGGED_IN | FOUND | READY
check_mcp(enabled, python=None, *, probe=False) -> McpStatus     # can the reversehire server start with that Python (module lookup, or an import in a child process)
ChatSession(cli, *, data_dir, callbacks=ChatCallbacks(on_text, on_tool, on_notice, on_done, on_error), language="en", timeout_s=900, python=None, mcp_env=None, model=None, max_turns=12)
    # send(text) -> bool never blocks and never raises (every problem becomes on_error); cancel(); reset(); one CLI process per question, resumed by its own session id;
    # callbacks run on a worker thread; exactly one of on_done / on_error comes last; timeout 900 s; the same class (SuggestSession in suggest/runner.py) serves the company suggestions with its own, tool-less command
build_claude_command(...) / build_codex_command(...)   # locked down: Claude `--restricted`, `--tools ""`, `--strict-mcp-config`, `--allowedTools mcp__reversehire`, `--permission-mode dontAsk`;
    # Codex `--ignore-user-config`, `--ignore-rules`, read-only sandbox, approval never, shell/web/browser/plugin/hook features off; question on stdin; empty working folder <data>/assistant/workspace
```
The tools the assistant may use are the nine of `mcp_server.TOOLS` (a test keeps `REVERSEHIRE_TOOLS` equal). `Settings.assistant` is `"claude"` or `"codex"`; `claude_path` / `codex_path` override the detection.

## export (company card and application as documents)
```python
# export/__init__.py   (the dialog and PySide6 are imported lazily)
build_company_card_document(card, *, tr=None, today=None, images=None, chart=None) -> ExportDocument    # the nine card sections; chart = (PNG bytes, caption)
build_application_document(card, letter_subject, letter_body, cv_pdf_path=None, *, signature_name="", contact=None, sender_lines=(), date_text=None, tr=None, today=None, chart=None) -> ExportDocument
    # letter, then the CV (PDF pages as they are), then the card as a private appendix ("PRIVATE REMINDER: NOT TO BE SENT")
render_html(doc) / render_pdf(doc, path) / render_docx(doc, path) / render_xlsx(doc, path)   # xlsx written with the standard library only; a value starting with "=" is text; python-docx for Word
ExportPreviewDialog(doc, parent, tr=...)    # preview first, then Print… / Save PDF / Save Excel / Save Word; rendering on a worker thread; file dialog and printer are injectable
```
Unknown fields stay blank, every value with a known source carries a numbered footnote (name and link).

## cv_library and display_text
```python
# cv_library.py  ("My CV" tab: one CV in several language versions; no Qt, thread-safe)
class CVLibrary:   # copies of the files under <data folder>/cv/<language>/, index in cv/library.json (written atomically)
    def versions() -> list[CVVersion]; def primary() -> CVVersion | None     # English is the preferred primary
    def add(path, language, label=None) -> CVVersion; def replace(version_id, path); def remove(version_id)
    def set_primary(version_id); def update(version_id, *, language=None, label=None); def save_photo(version_id, png)
def compare_profiles(a, b, label_a, label_b) -> list[Difference]   # jobs, date ranges, skills, languages, degrees; empty = the versions agree

# display_text.py  (display layer: with the English interface no Italian or Ukrainian text is shown)
def show(text, *, english=None) -> Shown                  # reads the caches only, never runs the model (safe on the GUI thread)
def display(text) -> str; def tooltip(text) -> str
def prewarm(texts, store=None, *, ui_language=None) -> int   # translates in worker threads (CV analysis, assessment, opening a search)
def missing_languages() -> set[str]                       # languages whose local translation model is not installed
```
Order of the translation sources: the built-in tables of `cv/english.py`, then the local model of `cv/translate.py`. The Italian and Ukrainian interfaces show the texts unchanged (there are no en-it or en-uk models). A text that is not translated yet keeps its original wording with an "(original language)" marker.

## config
```python
# config.py
# Data folder, see resolve_data_location (first match wins):
#   1. REVERSEHIRE_HOME; 2. "<program folder>/userdata" when the program folder is writable (portable, the normal case);
#   3. the per-user folder: %LOCALAPPDATA%\ReverseHire (Windows), $XDG_DATA_HOME/reversehire or ~/.local/share/reversehire
#      (Linux), ~/Library/Application Support/ReverseHire (macOS); 4. a folder in the system temporary folder (logged as a warning)
@dataclass(frozen=True)
class DataLocation: folder: Path; mode: str; program_folder: Path | None; migrated_from: Path | None   # mode: env | portable | user | temp; .reason
def resolve_data_location(*, env=None, platform=None, program=True, home=None, writable=..., temp=None, migrate=True) -> DataLocation
def data_location() -> DataLocation     # computed once per process
def data_folder() -> Path               # data_location().folder, created if missing
def migrate_legacy_data(target, legacy) -> Path | None   # COPIES config, store and log of an older version (%APPDATA%\ReverseHire, ~/.reversehire); never moves; writes migrated_from.txt
def cloud_sync_provider(path) -> str | None              # OneDrive, Dropbox... (Diagnostics warns)
@dataclass
class Settings:   # fields added in recent rounds, with their defaults
    paid_registry_provider: str = ""    # "openapi" / "openapi.it" / "openapi.com" (the only implemented provider); with paid_registry_key it enables the confirmed paid lookup
    paid_registry_key: str = ""         # a secret: registered with applog.register_secret, never logged
    osm_region: str = "emilia-romagna"  # region of the local map data
    discovery_sources: str = "osm_extract,overpass,wikidata"   # comma separated, in order
    request_pause_s: float = 2.0        # minimum pause between two requests to the same host (discovery, registers)
    mcp_enabled: bool = False
    prefer_permanent: bool = True
    prefer_remote: bool = True
    min_salary_year: int = 0            # 0 = not set
    max_travel_days_year: int = -1      # -1 = use the CV value (config.json only, not in the settings window)
    job_boards_enabled: bool = False
    mail: dict = {}                     # mail.MailSettings.to_dict(): templates, reference PDF, the user's own details, preferred method
    assistant: str = "claude"           # "claude" | "codex" (the assistant strip)
    claude_path: str = ""; codex_path: str = ""   # "" = find the program automatically
    chat_notice_shown: bool = False     # "counts against your own plan" shown once
    chat_expanded: bool = False         # the conversation of the strip is open
    startup_language: str = "en"        # the language every start begins in: "en" | "it" | "uk" | "last" (the language of the previous session);
                                        # app.start_language applies it (config.apply_startup_language); `language` is the SESSION language
    ocr_advanced: str = "ask"           # advanced OCR reader: "ask" (offer the download once) | "always" (use it, download it when needed) | "never"
    jobsearch_sources: dict = {}        # source id -> bool; only for the sources that may be enabled
    jobsearch_max_pages: int = 20       # posting pages read per agency and search (JOBSEARCH_PAGES_RANGE = 1..100)
    jobsearch_timeout_s: float = 120.0  # a slow source is stopped after this (JOBSEARCH_TIMEOUT_RANGE_S = 10..900)
    adzuna_app_id: str = ""; adzuna_app_key: str = ""; jooble_key: str = ""; careerjet_key: str = ""   # JOBSEARCH_KEY_FIELDS: secrets, plain text in config.json, never logged
    knockout_answers: dict = {}         # the user's truthful screening answers (clean_knockout_answers); used only by the simulators
    map_tile_url: str = ""              # "" = OpenStreetMap tiles; else a template with {z} {x} {y} (https, loopback http or file://)
    map_offline: bool = False           # no tiles at all: markers on a plain background. In config.json it is stored POSITIVELY as "map_background" (default true; Settings > Appearance "Show the map background"); an old "map_offline" key in a file is not carried over
    affinity_preferences: dict = {}     # affinity.AffinityPreferences.to_dict(): directions, company-type weights, distance limit ({} = defaults)
    affinity_weights: dict = {}         # affinity.Weights.to_dict(): the personal weights from "Calibrate from my judgements" ({} = defaults)
    suggest_allow_web_search: bool = False   # opt-in: the assistant may use its web search when it suggests companies (default: no tool at all)
    suggest_notice_shown: bool = False       # the brief preview / cost notice was accepted once
    idle_enabled: bool = True           # idle analysis (background package) on/off
    idle_minutes: int = 5               # minutes without user input before it starts
    idle_daily_budget: int = 600        # requests a day
    idle_only_on_mains: bool = True     # only on AC power
    idle_paused: bool = False           # the user's pause switch
    idle_queue_agreed: bool = False     # the user agreed once to queue companies for the idle analysis (asked the first time "Analyse the best now" would queue the rest, at most 200)
    register_regions: list = ["emilia-romagna"]   # regions downloaded into the regional register
    register_rna_months: int = 24       # months of RNA state-aid files read (0 = the whole archive, at most 120)
    register_auto_check: bool = True    # check the official sources for updates at start-up
    register_sources: list = ["rna", "gleif", "opencoesione", "agcm"]   # official sources switched on
def mcp_enabled() -> bool               # re-reads config.json (the MCP server calls it before every tool)
def read_mcp_usage(path=None) -> dict   # last call time and counter, mcp_usage.json
def record_mcp_use(path=None, now=None) -> dict
```

## store and applog (robustness)
```python
# store.py
Store(path)    # one sqlite3 connection PER THREAD (created on first use, closed with the thread or the store); no process-wide lock around reads
mark_gui_thread()          # the window declares its thread: there the busy timeout is GUI_BUSY_TIMEOUT_MS (2 s) and locked() waits GUI_LOCK_TIMEOUT_S (2 s); then StoreBusyError (logged, shown as a notice), never a hang
# SCHEMA_VERSION = 5 (meta "schema_version"; tables are CREATE TABLE IF NOT EXISTS, columns added by version in _MIGRATIONS): 2 = company card columns,
#   3 = presentation / people / financials of the card, 4 = the job search: tables job_searches, job_postings, job_search_postings,
#   5 = the affinity: tables affinity_results (PK company_key + cv_fp + settings_fp; data_fp, verdict, affinity, result JSON) and
#   affinity_feedback (company_key, agrees, user_verdict, verdict, cv_fp, result JSON, created) with an index on company_key.
#   Not versioned (created on every open): search_notes (kind "companies" | "jobs", search_id, title, note) = the user's title and note of a previous search
# search notes: SEARCH_NOTE_KINDS = ("companies", "jobs"); search_note_set(kind, search_id, title="", note="") ("" and "" deletes; title <= 200, note <= 4000 chars),
#   search_note_get(kind, search_id) -> {"title", "note"}, search_notes(kind) -> {search_id: {"title", "note"}}
# affinity (schema 5): affinity_save(company_key, cv_fp, settings_fp, data_fp, result_json, *, verdict="", affinity=None); affinity_read(company_key, cv_fp, settings_fp)
#   -> {"data_fp", "result" (JSON text), "verdict", "affinity", "created", ...} | None; affinity_read_many(company_keys, cv_fp, settings_fp) -> {key: row};
#   affinity_prune(keep_cv_fp, keep_settings_fp) -> int (deletes the results of other CVs or settings); affinity_count()
#   feedback_add(company_key, agrees, *, verdict="", user_verdict=None, cv_fp="", result_json="{}", when=None) -> id; feedback_rows() (oldest first);
#   feedback_latest(company_key); feedback_count() (companies judged: the latest judgement per company counts once)
#   (company_key is "card:<card key>", "result:<card key>" or "company:<card key>", see gui/affinity_service.key_of)
# cards: card_by_identifier(column, value) also exists next to card_insert / card_update / card_read / card_by_key / card_rows / card_delete / favourite_card_keys
# job search: create_job_search(query, understood=None, *, place="", lat=None, lon=None, radius_km=None) -> id; finish_job_search(id, status, postings, *, reports=None, stats=None)
#   -> posting ids in rank order; save_posting(posting, *, origin="pasted") -> id; list_postings(*, origin=None, favourite=None); read_posting(id);
#   set_posting_favourite(id, on); delete_posting(id); list_job_searches(); read_job_search(id); postings_of_job_search(id); delete_job_search(id)
#   (a posting is stored once, by its URL else its de-duplication key; origin "search" | "pasted"; names of people are never stored)
# WAL journal on a local folder (not cloud-synced, not a packaged-app redirection, not a network drive), else the classic DELETE journal; writes use BEGIN IMMEDIATE; ":memory:" keeps one shared connection behind a lock
# applog.py
configure(folder=None, level=INFO, *, computer=None, max_bytes=None, backups=None) -> Path     # once per process; never raises
# line format: "[YYYY-MM-DD][HH:MM:SS][COMPUTER-NAME]: LEVEL module [thread]: message" on every physical line (continuation lines of a traceback included)
# rolling files: reversehire.log and .1 ... .9, 10 MB each, MAX_TOTAL_BYTES = 100 MB; when the file cannot be renamed (open elsewhere on Windows) the oldest half is cut in place
# every error of the handler is swallowed and counted (dropped()); register_secret(value) / mask(value): keys become "***"; the CV text and e-mail bodies are never passed to the log
```

## GUI helpers that hold rules
- `gui/safety.py`: `install()` sets `sys.excepthook`, `threading.excepthook` and a Qt message handler: an exception is logged with its traceback and shown as a status-bar notice (`error.unexpected`), never a crash and never a modal box from a worker; `safe_slot(function)` wraps callbacks wired by hand; `Watchdog`: a QTimer beats on the GUI thread every 500 ms and a daemon thread logs the GUI thread's stack (and the other threads' current lines) once per stall when the last beat is older than 5 s, then how long it lasted.
- `gui/activity.py`: an application-wide event filter that logs every button, menu item, combo-box choice, tab change and dialog opened or closed, by its label only (never what was typed). `gui/table_sort.py` logs a click on a table header, also in panels that sort their own rows; the choice of a CV file logs only its extension and size, never its name.
- `gui/menus.py`: `MenuMirrors(parent).mirror(button, text=None)` gives a button one `QAction` for the menu bar (File, CV, Search, Results, Job search, Companies, Tools, ?); triggering it shows the button's tab and clicks the button, so both run the same handler; the action follows the button's enabled state and its text. `bind_action(button, action)` records a button that already runs a menu action.
- `gui/hover.py`: hover and keyboard-focus highlight (stylesheet) and a short eased growth of buttons (5 %, 120 ms) that does not change the layout; off when the system disables animations (Windows) or with `REVERSEHIRE_REDUCED_MOTION=1`.
- `gui/progress_bar.py` (`BarState`, `StatusProgress`: the one status-bar progress bar shared by the CV analysis, the search, the recruiter analysis, the job search and the advanced-reader download; see "progress"); `gui/ocr_reader.py` (`AdvancedReader`: the one-time offer, the download in a worker, removal, "Read again with the advanced reader", the Tools menu and the state line of Settings).
- `gui/recruiter_analysis.py` (the Recruiter analysis tab: target = role | posting | company, one system or all, pipeline cards, record preview, simulated score, top fixes; the analysis runs on a worker with progress and cancel), `gui/jobsearch_panel.py` (the postings saved from "Paste posting / company page…", `store.list_postings(origin="pasted")`, are one more local source, `PASTED_SOURCE` = "pasted" shown as "Pasted by you"; the buttons "Pasted postings (N)", "Edit pasted posting…" and "Delete pasted posting" list, re-open in the paste window and delete them; the "Understood as" role chip shows the corrected words, `corrected_words`) and `gui/jobsearch_dialogs.py` (the Job search tab, the "Who might be behind this ad?" and paste/review windows), `gui/cv_match.py` ("Match with my CV": `recruiter_sim.match_posting` computed in a worker, cached per posting), `gui/settings_extra.py` (the Job search and Screening answers settings pages and the map fields' validation), `gui/window_features.py` (the new views' wiring in the main window: simulate a posting, create a card from a posting, hypotheses, maps) and `gui/lazy_map.py`.
- `gui/main_window.py`: there is NO confirmation step for the profile. The profile is valid as soon as a CV has been analysed; every edit in `gui/profile_panel.py` is applied at once, saved after `AUTOSAVE_MS` = 500 ms (the window persists it on a worker in the Store cache, namespace `profile_current`, key `current`), can be undone (`UNDO_LIMIT` = 30 states; Ctrl+Z, the undo button, the CV menu) and is restored at the next start-up (`restore_profile`). "Start search" (`_profile_ready`) needs an analysed CV with at least one role; unsaved edits are saved first; otherwise the missing field is named next to a button that jumps to it in tab 1 (nothing is invented). In province and region mode the address is optional (the search starts from the chief town); every refusal is shown next to the button and logged.
- `gui/table_sort.py` (a click on a header is always logged, also when the panel sorts its own rows): `enable(view, *, delegate=False, kinds=None, key=None, on_sorted=None) -> TableSorter` gives a table header-click sorting: first click ascending, second descending, the arrow in the header; numbers by value ("9 %" < "10 %", a range by its lower bound), dates chronologically, text alphabetically without case or accents, blank cells always last in both directions; the order survives refills and is remembered for the session per table (`table_key`). A cell may carry its exact key in `SORT_ROLE`. Physical mode reorders the items (every row keeps its original index in `ROW_ID_ROLE`: `source_row(view, row)` / `view_row(view, index)` translate); `delegate=True` is for panels that sort their own rows with `value_key` / `sorted_by` and report with `TableSorter.record(column, ascending)`. Pure helpers: `parse_number`, `parse_date`, `text_key`, `value_key(value, kind=None)`, `fold`.
- `gui/table_filter.py`: `attach(view, *, kinds=None, key=None, sorting="physical") -> TableFilter` adds the sorting above and a controller that HIDES (never removes) the rows that do not pass the tests other features plug in with `set_predicate(name, fn(row_index) -> bool)`: the shared search box (`"search"`) and the place filters (`"geo"`); all tests combine with AND; a test receives the row's index in the panel's own list. `install(app)` attaches every `QTableView` automatically (a table with the property `NO_FILTER_PROPERTY` is skipped).
- `gui/search_box.py`: `SearchBox(documents, *, runner=None, embedder_provider=None, store=None, name=...)` is the one "search by meaning" field of every list (My companies, Favourites, Results, Job search results, Previous searches) over `scoring/semantic_index.py`: typing runs at once an exact-word pass on the GUI thread (no model), `DEBOUNCE_MS` = 400 ms later the meaning pass runs on the window's worker, building the index incrementally (vectors cached in the Store; progress in the status line); `hit(key)` gives the `Hit` (relevance 0..100 %, the matching sentence and its source), `bind_table(view, key_of_row)` hides the rows that do not match (predicate `"search"`), `hit_text(hit)` / `hit_html(hit)` format the matching sentence. Without the model the status line says the search runs in keyword mode.
- `gui/geo_filter.py`: `GeoFilter(view, location_of_row, parent=None, *, name="")` = three linked multi-select filters Country -> Region -> Province above a list; each level is one `QAction` with a menu of checkable values with counts ("Emilia-Romagna (12)"), "All" by default; a narrower choice drops the regions / provinces that no longer belong; predicate `"geo"` of the table filter; places come from `geo/places.py` (counted only when a menu opens).
- `gui/context_menu.py`: `install(table, entries, *, delete_action=None, menu_runner=None)` shows a right-click menu built from the QActions the buttons and menus already use (`entries()` is called each time; `None` = separator; disabled or hidden actions are left out); a right-click on an unselected row selects it first; the Delete key triggers `delete_action`; `install_buttons(table, groups, *, delete_button=None)` does the same from buttons.
- `gui/social_links.py`: `SocialLinksBar` = a row of small link buttons beside the company name (card header, Results detail panel): website, social networks, company pages on job sites, Wikipedia / Wikidata and a map search, from `web/social.py` (`classify(url)`, `links_for_card(card)`, `links_for_company(company)` -> `SocialLink(network, url, kind, source, source_url, where)`; no network, no Qt). A profile URL is never guessed: it is listed as a page only when it was found (the company's site, Wikidata, OpenStreetMap, the card); otherwise a LinkedIn company SEARCH is offered, and the map is always a search by name and town; both are `kind="search"` (dashed in the interface; the app only opens them in the browser). Buttons and the "Links of this company" menu share their `QAction`s.
- `gui/affinity_service.py` (`window.affinity_service`, built by `gui/window_affinity.py`): the public API for every feature that shows the affinity (all calls on the GUI thread; the work runs on workers in batches of 20, deadline 180 s): `result_for(item)` / `result_for_card(card)` (the cached result, never computed here; None while missing or stale), `request(items, on_done=None)` / `request_cards(...)`, `results_for(items, request=True)`, `compute_for_candidates(items, on_done, *, direction=None)` (things that are not saved cards), `sort_key(result)` (None sorts last), `fingerprint()`, `record_feedback(card_or_key, result, agree, user_verdict=None)`, `feedback_for(...)`, `feedback_count()`, `listeners` (redraw hooks), `key_of(item)` ("card:", "result:", "company:" or "demand:" + key). Results are cached in memory (6000) and in the Store (schema 5) and recomputed when the CV, the company data or the affinity settings change; without a CV nothing is computed.
- `gui/affinity_box.py` (the four list columns `AFFINITY_COLUMNS = ("affinity", "verdict", "fits_as", "company_type")` with numeric sort keys, and `AffinityBox`, the box at the top of the company card with the verdict, details, Agree / Disagree) and `gui/affinity_settings.py` (the **Affinity** settings page: directions, company-type weights, distance limit, "only companies within my directions", "Calibrate from my judgements" with the weights before and after).
- `gui/interest_flow.py` (`InterestFlowWindow`, one non-modal window "Who could be interested in my profile") and `gui/window_interest.py` (`InterestMixin`: `window.interest_action`, `open_interest_flow()`, `interest_enqueue(rows)`, `idle_scheduler`, `idle_status_text()`, `idle_pause_action`, `set_idle_paused(on)`). Four steps: AREA (country / region / province, or a radius around a town; directions and company types prefilled from the affinity settings), FAST (offline: the saved companies, the regional register and the local map index when it was already built, never downloaded here, ordered by `background.screen_companies`), ASSISTANT (the reusable `SuggestionsView`), DEEP (the best N rows, default 10, are analysed now: card, the card pipeline `companies.build_cards`, then the affinity; the others, at most `IDLE_QUEUE_MAX` = 200, are queued for the idle analysis, the first time only after the user agreed: `Settings.idle_queue_agreed`, `interest.idle.ask_queue`). The idle job builds one card with the card pipeline, counts its requests against the daily budget and asks the affinity service for its result; the scheduler state is kept in `<data folder>/idle_analysis.json`; any user input, and any foreground work (a search, a job search, a card build, a suggestion request, the flow's own analysis), pauses a running job at once. Everything slow runs on the window's workers (`_in_background`: deadline, cancel).
- `gui/suggestions_view.py`: `SuggestionsView(window, brief_provider=..., on_added=...)` (`start()`, `expand()`, `cancel()`, `running`, `actions()`): before the FIRST use (`settings.suggest_notice_shown` False) a non-modal window shows exactly what will be sent (`brief.preview_text()`), the exclusion list (its count, `interest.suggest.exclude_note`, and the names on "Show the names"; `EXCLUDE_MAX` = 60 names: the companies already suggested, then the saved companies of the area; `suggest.engine.MAX_EXCLUDED_SENT` = 300 is the ceiling over a conversation) and the cost notice; a usage-limit failure of the assistant is reported as such (`chat.error.usage_limit`, with the reset time when the CLI gave one); the list shows a status badge per row (VERIFIED / PARTIAL / UNVERIFIED / REJECTED), the verification facts with their sources and the assistant's sentence in a column labelled as its opinion; UNVERIFIED rows are greyed and cannot be added, REJECTED rows are hidden until "Show rejected"; "Add to my companies" (VERIFIED / PARTIAL only) calls `companies.add_manual` with `VerifiedSuggestion.to_company_seed()`. It is also embedded under the companies list of the Job search tab (`gui/jobsearch_companies.py`, `SpontaneousCompaniesView`: the "Companies for a spontaneous application" list from `jobsearch/spontaneous.py`, with the affinity for the direction of the search and the state of each company's data: "Card built", "Not analysed yet", "Not saved").
- `gui/register_manager.py` (`RegisterManagerWindow`, the ONE window of the register: Companies, Official sources, Lists; under the Companies table the coverage note of `db.coverage_note(province, "")`, also shown in tab 2 by `SearchPanel.refresh_coverage`; after a download `registers.manager.periods_note` adds "Months loaded: 12/2024–11/2026 (24)." and, for months that failed, "n month(s) not available yet and skipped: …" to the result of a monthly source such as RNA), `gui/window_registers.py` (`RegistersMixin`: `window.register_manager` (lazy `registers.shared_manager`), `open_register_manager()`, `register_candidates(area=None, limit=200)` (a database read: call it on a worker), `register_signals_for(company_or_card)` -> dated facts "hiring" (hired with an incentive, RNA), "aid" (latest public aid, RNA) and "legality" (AGCM) found by tax code or VAT number), `gui/register_settings.py` and `gui/background_settings.py` (the register settings and the **Idle analysis** page with the suggestion settings) and `gui/settings_registry.py` (extension points: a module listed in `SECTION_MODULES` defines `SECTIONS`; `NEW_PAGES` inserts the pages `affinity` and `background`; `validate(base, values) -> (changes, errors)`: errors are shown and nothing is saved).
- `gui/financials_paste.py` (`PasteFiguresDialog`: the pasted text is read by `financials/paste.py` into an editable grid, one row per year, with the source the user states; nothing is saved until the user presses Save AND confirms; the dialog does not block the window) and the company card's "Open company-information sites" button (`financials/info_sites.py`).
- `gui/window_applications.py` (`ApplicationsMixin`: the tab opens `applications.ApplicationService` on a worker; "Write e-mail" (`MailDialog.opened_listeners`; the dialog's editable "Role:" field starts with the posting's title when the e-mail answers a posting, else the profile's first role, `MailDialog.role`) and File > Export > application (`ExportPreviewDialog.exported_listeners`) record a DRAFT application on a worker; the card shows the company's applications; at start-up a notice tells how many follow-ups are due and how many appointments are today or tomorrow).
- `gui/workers.py` (`QtRunner(parent).run(work, on_success, on_error, name=..., timeout=...)`: the same shape as `MainWindow._in_background`, for dialogs used without the main window), `gui/coordinates.py` (no Qt: `parse_coordinates(text)` accepts "44.4937, 11.3430", comma decimals, degrees-minutes-seconds with hemisphere and a Google Maps URL with "@lat,lon"; `compute_start_point(lat, lon, azimuth="", distance="")` moves a point by an azimuth and a distance, at most `DISTANCE_MAX_KM` = 1000) and `gui/start_point_dialog.py`.

## sysinfo (About window)
```python
# sysinfo.py
def collect(*, screen=None, qt=None, mcp=None, labels=None) -> dict[str, str]
    # KEYS: user, computer, os, uptime, processor, cores, memory, gpu, screen, disks, network, python, qt, app_version, ocr, mcp.
    # Shown on screen only; never saved, logged or sent. user = session_user() (GetUserNameW / the password database; not an
    # environment variable); os = edition, feature update and build on Windows; disks = fixed_disks() with the program and data
    # disks marked (blocking: call on a worker); app_version = "0.1.3 (build 3, built on 2026-11-18)" or "... source copy, not built"
def as_text(values: dict[str, str], names: dict[str, str]) -> str   # for the Copy button
```
`sysinfo.data_folder()` and `sysinfo.embedding_model()` still exist but are no longer rows of the About window: the data folder (and the rule that chose it) and the embedding model are reported by Diagnostics (`check_data_folder`, `check_embedding_model`), which also reports the version and build in its first line.

## diagnostics ("? > Diagnostics…" and the installer's self-test)
```python
# diagnostics.py
@dataclass
class Check: id: str; status: str; detail: str     # status PASS | WARN | FAIL | INFO | SKIP
def run_checks(settings=None, *, online=True, ids=None, http_get=None, load_model=None,
               sac_reader=None, progress=None) -> list[Check]   # never raises
def as_text(checks, titles=None) -> str; def counts(checks) -> tuple[int, int]
def selftest_exit_code(checks) -> int            # 0 ok, 2 only the model failed, 1 other failures
def main(argv=None) -> int                       # python -m reversehire.diagnostics [--selftest]
```

## Data model (models.py): fields added in this round
All new fields have defaults, so profiles and assessments stored by older versions still load.

- `CVProfile`: `experiences: list[WorkExperience]` (most recent first), `gaps: list[EmploymentGap]` (most recent first), `mobility: Mobility`, `home: str | None`, `seniority: str | None` ("junior" | "mid" | "senior"), `seniority_evidence: list[Evidence]`. Also `translated: bool`, `translation: str` ("" | "tables" | "local"), `untranslated: list[str]`.
- `ProfileItem.original: str | None` and `WorkExperience.original_title: str | None`: CV wording of a translated value.
- `WorkExperience(title, employer="", start="", end="", months=None, evidence=[])`: dates as "YYYY-MM" or "YYYY"; `end` may be "present".
- `EmploymentGap(start, end, months)`.
- `Mobility(travel="unknown", travel_days_per_year=None, travel_evidence=[], relocation="unknown", relocation_evidence=[], driving_licence=[], own_car=None, licence_evidence=[])`: `travel` is "declared" | "likely" | "excluded" | "unknown"; `relocation` is "declared" | "excluded" | "unknown"; a percentage in the CV is converted on 220 working days.
- `Posting`: `contract`, `remote`, `salary`, `salary_min`, `salary_max`, `salary_period`, `salary_currency`, `travel`, `travel_note` (see the web section).
- `RequirementCheck(text, priority="context", kind="skill", level=None, status="uncertain", evidence=None, blocking=False)`: `priority` must | nice | context; `kind` years | language | degree | licence | car | travel | skill | duty; `level` 0..4 or None; `status` met | partial | uncertain | not_met | contradicted.
- `Assessment`: `desirability`, `spontaneous`, `match`, `blocked`, `blocked_reason`, `requirements`.
- `Company.origin`: "osm", "manual", "jobboard", "import" or the name of a discovery source ("regional_register", "assistant" for a company the user added from a verified suggestion, ...). `Company.ids` holds legal identifiers when known (`vat`, `tax_code`, `rea`, `lei`, `wikidata`, `pec`, `ateco`), `Company.sources` their per-field provenance (field, value, source, URL, date) and `Company.fields` extra discovery data (town, province, phone, e-mail, industry, employees, `why_selected`), never used as scoring text.

