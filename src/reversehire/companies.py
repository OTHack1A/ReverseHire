"""Personal area "My companies": company cards saved by the user.

A card is created when the user stars a company in the results (or saves it by hand,
or imports it) and keeps a snapshot of the last assessment plus the user's own fields
(status, notes, contacts, tags). There is one card per company key (see ``card_key``);
saving the same company again updates the data that comes from the search and never
touches the user's fields.

The full card (owner's specification, sections in this order):
 1. header: name, REA, ATECO, codice fiscale, partita IVA;
 2. registry data ("anagrafica"): legal form, registered office and operating sites,
    town/province/postcode, PEC, phone, e-mail, share capital, founding year, size
    hints and the company's own website;
 3. profiles sought (careers snapshot: postings with their attributes, spontaneous
    application channel, date last checked);
 4. products and services (from the website, with source URLs);
 5. selection system (ATS and what the platform offers, per its vendor);
 6. CV screening (platform capability, explicit employer statements, indicators, advice).
Added after the six sections (schema 3):
 7. presentation and history: sentences taken from the site (web/profile.py);
 8. people and contacts: HR first, with source URL, deletable (web/people.py);
 9. financials: revenue and profit of the last years (financials/), with source, year and
    scope, "why missing" when empty, CSV import and hand entry.
Identity status (CONFIRMED / PROBABLE / UNVERIFIED / CONFLICT, registry/confirm.py)
with per-field provenance (source, URL, where on the page, date). ATECO comes only from a
register (Registro Imprese entered by hand, an imported row or the paid provider); what is
not found stays blank, with a per-field hint on where to get it for free by hand
(`FIELD_HINTS`) and the note that a paid key can fill it automatically. When sources give
different values for a field they are kept and shown ("field_conflicts"), never picked
silently.

Building a card (`build_card`, `refresh_card`) is slow and polite: it reads the site
through the shared Fetcher (robots.txt, per-host delay, cache), calls VIES and GLEIF
through registry/http.py (one request every 2 s per host, 30-day cache), Wikidata and the
ESEF filings through the polite discovery gateway, and runs only on demand ("Create full
card", also for several cards in a row: `build_cards`). Every step is logged, saved as
soon as it is done and cancellable. The paid provider is never called by a build: only by
`fetch_paid` after the user's confirmation. `save_from_result` never uses the network.

The rows live in the ``companies`` table of the app's SQLite ``Store``; every function
here takes the store as first argument and is thread-safe (the store's lock is held
for each read-modify-write sequence).
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Callable
from urllib.parse import quote, urlsplit

from .models import Assessment, Company, HiringStatus
from .registry import confirm
from .registry.checksums import cf_ok, is_personal_cf, normalize_vat, piva_ok
from .store import Store

log = logging.getLogger(__name__)

CARD_STATUSES = ("interesting", "applied", "interview", "offer", "closed")
DEFAULT_STATUS = "interesting"
ORIGINS = ("osm", "manual", "jobboard", "import")
_ORIGIN_NAME = re.compile(r"^[a-z][a-z0-9_]{1,23}$")  # discovery sources may add their own

# Description built from the assessment reasons (best website sentences).
MAX_DESCRIPTION_CHARS = 1500
MAX_DESCRIPTION_SENTENCES = 8

# Fields the user may change with update(); "key", "origin", the assessment
# snapshot and the timestamps are managed here.
EDITABLE_FIELDS = (
    "name", "site", "address", "lat", "lon", "distance_km", "sectors", "description",
    "status", "favourite", "notes", "contacts", "tags",
)
# OSM keys whose value describes the activity (shown as a sector when no
# known sector matches).
_ACTIVITY_KEYS = ("office", "craft", "shop", "industrial", "amenity", "healthcare", "company")

# Card sections in the owner's order, and the units `refresh_card` can rebuild.
SECTIONS = ("header", "registry", "hiring", "products", "ats", "screening", "profile", "people", "financials")
REFRESH_SECTIONS = ("identity", "profile", "people", "financials", "hiring", "products", "ats", "screening")
# Registry fields of section 2 (values are {"value", "source", "url", "where", "date"}).
REGISTRY_FIELDS = ("legal_form", "registered_office", "operating_site", "address", "town", "province", "postcode",
                   "pec", "phone", "email", "share_capital", "founded", "employees", "lei", "wikidata",
                   "parent_company", "single_member", "in_liquidation", "group_vat", "linkedin", "social",
                   "sdi_code")
# Registry fields that may hold several values (each with its own provenance).
LIST_FIELDS = ("operating_site", "group_vat", "social")
# Fields still blank after the free sources: each has a hint on where to get it (FIELD_HINTS).
PAID_FIELDS = ("tax_code", "vat", "rea", "ateco", "legal_form", "pec", "registered_office",
               "share_capital", "employees", "founded")
MISSING_FIELDS = PAID_FIELDS
# Where a blank field can be found for free by hand (English defaults of "card.hint.<field>").
FIELD_HINTS = {
    "vat": "usually printed on the company's home page or legal notice; free search on registroimprese.it",
    "tax_code": "free on registroimprese.it after a free registration (“Codice Fiscale”)",
    "rea": "free on registroimprese.it after a free registration (“Numero REA”)",
    "ateco": "free on registroimprese.it: search the company, the result shows the ATECO code",
    "legal_form": "free on registroimprese.it (search result); the full form after a free registration",
    "pec": "free on registroimprese.it: search the company and press “MOSTRA” next to PEC",
    "registered_office": "free on registroimprese.it (search result: address of the registered office)",
    "share_capital": "in a Registro Imprese visura (5.00 EUR) or on the company's legal notice when published",
    "employees": "in the company's reports or a Registro Imprese visura; rarely published free",
    "founded": "in a Registro Imprese visura (date of incorporation) or on the company's history page",
}
PAID_KEY_HINT = "a paid key (Settings) can fill it automatically"
REGISTRO_IMPRESE_URL = "https://www.registroimprese.it/"
REGISTRO_IMPRESE_VIA = "Registro Imprese (entered by hand)"
# Fields of the "Registro Imprese" paste form, in display order.
REGISTRO_FORM_FIELDS = ("ateco", "pec", "rea", "tax_code", "legal_form", "registered_office")
# Preferred source per field (first = most trusted).
_PRIORITY = ("register", "vies", "gleif", "website_jsonld", "website", "wikidata")
_REGISTER_ONLY = {"ateco"}
MAX_POSTINGS_KEPT = 30
MAX_POSTING_DESCRIPTION = 4000  # characters of a posting description kept in the hiring snapshot
MAX_VIES_CALLS = 3
FINANCIALS_FRESH_S = 24 * 3600  # a full rebuild within a day keeps the free financial figures
ATS_POSTING_PAGES = 3  # postings pages read (mostly from the cache) to find the application ATS
# Fields whose different values from different sources are shown as a conflict.
CONFLICT_FIELDS = ("rea", "share_capital", "pec", "founded", "legal_form", "registered_office", "lei")


@dataclass
class CompanyCard:
    """A company saved in "My companies"."""

    id: int
    key: str
    name: str
    site: str | None = None
    address: str = ""
    lat: float | None = None
    lon: float | None = None
    distance_km: float | None = None
    osm_tags: dict[str, str] = field(default_factory=dict)
    sectors: list[str] = field(default_factory=list)
    description: str = ""
    assessment: dict | None = None  # Assessment.to_dict() of the last assessment
    status: str = DEFAULT_STATUS
    favourite: bool = False
    notes: str = ""
    contacts: str = ""
    tags: list[str] = field(default_factory=list)
    source_search_id: int | None = None
    origin: str = "osm"
    created: float = 0.0
    updated: float = 0.0
    # --- company card (schema 2) ---
    legal_name: str = ""
    vat: str = ""
    tax_code: str = ""
    rea: str = ""
    ateco: str = ""
    website: str = ""
    registry: dict = field(default_factory=dict)
    products: dict = field(default_factory=dict)
    hiring: dict = field(default_factory=dict)
    ats: dict = field(default_factory=dict)
    screening: dict = field(default_factory=dict)
    identity_status: str = confirm.UNVERIFIED
    identity: dict = field(default_factory=dict)
    provenance: list[dict] = field(default_factory=list)
    identity_checked: float | None = None
    registry_checked: float | None = None
    hiring_checked: float | None = None
    products_checked: float | None = None
    ats_checked: float | None = None
    screening_checked: float | None = None
    # --- schema 3
    profile: dict = field(default_factory=dict)
    people: dict = field(default_factory=dict)
    financials: dict = field(default_factory=dict)
    profile_checked: float | None = None
    people_checked: float | None = None
    financials_checked: float | None = None

    @classmethod
    def from_row(cls, row: dict) -> "CompanyCard":
        values = {k: row[k] for k in cls.__dataclass_fields__ if k in row}
        for name in ("registry", "products", "hiring", "ats", "screening", "identity", "profile", "people",
                     "financials"):
            if not isinstance(values.get(name), dict):
                values[name] = {}
        if not isinstance(values.get("provenance"), list):
            values["provenance"] = []
        for name in ("legal_name", "vat", "tax_code", "rea", "ateco", "website"):
            if values.get(name) is None:
                values[name] = ""
        if not values.get("identity_status"):
            values["identity_status"] = confirm.UNVERIFIED
        return cls(**values)

    def to_company(self) -> Company:
        """The card as a Company (e.g. to re-run an analysis)."""
        ids = {k: getattr(self, k) for k in ("vat", "tax_code", "rea", "ateco") if getattr(self, k)}
        for name in ("lei", "wikidata", "pec"):
            value = self.registry_value(name)
            if value:
                ids[name] = value
        return Company(
            name=self.name,
            lat=self.lat,
            lon=self.lon,
            address=self.address,
            site=self.site,
            osm_tags=dict(self.osm_tags),
            distance_km=self.distance_km,
            origin=self.origin,
            ids=ids,
            sources=[dict(f) for f in self.provenance],
        )

    def assessment_obj(self) -> Assessment | None:
        return Assessment.from_dict(self.assessment) if self.assessment else None

    @property
    def hiring_status(self) -> str:
        if not self.assessment:
            status = (self.hiring or {}).get("status")
            return str(status) if status else HiringStatus.NOT_ASSESSED.value
        return str(self.assessment.get("status", HiringStatus.NOT_ASSESSED.value))

    @property
    def postings(self) -> list[dict]:
        found = list((self.assessment or {}).get("postings", []))
        return found or list((self.hiring or {}).get("postings", []))

    @property
    def posting_titles(self) -> list[str]:
        return [p.get("title", "") for p in self.postings if p.get("title")]

    @property
    def relevance(self) -> float | None:
        return (self.assessment or {}).get("relevance")

    @property
    def fit(self) -> float | None:
        return (self.assessment or {}).get("fit")

    @property
    def careers_url(self) -> str | None:
        return (self.assessment or {}).get("careers_url") or (self.hiring or {}).get("careers_url")

    @property
    def is_hiring(self) -> bool:
        return self.hiring_status == HiringStatus.HIRING.value or bool(self.postings)

    # --- card helpers

    def registry_value(self, name: str) -> str:
        item = (self.registry or {}).get(name)
        if isinstance(item, dict):
            return str(item.get("value") or "")
        if isinstance(item, list):
            return "; ".join(str(i.get("value") or "") for i in item if isinstance(i, dict))
        return ""

    def header_value(self, name: str) -> str:
        return str(getattr(self, name, "") or "")

    def value_of(self, name: str) -> str:
        if name in ("legal_name", "vat", "tax_code", "rea", "ateco", "website"):
            return self.header_value(name)
        return self.registry_value(name)

    def source_of(self, name: str, value: str | None = None) -> dict | None:
        """Provenance fact of a field (the one matching `value`, else the first)."""
        fact_field = "name" if name == "legal_name" else name
        value = self.value_of(name) if value is None else value
        facts = [f for f in self.provenance if f.get("field") == fact_field]
        matching = [f for f in facts if value and _same_value(fact_field, str(f.get("value", "")), value)]
        candidates = matching or facts
        if not candidates:
            return None
        # the most trusted source first (register > VIES > GLEIF > website)
        return min(candidates, key=lambda f: _PRIORITY.index(f.get("source")) if f.get("source") in _PRIORITY
                   else len(_PRIORITY))

    @property
    def missing_paid(self) -> list[str]:
        """Blank fields after the free sources (each has a hint: FIELD_HINTS)."""
        return [f for f in PAID_FIELDS if not self.value_of(f)]

    @property
    def built(self) -> bool:
        return self.identity_checked is not None

    # --- schema 3: presentation, history, contacts, financials

    @property
    def presentation(self) -> dict:
        """{"text", "url", "where", "source", "date"} or {}."""
        return dict((self.profile or {}).get("presentation") or {})

    @property
    def history(self) -> dict:
        return dict((self.profile or {}).get("history") or {})

    @property
    def contacts_list(self) -> list[dict]:
        """People and role mailboxes found (HR first): name, role, email, phone, source_url,
        kind ("hr", "management", "general", "privacy"), where, origin, date, id."""
        return [dict(c) for c in (self.people or {}).get("items", []) if isinstance(c, dict)]

    def hr_contacts(self) -> list[dict]:
        return [c for c in self.contacts_list if c.get("kind") == "hr"]

    @property
    def hr_email(self) -> str:
        """The best address for an application: an HR contact's e-mail, '' when none (privacy
        mailboxes are never proposed). Order: a named HR person with an address (the HR
        manager has priority), then an HR role mailbox ("selezione@", "hr@"), then any other
        address found in an HR context."""
        from .web.people import email_kind

        hr = [c for c in self.hr_contacts() if c.get("email")]
        for contact in hr:
            if contact.get("name"):
                return contact["email"]
        for contact in hr:
            if email_kind(contact["email"]) == "hr":
                return contact["email"]
        return hr[0]["email"] if hr else ""

    @property
    def hr_contact_name(self) -> str:
        for contact in self.hr_contacts():
            if contact.get("name"):
                return contact["name"]
        return ""

    def contact_line(self) -> str:
        """One line for a letter ("Anna Rossi, HR Manager - selezione@example.com")."""
        hr = self.hr_contacts()
        named = next((c for c in hr if c.get("name")), None)
        mail = self.hr_email
        parts = []
        if named:
            parts.append(", ".join(x for x in (named.get("name"), named.get("role")) if x))
        if mail:
            parts.append(mail)
        return " - ".join(parts)

    @property
    def full_address(self) -> str:
        """Registered office with postcode, town and province ("Via X 1, 40100 Bologna (BO)")."""
        street = self.registry_value("registered_office")
        postcode, town, province = (self.registry_value(n) for n in ("postcode", "town", "province"))
        tail = " ".join(x for x in (postcode, town) if x)
        if province and province.casefold() not in tail.casefold():
            tail = f"{tail} ({province})" if tail else province
        if street and tail and (not postcode or postcode not in street):
            return f"{street}, {tail}"
        return street or tail

    def financial_series(self):
        """The stored financials.FinancialSeries (empty when none)."""
        from .financials import FinancialSeries

        return FinancialSeries.from_dict((self.financials or {}).get("series") or {})

    def financial_result(self):
        """A financials.CollectResult rebuilt from the stored series and source reports (for
        the "why missing" text and the notes, in the display language)."""
        from .financials.base import Reason
        from .financials.collect import CollectResult, SourceReport

        reports = []
        for item in (self.financials or {}).get("reports", []) or []:
            if not isinstance(item, dict):
                continue
            reason_data = item.get("reason") or None
            found = None
            if isinstance(reason_data, dict) and reason_data.get("key"):
                found = Reason(str(reason_data["key"]), str(reason_data.get("default", "")),
                               tuple(tuple(x) for x in reason_data.get("params") or ()))
            reports.append(SourceReport(str(item.get("source", "")), str(item.get("state", "")), found,
                                        str(item.get("detail", "")), int(item.get("years", 0) or 0),
                                        int(item.get("requests", 0) or 0)))
        return CollectResult(series=self.financial_series(), reports=reports)

    def revenue_trend(self):
        """financials.trend.RevenueTrend of the stored figures (the chosen scope)."""
        from .financials.trend import revenue_trend

        scope = (self.financials or {}).get("scope")
        try:
            return revenue_trend(self.financial_series(), scope)
        except Exception:  # noqa: BLE001 - a damaged series must not break the lists
            log.debug("Revenue trend not computed", exc_info=True)
            from .financials.trend import RevenueTrend

            return RevenueTrend()

    def latest_employees(self):
        """(employees, year, source) of the stored figures, else None."""
        from .financials.trend import latest_employees

        try:
            return latest_employees(self.financial_series(), (self.financials or {}).get("scope"))
        except Exception:  # noqa: BLE001
            return None


# --- helpers ----------------------------------------------------------------


def card_key(company: Company) -> str:
    """Stable identity of a company: the same key the pipeline uses."""
    from .pipeline import company_key  # lazy: pipeline imports network modules

    return company_key(company)


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _today() -> str:
    return date.today().isoformat()


def _same_value(name: str, a: str, b: str) -> bool:
    if name == "vat":
        return normalize_vat(a) == normalize_vat(b)
    return " ".join(_fold(a).split()) == " ".join(_fold(b).split())


def clean_tags(tags) -> list[str]:
    """Stripped, non-empty, de-duplicated (case-insensitively) tags."""
    if isinstance(tags, str):
        tags = tags.split(",")
    result: list[str] = []
    seen: set[str] = set()
    for tag in tags or []:
        tag = " ".join(str(tag).split())
        if tag and _fold(tag) not in seen:
            seen.add(_fold(tag))
            result.append(tag)
    return result


def sectors_of(company: Company) -> list[str]:
    """Sector names (English) from the company's OSM tags."""
    from .geo.sectors import SECTORS

    names: list[str] = []
    for sector in SECTORS.values():
        for key, value in sector.osm_tags:
            present = company.osm_tags.get(key)
            if present is not None and value != "*" and present == value:
                names.append(sector.name_en)
                break
    for key in _ACTIVITY_KEYS:
        value = company.osm_tags.get(key)
        if value and value not in ("yes", "no"):
            names.append(value.replace("_", " "))
    return clean_tags(names)


def description_from(assessment: Assessment | None) -> str:
    """Text about the company: the best website sentences captured by the assessment."""
    if assessment is None:
        return ""
    sentences: list[str] = []
    seen: set[str] = set()
    for evidence in assessment.reasons:
        text = " ".join((evidence.text or "").split())
        if not text or evidence.source == "cv" or _fold(text) in seen:
            continue
        seen.add(_fold(text))
        sentences.append(text)
        if len(sentences) >= MAX_DESCRIPTION_SENTENCES:
            break
    description = "\n".join(sentences)
    if len(description) > MAX_DESCRIPTION_CHARS:
        cut = description.rfind(" ", 0, MAX_DESCRIPTION_CHARS)
        description = description[: cut if cut > 0 else MAX_DESCRIPTION_CHARS]
    return description


def _check_status(status: str) -> str:
    if status not in CARD_STATUSES:
        raise ValueError(f"Invalid card status: {status!r}")
    return status


def _origin(value: str | None) -> str:
    value = (value or "").strip().lower()
    if value in ORIGINS or _ORIGIN_NAME.match(value or "-"):
        return value
    return "osm"


# --- provenance and identity --------------------------------------------------


def _fact(name: str, value, source: str, url: str = "", when: str | None = None) -> dict:
    return {"field": name, "value": " ".join(str(value or "").split()), "source": source,
            "url": url or "", "date": when or _today()}


def facts_from_company(company: Company) -> list[dict]:
    """Provenance facts carried by a Company (ids and sources attached by discovery or
    an import). Identifiers without their own source fact take the origin as source
    ("import" counts as a register row)."""
    facts = [_fact(f.get("field", ""), f.get("value", ""), str(f.get("source") or company.origin),
                   str(f.get("url") or ""), str(f.get("date") or "") or None)
             for f in company.sources or [] if isinstance(f, dict) and f.get("field") and f.get("value")]
    default_source = "register" if company.origin == "import" else (company.origin or "osm")
    for key, value in (company.ids or {}).items():
        name = "vat" if key == "vat" else key
        if not value:
            continue
        if any(f["field"] == name and _same_value(name, f["value"], str(value)) for f in facts):
            continue
        facts.append(_fact(name, value, default_source))
    return [f for f in facts if f["value"]]


def merge_facts(old: list[dict], new: list[dict], replace_sources: tuple[str, ...] = ()) -> list[dict]:
    """Old facts minus those of the re-read sources, plus the new ones (no duplicates)."""
    kept = [f for f in old or [] if f.get("source") not in replace_sources]
    result: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for fact in kept + list(new or []):
        name, source = str(fact.get("field") or ""), str(fact.get("source") or "")
        value = str(fact.get("value") or "")
        if not name or not value:
            continue
        key = (name, _fold(value) if name != "vat" else normalize_vat(value), source)
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(fact))
    return result


def _pick(facts: list[dict], name: str, *, prefer: str | None = None) -> dict | None:
    """The most trusted fact of a field (register > VIES > GLEIF > website JSON-LD > website)."""
    candidates = [f for f in facts if f.get("field") == name and f.get("value")]
    if name in _REGISTER_ONLY:
        candidates = [f for f in candidates if f.get("source") == "register"]
    if prefer:
        preferred = [f for f in candidates if _same_value(name, f["value"], prefer)]
        if preferred:
            candidates = preferred

    def rank(fact: dict) -> int:
        source = fact.get("source")
        return _PRIORITY.index(source) if source in _PRIORITY else len(_PRIORITY)

    return min(candidates, key=rank) if candidates else None


def compute_card_fields(card_name: str, facts: list[dict], *, site: str = "",
                        vies_state: str = "") -> dict:
    """Header columns, registry section, identity status and identity details from facts."""
    result = confirm.evaluate(facts, card_name, vies_not_registered=vies_state == "not_registered")
    columns: dict = {}
    prefer = result.matched_identifier or ""
    for name, column in (("name", "legal_name"), ("vat", "vat"), ("tax_code", "tax_code"),
                         ("rea", "rea"), ("ateco", "ateco")):
        fact = _pick(facts, name, prefer=prefer if name in ("vat", "tax_code") else None)
        if name == "name" and fact is not None and fact.get("source") == "vies":
            fact = _untruncated_name(fact, facts)
        value = fact["value"] if fact else ""
        if name == "vat":
            value = normalize_vat(value) if piva_ok(normalize_vat(value)) else ""
        if name == "tax_code" and value and not cf_ok(value.upper()):
            value = ""
        columns[column] = value
    registry: dict = {}
    keys = ("value", "source", "url", "where", "date")
    from .web.identity import legal_form_of

    # the legal form in the register's name ("NETFRAME SRL" at VIES) beats the website's
    facts = list(facts) + [{"field": "legal_form", "value": legal_form_of(f["value"]), "source": f.get("source"),
                            "url": f.get("url", ""), "date": f.get("date", ""), "where": "register name"}
                           for f in facts if f.get("field") == "name" and f.get("source") in ("vies", "register")
                           and legal_form_of(str(f.get("value", "")))
                           and not any(g.get("field") == "legal_form" and g.get("source") == f.get("source")
                                       for g in facts)]
    for name in REGISTRY_FIELDS:
        if name in LIST_FIELDS:
            items: list[dict] = []
            for fact in facts:
                if fact.get("field") == name and fact.get("value") and \
                        not any(_same_value(name, i["value"], fact["value"]) for i in items):
                    items.append({k: fact.get(k, "") for k in keys})
            if items:  # one link per network: up to 12 social profiles (web/social.py NETWORKS)
                registry[name] = items[:12 if name == "social" else 5]
            continue
        if name == "email":
            # the company's mailbox: never a privacy/DPO address or a person's own address
            fact = _pick([f for f in facts if f.get("field") != "email" or _company_mailbox(str(f.get("value", "")))],
                         name)
        else:
            fact = _pick(facts, name)
        if fact:
            registry[name] = {k: fact.get(k, "") for k in keys}
    if "legal_form" not in registry and columns.get("legal_name"):
        from .web.identity import legal_form_of

        form = legal_form_of(columns["legal_name"])
        name_fact = _pick(facts, "name")
        if form and name_fact:
            registry["legal_form"] = {"value": form, "source": name_fact.get("source", ""),
                                      "url": name_fact.get("url", ""), "date": name_fact.get("date", "")}
    website = site or ""
    site_fact = _pick(facts, "website")
    if not website and site_fact:
        website = site_fact["value"]
    columns["website"] = website
    columns["registry"] = registry
    columns["identity_status"] = result.status
    identity = result.to_dict()
    identity["missing"] = [f for f in PAID_FIELDS if not (columns.get(f) or (registry.get(f) or {}).get("value"))]
    identity["field_conflicts"] = field_conflicts(facts)
    columns["identity"] = identity
    return columns


def _untruncated_name(vies_fact: dict, facts: list[dict]) -> dict:
    """VIES cuts long names ("SACMI COOPERATIVA MECCANICI IMOLA SOCIETA' COOPERATIVA IN BR"):
    the register-class name that starts with the same words and goes on is used instead."""
    def key(text: str) -> str:
        return " ".join(re.sub(r"[^\w ]", " ", _fold(text)).split())

    short = key(vies_fact.get("value", ""))
    longer = [f for f in facts if f.get("field") == "name" and f.get("source") in ("gleif", "register")
              and key(f.get("value", "")).startswith(short) and len(key(f.get("value", ""))) > len(short)]
    return longer[0] if longer and len(short) >= 40 else vies_fact


def _company_mailbox(address: str) -> bool:
    """A role mailbox of the company (info@, contact@, sales@...), not a privacy/DPO address
    nor a person's own address ("p.rossi@", "anna.rossi@")."""
    from .web.people import _PRIVACY_LOCAL

    local = (address or "").split("@")[0].lower()
    if _PRIVACY_LOCAL.search(local):
        return False
    return not re.fullmatch(r"[a-z]{1,2}\.[a-z]{2,}|[a-z]{2,}[._][a-z]{2,}\d*", local) or bool(
        re.match(r"(?:info|contact|sales|office|hr|jobs|press|marketing|amministrazione|commerciale)[._]", local))


def _conflict_key(name: str, value: str) -> str:
    if name == "registered_office":
        # "VIA PRIVATA NINO BONNET, 10 (MILANO)" = "Via Privata Nino Bonnet 10"; "VIA X N 91" = "Via X 91"
        text = re.sub(r"\(.*?\)", " ", _fold(value))
        text = re.split(r"\b\d{5}\b", text)[0]
        tokens = [t for t in re.sub(r"[^\w ]", " ", text).split() if t not in ("n", "nr", "no", "civ")]
        for i, token in enumerate(tokens):
            if any(c.isdigit() for c in token):
                return " ".join(tokens[: i + 1])
        return " ".join(tokens)
    if name == "founded":
        year = re.search(r"\b(1[89]\d{2}|20\d{2})\b", value or "")
        return year.group(1) if year else _fold(value)
    if name == "rea":
        from .registry.checksums import rea_parts

        return rea_parts(value)[1]
    if name == "share_capital":
        # "EUR 22,470,504.68" = "EUR 22.470.504,68" = "22470504": the whole euros only
        amount = re.search(r"\d[\d.,'’ ]*", value or "")
        token = amount.group(0).strip() if amount else ""
        token = token[:-3] if re.search(r"[.,]\d{2}$", token) else token
        return re.sub(r"\D", "", token).lstrip("0")
    if name == "legal_form":
        return re.sub(r"[^a-z]", "", _fold(value))
    return " ".join(re.sub(r"[^\w ]", " ", _fold(value)).split())


def field_conflicts(facts: list[dict]) -> dict[str, list[dict]]:
    """{field: [{"value", "source", "url"}...]} for the CONFLICT_FIELDS whose sources state
    different values (year for "founded", number for "rea"; shown on the card, never picked
    silently). For the registered office only the register-class sources are compared with
    each other, and the website with them (several website addresses are often offices)."""
    out: dict[str, list[dict]] = {}
    for name in CONFLICT_FIELDS:
        items = [f for f in facts if f.get("field") == name and f.get("value")]
        if name == "registered_office":
            strong = [f for f in items if f.get("source") in ("register", "vies", "gleif")]
            if not strong:
                continue
            items = strong
        groups: dict[str, dict] = {}
        for fact in items:
            key = _conflict_key(name, str(fact["value"]))
            if key and key not in groups:
                groups[key] = {"value": fact["value"], "source": fact.get("source", ""), "url": fact.get("url", "")}
        if len(groups) > 1:
            out[name] = list(groups.values())
    return out


def _recompute(store: Store, card_id: int, extra: dict | None = None) -> None:
    """Re-evaluates header, registry and identity of a stored card from its provenance."""
    row = store.card_read(card_id)
    if row is None:
        raise KeyError(card_id)
    card = CompanyCard.from_row(row)
    previous = card.identity or {}
    fields = compute_card_fields(card.name, card.provenance, site=card.site or card.website,
                                 vies_state=str((previous.get("vies") or {}).get("state", "")))
    for key in ("vies", "gleif", "wikidata", "pages", "checked"):
        if key in previous:
            fields["identity"][key] = previous[key]
    if extra:
        identity_extra = extra.pop("identity_extra", None)
        if identity_extra:
            fields["identity"].update(identity_extra)
        fields.update(extra)
    store.card_update(card_id, fields)


def hiring_snapshot(status, postings, careers_url: str | None, postings_source: str = "",
                    site: str = "") -> dict:
    """The "profiles sought" section from a site outcome or an assessment."""
    items = []
    for posting in list(postings or [])[:MAX_POSTINGS_KEPT]:
        get = posting.get if isinstance(posting, dict) else lambda k, p=posting: getattr(p, k, None)
        item = {k: get(k) for k in ("title", "url", "location", "contract", "remote", "salary",
                                    "salary_period", "travel", "travel_note") if get(k)}
        lines = (" ".join(line.split()) for line in str(get("description") or "").splitlines())
        description = "\n".join(line for line in lines if line)  # requirements are read per line
        if description:
            # kept (bounded) so that a card built without an assessment still has the
            # requirement text of its postings (the affinity reads it)
            item["description"] = description[:MAX_POSTING_DESCRIPTION]
        items.append(item)
    status_value = status.value if isinstance(status, HiringStatus) else str(status or "")
    return {
        "status": status_value or HiringStatus.NOT_ASSESSED.value,
        "careers_url": careers_url or "",
        "postings_source": postings_source or "",
        "postings": items,
        "spontaneous": postings_source == "unsolicited",
        "source_url": site or "",
        "date": _today(),
    }


# --- service API ------------------------------------------------------------


def get(store: Store, card_id: int) -> CompanyCard | None:
    row = store.card_read(card_id)
    return None if row is None else CompanyCard.from_row(row)


def get_by_key(store: Store, key: str) -> CompanyCard | None:
    row = store.card_by_key(key)
    return None if row is None else CompanyCard.from_row(row)


def find(store: Store, company: Company) -> CompanyCard | None:
    """The card of a company from the results, if it was saved."""
    return get_by_key(store, card_key(company))


def save_from_result(
    store: Store,
    company: Company,
    assessment: Assessment | None,
    search_id: int | None = None,
) -> CompanyCard:
    """Create the card of a result, or refresh it if it exists.

    On refresh only the search-derived data changes (name, site, address,
    position, distance, OSM tags, sectors, description, assessment, source
    search); empty new values never replace known ones. The user's fields
    (status, favourite, notes, contacts, tags) and the origin are kept.
    Identifiers and provenance attached to the Company (discovery, imports) are
    merged into the card and the identity status is re-evaluated; the hiring
    snapshot follows the assessment. No network access.
    """
    key = card_key(company)
    sectors = sectors_of(company)
    description = description_from(assessment)
    facts = facts_from_company(company)
    hiring = None
    if assessment is not None:
        hiring = hiring_snapshot(assessment.status, assessment.postings, assessment.careers_url,
                                 "", company.site or "")
    with store.locked():
        existing = store.card_by_key(key)
        if existing is None:
            row = {
                "key": key,
                "name": company.name,
                "site": company.site,
                "address": company.address or "",
                "lat": company.lat,
                "lon": company.lon,
                "distance_km": company.distance_km,
                "osm_tags": dict(company.osm_tags),
                "sectors": sectors,
                "description": description,
                "assessment": assessment.to_dict() if assessment else None,
                "source_search_id": search_id,
                "origin": _origin(company.origin),
                "provenance": facts,
            }
            if hiring is not None:
                row.update(hiring=hiring, hiring_checked=time.time())
            card_id = store.card_insert(row)
            if facts:
                _recompute(store, card_id)
            return get(store, card_id)

        changes: dict = {"name": company.name or existing["name"]}
        for column, value in (
            ("site", company.site),
            ("address", company.address),
            ("lat", company.lat),
            ("lon", company.lon),
            ("distance_km", company.distance_km),
            ("osm_tags", dict(company.osm_tags) or None),
            ("sectors", sectors or None),
            ("description", description),
            ("assessment", assessment.to_dict() if assessment else None),
            ("source_search_id", search_id),
        ):
            if value not in (None, ""):
                changes[column] = value
        if hiring is not None:
            changes.update(hiring=hiring, hiring_checked=time.time())
        if facts:
            changes["provenance"] = merge_facts(existing.get("provenance") or [], facts)
        store.card_update(existing["id"], changes)
        if facts:
            _recompute(store, existing["id"])
        return get(store, existing["id"])


def add_manual(
    store: Store,
    name: str,
    site: str | None = None,
    *,
    address: str = "",
    lat: float | None = None,
    lon: float | None = None,
    sectors: list[str] | None = None,
    description: str = "",
    status: str = DEFAULT_STATUS,
    favourite: bool = False,
    notes: str = "",
    contacts: str = "",
    tags: list[str] | None = None,
) -> CompanyCard:
    """Card entered by hand (origin "manual").

    If a card with the same key exists (same site, or same name without a
    site), no duplicate is created: the given non-empty fields are applied
    to it and it is returned.
    """
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("A company card needs a name")
    _check_status(status)
    site = (site or "").strip() or None
    company = Company(name=name, lat=lat, lon=lon, address=address, site=site, origin="manual")
    key = card_key(company)
    fields = {
        "name": name,
        "site": site,
        "address": address,
        "lat": lat,
        "lon": lon,
        "sectors": clean_tags(sectors) if sectors else None,
        "description": description,
        "notes": notes,
        "contacts": contacts,
        "tags": clean_tags(tags) if tags else None,
    }
    with store.locked():
        existing = store.card_by_key(key)
        if existing is None and site is None:
            # Without a site the key is the name's: a card of the same company saved with
            # its website ("Create company card" of a posting that names only the employer)
            # is that card, not a new one without a site.
            wanted = " ".join(_fold(name).split())
            same = [c for c in list_cards(store) if " ".join(_fold(c.name).split()) == wanted]
            if len(same) == 1:
                existing = {"id": same[0].id}
        elif existing is None:
            # The same website host is the same company: "https://accenture.com" (from a
            # posting) is the card saved as "https://www.accenture.com/it-it".
            host = _site_host(site)
            same = [c for c in list_cards(store) if host and _site_host(c.site or c.website) == host]
            if len(same) == 1:
                existing = {"id": same[0].id}
                fields["site"] = None  # the saved site (with its path) is kept
        if existing is not None:
            changes = {k: v for k, v in fields.items() if v not in (None, "")}
            if status != DEFAULT_STATUS:
                changes["status"] = status
            if favourite:
                changes["favourite"] = True
            store.card_update(existing["id"], changes)
            return get(store, existing["id"])
        row = {k: v for k, v in fields.items() if v is not None}
        row.update(key=key, status=status, favourite=favourite, origin="manual")
        return get(store, store.card_insert(row))


def list_cards(
    store: Store,
    *,
    status: str | None = None,
    favourites_only: bool = False,
    hiring_only: bool = False,
    text: str | None = None,
) -> list[CompanyCard]:
    """Cards, most recently updated first, with simple filters.

    ``text`` is a plain case- and accent-insensitive substring filter on
    name, site, address, sectors, tags, notes, contacts, description and the
    legal identifiers; for the semantic search use
    ``scoring.company_search.search_cards``.
    """
    if status is not None:
        _check_status(status)
    needle = _fold(" ".join((text or "").split()))
    cards = [CompanyCard.from_row(r) for r in store.card_rows()]
    result = []
    for card in cards:
        if status is not None and card.status != status:
            continue
        if favourites_only and not card.favourite:
            continue
        if hiring_only and not card.is_hiring:
            continue
        if needle:
            haystack = _fold(
                " ".join(
                    [card.name, card.site or "", card.address, card.notes, card.contacts,
                     card.description, card.legal_name, card.vat, card.tax_code, card.rea, card.ateco,
                     *card.sectors, *card.tags, *card.posting_titles]
                )
            )
            if needle not in " ".join(haystack.split()):
                continue
        result.append(card)
    return result


def update(store: Store, card_id: int, **fields) -> CompanyCard:
    """Change the user-editable fields of a card (see EDITABLE_FIELDS).

    The key never changes, so a card keeps matching the same company in the
    results even if its site is edited. Raises KeyError for a missing card,
    ValueError for unknown fields or an invalid status.
    """
    unknown = set(fields) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"Fields not editable: {sorted(unknown)}")
    if "status" in fields:
        _check_status(fields["status"])
    if "name" in fields:
        fields["name"] = " ".join((fields["name"] or "").split())
        if not fields["name"]:
            raise ValueError("A company card needs a name")
    for column in ("tags", "sectors"):
        if column in fields:
            fields[column] = clean_tags(fields[column])
    if "favourite" in fields:
        fields["favourite"] = bool(fields["favourite"])
    for column in ("notes", "contacts", "description", "address"):
        if column in fields and fields[column] is None:
            fields[column] = ""
    with store.locked():
        if store.card_read(card_id) is None:
            raise KeyError(card_id)
        if fields:
            store.card_update(card_id, fields)
        return get(store, card_id)


def set_favourite(
    store: Store,
    target: int | str | Company,
    on: bool,
    *,
    assessment: Assessment | None = None,
    search_id: int | None = None,
) -> CompanyCard | None:
    """Star or un-star a card.

    ``target`` is a card id, a card key or a Company from the results. Starring
    a Company without a card creates the card (with ``assessment`` and
    ``search_id``). Un-starring keeps the card (the user may have notes on it);
    use ``delete`` to remove it. Returns the card, or None when there was
    nothing to un-star.
    """
    with store.locked():
        if isinstance(target, Company):
            row = store.card_by_key(card_key(target))
            if row is None:
                if not on:
                    return None
                row = {"id": save_from_result(store, target, assessment, search_id).id}
        elif isinstance(target, str):
            row = store.card_by_key(target)
        else:
            row = store.card_read(int(target))
        if row is None:
            return None
        store.card_update(row["id"], {"favourite": bool(on)})
        return get(store, row["id"])


def is_favourite(store: Store, company: Company) -> bool:
    row = store.card_by_key(card_key(company))
    return bool(row and row["favourite"])


def favourite_keys(store: Store) -> set[str]:
    """Keys of the starred cards: compare with ``card_key(company)`` in the results table."""
    return store.favourite_card_keys()


def saved_keys(store: Store) -> set[str]:
    """Keys of all cards (starred or not)."""
    return {row["key"] for row in store.card_rows()}


def revenue_trends(store: Store) -> dict[str, dict]:
    """card key -> RevenueTrend.to_dict() of the cards whose stored figures give a trend."""
    out: dict[str, dict] = {}
    for card in list_cards(store):
        trend = card.revenue_trend()
        if trend.known:
            out[card.key] = trend.to_dict()
    return out


def annotate_revenue_trends(store: Store, found: list[Company]) -> int:
    """Puts the revenue trend of the user's cards into `company.fields["revenue_trend"]` of
    the matching discovered companies (read by discovery/ranking.py for its small,
    explained bonus and by the results table). Returns how many were annotated."""
    trends = revenue_trends(store)
    if not trends:
        return 0
    count = 0
    for company in found:
        trend = trends.get(card_key(company))
        if trend is not None:
            try:
                company.fields["revenue_trend"] = trend
                count += 1
            except (AttributeError, TypeError):
                continue
    return count


def delete(store: Store, card_id: int) -> None:
    store.card_delete(card_id)


def cards_to_enrich(store: Store) -> list[CompanyCard]:
    """Favourite cards with a website whose full card was never built (enriched on demand)."""
    return [c for c in list_cards(store, favourites_only=True) if c.site and not c.built]


# --- register data entered by the owner -----------------------------------------


def apply_register_data(store: Store, card_id: int, raw: dict, *, url: str = "",
                        when: str | None = None) -> tuple[CompanyCard, dict[str, str]]:
    """Adds register data typed or imported by the owner (source "register").

    Invalid identifiers are not stored and come back in the error dictionary
    (field -> English message). The identity status is re-evaluated.
    """
    from .registry.visura import clean_fields

    clean, errors = clean_fields(raw)
    facts = []
    for name, value in clean.items():
        fact_field = name if name != "registered_office" else "registered_office"
        facts.append(_fact(fact_field, value, "register", url or "manual entry", when))
    with store.locked():
        row = store.card_read(card_id)
        if row is None:
            raise KeyError(card_id)
        if facts:
            # a newer register entry replaces the older register value of the same field
            changed = {f["field"] for f in facts}
            old = [f for f in row.get("provenance") or []
                   if not (f.get("source") == "register" and f.get("field") in changed)]
            store.card_update(card_id, {"provenance": merge_facts(old, facts),
                                        "registry_checked": time.time()})
            _recompute(store, card_id)
        return get(store, card_id), errors


def import_register_csv(store: Store, card_id: int, text: str, *, filename: str = ""
                        ) -> tuple[CompanyCard, dict[str, str]]:
    """Imports the row of this company from a "visura" / "Elenco imprese" CSV export.

    Raises ValueError when the file has no usable row for the card.
    """
    from .registry.visura import parse_csv, pick_row

    card = get(store, card_id)
    if card is None:
        raise KeyError(card_id)
    rows = parse_csv(text)
    row = pick_row(rows, vat=card.vat, tax_code=card.tax_code, name=card.legal_name or card.name)
    if row is None:
        raise ValueError("No row of this company in the file (match by partita IVA, codice fiscale or name).")
    return apply_register_data(store, card_id, row, url=f"CSV {filename}".strip())


# --- building the full card (network, slow) ------------------------------------------


Progress = Callable[[str, int, int], None]


def _resolve_card(store: Store, target) -> CompanyCard:
    if isinstance(target, CompanyCard):
        card = get(store, target.id)
    elif isinstance(target, Company):
        card = find(store, target) or save_from_result(store, target, None)
    elif isinstance(target, int):
        card = get(store, target)
    else:
        text = " ".join(str(target or "").split())
        if not text:
            raise ValueError("Give a company card, a Company or a website address")
        site = text if "://" in text else "https://" + text
        host = urlsplit(site).hostname or text
        name = host[4:] if host.startswith("www.") else host
        company = Company(name=name, lat=None, lon=None, site=site, origin="manual")
        card = find(store, company)
        if card is None:
            card = add_manual(store, name, site)
    if card is None:
        raise KeyError(target)
    return card



def _site_host(url: str | None) -> str:
    """"accenture.com" for "https://www.accenture.com/it-it" ("" without a site)."""
    url = (url or "").strip()
    if not url:
        return ""
    return (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower().removeprefix("www.")


def posting_pages(urls: list[str], site: str, fetch_page, skip: list[str], cancelled=lambda: False) -> list[dict]:
    """Pages of the first postings, for the selection-system detection: a posting on the
    company's own site is read (from the cache, usually); a posting elsewhere counts by
    its link alone (joblink.allibo.com/..., *.myworkdayjobs.com/...)."""
    site_host = (urlsplit(site or "").hostname or "").lower().removeprefix("www.")
    pages: list[dict] = []
    for link in [u for u in dict.fromkeys(urls) if u and u not in skip][:ATS_POSTING_PAGES]:
        if cancelled():
            break
        host = (urlsplit(link).hostname or "").lower().removeprefix("www.")
        if host != site_host:
            pages.append({"url": link, "html": f'<a href="{html.escape(link, quote=True)}"></a>', "text": ""})
            continue
        try:
            page = fetch_page(link)
        except Exception:  # noqa: BLE001 - a posting page is optional evidence
            log.info("Posting page not read for the selection system", exc_info=True)
            page = None
        if page is not None:
            pages.append({"url": page.final_url, "html": page.html, "text": ""})
    return pages

def _normal_site(url: str | None) -> str:
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    if url and not urlsplit(url).path:
        url += "/"
    return url


class _CancelGateway:
    """The polite gateway with the build's cancel event passed to every request."""

    def __init__(self, gateway, cancel: threading.Event | None):
        self.gateway = gateway
        self.cancel = cancel

    def get(self, url: str, **kwargs):
        if self.cancel is not None:
            kwargs.setdefault("cancel", self.cancel)
        return self.gateway.get(url, **kwargs)

    def stats(self) -> dict:
        stats = getattr(self.gateway, "stats", None)
        return stats() if callable(stats) else {}


def _name_score(answer, own_names: list[str]) -> tuple[float, float]:
    """(similarity with the card name, best similarity with the website's legal names): the
    card name says which company is meant; a site often names its group companies too."""
    card_name, *website = own_names or [""]
    return (confirm.name_similarity(card_name, answer.name) if card_name else 0.0,
            max([confirm.name_similarity(n, answer.name) for n in website if n] or [0.0]))


def _vies_choice(answers: list, own_names: list[str]):
    """The valid VIES answer whose name best matches the card name (then the website's legal
    names); None when no answer is valid."""
    valid = [a for a in answers if a.valid]
    return max(valid, key=lambda a: _name_score(a, own_names)) if valid else None


def _gleif_without_identifiers(client, card: CompanyCard, facts: list[dict]):
    """(GleifRecord or None, state) for a site without any identifier: by the website's legal
    name, else the card name; the record must have the same name words (legal forms ignored)
    and the town stated by the site (no town on the site: not accepted)."""
    from .registry import gleif

    towns = {_fold(f["value"]) for f in facts if f.get("field") == "town" and f.get("value")}
    if not towns:
        return None, ""
    names = [f["value"] for f in facts if f.get("field") == "name" and f.get("source") in confirm.WEBSITE_SOURCES]
    state = ""
    for candidate in list(dict.fromkeys(names + [card.name]))[:2]:
        answer = gleif.search_name(client, candidate)
        state = state or answer.state
        wanted = set(confirm.name_tokens(candidate))
        same = [r for r in answer.records if wanted and set(confirm.name_tokens(r.legal_name)) == wanted
                and _fold(r.town) in towns]
        if len(same) == 1:
            record = same[0]
            log.info("GLEIF record found by name %r and town %s: %s", candidate, record.town, record.lei)
            return record, "found (by name and town)"
        if len(same) > 1:
            log.info("GLEIF by name %r: %d records in the same town; none used", candidate, len(same))
    return None, state


def _posting_texts(card: CompanyCard, fetcher, cancel) -> list[tuple[str, str]]:
    """Visible text of at most MAX_POSTINGS posting pages (usually from the page cache)."""
    from .web.people import MAX_POSTINGS
    from .web.text import visible_text

    out = []
    for posting in card.postings[:MAX_POSTINGS]:
        url = posting.get("url") if isinstance(posting, dict) else None
        if not url or (cancel is not None and cancel.is_set()):
            continue
        try:
            page = fetcher.fetch_page(url)
        except Exception:  # noqa: BLE001 - a posting page must never break the card
            page = None
        if page is not None:
            out.append((page.final_url, visible_text(page.html)))
    return out


def _report_dict(report) -> dict:
    reason = report.reason
    return {"source": report.source, "state": report.state, "detail": report.detail, "years": report.years,
            "requests": report.requests,
            "reason": {"key": reason.key, "default": reason.default, "params": [list(p) for p in reason.params]}
            if reason is not None else None}


def build_card(store: Store, company_or_url, fetcher, settings, *, cancel: threading.Event | None = None,
               progress: Progress | None = None, outcome=None, registry=None, gateway=None,
               sections: set[str] | tuple[str, ...] | None = None, roles: list[str] | None = None
               ) -> CompanyCard:
    """Builds (or refreshes) the full card of a company: site, identity pages, free
    registers (VIES, GLEIF, Wikidata), presentation and history, people and contacts,
    financials (ESEF, Wikidata), selection system and CV screening, products.

    `company_or_url` is a CompanyCard, a card id, a Company or a website address (a
    manual card is created when needed). `outcome` is a ready `web.analysis.SiteOutcome`
    (from the search) to avoid reading the site twice. `registry` is a
    `registry.http.RegistryClient` and `gateway` a `discovery.gateway.PoliteGateway`
    (both created and closed here when None; the gateway reuses the registry client's
    HTTP client when only `registry` is given). `sections` limits the work to some of
    REFRESH_SECTIONS. Every completed step is saved at once and logged; a cancel stops
    between steps (and inside network waits) and keeps what was done. The paid provider
    is never called here (see `fetch_paid`).
    """
    from .discovery.gateway import Cancelled, PoliteGateway
    from .registry import gleif, vies
    from .registry import wikidata as wikidata_identity
    from .registry.http import RegistryClient, RegistryError
    from .web import products as web_products
    from .web import screening as web_screening
    from .web.analysis import analyze_site
    from .web.identity import scan_identity
    from .web.people import people_of
    from .web.profile import profile_of

    wanted = set(sections or REFRESH_SECTIONS)
    unknown = wanted - set(REFRESH_SECTIONS) - {"registry", "header"}
    if unknown:
        raise ValueError(f"Unknown card sections: {sorted(unknown)}")
    if wanted & {"registry", "header"}:
        wanted -= {"registry", "header"}
        wanted.add("identity")
    card = _resolve_card(store, company_or_url)
    site = _normal_site(card.site or card.website)
    needs_scan = bool(wanted & {"identity", "profile", "people"})
    steps: list[str] = []
    if site and wanted & {"hiring", "ats", "screening"} and outcome is None:
        steps.append("site")
    if site and needs_scan:
        steps.append("identity")
    if "identity" in wanted:
        steps.append("registers")
    if site and "profile" in wanted:
        steps.append("profile")
    if "people" in wanted:
        steps.append("people")
    if "financials" in wanted:
        steps.append("financials")
    if site and wanted & {"ats", "screening"}:
        steps.append("ats")
    if site and "products" in wanted:
        steps.append("products")
    total = len(steps)
    done = 0
    started = time.monotonic()
    label = f"card {card.id} ({card.name})"
    log.info("Card build started: %s, steps: %s", label, ", ".join(steps) or "none")

    def step(key: str) -> bool:
        nonlocal done
        if cancel is not None and cancel.is_set():
            log.info("Card build cancelled before step %s: %s", key, label)
            return False
        if progress is not None:
            try:
                progress(key, done, total)
            except Exception:
                pass
        log.info("Card build step %d/%d %s: %s", done + 1, total, key, label)
        done += 1
        return True

    def cancelled() -> bool:
        return cancel is not None and cancel.is_set()

    own_gateway = None
    if gateway is None and wanted & {"identity", "financials"}:
        own_gateway = PoliteGateway(settings, store, getattr(registry, "_client", None) if registry is not None
                                    else None)
    polite = _CancelGateway(gateway or own_gateway, cancel) if (gateway or own_gateway) is not None else None
    wikidata_people: list[dict] | None = None
    try:
        # 1. the site (hiring snapshot)
        if "site" in steps:
            if not step("site"):
                return get(store, card.id)
            outcome = analyze_site(site, fetcher, settings, cancel, roles=roles)
        if outcome is not None and "hiring" in wanted and not cancelled():
            if outcome.status != HiringStatus.NOT_ASSESSED:
                snapshot = hiring_snapshot(outcome.status, outcome.postings, outcome.careers_url,
                                           outcome.postings_source, site)
                store.card_update(card.id, {"hiring": snapshot, "hiring_checked": time.time()})

        # 2. identity pages of the site (also the pages of the presentation and the contacts)
        scan = None
        if "identity" in steps:
            if not step("identity"):
                return get(store, card.id)
            scan = scan_identity(site, fetcher, cancel=cancel)
            if "identity" in wanted:
                website_facts = scan.facts()
                if scan.pages_read:
                    website_facts.append(_fact("website", site, "website", site, scan.date))
                with store.locked():
                    row = store.card_read(card.id)
                    facts = merge_facts(row.get("provenance") or [], website_facts,
                                        replace_sources=("website", "website_jsonld"))
                    store.card_update(card.id, {"provenance": facts})

        # 3. free registers: VIES, GLEIF, Wikidata
        if "registers" in steps and not cancelled():
            if not step("registers"):
                return get(store, card.id)
            own = registry is None
            client = registry or RegistryClient(settings, store, cancel=cancel)
            try:
                current = get(store, card.id)
                facts = list(current.provenance)
                vies_state, vies_detail, gleif_state = "", "", ""
                vats: list[str] = []
                for fact in sorted(facts, key=lambda f: (f.get("source") not in confirm.WEBSITE_SOURCES,
                                                         f.get("field") != "vat")):
                    number = normalize_vat(fact.get("value", ""))
                    if fact.get("field") in ("vat", "tax_code") and piva_ok(number) and number not in vats:
                        vats.append(number)
                own_names = [current.name] + [f["value"] for f in facts if f.get("field") == "name"
                                              and f.get("source") in confirm.WEBSITE_SOURCES]
                new_facts: list[dict] = []
                answers = []
                for number in vats[:MAX_VIES_CALLS]:
                    if cancelled():
                        break
                    answer = vies.lookup(client, number)
                    answers.append(answer)
                    if answer.valid and _name_score(answer, own_names)[0] >= confirm.NAME_THRESHOLD:
                        break  # this VAT is the company's own: no need to ask for the others
                chosen = _vies_choice(answers, own_names)
                if chosen is not None:
                    vies_state, vies_detail = "valid", chosen.detail
                    new_facts += chosen.facts()
                elif answers:
                    vies_state, vies_detail = answers[0].state, answers[0].detail
                others = [{"vat": a.vat, "state": a.state, "name": a.name} for a in answers if a is not chosen]
                codes = [f["value"].upper() for f in facts if f.get("field") == "tax_code"
                         and cf_ok(f["value"].upper()) and not is_personal_cf(f["value"])]
                preferred = [chosen.vat] if chosen is not None else []
                codes += [v for v in preferred + vats if v not in codes]
                gleif_record = None
                if codes and not cancelled():
                    answer = gleif.by_tax_code(client, codes[0])
                    gleif_state = answer.state
                    if answer.records:
                        gleif_record = answer.records[0]
                leis = [f["value"] for f in facts if f.get("field") == "lei"]
                if leis and gleif_record is None and not cancelled():
                    answer = gleif.by_lei(client, leis[0])
                    gleif_state = answer.state if answer.state != "not_found" else gleif_state or "not_found"
                    if answer.records:
                        gleif_record = answer.records[0]
                legal_name = chosen.name if chosen is not None else ""
                if gleif_record is None and legal_name and gleif_state != "unavailable" and not cancelled():
                    answer = gleif.by_name(client, legal_name)
                    gleif_state = f"{answer.state} (by name)" if answer.state == "found" else gleif_state or answer.state
                    if answer.records:
                        gleif_record = answer.records[0]
                if gleif_record is None and not vats and not codes and not leis and not cancelled():
                    # the site prints no identifier: GLEIF by the site's legal name or the card
                    # name, accepted only for the same words AND the same town as the site's
                    gleif_record, by_name_state = _gleif_without_identifiers(client, current, facts)
                    gleif_state = by_name_state or gleif_state
                    if gleif_record is not None and piva_ok(gleif_record.registered_as) and not cancelled():
                        answer = vies.lookup(client, gleif_record.registered_as)
                        answers.append(answer)
                        vies_state, vies_detail = answer.state, answer.detail
                        if answer.valid:
                            chosen = answer
                            new_facts += answer.facts()
                if gleif_record is not None:
                    new_facts += gleif_record.facts()
                # Wikidata: only by the item linked from the site, the LEI or the VAT number
                qids = [f["value"] for f in facts if f.get("field") == "wikidata"
                        and f.get("source") in confirm.WEBSITE_SOURCES]
                wiki = wikidata_identity.WikidataIdentity("skipped")
                if not cancelled():
                    wiki = wikidata_identity.lookup(
                        polite, qid=qids[0] if qids else "", vat=chosen.vat if chosen is not None else
                        (vats[0] if vats else ""), lei=gleif_record.lei if gleif_record is not None else
                        (leis[0] if leis else ""), cancel=cancel)
                replace = ("vies", "gleif") + (("wikidata",) if wiki.state == "found" else ())
                if wiki.state == "found":
                    new_facts += wiki.facts
                    wikidata_people = wiki.people
                    # a VAT number known only from Wikidata: VIES says whose it is
                    wiki_vats = [f["value"] for f in wiki.facts if f["field"] == "vat" and piva_ok(f["value"])
                                 and f["value"] not in [a.vat for a in answers]]
                    if chosen is None and wiki_vats and not cancelled():
                        answer = vies.lookup(client, wiki_vats[0])
                        answers.append(answer)
                        names = own_names + ([gleif_record.legal_name] if gleif_record is not None else [])
                        if answer.valid and max(_name_score(answer, names)) >= confirm.NAME_THRESHOLD:
                            chosen = answer
                            vies_state, vies_detail = "valid", answer.detail
                            new_facts += answer.facts()
                with store.locked():
                    row = store.card_read(card.id)
                    merged = merge_facts(row.get("provenance") or [], new_facts, replace_sources=replace)
                    identity = dict(row.get("identity") or {})
                    identity["vies"] = {"state": vies_state or ("skipped" if not vats else ""), "detail": vies_detail,
                                        "checked": [a.vat for a in answers], "others": others}
                    identity["gleif"] = {"state": gleif_state or "skipped"}
                    identity["wikidata"] = {"state": wiki.state, "qid": wiki.qid}
                    identity["checked"] = _today()
                    if scan is not None:
                        identity["pages"] = [{"url": p["url"], "kind": p["kind"]} for p in scan.pages]
                    store.card_update(card.id, {"provenance": merged, "identity": identity,
                                                "identity_checked": time.time(), "registry_checked": time.time()})
                    _recompute(store, card.id)
                log.info("Card registers: %s: VIES %s (%d asked), GLEIF %s, Wikidata %s", label,
                         vies_state or "skipped", len(answers), gleif_state or "skipped", wiki.state)
            except (RegistryError, Cancelled):
                log.info("Card registers interrupted (cancelled): %s", label)
            finally:
                if own:
                    client.close()

        # 4. presentation and history (pages of the identity scan: no request)
        if "profile" in steps and not cancelled() and scan is not None:
            if not step("profile"):
                return get(store, card.id)
            found = profile_of(scan.pages)
            store.card_update(card.id, {"profile": found, "profile_checked": time.time()})
            log.info("Card profile: %s: presentation %s, history %s", label,
                     "found" if found["presentation"] else "none", "found" if found["history"] else "none")

        # 5. people and contacts
        if "people" in steps and not cancelled():
            if not step("people"):
                return get(store, card.id)
            current = get(store, card.id)
            old = current.people or {}
            deleted = list(old.get("deleted") or [])
            extra = wikidata_people if wikidata_people is not None else \
                [c for c in current.contacts_list if c.get("origin") == "wikidata"]
            if scan is None and site and not cancelled():
                scan = scan_identity(site, fetcher, cancel=cancel)
            pages = scan.pages if scan is not None else []
            postings = _posting_texts(current, fetcher, cancel)
            items = people_of(pages, postings, extra, deleted=deleted, site=site)
            store.card_update(card.id, {"people": {"items": items, "deleted": deleted, "date": _today()},
                                        "people_checked": time.time()})
            log.info("Card people: %s: %d contacts (%d HR)", label, len(items),
                     sum(1 for c in items if c.get("kind") == "hr"))

        # 6. financials from the free sources (ESEF for listed companies, Wikidata)
        if "financials" in steps and not cancelled():
            if not step("financials"):
                return get(store, card.id)
            from .financials import FinancialsIdentity, collect_financials
            from .financials.collect import automatic_sources

            current = get(store, card.id)
            recent = current.financials_checked and time.time() - current.financials_checked < FINANCIALS_FRESH_S
            if recent and sections is None and (current.financials or {}).get("reports"):
                # the annual-report files are large: a full rebuild on the same day keeps them
                log.info("Card financials: %s: checked less than a day ago, kept", label)
                result = None
            else:
                result = collect_financials(FinancialsIdentity.from_card(current), automatic_sources(),
                                            polite, series=current.financial_series())
            if result is not None and not result.cancelled:
                stored = dict(current.financials or {})
                stored.update(series=result.series.to_dict(), reports=[_report_dict(r) for r in result.reports],
                              date=_today())
                store.card_update(card.id, {"financials": stored, "financials_checked": time.time()})
            if result is not None:
                log.info("Card financials: %s: %d years (%s)", label, len(result.series.years()),
                         ", ".join(f"{r.source} {r.state}" for r in result.reports))

        # 7. selection system and CV screening
        if "ats" in steps and not cancelled():
            if not step("ats"):
                return get(store, card.id)
            current = get(store, card.id)
            careers_url = (outcome.careers_url if outcome is not None else None) or current.careers_url
            postings_source = (outcome.postings_source if outcome is not None else "") or \
                (current.hiring or {}).get("postings_source", "")
            pages: list[dict] = []
            checked: list[str] = []
            careers_page = fetcher.fetch_page(careers_url) if careers_url else None
            if careers_page is not None:
                pages.append({"url": careers_page.final_url, "html": careers_page.html,
                              "text": _visible(careers_page.html)})
                checked.append(careers_page.final_url)
                for link in web_screening.privacy_links(careers_page.html, careers_page.final_url):
                    if cancelled():
                        break
                    privacy = fetcher.fetch_page(link)
                    if privacy is not None:
                        pages.append({"url": privacy.final_url, "html": privacy.html,
                                      "text": _visible(privacy.html)})
                        checked.append(privacy.final_url)
            statement_pages = list(pages)
            if scan is not None:
                for page in scan.pages:
                    if page["kind"] in ("privacy", "legal") and page["url"] not in checked:
                        statement_pages.append(page)
                        checked.append(page["url"])
            # The application link of an ATS is often only on the postings' pages (VEM
            # Sistemi: the careers page has no link, "Posizioni aperte" and each posting
            # link joblink.allibo.com): those pages count for the selection system too.
            posting_urls = [p.url for p in outcome.postings if getattr(p, "url", "")] if outcome is not None else \
                [p.get("url", "") for p in (current.hiring or {}).get("postings", []) if p.get("url")]
            platform_pages = pages + posting_pages(posting_urls, site, fetcher.fetch_page, checked, cancelled)
            platforms = web_screening.detect_platforms(platform_pages, postings_source, careers_url)
            statements = web_screening.scan_statements(statement_pages)
            report = web_screening.assess(platforms, statements, checked)
            vendors = []
            for hit in platforms:
                vendor = web_screening.ATS_KNOWLEDGE.get(hit.key)
                entry = {"key": hit.key, "name": hit.name, "url": hit.url}
                if vendor is not None:
                    entry.update(offer=vendor.offer_text(), parsing=vendor.parsing, ranking=vendor.ranking,
                                 ai=vendor.ai, ai_note=vendor.ai_note, sources=list(vendor.sources),
                                 evidence=vendor.evidence, reviewed=vendor.reviewed)
                vendors.append(entry)
            ats = {"platforms": vendors, "careers_url": careers_url or "", "date": _today()}
            screening = report.to_dict()
            screening["date"] = _today()
            now = time.time()
            changes = {}
            if wanted & {"ats"}:
                changes.update(ats=ats, ats_checked=now)
            if wanted & {"screening"}:
                changes.update(screening=screening, screening_checked=now)
            if changes:
                store.card_update(card.id, changes)

        # 8. products and services (home page, from the cache)
        if "products" in steps and not cancelled():
            if not step("products"):
                return get(store, card.id)
            home = fetcher.fetch_page(site)
            if home is not None:
                found = web_products.extract_products(home.html, home.final_url, home.text)
                found["date"] = _today()
                store.card_update(card.id, {"products": found, "products_checked": time.time()})
    finally:
        if own_gateway is not None:
            own_gateway.close()
    if progress is not None:
        try:
            progress("done", total, total)
        except Exception:
            pass
    log.info("Card build %s: %s in %.1f s", "cancelled" if cancelled() else "finished", label,
             time.monotonic() - started)
    return get(store, card.id)


def build_cards(store: Store, card_ids: list[int], fetcher, settings, *, cancel: threading.Event | None = None,
                progress: Callable[[int, int, str, str], None] | None = None, registry=None, gateway=None,
                sections=None) -> list[CompanyCard]:
    """Builds several cards one after the other (polite: never in parallel), for "Create
    full card" on the selected cards or on all favourites. `progress(index, count, name,
    step)`. A cancel stops after the current step; the cards done so far are kept. A card
    that fails is logged and skipped."""
    built: list[CompanyCard] = []
    count = len(card_ids)
    for index, card_id in enumerate(card_ids):
        if cancel is not None and cancel.is_set():
            break
        card = get(store, card_id)
        if card is None:
            continue

        def relay(key: str, _done: int, _total: int, i=index, name=card.name) -> None:
            if progress is not None:
                progress(i, count, name, key)

        try:
            built.append(build_card(store, card_id, fetcher, settings, cancel=cancel, progress=relay,
                                    registry=registry, gateway=gateway, sections=sections))
        except Exception:  # noqa: BLE001 - one bad site must not stop the others
            log.warning("Card build failed for card %s (%s)", card_id, card.name, exc_info=True)
    log.info("Batch card build: %d of %d cards done%s", len(built), count,
             " (cancelled)" if cancel is not None and cancel.is_set() else "")
    return built


def _visible(html: str) -> str:
    from .web.text import visible_text

    return visible_text(html)


def refresh_card(store: Store, card_id: int, fetcher, settings, section: str, *,
                 cancel: threading.Event | None = None, progress: Progress | None = None,
                 outcome=None, registry=None, gateway=None) -> CompanyCard:
    """Rebuilds one section: "identity" (= header + registry data), "profile", "people",
    "financials", "hiring", "products", "ats" or "screening"."""
    if section == "registry" or section == "header":
        section = "identity"
    if section not in REFRESH_SECTIONS:
        raise ValueError(f"Unknown card section: {section!r}")
    return build_card(store, card_id, fetcher, settings, cancel=cancel, progress=progress,
                      outcome=outcome, registry=registry, gateway=gateway, sections={section})


# --- contacts, Registro Imprese by hand, paid provider, financials by hand --------------------


def delete_contact(store: Store, card_id: int, contact_id: str) -> CompanyCard:
    """Removes a contact from the card; a later build does not add it again."""
    with store.locked():
        row = store.card_read(card_id)
        if row is None:
            raise KeyError(card_id)
        people = dict(row.get("people") or {})
        items = [c for c in people.get("items", []) if isinstance(c, dict) and c.get("id") != contact_id]
        deleted = list(people.get("deleted") or [])
        if contact_id not in deleted:
            deleted.append(contact_id)
        people.update(items=items, deleted=deleted)
        store.card_update(card_id, {"people": people})
    log.info("Contact deleted from card %s", card_id)
    return get(store, card_id)


def registro_imprese_query(card: CompanyCard) -> str:
    """What to type in the free search of registroimprese.it: the codice fiscale when known
    (exact), else the legal name, else the card name."""
    return card.tax_code or card.vat or card.legal_name or strip_legal_form(card.name) or card.name


def apply_registro_imprese(store: Store, card_id: int, raw: dict, *, when: str | None = None
                           ) -> tuple[CompanyCard, dict[str, str]]:
    """Values the user copied from the free Registro Imprese search (ATECO, PEC, REA,
    codice fiscale, legal form, registered office). Validated: checksums, REA format,
    ATECO against the bundled ISTAT ATECO 2025 table; stored with source "register",
    shown as "Registro Imprese (entered by hand)" with the date."""
    from .discovery.ateco import describe
    from .registry.visura import clean_fields

    values = {k: v for k, v in (raw or {}).items() if k in REGISTRO_FORM_FIELDS or k == "rea_province"}
    clean, errors = clean_fields(values)
    if clean.get("ateco") and describe(clean["ateco"]) is None:
        errors["ateco"] = "ATECO: not a code of the ATECO 2025 table (e.g. 62.10.00)."
        clean.pop("ateco")
    if clean.get("pec") and not re.fullmatch(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", clean["pec"]):
        errors["pec"] = "PEC: an e-mail address."
        clean.pop("pec")
    facts = [_fact(name, value, "register", REGISTRO_IMPRESE_URL, when) | {"where": REGISTRO_IMPRESE_VIA}
             for name, value in clean.items()]
    if facts:
        _add_register_facts(store, card_id, facts)
    log.info("Registro Imprese data entered by hand on card %s: %s", card_id, ", ".join(sorted(clean)) or "none")
    return get(store, card_id), errors


def _add_register_facts(store: Store, card_id: int, facts: list[dict]) -> None:
    with store.locked():
        row = store.card_read(card_id)
        if row is None:
            raise KeyError(card_id)
        changed = {f["field"] for f in facts}
        old = [f for f in row.get("provenance") or []
               if not (f.get("source") == "register" and f.get("field") in changed)]
        store.card_update(card_id, {"provenance": merge_facts(old, facts), "registry_checked": time.time()})
        _recompute(store, card_id)


def fetch_paid(store: Store, card_id: int, settings, *, client=None) -> tuple[CompanyCard, dict[str, str]]:
    """ONE paid lookup (registry/paid.py) for a card, ONLY after the user confirmed the cost
    shown by `paid.estimate`. Register fields are validated and stored with source
    "register" ("Openapi IT-full (paid)"); the latest revenue/employees go to the card's
    financial series (scope "separate"). Raises paid.PaidRegistryUnavailable."""
    from .financials.openapi import parse_full
    from .registry import paid
    from .registry.visura import clean_fields

    card = get(store, card_id)
    if card is None:
        raise KeyError(card_id)
    raw, payload = paid.fetch_company(settings, vat=card.vat, tax_code=card.tax_code, client=client)
    clean, errors = clean_fields(raw)
    facts = [_fact(name, value, "register", paid.PRODUCT_URL) | {"where": paid.VIA} for name, value in clean.items()]
    if facts:
        _add_register_facts(store, card_id, facts)
    records = parse_full(payload)
    if records:
        from .financials import FinancialSeries

        series = get(store, card_id).financial_series()
        extra = FinancialSeries()
        for record in records:
            extra.add(record)
        series.merge(extra)
        stored = dict(get(store, card_id).financials or {})
        stored.update(series=series.to_dict(), date=_today())
        store.card_update(card_id, {"financials": stored, "financials_checked": time.time()})
    log.info("Paid register data stored on card %s: %d fields, %d financial years", card_id, len(clean),
             len(records))
    return get(store, card_id), errors


def _save_series(store: Store, card_id: int, series) -> CompanyCard:
    with store.locked():
        row = store.card_read(card_id)
        if row is None:
            raise KeyError(card_id)
        stored = dict(row.get("financials") or {})
        stored.update(series=series.to_dict(), date=_today())
        store.card_update(card_id, {"financials": stored, "financials_checked": time.time()})
    return get(store, card_id)


def add_financial_year(store: Store, card_id: int, year: int, *, revenue=None, profit=None, employees=None,
                       scope: str = "separate", currency: str = "EUR", note: str = "") -> CompanyCard:
    """Figures of one year typed by the user (e.g. from a bilancio bought on Registro
    Imprese); they win over the free sources (financials/model.py authority)."""
    from .financials import add_value

    card = get(store, card_id)
    if card is None:
        raise KeyError(card_id)
    series = card.financial_series()
    add_value(series, int(year), revenue=revenue, profit=profit, employees=employees, scope=scope,
              currency=currency, note=note)
    return _save_series(store, card_id, series)


def add_pasted_years(store: Store, card_id: int, rows: list[dict], *, source: str, currency: str = "EUR",
                     scope: str = "separate") -> CompanyCard:
    """Figures the user pasted from a company-information page, reviewed and confirmed in
    "Paste figures…" (gui/financials_paste.py). `rows`: [{"year", "revenue"/"profit"/
    "employees": financials.paste.Figure | None}]; a range stays a range (FinancialYear.ranges,
    the amount None). Stored with source "pasted" (financials/model.py authority) and the
    source the user stated in the note. A new paste of the same year replaces the old one."""
    from .financials import add_value
    from .financials.paste import SOURCE as PASTED_SOURCE

    card = get(store, card_id)
    if card is None:
        raise KeyError(card_id)
    series = card.financial_series()
    stated = " ".join(str(source or "").split())[:200]
    for row in rows:
        amounts, ranges = {}, {}
        for name in ("revenue", "profit", "employees"):
            figure = row.get(name)
            if figure is None:
                continue
            if figure.is_range:
                ranges[name] = [figure.low, figure.high]
            else:
                amounts[name] = float(figure.low)
        if not amounts and not ranges:
            continue
        add_value(series, int(row["year"]), scope=scope, currency=currency, source=PASTED_SOURCE,
                  note=f"source: {stated}" if stated else "", ranges=ranges, **amounts)
    log.info("Pasted financial figures stored on card %s: %d year(s)", card_id, len(rows))
    return _save_series(store, card_id, series)


def import_financials_csv(store: Store, card_id: int, text: str, *, scope: str = "separate",
                          multiplier: float = 1.0, filename: str = "") -> tuple[CompanyCard, list[str]]:
    """A balance-sheet CSV (financials/manual.py format); returns the card and the rows
    that could not be read."""
    from .financials import parse_csv

    card = get(store, card_id)
    if card is None:
        raise KeyError(card_id)
    result = parse_csv(text, default_scope=scope, multiplier=multiplier,
                       note=f"imported from {filename}" if filename else "imported CSV")
    if result.series.empty:
        return card, result.errors or ["no data"]
    series = card.financial_series()
    series.merge(result.series)
    return _save_series(store, card_id, series), result.errors


# --- workplace and LinkedIn -----------------------------------------------------------

# Legal forms removed from the end of a company name (case-insensitive, dots and spaces
# between the letters optional). Abbreviations are written as letter groups: "s r l"
# matches "srl", "s.r.l.", "S. r. l." and "S.R.L"; long forms as words.
_LEGAL_FORM_WORDS = (
    "società a responsabilità limitata semplificata", "societa a responsabilita limitata semplificata",
    "società a responsabilità limitata", "societa a responsabilita limitata",
    "società per azioni", "societa per azioni", "società in accomandita per azioni",
    "societa in accomandita per azioni", "società in accomandita semplice", "societa in accomandita semplice",
    "società in nome collettivo", "societa in nome collettivo", "società consortile a responsabilità limitata",
    "societa consortile a responsabilita limitata", "società cooperativa sociale", "societa cooperativa sociale",
    "società cooperativa", "societa cooperativa", "società semplice", "societa semplice",
    "società benefit", "societa benefit", "cooperativa sociale", "unipersonale", "in liquidazione",
    "limited", "incorporated", "corporation", "company",
)
_LEGAL_FORM_LETTERS = (
    "s r l s", "s r l", "s p a", "s a p a", "s a s", "s n c", "s c a r l", "s c r l", "s c p a", "s s",
    "s c", "s a", "s l", "s a r l", "b v", "n v", "e k", "a g", "s e",
)
_LEGAL_FORM_PLAIN = ("srls", "srl", "spa", "sapa", "sas", "snc", "scarl", "scrl", "scpa", "ltd", "llc", "inc",
                     "corp", "co", "gmbh", "ag", "kg", "ohg", "plc", "sarl", "bv", "nv", "sa", "sl", "onlus",
                     "soc coop", "soc. coop", "coop", "cooperativa")


def _legal_form_pattern() -> re.Pattern:
    alternatives = [r"\s+".join(re.escape(w) for w in form.split()) for form in _LEGAL_FORM_WORDS]
    alternatives += [r"\.?\s*".join(form.split()) + r"\.?" for form in _LEGAL_FORM_LETTERS]
    alternatives += [r"\.?\s*".join(re.escape(w.rstrip(".")) for w in form.split()) + r"\.?"
                     for form in _LEGAL_FORM_PLAIN]
    alternatives.sort(key=len, reverse=True)
    return re.compile(r"(?:^|[\s,;\-–(]+)(?:&\s*c\.?\s+)?(?:" + "|".join(alternatives) + r")\)?[\s.,;]*$",
                      re.IGNORECASE)


_LEGAL_FORM = _legal_form_pattern()
_TRAILING_CONNECTOR = re.compile(r"(?:[\s,;\-–]+|\s+&\s*c\.?|\s+&)$", re.IGNORECASE)
# "Beta s.a.s. di Mario Rossi & C.": the partners' names follow the legal form
_FORM_OF_PARTNERS = re.compile(r"\s+(?:s\.?\s*a\.?\s*s|s\.?\s*n\.?\s*c|s\.?\s*s)\.?\s+di\s+.*$", re.IGNORECASE)


def strip_legal_form(name: str) -> str:
    """The company name without its legal form at the end ("Top Automazioni S.r.l." ->
    "Top Automazioni"; "Alfa Società per Azioni" -> "Alfa"; "Beta srl unipersonale" ->
    "Beta"). Never returns an empty name: a name that is only a legal form is kept."""
    original = " ".join((name or "").split())
    text = _FORM_OF_PARTNERS.sub("", original).strip() or original
    for _ in range(4):  # "Srl Unipersonale", "S.p.A. Società Benefit"
        stripped = _TRAILING_CONNECTOR.sub("", _LEGAL_FORM.sub("", text)).strip()
        if stripped == text or not stripped:
            break
        text = stripped
    return text or original


LINKEDIN_COMPANY_SEARCH = "https://www.linkedin.com/search/results/companies/?keywords="


def linkedin_search_url(name: str) -> str:
    """LinkedIn's company search for `name` without its legal form (only opened in the
    browser: the app never reads LinkedIn)."""
    from urllib.parse import quote

    return LINKEDIN_COMPANY_SEARCH + quote(strip_legal_form(name), safe="")


def _fact_value(facts: list[dict], name: str) -> str:
    for fact in facts or []:
        if fact.get("field") == name and fact.get("value"):
            return str(fact["value"]).strip()
    return ""


def _town_text(town: str, province: str) -> str:
    town, province = " ".join((town or "").split()), " ".join((province or "").split())
    if town and province and province.casefold() not in town.casefold():
        return f"{town} ({province})"
    return town or province


def _posting_location(postings) -> str:
    for posting in list(postings or [])[:1]:  # the best posting only
        location = posting.get("location") if isinstance(posting, dict) else getattr(posting, "location", None)
        if location and str(location).strip():
            return " ".join(str(location).split())
    return ""


def workplace(card: CompanyCard | None = None, *, company: Company | None = None,
              assessment: Assessment | None = None) -> str:
    """Where the job is, in one format for every list: "Castel Maggiore (BO)" in Italy,
    "Lugano, Svizzera" abroad (geo/places.py), else the text as found, else ""."""
    raw = workplace_raw(card, company=company, assessment=assessment)
    try:
        from .geo.places import location_of_company

        found = location_of_company(company, card=card, assessment=assessment)
        if found.known and (raw or found.provenance != "coordinates"):
            return found.display()
    except Exception:  # noqa: BLE001 - the raw text is still right
        log.debug("Workplace not normalised", exc_info=True)
    return raw


def _address_town(address: str) -> tuple[str, str]:
    """(town, province code) of an Italian address with a postcode, else ("", "")."""
    import re as _re

    match = _re.search(r"(?<!\d)(\d{5})\s+([^,\d][^,]*)", address or "")
    if not match:
        return "", ""
    from .registers.italy_codes import province_from_postcode

    return match.group(2).strip().rstrip("."), province_from_postcode(match.group(1)) or ""


def workplace_raw(card: CompanyCard | None = None, *, company: Company | None = None,
                  assessment: Assessment | None = None) -> str:
    """Where the job is, as found: the location of the best posting when it states one,
    else the company's town (and province) from the card or the discovery data, else ""."""
    postings = assessment.postings if assessment is not None else None
    if not postings and card is not None:
        evaluation = card.assessment_obj() if card.assessment else None
        postings = evaluation.postings if evaluation is not None else card.postings
    place = _posting_location(postings)
    if place:
        return place
    if card is not None:
        # the operating site of the card's address ("Via …, 47521 Cesena") before the
        # registered office of the registers (a Cesena company owned by a Rome group was
        # shown in "Roma (RM)" in the list of companies for a spontaneous application)
        site_town, site_province = _address_town(getattr(card, "address", "") or "")
        if site_town:
            return _town_text(site_town, site_province)
        town = card.registry_value("town") or _fact_value(card.provenance, "town") \
            or card.osm_tags.get("addr:city", "")
        province = card.registry_value("province") or _fact_value(card.provenance, "province") \
            or card.osm_tags.get("addr:province", "")
        if town or province:
            return _town_text(town, province)
    if company is not None:
        fields = company.fields or {}
        town = fields.get("town") or _fact_value(company.sources, "town") or company.osm_tags.get("addr:city", "")
        province = fields.get("province") or _fact_value(company.sources, "province") \
            or company.osm_tags.get("addr:province", "")
        return _town_text(str(town or ""), str(province or ""))
    return ""


# --- export ---------------------------------------------------------------------------


def _default_tr(key: str, default: str, **params) -> str:
    return default.format(**params) if params else default


def card_text(card: CompanyCard, tr: Callable[..., str] | None = None, *, markdown: bool = True) -> str:
    """The whole card as Markdown (or plain text), sections in the owner's order.

    `tr(key, english_default, **params)` translates the labels (the GUI passes its
    i18n helper); without it the labels are English.
    """
    tr = tr or _default_tr
    out: list[str] = []
    h = (lambda s: f"## {s}") if markdown else (lambda s: s.upper())
    bullet = "- " if markdown else "  "
    blank = "—"

    def line(label: str, value: str, url: str = "") -> None:
        text = value or blank
        if url and value:
            text = f"{text} ({url})"
        out.append(f"{bullet}{label}: {text}")

    out.append(f"# {card.name}" if markdown else card.name)
    status = card.identity_status or confirm.UNVERIFIED
    out.append(tr("card.identity_line", "Identity: {status}",
                  status=tr(f"card.status.{status}", status.upper())))
    out.append("")
    out.append(h(tr("card.section.header", "1. Header")))
    def hint(name: str) -> str:
        if name not in FIELD_HINTS:
            return ""
        return f"{tr(f'card.hint.{name}', FIELD_HINTS[name])}; {tr('card.hint_paid', PAID_KEY_HINT)}"

    def field_line(label: str, name: str, value: str, url: str = "") -> None:
        if value:
            line(label, value, url)
        else:
            extra = hint(name)
            out.append(f"{bullet}{label}: {blank}" + (f" ({extra})" if extra else ""))

    for name, key, default in (("legal_name", "card.field.legal_name", "Legal name"),
                               ("rea", "card.field.rea", "REA"), ("ateco", "card.field.ateco", "ATECO"),
                               ("tax_code", "card.field.tax_code", "Tax code"),
                               ("vat", "card.field.vat", "VAT number")):
        fact = card.source_of(name)
        field_line(tr(key, default), name, card.value_of(name),
                   (fact or {}).get("url", "") if card.value_of(name) else "")
    out.append("")
    out.append(h(tr("card.section.registry", "2. Registry data")))
    line(tr("card.field.website", "Website"), card.website or card.site or "")
    for name, default in (("legal_form", "Legal form"), ("registered_office", "Registered office"),
                          ("operating_site", "Operating sites"), ("town", "Town"), ("province", "Province"),
                          ("postcode", "Postcode"), ("pec", "PEC"), ("phone", "Phone"), ("email", "E-mail"),
                          ("share_capital", "Share capital"), ("founded", "Founded"),
                          ("employees", "Size hint (employees)"), ("lei", "LEI"),
                          ("parent_company", "Directed and coordinated by"), ("linkedin", "LinkedIn page")):
        item = (card.registry or {}).get(name)
        url = item.get("url", "") if isinstance(item, dict) else ""
        value = card.registry_value(name)
        if not value and name in ("parent_company", "linkedin"):
            continue
        field_line(tr(f"card.field.{name}", default), name, value, url)
    for name, values in ((card.identity or {}).get("field_conflicts") or {}).items():
        out.append(f"{bullet}" + tr("card.field_conflict", "Sources differ for {field}: {values}",
                                    field=tr(f"card.field.{name}", name),
                                    values=", ".join(f"{v.get('value')} ({v.get('source')})" for v in values)))
    missing = card.missing_paid
    if missing:
        out.append(tr("card.missing_paid", "Not found in the free automatic sources: {fields}. Each blank field "
                      "says where to find it for free by hand.",
                      fields=", ".join(tr(f"card.field.{m}", m) for m in missing)))
    out.append("")
    out.append(h(tr("card.section.hiring", "3. Profiles sought")))
    hiring = card.hiring or {}
    line(tr("card.field.careers", "Careers page"), hiring.get("careers_url") or card.careers_url or "")
    for posting in card.postings[:MAX_POSTINGS_KEPT]:
        attrs = ", ".join(str(posting.get(k)) for k in ("contract", "remote", "salary", "travel") if posting.get(k))
        out.append(f"{bullet}{posting.get('title', '')}" + (f" ({attrs})" if attrs else "")
                   + (f" {posting.get('url')}" if posting.get("url") else ""))
    if hiring.get("spontaneous"):
        out.append(f"{bullet}" + tr("card.spontaneous", "Spontaneous applications accepted"))
    if hiring.get("date"):
        out.append(f"{bullet}" + tr("card.checked_on", "Checked on {date}", date=hiring["date"]))
    out.append("")
    out.append(h(tr("card.section.products", "4. Products and services")))
    products = card.products or {}
    if products.get("summary"):
        out.append(f"{products['summary']} ({products.get('summary_url', '')})")
    for item in products.get("items", []):
        out.append(f"{bullet}{item.get('name', '')} {item.get('url', '')}")
    out.append("")
    out.append(h(tr("card.section.ats", "5. Selection system")))
    platforms = (card.ats or {}).get("platforms", [])
    if not platforms:
        out.append(f"{bullet}" + tr("card.ats_none", "No applicant-tracking system detected."))
    for platform in platforms:
        out.append(f"{bullet}{platform.get('name', '')}: {platform.get('offer') or ''} {platform.get('url', '')}".rstrip())
        if platform.get("sources"):
            out.append(f"{bullet}{bullet}" + tr("card.vendor_sources", "Vendor sources (reviewed {date}): {urls}",
                                                date=platform.get("reviewed", ""),
                                                urls=", ".join(platform["sources"])))
    out.append("")
    out.append(h(tr("card.section.screening", "6. CV screening")))
    screening = card.screening or {}
    if screening:
        out.append(f"{bullet}" + tr("card.parsing", "Automatic parsing: {value}",
                                    value=tr(f"card.parsing.{screening.get('parsing', 'unknown')}",
                                             screening.get("parsing", "unknown"))))
        out.append(f"{bullet}" + tr("card.ai", "AI screening: {value}",
                                    value=tr(f"card.ai.{screening.get('ai', 'no_evidence')}",
                                             screening.get("ai", "no_evidence").replace("_", " "))))
        for statement in screening.get("statements", []):
            out.append(f"{bullet}“{statement.get('quote', '')}” ({statement.get('url', '')})")
        from .web.screening import ADVICE_TEXTS

        for key in screening.get("advice", []):
            out.append(f"{bullet}" + tr(f"card.advice.{key}", ADVICE_TEXTS.get(key, key)))
    else:
        out.append(f"{bullet}" + tr("card.not_built", "Not checked yet: use “Create full card”."))
    out.append("")
    out.append(h(tr("card.section.profile", "7. Presentation and history")))
    for key, default, entry in (("card.presentation", "Presentation", card.presentation),
                                ("card.history", "History", card.history)):
        if entry.get("text"):
            out.append(f"{tr(key, default)}: {entry['text']} ({entry.get('url', '')})")
        else:
            out.append(f"{bullet}{tr(key, default)}: {blank}")
    out.append("")
    out.append(h(tr("card.section.people", "8. People and contacts")))
    contacts = card.contacts_list
    if not contacts:
        out.append(f"{bullet}{blank}")
    for contact in contacts:
        kind = tr(f"card.contact.kind.{contact.get('kind', 'general')}", contact.get("kind", ""))
        parts = [x for x in (contact.get("name"), contact.get("role"), contact.get("email"), contact.get("phone")) if x]
        out.append(f"{bullet}[{kind}] " + " - ".join(parts) + (f" ({contact.get('source_url')})"
                                                              if contact.get("source_url") else ""))
    out.append("")
    out.append(h(tr("card.section.financials", "9. Financials")))
    series = card.financial_series()
    if series.empty:
        why = card.financial_result().why_missing(lambda key, default: tr(key, default)) if \
            (card.financials or {}).get("reports") else ""
        out.append(f"{bullet}" + (why.replace("\n", " ") or blank))
    else:
        for record in series.records_sorted():
            figures = [f"{name}: {getattr(record, name):,.0f} {record.currency if name != 'employees' else ''}".strip()
                       for name in ("revenue", "profit", "employees") if getattr(record, name) is not None]
            out.append(f"{bullet}{record.year} ({record.scope}, {record.source}): " + "; ".join(figures)
                       + (f" ({record.source_url})" if record.source_url else ""))
    return "\n".join(out).rstrip() + "\n"


def card_to_dict(card: CompanyCard, *, personal: bool = False) -> dict:
    """JSON view of a card for the MCP server: company data only. The user's own fields
    (notes, contacts, tags, status) are never included; a 16-character codice fiscale
    (a person: sole trader) and e-mail addresses that look personal are left out unless
    `personal` is True."""
    def keep_email(value: str) -> bool:
        local = (value or "").split("@")[0].lower()  # "mario.rossi@" looks like a person
        return personal or not re.fullmatch(r"[a-z]+[._][a-z]+\d*", local)

    registry = {}
    for name, item in (card.registry or {}).items():
        if isinstance(item, dict):
            if name == "email" and not keep_email(item.get("value", "")):
                continue
            registry[name] = {"value": item.get("value"), "source": item.get("source"), "url": item.get("url")}
        elif isinstance(item, list):
            registry[name] = [{"value": i.get("value"), "source": i.get("source"), "url": i.get("url")}
                              for i in item if isinstance(i, dict)]
    tax_code = card.tax_code if (personal or not is_personal_cf(card.tax_code)) else ""
    provenance = [f for f in card.provenance
                  if personal or not (f.get("field") == "tax_code" and is_personal_cf(str(f.get("value", ""))))
                  and not (f.get("field") == "email" and not keep_email(str(f.get("value", ""))))]
    identity = card.identity or {}
    people = [c for c in card.contacts_list
              if personal or (not c.get("name") and keep_email(c.get("email", "")))]
    return {
        "id": card.id,
        "name": card.name,
        "website": card.website or card.site,
        "header": {"legal_name": card.legal_name, "rea": card.rea, "ateco": card.ateco,
                   "tax_code": tax_code, "vat": card.vat},
        "identity": {"status": card.identity_status, "reasons": identity.get("reasons", []),
                     "agreements": identity.get("agreements", []), "conflicts": identity.get("conflicts", []),
                     "missing_from_free_sources": card.missing_paid,
                     "vies": identity.get("vies"), "gleif": identity.get("gleif")},
        "registry": registry,
        "profiles_sought": {k: v for k, v in (card.hiring or {}).items()},
        "products": card.products or {},
        "selection_system": card.ats or {},
        "cv_screening": card.screening or {},
        "presentation": card.presentation,
        "history": card.history,
        "contacts": [{k: c.get(k) for k in ("name", "role", "email", "phone", "kind", "source_url")} for c in people],
        "financials": card.financial_series().to_dict(),
        "field_conflicts": identity.get("field_conflicts", {}),
        "provenance": provenance,
        "checked": {k: getattr(card, f"{k}_checked") for k in
                    ("identity", "registry", "hiring", "products", "ats", "screening", "profile", "people",
                     "financials")},
        "untrusted": True,
    }
