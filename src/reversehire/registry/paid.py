"""Paid company-register providers. ONE is implemented: Openapi (company.openapi.com).

!!! THE OPENAPI ADAPTER IS UNTESTED AGAINST THE LIVE SERVICE !!!  No key exists yet: it was
written from the documented response fields in fonti/COMPANY_DATA_STUDY.md (section A.3)
and is unit-tested with FABRICATED answers only. Before relying on it, make one real call
with a real key and compare the answer with EXPECTED_SHAPE.

Rules (owner's): a paid call is made ONLY when the user has typed his own key in Settings
(`paid_registry_provider` = "openapi" / "openapi.it" / "openapi.com", `paid_registry_key`)
AND presses "Fetch from paid register" on a card AND confirms the cost shown before the
call (`estimate`). Never in tests, never automatically, never for many cards at once. The
key is sent only in the Authorization header to company.openapi.com, never logged, never
cached, never shown (repr hides it).

Endpoint: `GET https://company.openapi.com/IT-full/{vat_or_tax_code}` (Bearer key), the
same product as financials/openapi.py, so ONE call fills the registry fields AND the latest
revenue/employees: price 0.30 EUR single call, from about 0.08 EUR on plans (excl. VAT,
openapi.com pricing page read 2026-11-17). Documented fields read here: `companyName`,
`vatCode`, `taxCode`, `companyDetails.reaCode` (+ `cciaa`), `companyDetails.legalForm`,
`pec`, `atecoClassification` (ATECO 2025 code), `address.registeredOffice`, `ecofin.
shareCapital`, `employees.employee`, `companyDates.incorporationDate`, `webAndSocial.
website`; the answer may wrap the company in `data` (list or dict). Asynchronous answers
(an `id` to poll) are reported as an error, not polled.

Values are validated like hand-entered register data (checksums, REA and ATECO format) and
stored with source "register" (R in the identity rule), shown as "Openapi (paid)".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote

log = logging.getLogger(__name__)

UNTESTED_AGAINST_LIVE_SERVICE = True
BASE = "https://company.openapi.com"
ENDPOINT = "IT-full"
PRODUCT_URL = "https://openapi.com/products/italian-full-company"
PRICE_SINGLE_EUR = 0.30
PRICE_FROM_EUR = 0.08
VIA = "Openapi IT-full (paid)"
EXPECTED_SHAPE = {"data": [{
    "companyName": "", "vatCode": "", "taxCode": "",
    "companyDetails": {"reaCode": "", "cciaa": "", "legalForm": ""},
    "pec": "", "atecoClassification": {"ateco": {"code": "", "description": ""}},
    "address": {"registeredOffice": {"streetName": "", "zipCode": "", "town": "", "province": ""}},
    "ecofin": {"shareCapital": 0, "turnover": 0, "turnoverYear": 0},
    "employees": {"employee": 0}, "companyDates": {"incorporationDate": ""},
    "webAndSocial": {"website": ""},
}]}


class PaidRegistryUnavailable(Exception):
    """No paid lookup is possible (not configured, no key, refused, unreadable answer)."""

    def __init__(self, message: str, kind: str = "unavailable"):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class PaidProvider:
    id: str
    name: str
    offer: str
    cost_note: str
    fields: tuple[str, ...]
    url: str
    access: str  # "api_key", "contract", "manual"
    implemented: bool = False
    aliases: tuple[str, ...] = ()


# Fields a register lookup would add to a card (all optional in the answers).
REGISTER_FIELDS = ("vat", "tax_code", "rea", "ateco", "legal_form", "pec", "registered_office",
                   "share_capital", "employees", "founded", "status")

PROVIDERS: dict[str, PaidProvider] = {p.id: p for p in (
    PaidProvider(
        "openapi_it", "Openapi - Company IT-full",
        "Company data by partita IVA or codice fiscale: REA, ATECO, PEC, legal form, registered office, "
        "share capital, employees, incorporation date and the latest revenue.",
        f"{PRICE_SINGLE_EUR:.2f} EUR per company (from about {PRICE_FROM_EUR:.2f} EUR with a plan), excl. VAT",
        ("vat", "tax_code", "rea", "ateco", "legal_form", "pec", "registered_office",
         "share_capital", "employees", "founded"),
        PRODUCT_URL, "api_key", implemented=True,
        aliases=("openapi", "openapi.it", "openapi.com", "company.openapi.com")),
    PaidProvider(
        "registro_imprese_visura", "Registro Imprese - visura (InfoCamere)",
        "Official per-company extract, bought by hand on registroimprese.it.",
        "5.00 EUR per visura ordinaria of a capital company",
        REGISTER_FIELDS, "https://www.registroimprese.it", "manual"),
    PaidProvider(
        "registro_imprese_elenco", "Registro Imprese - Elenco Esteso Imprese (Telemaco)",
        "CSV list by ATECO and territory with CF, REA, ATECO, employees, share capital.",
        "5 EUR + 0.02-0.12 EUR per row",
        REGISTER_FIELDS, "https://www.registroimprese.it/elenchi-di-imprese", "manual"),
    PaidProvider(
        "atoka", "Cerved / Atoka (SpazioDati)",
        "Company search by ATECO and area with websites, contacts and financials.",
        "commercial licence, price on request",
        ("vat", "tax_code", "rea", "ateco", "legal_form", "pec", "employees", "share_capital"),
        "https://atoka.io", "contract"),
)}


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def configured_provider(settings) -> PaidProvider | None:
    """The provider named in `settings.paid_registry_provider` (id, alias or name)."""
    wanted = _clean(getattr(settings, "paid_registry_provider", "")).casefold()
    if not wanted:
        return None
    for provider in PROVIDERS.values():
        names = (provider.id.casefold(), provider.name.casefold(), *provider.aliases)
        if wanted in names or wanted in provider.name.casefold():
            return provider
    return None


def has_key(settings) -> bool:
    return bool(_clean(getattr(settings, "paid_registry_key", "")))


def can_fetch(settings) -> bool:
    """True only when an implemented provider is configured with the user's own key."""
    provider = configured_provider(settings)
    return bool(provider and provider.implemented and (provider.access != "api_key" or has_key(settings)))


def estimate(settings) -> str:
    """The cost of ONE fetch, shown before the call (English; the GUI translates the frame)."""
    provider = configured_provider(settings)
    if provider is None or not provider.implemented:
        return ""
    return f"{provider.name}: {provider.cost_note}."


def cost_note(settings) -> str:
    """Short English note under the button: which provider, its cost, and whether it can run."""
    provider = configured_provider(settings)
    if provider is None:
        return ("No paid register configured (Settings). Openapi IT-full costs "
                f"{PRICE_SINGLE_EUR:.2f} EUR per company (from {PRICE_FROM_EUR:.2f} EUR with a plan); "
                "a Registro Imprese visura 5.00 EUR, bought by hand.")
    if not provider.implemented:
        return f"{provider.name}: {provider.cost_note}. Not automated: buy it by hand and enter the data."
    if not has_key(settings):
        return f"{provider.name}: {provider.cost_note}. Type your key in Settings to use it."
    return f"{provider.name}: {provider.cost_note}. Asked only when you press the button and confirm."


# --- the Openapi answer ----------------------------------------------------------------------


def _path(item, *keys):
    for key in keys:
        if not isinstance(item, dict):
            return None
        item = item.get(key)
    return item


def _first(item, *paths) -> str:
    for path in paths:
        value = _path(item, *path)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool) and _clean(value):
            return _clean(value)
    return ""


def company_of(payload) -> dict | None:
    """The single company of an answer (`data` list/dict or the bare object)."""
    item = payload
    if isinstance(item, dict) and "data" in item:
        item = item["data"]
    if isinstance(item, list):
        item = item[0] if len(item) == 1 else None
    return item if isinstance(item, dict) else None


def parse_company(payload) -> dict[str, str]:
    """Raw register fields of an IT-full answer (pure function; validated by the caller)."""
    item = company_of(payload)
    if item is None:
        return {}
    ateco = _first(item, ("atecoClassification", "ateco2025", "code"), ("atecoClassification", "ateco", "code"),
                   ("atecoClassification", "code"), ("ateco",))
    office = _path(item, "address", "registeredOffice") or {}
    street = _first(office, ("streetName",), ("street",), ("toponym",))
    number = _first(office, ("streetNumber",))
    if street and number:
        street = f"{street} {number}"
    capital = _path(item, "ecofin", "shareCapital")
    founded = _first(item, ("companyDates", "incorporationDate"), ("companyDates", "registrationDate"))
    raw = {
        "name": _first(item, ("companyName",), ("denomination",)),
        "vat": _first(item, ("vatCode",), ("vat",)),
        "tax_code": _first(item, ("taxCode",)),
        "rea": _first(item, ("companyDetails", "reaCode"), ("reaCode",)),
        "rea_province": _first(item, ("companyDetails", "cciaa"), ("cciaa",)),
        "legal_form": _first(item, ("companyDetails", "legalForm", "description"), ("companyDetails", "legalForm"),
                             ("legalForm",)),
        "pec": _first(item, ("pec",), ("companyDetails", "pec")),
        "ateco": ateco,
        "registered_office": street,
        "postcode": _first(office, ("zipCode",), ("postalCode",)),
        "town": _first(office, ("town",), ("municipality",)),
        "province": _first(office, ("province", "code"), ("province",)),
        "share_capital": f"EUR {capital:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        if isinstance(capital, (int, float)) and not isinstance(capital, bool) else _clean(capital or ""),
        "employees": _first(item, ("employees", "employee")),
        "founded": founded[:10],
        "website": _first(item, ("webAndSocial", "website")),
    }
    return {k: v for k, v in raw.items() if v}


def fetch_company(settings, *, vat: str = "", tax_code: str = "", client=None) -> tuple[dict[str, str], object]:
    """ONE paid IT-full call: (raw register fields, the whole answer). Raises
    PaidRegistryUnavailable when not configured or refused. `client` is an httpx.Client
    (tests pass a MockTransport one; the app creates and closes its own)."""
    import httpx

    provider = configured_provider(settings)
    if provider is None:
        raise PaidRegistryUnavailable("no paid register configured", "not_configured")
    if not provider.implemented:
        raise PaidRegistryUnavailable(f"{provider.name}: buy it by hand (not automated)", "not_configured")
    key = _clean(getattr(settings, "paid_registry_key", ""))
    if not key:
        raise PaidRegistryUnavailable("no key in Settings", "no_key")
    code = re.sub(r"\s", "", vat or "").upper().removeprefix("IT") or re.sub(r"\s", "", tax_code or "").upper()
    if not code:
        raise PaidRegistryUnavailable("no partita IVA or codice fiscale on the card", "no_identifier")
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False)
    url = f"{BASE}/{ENDPOINT}/{quote(code, safe='')}"
    try:
        log.info("Paid register: one %s call for %s", ENDPOINT, code)
        try:
            response = client.get(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
        except httpx.HTTPError as error:
            raise PaidRegistryUnavailable(f"network: {type(error).__name__}", "network") from error
        if response.status_code in (401, 403):
            raise PaidRegistryUnavailable(f"HTTP {response.status_code}: the provider refused the key", "unauthorized")
        if response.status_code == 404:
            raise PaidRegistryUnavailable("the provider has no record for this company", "not_found")
        if response.status_code == 402:
            raise PaidRegistryUnavailable("HTTP 402: no credit left on the provider account", "no_credit")
        if response.status_code != 200:
            raise PaidRegistryUnavailable(f"HTTP {response.status_code}", "http")
        try:
            payload = response.json()
        except ValueError as error:
            raise PaidRegistryUnavailable("the answer is not JSON", "bad_answer") from error
    finally:
        if own:
            client.close()
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict) and "id" in data and not company_of(payload).get("companyName"):
        raise PaidRegistryUnavailable("asynchronous answer (not supported)", "async")
    raw = parse_company(payload)
    if not raw:
        raise PaidRegistryUnavailable("the answer holds no company data", "bad_answer")
    return raw, payload


def fetch(settings, *, tax_code: str = "", vat: str = "", client=None) -> dict[str, str]:
    """Register fields of one company (see fetch_company)."""
    return fetch_company(settings, vat=vat, tax_code=tax_code, client=client)[0]


def today() -> str:
    return date.today().isoformat()
