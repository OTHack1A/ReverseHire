"""Source E: company lists the user obtains and imports (CSV or XLSX).

Supported (columns are mapped automatically, the user can correct the mapping in the
preview dialog of the search panel):
(a) Registro Imprese "Elenchi di imprese" exports (Elenco Indirizzi / Elenco Esteso:
    denominazione, codice fiscale, partita IVA, REA, ATECO, indirizzo, comune,
    provincia, PEC, sito internet, addetti...). The exact column names are not
    verified yet (sources study A.1): the alias table is generous;
(b) the innovative start-up / PMI register lists (denominazione, codice fiscale,
    provincia, comune, ATECO, settore, sito internet...); the .xls download must be
    saved as .xlsx or .csv first;
(c) any generic list with at least a name (website and town recommended).

Imported companies keep their register fields (CF, P.IVA, REA, ATECO, PEC...): they
feed the company card. They are stored in the local Store (never published; register
data bought by the user is for the user's own use) and found again by every search
whose area contains them (by town position from the local map data, or by province).

Now: the lists live in the regional register
--------------------------------------------
New imports are stored as sources of the regional register (`registers/lists.py`), shown
in the Register manager and searched by the discovery source "regional_register". The
reading part of this module (`preview`, `records`, `automap`, `dedup_key`) is still the
importer. The Store storage below is kept for the lists of older versions: they are moved
into the register once (`registers.lists.migrate_imported_lists`, idempotent, the Store's
copy kept), and `ImportSource` is no longer built by `discovery.build_sources`.

The local register of older versions (several lists, user-editable)
-------------------------------------------------------------------
Every imported list is kept with its source and date; together they form the local
register, viewed and edited in the "Local register" dialog of tab 2. API (also the
entry points for a follow-up that plugs official downloads in: download the file,
read it with `preview` + `records`, then call `save` the first time and `refresh`
afterwards; nothing else changes):

- `save(store, name, kind, rows, source="", source_date="", path="") -> list_id`
  stores a list (rows = `records(preview)` dicts; each row gets a stable "_rid");
- `refresh(store, list_id, rows, path=None, source_date=None) -> RefreshReport`
  replaces the list's rows with a newer file: rows are matched by `dedup_key`
  (tax code / VAT, else name + municipality, folded); fields the user edited by hand
  ("_edited") and rows the user added ("_manual") are kept; the report counts
  added / updated / removed / unchanged / kept edits;
- `register(store) -> list[RegisterRow]`: the de-duplicated view of all lists
  (the same company in two lists appears once, newest list first, missing fields
  filled from the others); `all_records` (the discovery source E) uses it;
- `update_row`, `add_row`, `delete_row`, `delete`, `lists`, `read_list`.

Storage: the Store's cache table (namespace NAMESPACE), one JSON value per list plus an
index. Lists saved by older versions (no source, no "_rid") are read as they are and
get their row ids the first time they are read: no schema migration is needed.
Discovery: source E keeps the companies in the search area (`DiscoveryQuery.contains`)
and, when ATECO categories are chosen, drops imported rows whose register ATECO code is
outside them (rows without a code are kept: nothing to judge them by).
"""

from __future__ import annotations

import csv
import io
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..geo.regions import REGIONS
from ..geo.sectors import normalize_text
from ..models import Company
from . import ateco as ateco_table
from .merge import normalize_vat

NAMESPACE = "discovery_imports"
INDEX_KEY = "_index"
MAX_ROWS = 50_000
HEADER_SCAN_ROWS = 15

KIND_REGISTER, KIND_STARTUP, KIND_GENERIC = "register", "startup", "generic"

FIELDS = ("name", "site", "address", "town", "province", "postcode", "cf", "vat", "rea",
          "ateco", "ateco_text", "pec", "email", "phone", "legal_form", "employees", "start_date",
          "lat", "lon")

# Normalised header text -> field. Matched exactly first, then by "contains".
ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("denominazione", "ragione sociale", "denominazione impresa", "denominazione sociale",
             "nome impresa", "nome azienda", "nome", "name", "company", "company name", "azienda",
             "impresa", "organisation", "organization"),
    "cf": ("codice fiscale", "cf", "c f", "codice fiscale impresa", "fiscal code", "tax code",
           "numero registro imprese", "n registro imprese"),
    "vat": ("partita iva", "p iva", "piva", "p i", "vat", "vat number", "vat id", "partita i v a"),
    "rea": ("rea", "numero rea", "n rea", "nrea", "rea number", "numero r e a", "cciaa rea"),
    "ateco": ("codice ateco", "ateco", "ateco 2025", "ateco 2007", "codice ateco 2025",
              "codice ateco 2007", "codice attivita", "attivita prevalente codice", "codice attivita prevalente",
              "nace", "nace code"),
    "ateco_text": ("descrizione ateco", "descrizione attivita", "attivita prevalente", "attivita",
                   "settore", "settore attivita", "activity", "sector", "descrizione codice ateco"),
    "address": ("indirizzo", "indirizzo sede", "indirizzo sede legale", "sede legale", "via",
                "address", "street", "indirizzo completo"),
    "town": ("comune", "comune sede", "citta", "localita", "town", "city", "municipality",
             "comune sede legale"),
    "province": ("provincia", "prov", "sigla provincia", "province", "pr", "provincia sede"),
    "postcode": ("cap", "postcode", "zip", "codice postale", "c a p"),
    "pec": ("pec", "indirizzo pec", "posta elettronica certificata", "domicilio digitale"),
    "email": ("email", "e mail", "mail", "posta elettronica", "indirizzo email"),
    "phone": ("telefono", "tel", "phone", "numero telefono", "recapito telefonico"),
    "site": ("sito internet", "sito", "sito web", "website", "web", "url", "indirizzo sito",
             "indirizzo internet", "home page", "homepage"),
    "legal_form": ("natura giuridica", "forma giuridica", "legal form", "forma"),
    "employees": ("addetti", "numero addetti", "n addetti", "dipendenti", "classe di addetti",
                  "classe addetti", "employees", "classe di produzione"),
    "start_date": ("data iscrizione", "data inizio attivita", "data costituzione",
                   "data iscrizione sezione speciale", "start date"),
    "lat": ("lat", "latitude", "latitudine"),
    "lon": ("lon", "lng", "longitude", "longitudine"),
}

_PROVINCE_NAMES = {normalize_text(p.name): p.code for r in REGIONS.values() for p in r.provinces}


@dataclass
class ImportPreview:
    path: str
    kind: str
    header: list[str]
    rows: list[list[str]]          # data rows (without the header)
    mapping: dict[str, int]        # field -> column index
    header_row: int = 0
    warnings: list[str] = field(default_factory=list)

    def sample(self, n: int = 8) -> list[dict]:
        return [record_from_row(r, self.mapping) for r in self.rows[:n]]


class ImportError_(Exception):
    """The file cannot be read as a company list (message in English)."""


# --- reading files -------------------------------------------------------------------------------


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def read_table(path: str | Path) -> list[list[str]]:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        from .xlsx import read_rows

        return read_rows(path, max_rows=MAX_ROWS + HEADER_SCAN_ROWS)
    if suffix == ".xls":
        raise ImportError_("Old Excel .xls files cannot be read: open the file and save it as "
                           ".xlsx or .csv, then import it again.")
    text = _decode(path.read_bytes())
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    return rows[: MAX_ROWS + HEADER_SCAN_ROWS]


def _match(header_text: str) -> str | None:
    norm = normalize_text(header_text)
    if not norm:
        return None
    for name, aliases in ALIASES.items():
        if norm in aliases:
            return name
    for name, aliases in ALIASES.items():
        for alias in aliases:
            if len(alias) >= 4 and (norm.startswith(alias + " ") or norm.endswith(" " + alias)):
                return name
    return None


def automap(header: list[str]) -> dict[str, int]:
    """field -> column index, from the header texts (first match wins)."""
    mapping: dict[str, int] = {}
    for index, text in enumerate(header):
        name = _match(text)
        if name and name not in mapping:
            mapping[name] = index
    return mapping


def detect_kind(header: list[str], mapping: dict[str, int], file_name: str = "") -> str:
    lowered = normalize_text(" ".join(header) + " " + file_name)
    if "startup" in lowered or "start up" in lowered or "pmi innovativ" in lowered or "incubator" in lowered:
        return KIND_STARTUP
    if "rea" in mapping or ("ateco" in mapping and ("cf" in mapping or "vat" in mapping)):
        return KIND_REGISTER
    return KIND_GENERIC


def preview(path: str | Path) -> ImportPreview:
    rows = read_table(path)
    best_row, best_map = 0, {}
    for i, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        mapping = automap(row)
        if len(mapping) > len(best_map):
            best_row, best_map = i, mapping
    if "name" not in best_map:
        raise ImportError_("No column with the company name was found (expected e.g. "
                           "\"Denominazione\", \"Ragione sociale\" or \"Name\").")
    header = rows[best_row]
    data = [r for r in rows[best_row + 1:] if any((c or "").strip() for c in r)][:MAX_ROWS]
    result = ImportPreview(str(path), detect_kind(header, best_map, Path(path).name), header, data, best_map,
                           best_row)
    if "site" not in best_map:
        result.warnings.append("no_site_column")
    if "town" not in best_map and "province" not in best_map and "address" not in best_map:
        result.warnings.append("no_place_column")
    return result


def _cell(row: list[str], mapping: dict[str, int], name: str) -> str:
    index = mapping.get(name)
    if index is None or index < 0 or index >= len(row):
        return ""
    return " ".join(str(row[index] or "").split())


def province_code(text: str) -> str:
    value = (text or "").strip()
    if re.fullmatch(r"[A-Za-z]{2}", value):
        return value.upper()
    match = re.search(r"\(([A-Za-z]{2})\)", value)
    if match:
        return match.group(1).upper()
    return _PROVINCE_NAMES.get(normalize_text(value), "")


def record_from_row(row: list[str], mapping: dict[str, int]) -> dict:
    record = {name: _cell(row, mapping, name) for name in FIELDS if name in mapping}
    record = {k: v for k, v in record.items() if v}
    if record.get("vat"):
        record["vat"] = normalize_vat(record["vat"]) or record["vat"]
    if record.get("cf"):
        cf = record["cf"].upper().replace(" ", "")
        record["cf"] = normalize_vat(cf) or (cf.zfill(11) if cf.isdigit() and len(cf) < 11 else cf)
    if record.get("ateco"):
        code = ateco_table.normalize_code(record["ateco"].split(" ")[0])
        if code:
            record["ateco"] = code
    if record.get("province"):
        record["province"] = province_code(record["province"]) or record["province"]
    if record.get("rea"):
        rea = record["rea"].upper().replace(" ", "")
        match = re.fullmatch(r"([A-Z]{2})?[-/]?(\d{3,7})", rea)
        if match:
            prefix = match.group(1) or record.get("province", "")
            record["rea"] = f"{prefix}-{match.group(2)}" if prefix else match.group(2)
    for key in ("lat", "lon"):
        if key in record:
            try:
                record[key] = float(record[key].replace(",", "."))
            except ValueError:
                record.pop(key)
    if record.get("site") and "." not in record["site"]:
        record.pop("site")
    return record


def records(preview_: ImportPreview, mapping: dict[str, int] | None = None) -> list[dict]:
    mapping = mapping or preview_.mapping
    result = []
    for row in preview_.rows:
        record = record_from_row(row, mapping)
        if record.get("name"):
            result.append(record)
    return result


# --- storage -------------------------------------------------------------------------------------


PRIVATE_KEYS = ("_rid", "_edited", "_manual")
MANUAL_LIST_NAME = "manual"   # the list holding the rows the user adds outside any list


def _new_rid() -> str:
    return uuid.uuid4().hex[:12]


def _fold(text) -> str:
    return " ".join(normalize_text(str(text or "")).split())


def dedup_key(record: dict) -> str:
    """The identity of a register row: tax code / VAT (11 digits, or the 16-character
    personal code), else folded name + municipality (the province when no town)."""
    for name in ("vat", "cf"):
        value = str(record.get(name) or "").upper().replace(" ", "")
        if value:
            digits = normalize_vat(value)
            if digits:
                return "id:" + digits
            if re.fullmatch(r"[A-Z0-9]{16}", value):
                return "id:" + value
    name = _fold(record.get("name"))
    if not name:
        return ""
    place = _fold(record.get("town")) or _fold(record.get("province"))
    return f"name:{name}|{place}"


def public(record: dict) -> dict:
    """The record without the register's own keys (_rid, _edited, _manual)."""
    return {k: v for k, v in record.items() if k not in PRIVATE_KEYS}


def _index(store) -> list[str]:
    return [i for i in (store.cache_read(NAMESPACE, INDEX_KEY) or []) if isinstance(i, str)]


def read_list(store, list_id: str) -> dict | None:
    """The stored list {"name", "kind", "created", "source", "source_date", "path",
    "refreshed", "rows"}; rows of older versions get their "_rid" here (stored once)."""
    value = store.cache_read(NAMESPACE, list_id)
    if not isinstance(value, dict):
        return None
    rows = [r for r in (value.get("rows") or []) if isinstance(r, dict)]
    missing = [row for row in rows if not row.get("_rid")]
    for row in missing:
        row["_rid"] = _new_rid()
    value["rows"] = rows
    if missing:  # a list of an older version: its row ids are stored once, so edits find them
        _write_list(store, list_id, value)
    return value


def _write_list(store, list_id: str, value: dict) -> None:
    store.cache_write(NAMESPACE, list_id, value)


def save(store, name: str, kind: str, rows: list[dict], *, source: str = "", source_date: str = "",
         path: str = "") -> str:
    """Store an imported list (see the module docstring); returns its id."""
    list_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    clean = []
    for row in rows:
        if isinstance(row, dict):
            row = dict(row)
            row.setdefault("_rid", _new_rid())
            clean.append(row)
    _write_list(store, list_id, {"name": name, "kind": kind, "created": time.time(), "source": source or name,
                                 "source_date": source_date or "", "path": str(path or ""), "refreshed": 0,
                                 "rows": clean})
    store.cache_write(NAMESPACE, INDEX_KEY, _index(store) + [list_id])
    return list_id


def lists(store) -> list[dict]:
    """[{"id", "name", "kind", "created", "count", "source", "source_date", "refreshed",
    "path"}] of the stored lists, the most recently imported or refreshed first."""
    result = []
    for list_id in _index(store):
        value = store.cache_read(NAMESPACE, list_id)
        if isinstance(value, dict):
            result.append({"id": list_id, "name": value.get("name", ""), "kind": value.get("kind", ""),
                           "created": value.get("created", 0), "count": len(value.get("rows") or []),
                           "source": value.get("source") or value.get("name", ""),
                           "source_date": value.get("source_date", ""), "refreshed": value.get("refreshed", 0),
                           "path": value.get("path", "")})
    result.sort(key=lambda item: -float(item.get("refreshed") or item.get("created") or 0))
    return result


def delete(store, list_id: str) -> None:
    index = [i for i in _index(store) if i != list_id]
    store.cache_write(NAMESPACE, INDEX_KEY, index)
    store.cache_write(NAMESPACE, list_id, None)


@dataclass
class RegisterRow:
    """One company of the local register (de-duplicated across the lists)."""

    record: dict                    # merged fields (public keys only)
    list_id: str                    # the list the row is edited in (the newest holding it)
    rid: str
    key: str
    lists: list[str] = field(default_factory=list)   # names of every list holding it
    kind: str = KIND_GENERIC
    source: str = ""
    source_date: str = ""
    edited: list[str] = field(default_factory=list)


def register(store) -> list[RegisterRow]:
    """All lists merged: one row per company (`dedup_key`), the newest list first."""
    rows: list[RegisterRow] = []
    by_key: dict[str, RegisterRow] = {}
    for info in lists(store):
        value = read_list(store, info["id"])
        if value is None:
            continue
        for row in value["rows"]:
            if not row.get("name"):
                continue
            key = dedup_key(row) or "rid:" + row["_rid"]
            found = by_key.get(key)
            if found is None:
                found = RegisterRow(public(row), info["id"], row["_rid"], key, [info["name"]],
                                    value.get("kind", KIND_GENERIC), info["source"], info["source_date"],
                                    list(row.get("_edited") or []))
                by_key[key] = found
                rows.append(found)
                continue
            for name, field_value in public(row).items():
                if field_value not in (None, "") and found.record.get(name) in (None, ""):
                    found.record[name] = field_value
            if info["name"] not in found.lists:
                found.lists.append(info["name"])
    return rows


def all_records(store) -> list[tuple[str, dict]]:
    """(list kind, record) of the local register, without duplicates across lists."""
    return [(row.kind, dict(row.record)) for row in register(store)]


def _find_row(value: dict, rid: str) -> dict | None:
    return next((r for r in value["rows"] if r.get("_rid") == rid), None)


def update_row(store, list_id: str, rid: str, fields: dict) -> bool:
    """Changes fields of one row by hand: they are marked "_edited", so a refresh from
    a newer file keeps them. Empty values remove the field. False when not found."""
    value = read_list(store, list_id)
    row = _find_row(value, rid) if value else None
    if row is None:
        return False
    edited = set(row.get("_edited") or [])
    for name, field_value in fields.items():
        if name in PRIVATE_KEYS or name not in FIELDS:
            continue
        text = " ".join(str(field_value or "").split())
        if name == "province" and text:
            text = province_code(text) or text
        if text:
            row[name] = text
        else:
            row.pop(name, None)
        edited.add(name)
    row["_edited"] = sorted(edited)
    _write_list(store, list_id, value)
    return True


def add_row(store, fields: dict, list_id: str | None = None) -> tuple[str, str] | None:
    """A company added by hand (to `list_id`, else to the "manual" list, created on
    first use); returns (list_id, rid), None without a name."""
    record = {k: " ".join(str(v or "").split()) for k, v in fields.items() if k in FIELDS and v}
    if not record.get("name"):
        return None
    if record.get("province"):
        record["province"] = province_code(record["province"]) or record["province"]
    if list_id is None:
        list_id = next((i["id"] for i in lists(store) if i["kind"] == MANUAL_LIST_NAME), None)
        if list_id is None:
            list_id = save(store, MANUAL_LIST_NAME, MANUAL_LIST_NAME, [], source=MANUAL_LIST_NAME)
    value = read_list(store, list_id)
    if value is None:
        return None
    record["_edited"] = sorted(record)
    record["_rid"] = _new_rid()
    record["_manual"] = True
    value["rows"].append(record)
    _write_list(store, list_id, value)
    return list_id, record["_rid"]


def delete_row(store, list_id: str, rid: str) -> bool:
    value = read_list(store, list_id)
    if value is None:
        return False
    before = len(value["rows"])
    value["rows"] = [r for r in value["rows"] if r.get("_rid") != rid]
    if len(value["rows"]) == before:
        return False
    _write_list(store, list_id, value)
    return True


@dataclass
class RefreshReport:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    kept_edits: int = 0     # rows whose hand edits won over the new file


def refresh(store, list_id: str, new_rows: list[dict], *, path: str | None = None,
            source_date: str | None = None) -> RefreshReport:
    """Replaces a list's rows with a newer file of the same list (see the module
    docstring). Rows added by hand stay; fields edited by hand win over the file."""
    value = read_list(store, list_id)
    if value is None:
        raise KeyError(list_id)
    report = RefreshReport()
    old_by_key: dict[str, dict] = {}
    for row in value["rows"]:
        old_by_key.setdefault(dedup_key(row) or "rid:" + row["_rid"], row)
    result, seen = [], set()
    for incoming in new_rows:
        if not isinstance(incoming, dict) or not incoming.get("name"):
            continue
        key = dedup_key(incoming)
        if key in seen:
            continue   # the same company twice in the file: the first one counts
        seen.add(key)
        old = old_by_key.get(key)
        row = dict(public(incoming))
        if old is None:
            row["_rid"] = _new_rid()
            result.append(row)
            report.added += 1
            continue
        edited = set(old.get("_edited") or [])
        for name in edited:
            if name in old:
                row[name] = old[name]
            else:
                row.pop(name, None)
        row["_rid"] = old["_rid"]
        if edited:
            row["_edited"] = sorted(edited)
            report.kept_edits += 1
        if old.get("_manual"):
            row["_manual"] = True
        if public(row) != public(old):
            report.updated += 1
        else:
            report.unchanged += 1
        result.append(row)
    for key, old in old_by_key.items():
        if key in seen:
            continue
        if old.get("_manual"):
            result.append(old)   # added by hand: never removed by a file
        else:
            report.removed += 1
    value["rows"] = result
    value["refreshed"] = time.time()
    if path:
        value["path"] = str(path)
    if source_date is not None:
        value["source_date"] = source_date
    _write_list(store, list_id, value)
    return report


def ateco_allowed(record: dict, codes: list[str] | None) -> bool:
    """True when the row fits the chosen ATECO categories (no categories chosen, or no
    code on the row: kept)."""
    if not codes:
        return True
    code = str(record.get("ateco") or "").strip()
    if not code or not ateco_table.normalize_code(code.split(" ")[0]):
        return True
    try:
        return bool(ateco_table.matches(code, list(codes)))
    except Exception:  # noqa: BLE001 - an odd code is not a reason to drop the company
        return True


def to_company(record: dict, place_lookup=None) -> Company:
    """A Company with origin "import"; the register fields go to `discovery_fields`.
    `place_lookup(town, province) -> (lat, lon, province) | None` gives the town's
    position when the record has no coordinates (marked "position": "town")."""
    from ..geo.overpass import normalize_site

    lat, lon = record.get("lat"), record.get("lon")
    extra = {k: v for k, v in record.items() if k not in ("name", "site", "address", "lat", "lon") and v}
    if (lat is None or lon is None) and place_lookup is not None and record.get("town"):
        found = place_lookup(record["town"], record.get("province", ""))
        if found:
            lat, lon = found[0], found[1]
            extra["position"] = "town"
            if not extra.get("province") and found[2]:
                extra["province"] = found[2]
    address = record.get("address", "")
    town = " ".join(p for p in (record.get("postcode", ""), record.get("town", "")) if p)
    if town and town.lower() not in address.lower():
        address = ", ".join(p for p in (address, town) if p)
    company = Company(name=record["name"], lat=lat, lon=lon, address=address,
                      site=normalize_site(record.get("site")), origin="import")
    company.discovery_fields = extra
    return company
