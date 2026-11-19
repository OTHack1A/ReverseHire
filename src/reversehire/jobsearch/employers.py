"""From a posting to a company card, and the possible employers behind an agency ad.

`company_seed_from_posting(posting)` -> a dict whose top-level keys are the keyword
arguments of `companies.add_manual` (name, site, address, lat, lon, sectors, description,
notes, tags) plus "extra" (provenance, agency, hidden employer flag): the integrator calls
`companies.add_manual(store, **{k: v for k, v in seed.items() if k != "extra"})`.

`possible_employers(posting, company_index, cards, agency_clients)` implements the additive
score of RECRUITER_STUDY §2.5 on the local data only (OSM index companies, company cards,
technologies seen on the companies' own sites and ads, the agency's public client list):

    hard filters: within the radius of the ad (25 km; 40 km when only the province is
                  known), not a staffing agency (ATECO 78 or agency name), not the publisher
    S(c) = 2.0·geo      geo = exp(-d / 12 km)
         + 1.5·sector   1 same ATECO division as the ad's sector words, 0.5 same section,
                        0.2 a compatible industrial site without code, 0
         + 3.0·tech     Σ idf(t) over technologies shared with the ad / Σ idf(t) of the ad
                        technologies (idf over the companies considered: rare ones weigh more)
         + 2.5·role     1 if the company's own ads show a similar title in the last 90 days,
                        0.5 if older or undated
         + 1.5·agency   1 if a named client of the publishing agency
         + 1.0·ccnl     1 consistent with the company's ATECO, -1 contradictory, 0 unknown
         + 0.8·size     1 agreeing size, 0 disagreeing, 0.5 company size unknown (only when
                        the ad gives a size hint)
         + 0.8·wording  share of distinctive 4-word phrases shared with the company's own ads
    P(c) = exp(S(c)) / (exp(S0) + Σ exp(S(j))),  S0 = 3.0 + ln(max(1, N))
           (N = companies considered: the "company not in our list" share keeps most of the
           mass when only distance speaks, whatever the size of the area)
    shown: top 5 with P >= 10 %, each with short reason chips and the fixed label
    "unverified hypothesis"; labels: P >= 0.5 and margin >= 0.2 "strong hint",
    0.2..0.5 "plausible", below "possible".
Deterministic (ties broken by name). Never presents a guess as fact.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from urllib.parse import urlsplit

from ..geo.distance import haversine_km
from . import lexicon
from ._util import fold, log, parse_date, words
from .models import Posting, title_key
from .texts import text

W_GEO, W_SECTOR, W_TECH, W_ROLE, W_AGENCY, W_CCNL, W_SIZE, W_WORDING = 2.0, 1.5, 3.0, 2.5, 1.5, 1.0, 0.8, 0.8
GEO_SCALE_KM = 12.0
RADIUS_KM = 25.0
PROVINCE_RADIUS_KM = 40.0
S0_BASE = 3.0
TOP = 5
MIN_SHARE = 0.10
ROLE_RECENT_DAYS = 90
ROLE_SIMILARITY = 0.5
WORDING_SHINGLE = 4
WORDING_FULL = 5   # shared distinctive phrases giving the full wording credit
AGENCY_DIVISION = 78

_URL = re.compile(r"https?://[^\s<>\"')]+|www\.[a-z0-9.-]+\.[a-z]{2,}", re.I)
_EMAIL = re.compile(r"[\w.+-]+@([a-z0-9-]+(?:\.[a-z0-9-]+)+)", re.I)
_NOT_EMPLOYER_HOSTS = ("linkedin.", "indeed.", "facebook.", "instagram.", "google.", "youtube.", "twitter.",
                       "x.com", "infojobs.", "monster.", "subito.", "jooble.", "adzuna.", "careerjet.",
                       "europa.eu", "gov.it", "garanteprivacy.", "odm-agenzielavoro.", "randstad.", "adecco.",
                       "manpower.", "synergie", "gigroup.", "umana.", "etjca.", "openjobmetis.", "lavoro.gov")


@dataclass
class Chip:
    key: str
    params: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return text(self.key, **self.params)

    def to_dict(self) -> dict:
        return {"key": self.key, "params": dict(self.params), "text": self.text}


@dataclass
class EmployerHypothesis:
    name: str
    probability: float
    score: float
    label_key: str                       # jobsearch.employers.label.strong|plausible|possible
    reasons: list[Chip] = field(default_factory=list)
    components: dict = field(default_factory=dict)
    company_key: str = ""
    card_id: int | None = None
    site: str | None = None
    lat: float | None = None
    lon: float | None = None
    distance_km: float | None = None
    unverified_key: str = "jobsearch.employers.unverified"

    @property
    def label(self) -> str:
        return text(self.label_key)

    @property
    def unverified(self) -> str:
        return text(self.unverified_key)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["reasons"] = [c.to_dict() for c in self.reasons]
        data["label"] = self.label
        data["unverified"] = self.unverified
        return data


@dataclass
class EmployerEstimate:
    hypotheses: list[EmployerHypothesis] = field(default_factory=list)
    other_share: float = 1.0             # "another company not in the list"
    considered: int = 0
    header_key: str = "jobsearch.employers.header"
    note_key: str | None = None          # "jobsearch.employers.none" | "...no_location" | None

    def to_dict(self) -> dict:
        return {"hypotheses": [h.to_dict() for h in self.hypotheses], "other_share": self.other_share,
                "considered": self.considered, "header": text(self.header_key),
                "other": text("jobsearch.employers.other", percent=f"{100 * self.other_share:.0f}"),
                "note": text(self.note_key) if self.note_key else None}


# --- seed for a company card --------------------------------------------------------------

def _domain(url: str) -> str:
    host = (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def company_seed_from_posting(posting: Posting) -> dict:
    """Card fields from an ad: the employer when named, else the agency (branch)."""
    try:
        sites = []
        for match in _URL.findall(posting.description or ""):
            domain = _domain(match.rstrip(".,;"))
            if domain and not any(bad in domain for bad in _NOT_EMPLOYER_HOSTS) and domain not in sites:
                sites.append(domain)
        for domain in _EMAIL.findall(posting.description or ""):
            domain = domain.lower()
            if not any(bad in domain for bad in _NOT_EMPLOYER_HOSTS) and domain not in sites and \
                    not domain.endswith(("gmail.com", "libero.it", "hotmail.com", "outlook.com", "yahoo.it")):
                sites.append(domain)
        hidden = posting.employer_hidden
        name = posting.company if posting.company else (posting.agency or "")
        if hidden and posting.agency_branch:
            name = f"{posting.agency} - {posting.agency_branch}"
        site = None
        if not hidden and sites:
            site = "https://" + sites[0]
        elif hidden:
            agency_host = next((h for h in (_domain(posting.url),) if h), "")
            site = ("https://" + agency_host) if agency_host else None
        address = posting.location or ", ".join(x for x in (posting.town, posting.province) if x)
        hints = posting.employer_hints or {}
        notes = f"From the job ad: {posting.title} ({posting.url})"
        return {
            "name": name,
            "site": site,
            "address": address,
            "lat": posting.lat,
            "lon": posting.lon,
            "sectors": list(hints.get("sectors") or []) or None,
            "description": "",
            "notes": notes,
            "tags": ["job search"] + (["agency"] if hidden or posting.is_agency else []),
            "extra": {"source": posting.source, "source_url": posting.url, "title": posting.title,
                      "is_agency": posting.is_agency, "agency": posting.agency, "hidden_employer": hidden,
                      "domain": sites[0] if sites and not hidden else None, "town": posting.town,
                      "province": posting.province, "date_posted": posting.date_posted},
        }
    except Exception as error:
        log.warning("Company seed not built: %s", type(error).__name__)
        return {"name": posting.company or posting.agency or "", "site": None, "address": posting.location,
                "lat": posting.lat, "lon": posting.lon, "sectors": None, "description": "", "notes": "",
                "tags": ["job search"], "extra": {"source_url": posting.url}}


# --- candidates ---------------------------------------------------------------------------

@dataclass
class _Candidate:
    key: str
    name: str
    lat: float | None
    lon: float | None
    ateco: str = ""
    tags: dict = field(default_factory=dict)
    employees: int | None = None
    technologies: set = field(default_factory=set)
    ads: list = field(default_factory=list)        # [(title, date | None, text)]
    card_id: int | None = None
    site: str | None = None


def _get(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _name_key(name: str) -> str:
    folded = fold(name)
    folded = re.sub(r"\b(s\.?p\.?a|s\.?r\.?l\.?s?|s\.?n\.?c|s\.?a\.?s|spa|srl|srls|snc|sas|gmbh|ltd|group|"
                    r"gruppo|italia|italy)\b\.?", " ", folded)
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", folded).split())


def _employees(value) -> int | None:
    found = re.search(r"\d[\d.]*", str(value or ""))
    if not found:
        return None
    try:
        return int(found.group(0).replace(".", ""))
    except ValueError:
        return None


def _card_technologies(card) -> tuple[set, list]:
    """Technologies from the card's site data (products, description, postings) and its ads."""
    texts = []
    products = _get(card, "products") or {}
    if isinstance(products, dict):
        texts.append(str(products.get("summary") or ""))
        texts += [str(i.get("name") or "") for i in products.get("items") or [] if isinstance(i, dict)]
    texts.append(str(_get(card, "description") or ""))
    profile = _get(card, "profile") or {}
    if isinstance(profile, dict):
        presentation = profile.get("presentation") or {}
        if isinstance(presentation, dict):
            texts.append(str(presentation.get("text") or ""))
        explicit = profile.get("technologies")
    else:
        explicit = None
    ads = []
    postings = _get(card, "postings")
    if postings is None:
        postings = []
    hiring = _get(card, "hiring") or {}
    when = parse_date(hiring.get("date")) if isinstance(hiring, dict) else None
    for item in postings:
        title = str(_get(item, "title") or "")
        body = str(_get(item, "description") or "")
        posted = parse_date(_get(item, "date_posted")) or when
        if title:
            ads.append((title, posted, body))
            texts.append(title + "\n" + body)
    tech = set(lexicon.extract_skills("\n".join(texts), limit=80))
    if isinstance(explicit, (list, tuple, set)):
        tech.update(str(x) for x in explicit if x)
    return tech, ads


def _candidates(company_index, cards, company_technologies) -> list[_Candidate]:
    result: dict[str, _Candidate] = {}
    for company in company_index or []:
        name = str(_get(company, "name") or "").strip()
        if not name:
            continue
        key = _name_key(name)
        ids = _get(company, "ids") or {}
        extra = _get(company, "fields") or {}
        candidate = result.get(key) or _Candidate(key=key, name=name, lat=_get(company, "lat"),
                                                  lon=_get(company, "lon"))
        candidate.ateco = candidate.ateco or str(ids.get("ateco") or extra.get("ateco") or "")
        candidate.tags = dict(_get(company, "osm_tags") or {}) or candidate.tags
        candidate.employees = candidate.employees or _employees(extra.get("employees"))
        candidate.site = candidate.site or _get(company, "site")
        result[key] = candidate
    for card in cards or []:
        name = str(_get(card, "name") or "").strip()
        if not name:
            continue
        key = _name_key(name)
        candidate = result.get(key) or _Candidate(key=key, name=name, lat=_get(card, "lat"), lon=_get(card, "lon"))
        if candidate.lat is None:
            candidate.lat, candidate.lon = _get(card, "lat"), _get(card, "lon")
        candidate.ateco = str(_get(card, "ateco") or "") or candidate.ateco
        candidate.card_id = _get(card, "id")
        candidate.site = _get(card, "site") or candidate.site
        tech, ads = _card_technologies(card)
        candidate.technologies |= tech
        candidate.ads += ads
        registry = _get(card, "registry") or {}
        if isinstance(registry, dict) and candidate.employees is None:
            employees = registry.get("employees")
            candidate.employees = _employees(employees.get("value") if isinstance(employees, dict) else employees)
        candidate.tags = dict(_get(card, "osm_tags") or {}) or candidate.tags
        result[key] = candidate
    for key, tech in (company_technologies or {}).items():
        candidate = result.get(_name_key(str(key)))
        if candidate is not None:
            candidate.technologies |= {str(t) for t in tech}
    return sorted(result.values(), key=lambda c: c.name.casefold())


def _shingles(text_value: str) -> set[str]:
    tokens = [w for w in words(text_value) if w not in lexicon.GENERIC_WORDS]
    return {" ".join(tokens[i:i + WORDING_SHINGLE]) for i in range(max(0, len(tokens) - WORDING_SHINGLE + 1))}


def _title_similarity(a: str, b: str) -> float:
    wa = {lexicon.group_id(w) for w in title_key(a).split() if w not in lexicon.GENERIC_WORDS}
    wb = {lexicon.group_id(w) for w in title_key(b).split() if w not in lexicon.GENERIC_WORDS}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _industrial(tags: dict) -> bool:
    return bool(tags.get("industrial") or tags.get("man_made") == "works" or tags.get("landuse") == "industrial"
                or tags.get("craft"))


def _is_agency(candidate: _Candidate) -> bool:
    return lexicon.division(candidate.ateco) == AGENCY_DIVISION or lexicon.is_agency_name(candidate.name) or \
        fold(candidate.tags.get("office", "")) in ("employment_agency",)


def estimate_employers(posting: Posting, company_index=(), cards=(), agency_clients=None, *,
                       company_technologies: dict | None = None, ad_location: tuple[float, float] | None = None,
                       today: date | None = None, top: int = TOP, min_share: float = MIN_SHARE,
                       place_lookup=None) -> EmployerEstimate:
    """Full estimate (hypotheses + the "not in our list" share). Never raises."""
    estimate = EmployerEstimate()
    try:
        today = today or date.today()
        lat, lon = ad_location or (posting.lat, posting.lon)
        if lat is None or lon is None:
            from .pipeline import locate

            probe = Posting.from_dict(posting.to_dict())
            locate(probe, place_lookup)
            lat, lon = probe.lat, probe.lon
        if lat is None or lon is None:
            estimate.note_key = "jobsearch.employers.no_location"
            return estimate
        radius = PROVINCE_RADIUS_KM if posting.geo_quality in ("province", "region") or not posting.town else RADIUS_KM
        full_text = posting.title + "\n" + posting.description
        ad_tech = set(posting.skills or lexicon.extract_skills(full_text))
        hints = posting.employer_hints or {}
        ad_divisions = set(hints.get("sector_divisions") or [])
        if not ad_divisions:
            for _, divisions in lexicon.sector_hints(full_text + "\n" + (posting.industry or "")):
                ad_divisions.update(divisions)
        ad_sections = {lexicon.section(d) for d in ad_divisions}
        ccnl = lexicon.ccnl_hint(full_text)
        size_band, _ = lexicon.size_hint(full_text)
        if hints.get("size") and not size_band:
            size_band = hints["size"]
        publisher = _name_key(posting.agency or posting.company or "")
        clients = set()
        for agency, names in (agency_clients or {}).items():
            mine, theirs = fold(posting.agency or ""), fold(agency)
            if mine and theirs and (theirs in mine or mine in theirs):
                clients |= {_name_key(n) for n in names}
        ad_shingles = _shingles(posting.description)

        pool = []
        for candidate in _candidates(company_index, cards, company_technologies):
            if candidate.lat is None or candidate.lon is None:
                continue
            if _is_agency(candidate) or (publisher and candidate.key == publisher):
                continue
            distance = haversine_km(lat, lon, float(candidate.lat), float(candidate.lon))
            if distance > radius:
                continue
            pool.append((candidate, distance))
        estimate.considered = len(pool)
        if not pool:
            estimate.note_key = "jobsearch.employers.none"
            return estimate

        document_frequency: dict[str, int] = {}
        with_tech = 0
        for candidate, _ in pool:
            if candidate.technologies:
                with_tech += 1
            for tech in candidate.technologies:
                document_frequency[tech] = document_frequency.get(tech, 0) + 1

        def idf(tech: str) -> float:
            return math.log((with_tech + 1) / (document_frequency.get(tech, 0) + 1)) + 1.0

        scored = []
        for candidate, distance in pool:
            parts: dict[str, float] = {}
            chips: list[Chip] = []
            parts["geo"] = math.exp(-distance / GEO_SCALE_KM)
            chips.append(Chip("jobsearch.employers.chip.distance", {"km": f"{distance:.0f}"}))
            division = lexicon.division(candidate.ateco)
            sector = 0.0
            if ad_divisions and division is not None:
                if division in ad_divisions:
                    sector = 1.0
                    chips.append(Chip("jobsearch.employers.chip.ateco", {"code": candidate.ateco}))
                elif lexicon.section(division) in ad_sections:
                    sector = 0.5
                    chips.append(Chip("jobsearch.employers.chip.sector_section", {"code": candidate.ateco}))
            elif ad_divisions and division is None and _industrial(candidate.tags) and "C" in ad_sections:
                sector = 0.2
                chips.append(Chip("jobsearch.employers.chip.sector_compatible"))
            parts["sector"] = sector
            shared = sorted(ad_tech & candidate.technologies, key=lambda t: (-idf(t), t))
            total_idf = sum(idf(t) for t in ad_tech)
            parts["tech"] = (sum(idf(t) for t in shared) / total_idf) if total_idf and shared else 0.0
            for tech in shared[:2]:
                chips.append(Chip("jobsearch.employers.chip.tech", {"tech": tech}))
            role = 0.0
            for title, posted, _ in candidate.ads:
                if _title_similarity(posting.title, title) >= ROLE_SIMILARITY:
                    recent = posted is not None and (today - posted).days <= ROLE_RECENT_DAYS
                    value = 1.0 if recent else 0.5
                    if value > role:
                        role = value
                        if recent:
                            chips_role = Chip("jobsearch.employers.chip.role",
                                              {"title": title, "when": posted.strftime("%m/%Y")})
                        else:
                            chips_role = Chip("jobsearch.employers.chip.role_old", {"title": title})
            parts["role"] = role
            if role:
                chips.append(chips_role)
            parts["agency"] = 1.0 if candidate.key in clients else 0.0
            if parts["agency"]:
                chips.append(Chip("jobsearch.employers.chip.agency_client", {"agency": posting.agency or ""}))
            ccnl_value = 0.0
            if ccnl and division is not None:
                label, ok, bad = ccnl
                if division in ok:
                    ccnl_value = 1.0
                    chips.append(Chip("jobsearch.employers.chip.ccnl", {"ccnl": label}))
                elif division in bad:
                    ccnl_value = -1.0
                    chips.append(Chip("jobsearch.employers.chip.ccnl_conflict", {"ccnl": label}))
            parts["ccnl"] = ccnl_value
            size = 0.0
            if size_band:
                band = lexicon.band_of(candidate.employees)
                if band is None:
                    size = 0.5
                elif lexicon.size_agrees(size_band, band):
                    size = 1.0
                    chips.append(Chip("jobsearch.employers.chip.size", {"size": band}))
            parts["size"] = size
            wording = 0.0
            if ad_shingles and candidate.ads:
                own = set()
                for _, _, body in candidate.ads:
                    own |= _shingles(body)
                common = len(ad_shingles & own)
                wording = min(1.0, common / WORDING_FULL)
                if wording >= 0.4:
                    chips.append(Chip("jobsearch.employers.chip.wording"))
            parts["wording"] = wording
            score = (W_GEO * parts["geo"] + W_SECTOR * parts["sector"] + W_TECH * parts["tech"] +
                     W_ROLE * parts["role"] + W_AGENCY * parts["agency"] + W_CCNL * parts["ccnl"] +
                     W_SIZE * parts["size"] + W_WORDING * parts["wording"])
            scored.append((candidate, distance, score, parts, chips))

        s0 = S0_BASE + math.log(max(1, len(pool)))
        peak = max([s0] + [s for _, _, s, _, _ in scored])
        other = math.exp(s0 - peak)
        weights = [math.exp(s - peak) for _, _, s, _, _ in scored]
        total = other + sum(weights)
        estimate.other_share = round(other / total, 4)
        ranked = sorted(zip(scored, weights), key=lambda item: (-item[1], item[0][0].name.casefold()))
        probabilities = [w / total for _, w in ranked]
        for index, ((candidate, distance, score, parts, chips), weight) in enumerate(ranked[:top]):
            share = weight / total
            if share < min_share:
                break
            runner_up = probabilities[index + 1] if index + 1 < len(probabilities) else 0.0
            if share >= 0.5 and share - max(runner_up, estimate.other_share if index == 0 else 0.0) >= 0.2:
                label = "jobsearch.employers.label.strong"
            elif share >= 0.2:
                label = "jobsearch.employers.label.plausible"
            else:
                label = "jobsearch.employers.label.possible"
            estimate.hypotheses.append(EmployerHypothesis(
                name=candidate.name, probability=round(share, 4), score=round(score, 3), label_key=label,
                reasons=chips, components={k: round(v, 3) for k, v in parts.items()}, company_key=candidate.key,
                card_id=candidate.card_id, site=candidate.site, lat=candidate.lat, lon=candidate.lon,
                distance_km=round(distance, 1)))
        if not estimate.hypotheses:
            estimate.note_key = "jobsearch.employers.none"
    except Exception as error:
        log.warning("Possible employers not estimated: %s", type(error).__name__)
        estimate.hypotheses = []
        estimate.note_key = "jobsearch.employers.none"
    return estimate


def possible_employers(posting: Posting, company_index=(), cards=(), agency_clients=None, **kwargs
                       ) -> list[EmployerHypothesis]:
    """Top hypotheses (at most 5, each >= 10 %). See `estimate_employers` for the share of
    "another company not in the list", which the UI must always show."""
    return estimate_employers(posting, company_index, cards, agency_clients, **kwargs).hypotheses
