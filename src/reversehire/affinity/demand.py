"""``CompanyDemand``: everything known about what a company seeks and does, CV-independent.

Built from a company card (``companies.CompanyCard`` or its stored row dict) or from a
search result (``models.Company`` + ``Assessment`` + optional ``web.analysis.SiteOutcome``),
read by attribute or key so that no network or store module is imported.

Items (``DemandItem``), each with origin, weight and quoted evidence (text + source URL
or card field):

    origin             weight                         from
    explicit_must      5   (requirements.PRIORITY_WEIGHTS)  posting requirement lines
    explicit_nice      3                                    (scoring.requirements.extract_items,
    explicit_context   1 (+1 duty; evidence only)            skill dictionary hits)
    stated             STATED_WEIGHT = 2                    a technology named on the site /
                                                            products / presentation
    inferred_product   rule weight (0.3-0.7) x 1.0          knowledge.product_rules
    inferred_type      rule weight x TYPE_FACTOR[confidence] knowledge.type_needs

A term already known with a stronger origin is not repeated (explicit > stated >
inferred); an explicit requirement keeps one item per posting. Roles (``DemandRole``):
posting titles (weight 1) and the roles of product rules / company types (inferred).
Sectors (``SectorClaim``): OSM-derived card sectors, the register ATECO (via
``discovery.ateco.sectors_for_codes``) and sector keywords in the site text.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field

from ..cv import skills as cv_skills
from ..geo.sectors import SECTORS, sector_scores
from ..models import Posting, ProfileItem
from ..scoring import requirements as req
from ..scoring.relevance import normalize_text
from . import knowledge as kn
from .knowledge import SourceText

EXPLICIT_MUST, EXPLICIT_NICE, EXPLICIT_CONTEXT = "explicit_must", "explicit_nice", "explicit_context"
STATED, INFERRED_PRODUCT, INFERRED_TYPE = "stated", "inferred_product", "inferred_type"
EXPLICIT = (EXPLICIT_MUST, EXPLICIT_NICE, EXPLICIT_CONTEXT)
INFERRED = (INFERRED_PRODUCT, INFERRED_TYPE)
ORIGIN_RANK = {EXPLICIT_MUST: 0, EXPLICIT_NICE: 1, EXPLICIT_CONTEXT: 2, STATED: 3, INFERRED_PRODUCT: 4,
               INFERRED_TYPE: 5}

STATED_WEIGHT = 2.0
INFERRED_FACTOR = 1.0
TYPE_FACTOR = {"high": 0.8, "medium": 0.8, "low": 0.5}
ROLE_WEIGHT = {"posting": 1.0, INFERRED_PRODUCT: 0.8, INFERRED_TYPE: 0.7}
MAX_EVIDENCE = 3
MAX_FREE_WORDS = 6  # free must/nice lines up to this many words count as a skill item
TEXT_SECTOR_MIN = 0.99  # an exact sector keyword in a sentence
MAX_TEXT_SECTORS = 3
SECTOR_CLAIM_WEIGHT = {"ateco": 1.0, "osm": 0.8, "text": 0.6}
MIN_DESCRIPTION_CHARS = 40
_LEVEL_WORDS = (("junior", re.compile(r"\bjunior\b|\bjr\b|neolaureat|entry level|\bstage\b|tirocin|intern")),
                ("senior", re.compile(r"\bsenior\b|\bsr\b|\blead\b|\bhead\b|responsabile|\bmanager\b|principal")),
                ("mid", re.compile(r"\bmid\b|intermediate|\besperto\b|experienced")))
_GROWING, _SHRINKING = 0.03, -0.03  # yearly revenue change that counts as growth / decline


@dataclass
class Quote:
    text: str
    source: str
    field: str = ""

    @classmethod
    def of(cls, st: SourceText) -> "Quote":
        return cls(st.text, st.source, st.field)


@dataclass
class DemandItem:
    term: str
    origin: str
    weight: float
    key: str  # skill-dictionary entry ("e:<index>") or the normalised term ("t:...")
    family: str = ""
    posting_index: int | None = None
    rule: str = ""
    evidence: list[Quote] = field(default_factory=list)

    @property
    def explicit(self) -> bool:
        return self.origin in EXPLICIT

    @property
    def inferred(self) -> bool:
        return self.origin in INFERRED

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DemandItem":
        return cls(term=d["term"], origin=d["origin"], weight=float(d["weight"]), key=d["key"],
                   family=d.get("family", ""), posting_index=d.get("posting_index"), rule=d.get("rule", ""),
                   evidence=[Quote(**q) for q in d.get("evidence", [])])


@dataclass
class DemandRole:
    title: str
    origin: str  # "posting" | inferred_product | inferred_type
    weight: float
    posting_index: int | None = None
    evidence: Quote | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DemandRole":
        ev = d.get("evidence")
        return cls(title=d["title"], origin=d["origin"], weight=float(d["weight"]),
                   posting_index=d.get("posting_index"), evidence=Quote(**ev) if ev else None)


@dataclass
class SectorClaim:
    key: str  # geo.sectors key
    via: str  # "ateco" | "osm" | "text"
    evidence: Quote

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SectorClaim":
        return cls(key=d["key"], via=d["via"], evidence=Quote(**d["evidence"]))


@dataclass
class Asked:
    """A typed request of a posting (years, language, degree, seniority word)."""

    kind: str  # "years" | "language" | "degree" | "level"
    posting_index: int
    priority: str
    data: dict
    line: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Asked":
        return cls(**d)


@dataclass
class CompanyDemand:
    company_id: str
    name: str
    site: str = ""
    postings: list[Posting] = field(default_factory=list)
    items: list[DemandItem] = field(default_factory=list)
    roles: list[DemandRole] = field(default_factory=list)
    sectors: list[SectorClaim] = field(default_factory=list)
    ateco: str = ""
    ateco_source: str = ""
    types: list[kn.CompanyType] = field(default_factory=list)
    asked: list[Asked] = field(default_factory=list)
    workplaces: list[str] = field(default_factory=list)
    distance_km: float | None = None
    hiring_status: str = ""
    careers_url: str = ""
    postings_source: str = ""
    invitation: bool = False
    employees: float | None = None
    employees_source: str = ""
    revenue_trend: str | None = None  # "growing" | "stable" | "shrinking"
    revenue_change: float | None = None  # mean yearly change (0.05 = +5 %)
    revenue_years: list[int] = field(default_factory=list)
    revenue_source: str = ""
    texts: list[Quote] = field(default_factory=list)  # site text used (for evidence and the embedder)

    # --- data coverage ---

    @property
    def described_postings(self) -> list[int]:
        return [i for i, p in enumerate(self.postings) if len((p.description or "").strip()) >= MIN_DESCRIPTION_CHARS]

    @property
    def has_site_text(self) -> bool:
        return any(q.field != "name" for q in self.texts)

    @property
    def primary_type(self) -> str | None:
        return self.types[0].key if self.types else None

    def to_dict(self) -> dict:
        return {
            "company_id": self.company_id, "name": self.name, "site": self.site,
            "postings": [p.to_dict() for p in self.postings],
            "items": [i.to_dict() for i in self.items], "roles": [r.to_dict() for r in self.roles],
            "sectors": [s.to_dict() for s in self.sectors], "ateco": self.ateco, "ateco_source": self.ateco_source,
            "types": [t.to_dict() for t in self.types], "asked": [a.to_dict() for a in self.asked],
            "workplaces": list(self.workplaces), "distance_km": self.distance_km,
            "hiring_status": self.hiring_status, "careers_url": self.careers_url,
            "postings_source": self.postings_source, "invitation": self.invitation,
            "employees": self.employees, "employees_source": self.employees_source,
            "revenue_trend": self.revenue_trend, "revenue_change": self.revenue_change,
            "revenue_years": list(self.revenue_years), "revenue_source": self.revenue_source,
            "texts": [asdict(q) for q in self.texts],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CompanyDemand":
        return cls(
            company_id=str(d.get("company_id", "")), name=d.get("name", ""), site=d.get("site", ""),
            postings=[Posting.from_dict(p) for p in d.get("postings", [])],
            items=[DemandItem.from_dict(i) for i in d.get("items", [])],
            roles=[DemandRole.from_dict(r) for r in d.get("roles", [])],
            sectors=[SectorClaim.from_dict(s) for s in d.get("sectors", [])],
            ateco=d.get("ateco", ""), ateco_source=d.get("ateco_source", ""),
            types=[kn.CompanyType.from_dict(t) for t in d.get("types", [])],
            asked=[Asked.from_dict(a) for a in d.get("asked", [])],
            workplaces=list(d.get("workplaces", [])), distance_km=d.get("distance_km"),
            hiring_status=d.get("hiring_status", ""), careers_url=d.get("careers_url", ""),
            postings_source=d.get("postings_source", ""), invitation=bool(d.get("invitation", False)),
            employees=d.get("employees"), employees_source=d.get("employees_source", ""),
            revenue_trend=d.get("revenue_trend"), revenue_change=d.get("revenue_change"),
            revenue_years=list(d.get("revenue_years", [])), revenue_source=d.get("revenue_source", ""),
            texts=[Quote(**q) for q in d.get("texts", [])],
        )

    def fingerprint(self) -> str:
        data = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


# --- helpers -----------------------------------------------------------------------------


def _get(obj, name: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    return default if value is None else value


def item_key(term: str) -> tuple[str, str]:
    """(key, family) of a skill term: its dictionary entry when known."""
    entry = cv_skills.dictionary().lookup(term)
    if entry is not None:
        return f"e:{entry.index}", entry.family
    return "t:" + normalize_text(term), ""


def _to_posting(value) -> Posting | None:
    if isinstance(value, Posting):
        return value
    if isinstance(value, dict) and value.get("title"):
        try:
            return Posting.from_dict({"url": "", **value})
        except (KeyError, TypeError):
            return None
    if getattr(value, "title", None):
        return Posting(title=value.title, url=getattr(value, "url", "") or "",
                       description=getattr(value, "description", "") or "",
                       location=getattr(value, "location", "") or "", source=getattr(value, "source", "") or "",
                       contract=getattr(value, "contract", None), remote=getattr(value, "remote", None),
                       salary=getattr(value, "salary", None), salary_min=getattr(value, "salary_min", None),
                       salary_max=getattr(value, "salary_max", None),
                       salary_period=getattr(value, "salary_period", None),
                       salary_currency=getattr(value, "salary_currency", None),
                       travel=getattr(value, "travel", None), travel_note=getattr(value, "travel_note", None))
    return None


class _Builder:
    def __init__(self, company_id: str, name: str, site: str):
        self.demand = CompanyDemand(company_id=company_id, name=name, site=site)
        self._items: dict[tuple[str, int | None], DemandItem] = {}

    # --- items

    def add_item(self, term: str, origin: str, weight: float, quote: Quote | None, *, posting_index=None,
                 rule: str = "") -> None:
        key, family = item_key(term)
        explicit = origin in EXPLICIT
        slot = (key, posting_index if explicit else None)
        current = self._items.get(slot)
        if not explicit:
            # a term already asked or stated with a stronger origin is not repeated
            stronger = [i for (k, _), i in self._items.items() if k == key and ORIGIN_RANK[i.origin] < ORIGIN_RANK[origin]]
            if stronger:
                if quote is not None and origin == STATED:
                    for i in stronger:
                        if len(i.evidence) < MAX_EVIDENCE and quote not in i.evidence:
                            i.evidence.append(quote)
                return
        if current is None:
            entry = cv_skills.dictionary().lookup(term)
            label = entry.en if entry is not None else term
            item = DemandItem(label, origin, round(weight, 3), key, family, posting_index, rule,
                              [quote] if quote else [])
            self._items[slot] = item
            if not explicit:
                # drop weaker non-explicit duplicates
                for (k, p), other in list(self._items.items()):
                    if k == key and p is None and other is not item and ORIGIN_RANK[other.origin] > ORIGIN_RANK[origin]:
                        del self._items[(k, p)]
            return
        stronger = ORIGIN_RANK[origin] < ORIGIN_RANK[current.origin]
        if stronger:
            current.origin, current.rule = origin, rule
        current.weight = max(current.weight, round(weight, 3))
        if quote is not None and quote not in current.evidence:
            if stronger:  # the line of the strongest origin is quoted first
                current.evidence.insert(0, quote)
                del current.evidence[MAX_EVIDENCE:]
            elif len(current.evidence) < MAX_EVIDENCE:
                current.evidence.append(quote)

    def add_role(self, title: str, origin: str, quote: Quote | None, posting_index=None) -> None:
        norm = normalize_text(title)
        if not norm:
            return
        for r in self.demand.roles:
            if normalize_text(r.title) == norm and r.posting_index == posting_index:
                return
        if origin != "posting" and any(normalize_text(r.title) == norm for r in self.demand.roles):
            return
        self.demand.roles.append(DemandRole(title, origin, ROLE_WEIGHT[origin], posting_index, quote))

    # --- sources

    def postings(self, postings: list[Posting]) -> None:
        dictionary = cv_skills.dictionary()
        for index, posting in enumerate(postings):
            self.demand.postings.append(posting)
            url = posting.url or self.demand.site or ""
            self.add_role(posting.title, "posting", Quote(posting.title, url, "posting_title"), index)
            if posting.location and posting.location not in self.demand.workplaces:
                self.demand.workplaces.append(posting.location)
            title_norm = normalize_text(posting.title)
            for level, pattern in _LEVEL_WORDS:
                if pattern.search(title_norm):
                    self.demand.asked.append(Asked("level", index, "must", {"level": level}, posting.title))
                    break
            description = posting.description or ""
            if len(description.strip()) < MIN_DESCRIPTION_CHARS:
                continue
            from ..recruiter_sim.topics import topic_of

            for item in req.extract_items(description, posting):
                if item.kind == "offer":
                    continue
                quote = Quote(item.text, url, "posting")
                if item.kind in ("years", "language", "degree"):
                    self.demand.asked.append(Asked(item.kind, index, item.priority, dict(item.data), item.text))
                    continue
                if item.kind in req.TYPED_KINDS:
                    continue  # licence, car, travel: hard constraints, checked per posting
                priority = item.priority if item.kind == "skill" else "context"
                origin = {"must": EXPLICIT_MUST, "nice": EXPLICIT_NICE}.get(priority, EXPLICIT_CONTEXT)
                weight = req.PRIORITY_WEIGHTS[priority] + (req.DUTY_BONUS if item.kind == "duty" else 0)
                short = len(item.text.split()) <= 4 or cv_skills.list_like(item.text)
                hits = dictionary.find(item.text, list_context=short)
                if hits:
                    for hit in hits:
                        self.add_item(hit.entry.en, origin, weight, quote, posting_index=index)
                elif (priority in ("must", "nice") and item.kind == "skill"
                      and len(normalize_text(item.text).split()) <= MAX_FREE_WORDS and not topic_of(item.text)):
                    self.add_item(item.text, origin, weight, quote, posting_index=index)

    def site_texts(self, texts: list[SourceText]) -> None:
        dictionary = cv_skills.dictionary()
        for st in texts:
            if not st.text.strip():
                continue
            self.demand.texts.append(Quote.of(st))
            if st.field == "name":
                continue
            for hit in dictionary.find(st.text):
                sentence = kn.sentence_around(cv_skills.fold(st.text), hit.start, hit.end)
                self.add_item(hit.entry.en, STATED, STATED_WEIGHT, Quote(sentence, st.source, st.field))

    def inferred(self, texts: list[SourceText], ateco: str, ateco_source: str, osm_tags: dict) -> None:
        for need in kn.product_needs([t for t in texts if t.field != "name"]):
            quote = Quote.of(need.evidence) if need.evidence else None
            if need.is_role:
                self.add_role(need.term, INFERRED_PRODUCT, quote)
            else:
                self.add_item(need.term, INFERRED_PRODUCT, INFERRED_FACTOR * need.weight, quote, rule=need.rule)
        self.demand.types = kn.classify_company(texts, ateco, ateco_source, osm_tags)
        for ctype in self.demand.types:
            roles, skills = kn.type_needs(ctype.key)
            first = ctype.evidence[0].evidence if ctype.evidence else None
            quote = Quote.of(first) if first else None
            factor = TYPE_FACTOR.get(ctype.confidence, 0.5)
            for title in roles:
                self.add_role(title, INFERRED_TYPE, quote)
            for term, weight in skills:
                self.add_item(term, INFERRED_TYPE, factor * weight, quote, rule=f"type:{ctype.key}")

    def sectors(self, sector_names: list[str], ateco: str, ateco_source: str, texts: list[SourceText]) -> None:
        claims: list[SectorClaim] = []
        by_name = {s.name_en.casefold(): s.key for s in SECTORS.values()}
        for name in sector_names or []:
            key = by_name.get(str(name).casefold())
            if key:
                claims.append(SectorClaim(key, "osm", Quote(str(name), "osm", "sectors")))
        if ateco:
            from ..discovery.ateco import sectors_for_codes

            for key in sectors_for_codes([ateco]):
                claims.append(SectorClaim(key, "ateco", Quote(f"ATECO {ateco}", ateco_source, "ateco")))
        found = 0
        for st in texts:
            if st.field == "name" or found >= MAX_TEXT_SECTORS:
                continue
            pseudo = _Pseudo(st.text)
            for key, score in sorted(sector_scores(pseudo).items(), key=lambda kv: (-kv[1], kv[0])):
                if score < TEXT_SECTOR_MIN or any(c.key == key for c in claims):
                    continue
                claims.append(SectorClaim(key, "text", Quote(" ".join(st.text.split())[:220], st.source, st.field)))
                found += 1
                if found >= MAX_TEXT_SECTORS:
                    break
        seen: set[str] = set()
        for claim in sorted(claims, key=lambda c: -SECTOR_CLAIM_WEIGHT[c.via]):
            if claim.key not in seen:
                seen.add(claim.key)
                self.demand.sectors.append(claim)

    def finish(self) -> CompanyDemand:
        self.demand.items = sorted(self._items.values(),
                                   key=lambda i: (ORIGIN_RANK[i.origin], -i.weight, i.term.casefold(),
                                                  -1 if i.posting_index is None else i.posting_index))
        return self.demand


class _Pseudo:
    """A text seen by geo.sectors.sector_scores as a one-role profile."""

    def __init__(self, text: str):
        self.roles = [ProfileItem(text)]
        self.skills: list = []
        self.degrees: list = []


def _financials(builder: _Builder, financials: dict, registry_employees: str) -> None:
    from ..financials import FinancialSeries

    series = FinancialSeries.from_dict((financials or {}).get("series") or {})
    view = series.view() if not series.empty else None
    rows = [r for r in (view.rows if view else []) if r.revenue]
    demand = builder.demand
    if len(rows) >= 2:
        first, last = rows[0], rows[-1]
        years = max(1, last.year - first.year)
        change = ((last.revenue / first.revenue) ** (1 / years) - 1) if first.revenue > 0 else None
        if change is not None:
            demand.revenue_change = round(change, 4)
            demand.revenue_trend = "growing" if change >= _GROWING else "shrinking" if change <= _SHRINKING \
                else "stable"
            demand.revenue_years = [first.year, last.year]
            demand.revenue_source = last.source_url or last.source
    employees = [r for r in (view.rows if view else []) if r.employees]
    if employees:
        demand.employees = employees[-1].employees
        demand.employees_source = employees[-1].source_url or employees[-1].source
    elif registry_employees:
        m = re.search(r"\d[\d.,]*", registry_employees)
        if m:
            try:
                demand.employees = float(re.sub(r"[.,]", "", m.group(0)))
                demand.employees_source = "card:registry.employees"
            except ValueError:
                pass


# --- public builders ---------------------------------------------------------------------


def _card_postings(card) -> list[Posting]:
    assessment = _get(card, "assessment") or {}
    hiring = _get(card, "hiring") or {}
    raw = list(_get(assessment, "postings", []) or []) or list(_get(hiring, "postings", []) or [])
    postings = [p for p in (_to_posting(x) for x in raw) if p is not None]
    return postings


def _registry_value(card, name: str) -> str:
    item = (_get(card, "registry") or {}).get(name)
    if isinstance(item, dict):
        return str(item.get("value") or "")
    return ""


def demand_from_card(card, *, extra_postings: list | None = None) -> CompanyDemand:
    """CompanyDemand of a company card (``companies.CompanyCard`` or its row dict).

    ``extra_postings``: postings pasted by the user (models.Posting or dicts), added
    after the card's own.
    """
    site = str(_get(card, "site") or _get(card, "website") or "")
    company_id = str(_get(card, "id", "") or _get(card, "key", "") or _get(card, "name", ""))
    builder = _Builder(company_id, str(_get(card, "name", "")), site)
    texts: list[SourceText] = []
    products = _get(card, "products") or {}
    if products.get("summary"):
        texts.append(SourceText(products["summary"], products.get("summary_url") or site, "products"))
    for item in products.get("items", []) or []:
        if isinstance(item, dict) and item.get("name"):
            texts.append(SourceText(str(item["name"]), item.get("url") or site, "products"))
    profile = _get(card, "profile") or {}
    for part in ("presentation", "history"):
        block = profile.get(part) or {}
        if isinstance(block, dict) and block.get("text"):
            texts.append(SourceText(str(block["text"]), block.get("url") or site, part))
    assessment = _get(card, "assessment") or {}
    for reason in _get(assessment, "reasons", []) or []:
        text = _get(reason, "text", "")
        source = _get(reason, "source", "")
        if text and source and source not in ("cv", "osm") and source.startswith("http"):
            texts.append(SourceText(str(text), str(source), "site"))
    if not any(t.field == "site" for t in texts) and _get(card, "description"):
        texts.append(SourceText(str(_get(card, "description")), site or "card:description", "site"))
    texts.append(SourceText(str(_get(card, "name", "")), "card:name", "name"))
    ateco = str(_get(card, "ateco", "") or "")
    postings = _card_postings(card) + [p for p in (_to_posting(x) for x in extra_postings or []) if p]
    builder.postings(postings)
    builder.site_texts(texts)
    osm_tags = dict(_get(card, "osm_tags", {}) or {})
    builder.inferred(texts, ateco, "card:ateco", osm_tags)
    builder.sectors(list(_get(card, "sectors", []) or []), ateco, "card:ateco", texts)
    demand = builder.demand
    demand.ateco, demand.ateco_source = ateco, "card:ateco" if ateco else ""
    demand.distance_km = _get(card, "distance_km")
    hiring = _get(card, "hiring") or {}
    demand.hiring_status = str(_get(assessment, "status", "") or hiring.get("status", "") or "")
    demand.careers_url = str(_get(assessment, "careers_url", "") or hiring.get("careers_url", "") or "")
    demand.postings_source = str(hiring.get("postings_source", "") or "")
    demand.invitation = bool(hiring.get("spontaneous")) or demand.postings_source.startswith("unsolicited")
    address = str(_get(card, "address", "") or "")
    if address and address not in demand.workplaces:
        demand.workplaces.append(address)
    _financials(builder, _get(card, "financials") or {}, _registry_value(card, "employees"))
    return builder.finish()


def _osm_sector_names(osm_tags: dict) -> list[str]:
    names = []
    for sector in SECTORS.values():
        for key, value in sector.osm_tags:
            if value != "*" and osm_tags.get(key) == value:
                names.append(sector.name_en)
                break
    return names


def demand_from_result(company, assessment=None, outcome=None, *, extra_postings: list | None = None
                       ) -> CompanyDemand:
    """CompanyDemand of a search result (models.Company, Assessment, SiteOutcome)."""
    site = str(_get(company, "site", "") or "")
    ids = _get(company, "ids", {}) or {}
    company_id = str(ids.get("vat") or site or _get(company, "name", ""))
    builder = _Builder(company_id, str(_get(company, "name", "")), site)
    texts: list[SourceText] = []
    company_text = str(_get(outcome, "company_text", "") or "")
    if company_text:
        texts.append(SourceText(company_text, site or "site", "site"))
    for reason in _get(assessment, "reasons", []) or []:
        text, source = _get(reason, "text", ""), _get(reason, "source", "")
        if text and str(source).startswith("http") and text not in company_text:
            texts.append(SourceText(str(text), str(source), "site"))
    texts.append(SourceText(str(_get(company, "name", "")), "card:name", "name"))
    raw = list(_get(outcome, "postings", []) or []) or list(_get(assessment, "postings", []) or [])
    postings = [p for p in (_to_posting(x) for x in raw + list(extra_postings or [])) if p]
    ateco = str(ids.get("ateco", "") or "")
    osm_tags = dict(_get(company, "osm_tags", {}) or {})
    builder.postings(postings)
    builder.site_texts(texts)
    builder.inferred(texts, ateco, "company:ids.ateco", osm_tags)
    builder.sectors(_osm_sector_names(osm_tags), ateco, "company:ids.ateco", texts)
    demand = builder.demand
    demand.ateco, demand.ateco_source = ateco, "company:ids.ateco" if ateco else ""
    demand.distance_km = _get(company, "distance_km")
    status = _get(outcome, "status", None) or _get(assessment, "status", None)
    demand.hiring_status = str(getattr(status, "value", status) or "")
    demand.careers_url = str(_get(outcome, "careers_url", "") or _get(assessment, "careers_url", "") or "")
    demand.postings_source = str(_get(outcome, "postings_source", "") or "")
    demand.invitation = demand.postings_source.startswith("unsolicited")
    if _get(company, "address", ""):
        demand.workplaces.append(str(company.address))
    return builder.finish()


def build_demand(source) -> CompanyDemand:
    """Dispatch: a CompanyDemand (returned as is), a card / card row, a (company,
    assessment[, outcome]) tuple, or a models.Company."""
    if isinstance(source, CompanyDemand):
        return source
    if isinstance(source, tuple):
        return demand_from_result(*source)
    if isinstance(source, dict):
        if "items" in source and "company_id" in source:
            return CompanyDemand.from_dict(source)
        return demand_from_card(source)
    if hasattr(source, "products") or hasattr(source, "hiring"):
        return demand_from_card(source)
    return demand_from_result(source)
