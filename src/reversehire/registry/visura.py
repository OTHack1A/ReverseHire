"""Register data entered by the owner after paying: a manual form or one row of a
"visura"-style CSV (also the Registro Imprese "Elenco Esteso" export).

Column names of the purchased files are not verified yet ([M] in the sources study):
the aliases below cover the usual Italian headings and can be extended after the
first real file. Values are validated (checksums, REA and ATECO format) before they
reach a card; they are stored with source "register" (R in the identity rule).
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata

from .checksums import (
    PROVINCES, ateco_ok, cf_ok, normalize_ateco, normalize_cf, normalize_rea, normalize_vat, piva_ok,
)

SOURCE = "register"

# Fields of the manual form / CSV mapping, in display order.
FIELDS = ("name", "vat", "tax_code", "rea", "ateco", "legal_form", "registered_office", "postcode",
          "town", "province", "pec", "phone", "email", "website", "share_capital", "employees",
          "founded", "status")

_ALIASES = {
    "name": ("denominazione", "ragione sociale", "denominazione impresa", "nome impresa", "impresa",
             "company name", "name"),
    "vat": ("partita iva", "p iva", "piva", "p.iva", "partita i.v.a.", "vat", "vat number"),
    "tax_code": ("codice fiscale", "cf", "c.f.", "codice fiscale impresa", "numero registro imprese",
                 "n registro imprese", "numero iscrizione registro imprese", "tax code"),
    "rea": ("rea", "numero rea", "n rea", "n. rea", "numero r.e.a.", "nrea", "r.e.a."),
    "rea_province": ("cciaa", "provincia cciaa", "camera di commercio", "sigla cciaa", "cciaa rea"),
    "ateco": ("ateco", "codice ateco", "attivita ateco", "codice attivita", "ateco 2025",
              "codice ateco 2025", "ateco 2007", "codice ateco 2007", "attivita prevalente",
              "codice attivita prevalente", "attivita"),
    "legal_form": ("forma giuridica", "natura giuridica", "legal form"),
    "registered_office": ("indirizzo", "indirizzo sede", "indirizzo sede legale", "via",
                          "sede legale", "address"),
    "postcode": ("cap", "c.a.p.", "postcode"),
    "town": ("comune", "comune sede", "comune sede legale", "localita", "town", "city"),
    "province": ("provincia", "prov", "sigla provincia", "provincia sede", "province"),
    "pec": ("pec", "indirizzo pec", "posta elettronica certificata", "domicilio digitale"),
    "phone": ("telefono", "tel", "phone"),
    "email": ("email", "e-mail", "posta elettronica"),
    "website": ("sito internet", "sito web", "sito", "website", "url"),
    "share_capital": ("capitale sociale", "capitale", "capitale sociale versato", "share capital"),
    "employees": ("addetti", "numero addetti", "dipendenti", "numero dipendenti", "employees"),
    "founded": ("data costituzione", "data iscrizione", "data inizio attivita", "anno costituzione",
                "data di costituzione", "founded"),
    "status": ("stato attivita", "stato", "stato impresa", "status"),
}


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold())
    text = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.sub(r"[_\-/]+", " ", text).split())


_LOOKUP = {}
for _field, _names in _ALIASES.items():
    for _name in _names:
        _LOOKUP[_fold(_name)] = _field
        _LOOKUP[_fold(_name).replace(".", "")] = _field


def field_for_heading(heading: str) -> str | None:
    key = _fold(heading).strip(" :")
    return _LOOKUP.get(key) or _LOOKUP.get(key.replace(".", ""))


def parse_csv(text: str) -> list[dict[str, str]]:
    """Rows of a CSV export as {field: value}; unknown columns are dropped.

    Accepts ';', ',' or tab separators and a UTF-8 BOM. Raises ValueError when no
    known column is found.
    """
    text = (text or "").lstrip("﻿")
    if not text.strip():
        raise ValueError("The file is empty.")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if len(rows) < 2:
        raise ValueError("The file needs a heading row and at least one data row.")
    mapping = {i: field_for_heading(h) for i, h in enumerate(rows[0])}
    if not any(mapping.values()):
        raise ValueError("No known column (e.g. 'Codice fiscale', 'REA', 'ATECO') in the heading row.")
    result = []
    for row in rows[1:]:
        values: dict[str, str] = {}
        for i, cell in enumerate(row):
            name = mapping.get(i)
            cell = " ".join(cell.split())
            if name and cell and name not in values:
                values[name] = cell
        if values:
            result.append(values)
    return result


def clean_fields(raw: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Validated register fields and the errors per field (field -> English message).

    Invalid identifiers are reported, never stored.
    """
    values = {k: " ".join(str(v or "").split()) for k, v in (raw or {}).items()}
    clean: dict[str, str] = {}
    errors: dict[str, str] = {}
    for name in FIELDS:
        value = values.get(name, "")
        if not value:
            continue
        if name == "vat":
            number = normalize_vat(value)
            if piva_ok(number):
                clean[name] = number
            else:
                errors[name] = "VAT number: 11 digits with a valid check digit."
        elif name == "tax_code":
            code = normalize_cf(value)
            if cf_ok(code):
                clean[name] = code
            else:
                errors[name] = "Tax code: 11 digits or 16 characters with a valid check."
        elif name == "rea":
            rea = normalize_rea(value, values.get("rea_province") or values.get("province"))
            if rea:
                clean[name] = rea
            else:
                errors[name] = "REA: province code and number, e.g. BO-123456."
        elif name == "ateco":
            code = normalize_ateco(value)
            if ateco_ok(code):
                clean[name] = code
            else:
                errors[name] = "ATECO: a code such as 28.29.30."
        elif name == "province":
            code = value.upper()
            if code in PROVINCES:
                clean[name] = code
            else:
                errors[name] = "Province: two-letter code, e.g. BO."
        elif name == "postcode":
            if re.fullmatch(r"\d{5}", value):
                clean[name] = value
            else:
                errors[name] = "Postcode: five digits."
        else:
            clean[name] = value
    return clean, errors


def pick_row(rows: list[dict[str, str]], *, vat: str = "", tax_code: str = "",
             name: str = "") -> dict[str, str] | None:
    """The row of the given company: same VAT/CF, else the only row, else the same name."""
    numbers = {normalize_vat(vat), normalize_cf(tax_code)} - {""}
    for row in rows:
        if numbers & {normalize_vat(row.get("vat", "")), normalize_cf(row.get("tax_code", ""))}:
            return row
    if len(rows) == 1:
        return rows[0]
    wanted = _fold(name)
    for row in rows:
        if wanted and _fold(row.get("name", "")) == wanted:
            return row
    return None
