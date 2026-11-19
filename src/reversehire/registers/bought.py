"""Import of a Registro Imprese list bought by the user ("Elenco" CSV/XLSX, study A.5)
into the regional register, source "ri_bought".

Reading and column mapping are those of discovery/imports.py (`preview` + `records`,
the same alias table and the same preview dialog), called read-only: nothing is
duplicated here. Each record becomes a RegisterRow keyed by its tax code (the VAT
number when the list has no tax-code column: for companies they are usually equal, but
never assumed equal when both are given). Rows without an 11-digit code are skipped
(counted): a name alone never identifies a company.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .italy_codes import postcode_info, region_of_province
from .model import RI_BOUGHT, RegisterRow, clean_text, company_cf, is_natural_person_form, iso_date, legal_form

log = logging.getLogger(__name__)

# imports.FIELDS name -> register field
_MAP = {"name": "name", "vat": "vat", "rea": "rea", "ateco": "ateco", "ateco_text": "ateco_text",
        "address": "address", "town": "municipality", "province": "province", "postcode": "postcode",
        "pec": "pec", "site": "website", "legal_form": "legal_form", "employees": "employees_class",
        "start_date": "founded", "lat": "lat", "lon": "lon"}


@dataclass
class BoughtList:
    rows: list[RegisterRow] = field(default_factory=list)
    skipped_no_code: int = 0
    skipped_persons: int = 0
    total: int = 0
    kind: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str = ""


def read_bought_list(path: Path | str, mapping: dict[str, int] | None = None,
                     source_date: str = "") -> BoughtList:
    """RegisterRows of a bought list. Never raises (`error` says what went wrong)."""
    from ..discovery import imports

    result = BoughtList()
    try:
        preview = imports.preview(path)
        records = imports.records(preview, mapping)
    except Exception as error:  # noqa: BLE001 - imports.ImportError_ and read errors
        result.error = str(error) or type(error).__name__
        return result
    result.kind, result.warnings = preview.kind, list(preview.warnings)
    if not source_date:
        try:
            source_date = datetime.fromtimestamp(Path(path).stat().st_mtime).date().isoformat()
        except OSError:
            source_date = date.today().isoformat()
    for record in records:
        result.total += 1
        raw_cf = str(record.get("cf", "")).replace(" ", "")
        if (len(raw_cf) == 16 and not raw_cf.isdigit()) or is_natural_person_form(record.get("legal_form", "")):
            result.skipped_persons += 1      # sole traders are natural persons: not stored
            continue
        cf = company_cf(raw_cf) or company_cf(record.get("vat", ""))
        if not cf:
            result.skipped_no_code += 1
            continue
        fields: dict[str, str] = {}
        for key, target in _MAP.items():
            value = record.get(key)
            if value in (None, ""):
                continue
            fields[target] = str(value) if key in ("lat", "lon") else clean_text(value)
        if fields.get("legal_form"):
            fields["legal_form"] = legal_form(fields["legal_form"])
        if fields.get("founded"):
            fields["founded"] = iso_date(fields["founded"]) or fields["founded"]
        if fields.get("vat"):
            fields["vat"] = company_cf(fields["vat"]) or fields["vat"]
        region, cap_province = postcode_info(fields.get("postcode", ""))
        if not fields.get("province") and cap_province:
            fields["province"] = cap_province
        if fields.get("province"):
            region = region_of_province(fields["province"]) or region
        if region:
            fields["region"] = region
        result.rows.append(RegisterRow(RI_BOUGHT, cf, fields, source_date=source_date))
    return result


__all__ = ["BoughtList", "read_bought_list"]
