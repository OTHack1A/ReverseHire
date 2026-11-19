"""Paid adapter: Openapi (company.openapi.com) "IT-full" - latest revenue and employees.

!!! UNTESTED AGAINST THE LIVE SERVICE !!!  No Openapi key exists yet, so this adapter was
written only from the documented response fields found in the research
(fonti/COMPANY_DATA_STUDY.md section A.3) and is unit-tested with a FABRICATED response.
Before relying on it, make one real call with a real key and compare (see EXPECTED_SHAPE).
The real answer may wrap the company in `data` (list or dict) or not; both are accepted.

What it reads (documented): `ecofin.turnover` (revenue), `ecofin.turnoverYear` (its fiscal
year), `employees.employee`. The IT-full product does NOT document a net-profit field, so
profit stays blank. The "Company Balance Sheet - Italy" endpoint (with profit and several
years) is not implemented: its path and fields are not documented in the study.
The turnover comes from the company's own filed balance sheet, so the scope is "separate".

Cost: each call is billed (IT-full from about 0.08 EUR, 0.30 EUR single, excl. VAT). The
collector never runs paid sources unless the caller passes `allow_paid=True`; the owner's
key is passed in and is never logged or stored by this module. IT-full may answer
asynchronously (POST returns an `id`, result at /IT-check_id/{id}): that flow is not
implemented and is reported as an error.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from .base import FinancialSource, FinancialsIdentity, SourceError, reason, register_source
from .model import FinancialSeries, FinancialYear, now_iso

log = logging.getLogger("reversehire.financials")

BASE = "https://company.openapi.com"
ENDPOINT = "IT-full"
SOURCE = "openapi"
PRODUCT_URL = "https://openapi.com/products/italian-full-company"
UNTESTED_AGAINST_LIVE_SERVICE = True
EXPECTED_SHAPE = {"data": [{"ecofin": {"turnover": 0, "turnoverYear": 0},
                            "employees": {"employee": 0}}]}


def _num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_full(payload, *, retrieved_at: str = "") -> list[FinancialYear]:
    """Records from an IT-full answer (pure function, no network)."""
    item = payload
    if isinstance(item, dict) and "data" in item:
        item = item["data"]
    if isinstance(item, list):
        item = item[0] if len(item) == 1 else None  # several companies: ambiguous
    if not isinstance(item, dict):
        return []
    ecofin = item.get("ecofin") if isinstance(item.get("ecofin"), dict) else {}
    employees = item.get("employees") if isinstance(item.get("employees"), dict) else {}
    year = _num(ecofin.get("turnoverYear"))
    revenue = _num(ecofin.get("turnover"))
    staff = _num(employees.get("employee"))
    if year is None or (revenue is None and staff is None):
        return []
    note = "latest year only (IT-full)"
    if staff is not None:
        note += "; year of the employee count not stated by the provider"
    record = FinancialYear(year=int(year), revenue=revenue, profit=None, employees=staff,
                           currency="EUR", scope="separate", source=SOURCE, source_url=PRODUCT_URL,
                           retrieved_at=retrieved_at or now_iso(), note=note)
    return [] if record.empty else [record]


@register_source
class OpenapiSource(FinancialSource):
    name = SOURCE
    paid = True

    def __init__(self, api_key: str, gateway=None, *, base: str = BASE):
        super().__init__(gateway)
        self._key = (api_key or "").strip()
        self.base = base.rstrip("/")

    def __repr__(self) -> str:  # never show the key
        return "OpenapiSource(key=<set>)" if self._key else "OpenapiSource(key=<missing>)"

    def fetch(self, identity: FinancialsIdentity) -> FinancialSeries | None:
        self.last_reason = None
        if not self._key:
            self.last_reason = reason("fin.why.no_key")
            return None
        code = identity.vat_digits or identity.tax_code
        if not code:
            self.last_reason = reason(
                "fin.why.no_vat")
            return None
        reply = self._get(f"{self.base}/{ENDPOINT}/{quote(code, safe='')}",
                          headers={"Authorization": f"Bearer {self._key}", "Accept": "application/json"})
        status = getattr(reply, "status", 0)
        if status in (401, 403):
            raise SourceError("unauthorized", f"HTTP {status}: the provider refused the key")
        if status == 404:
            self.last_reason = reason("fin.why.provider_none")
            return None
        if status != 200:
            raise SourceError("http", f"HTTP {status}")
        try:
            payload = reply.json()
        except ValueError as error:
            raise SourceError("bad_answer", "not JSON") from error
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict) and "ecofin" not in data and "id" in data:
            raise SourceError("async_not_supported", "asynchronous IT-full answers are not supported")
        series = FinancialSeries()
        for record in parse_full(payload):
            series.add(record)
        if series.empty:
            self.last_reason = reason(
                "fin.why.provider_no_figures")
            return None
        return series
