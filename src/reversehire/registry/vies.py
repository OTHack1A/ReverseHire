"""VIES (EU VAT Information Exchange System) REST lookup: official name and address of an
Italian partita IVA.

Endpoint verified in the sources study (2026-11-17):
GET https://ec.europa.eu/taxation_customs/vies/rest-api/ms/IT/vat/{11 digits}
-> {"isValid", "requestDate", "userError", "name", "address", "vatNumber", ...}.

`isValid: false` means only that the number is not enabled for intra-EU operations
(many small Italian companies are not): it is "unknown to VIES", never "fake".
No REA, no ATECO. Free, no key; answers cached 30 days by RegistryClient.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .checksums import normalize_vat, piva_ok
from .http import RegistryClient, RegistryError

VIES_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{country}/vat/{vat}"
SOURCE = "vies"
# userError values meaning "the service could not answer now": not cached, not "unknown".
_TEMPORARY = {"MS_UNAVAILABLE", "MS_MAX_CONCURRENT_REQ", "SERVICE_UNAVAILABLE", "TIMEOUT",
              "GLOBAL_MAX_CONCURRENT_REQ", "SYSTEM_ERROR"}


@dataclass
class ViesResult:
    vat: str
    state: str  # "valid" | "not_registered" | "unavailable" | "invalid_number"
    name: str = ""
    address: str = ""
    street: str = ""
    postcode: str = ""
    town: str = ""
    province: str = ""
    url: str = ""
    date: str = field(default_factory=lambda: date.today().isoformat())
    detail: str = ""

    @property
    def valid(self) -> bool:
        return self.state == "valid"

    def facts(self) -> list[dict]:
        """Provenance facts (field, value, source, url, date) of a valid answer."""
        if not self.valid:
            return []
        values = [("vat", self.vat), ("name", self.name), ("registered_office", self.street),
                  ("postcode", self.postcode), ("town", self.town), ("province", self.province)]
        return [{"field": f, "value": v, "source": SOURCE, "url": self.url, "date": self.date}
                for f, v in values if v]


def parse_address(text: str) -> dict[str, str]:
    """'VIA EMILIA EST 1163 \\n41122 MODENA MO\\n' -> street, postcode, town, province."""
    lines = [" ".join(line.split()) for line in (text or "").replace("\r", "\n").split("\n")]
    lines = [line for line in lines if line and line != "---"]
    result = {"street": "", "postcode": "", "town": "", "province": ""}
    for i, line in enumerate(lines):
        found = re.match(r"^(\d{5})\s+(.+?)(?:\s+\(?([A-Z]{2})\)?)?$", line)
        if found:
            result["postcode"], result["town"] = found.group(1), found.group(2).strip()
            result["province"] = found.group(3) or ""
            result["street"] = ", ".join(lines[:i])
            return result
    result["street"] = ", ".join(lines)
    return result


def _cacheable(status: int, data) -> bool:
    if status != 200 or not isinstance(data, dict):
        return False
    return str(data.get("userError") or "").upper() not in _TEMPORARY


def lookup(client: RegistryClient, vat: str, country: str = "IT") -> ViesResult:
    """VIES answer for an Italian VAT number (never raises for "not registered").

    RegistryError is raised only for cancellation; network problems become state
    "unavailable" with the reason in `detail`.
    """
    number = normalize_vat(vat)
    url = VIES_URL.format(country=country, vat=number)
    if not piva_ok(number):
        return ViesResult(number, "invalid_number", url=url)
    try:
        answer = client.get_json(url, cache=_cacheable)
    except RegistryError as error:
        if error.kind == "cancelled":
            raise
        return ViesResult(number, "unavailable", url=url, detail=error.kind)
    data = answer.data if isinstance(answer.data, dict) else {}
    if answer.status != 200 or not data:
        return ViesResult(number, "unavailable", url=url, detail=f"HTTP {answer.status}")
    user_error = str(data.get("userError") or "").upper()
    if user_error in _TEMPORARY:
        return ViesResult(number, "unavailable", url=url, detail=user_error)
    when = str(data.get("requestDate") or "")[:10] or date.today().isoformat()
    if not data.get("isValid"):
        return ViesResult(number, "not_registered", url=url, date=when, detail=user_error)
    # The Italian register sends "name !!another name" (e.g. "VEM SISTEMI S.P.A. !!VEM
    # SISTEMI S.P", a truncated second denomination): the legal name is the first part.
    name = " ".join(str(data.get("name") or "").split("!!")[0].split())
    address = str(data.get("address") or "")
    parts = parse_address(address)
    return ViesResult(number, "valid", name="" if name == "---" else name,
                      address=" ".join(address.split()), url=url, date=when, **parts)
