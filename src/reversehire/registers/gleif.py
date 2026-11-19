"""GLEIF golden copy, LEI level 2 CSV (study A.1.2): the free "address book" of the
companies that have an LEI.

The `latest` API call gives the URL, size and record count of today's file (about
490 MB zipped, 5 GB of CSV, 3.5 M records). The CSV is streamed out of the ZIP (never
extracted). Italian records are selected by POSTCODE, not by the region field (18,981
Italian records have an empty region and some carry region-level codes such as IT-45).
The tax code comes from the registration-authority entity id (Registro Imprese,
RA000407) when it is an 11-digit number.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from .base import RegisterSource, UpdateCheck, text_stream
from .download import Manifest, RemoteFile
from .italy_codes import postcode_info, region_of_province
from .model import (GLEIF, CancelledError, RegisterProgress, RegisterRow, check_cancel, clean_text, company_cf,
                    is_natural_person_form, iso_date, legal_form, notify)

LATEST_URL = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/lei2/latest"
LOCAL_NAME = "gleif-lei2-golden-copy.csv.zip"
PROGRESS_EVERY = 64 * 1024 * 1024

# ISO 20275 entity legal form codes seen for Italian entities (frequencies and name
# suffixes checked on the 2026-11-18 golden copy, Emilia-Romagna subset).
ELF_CODES: dict[str, str] = {
    "OV32": "S.r.l.", "B5TO": "S.r.l.", "8GU1": "S.r.l.s.", "P418": "S.p.A.", "X32V": "S.p.A.",
    "T2X1": "S.n.c.", "CGSO": "S.a.s.", "2XXH": "S.s.", "QRZJ": "Soc. coop.", "CTNS": "Soc. coop.",
    "C1IC": "Soc. coop. consortile", "BL52": "Soc. consortile a r.l.", "LZZD": "Soc. consortile per azioni",
    "HN75": "Consorzio", "2J7Z": "Consorzio", "LHBB": "Impresa individuale", "75WC": "Fondazione",
    "1TON": "Associazione", "IMHA": "Ente ecclesiastico", "ADAZ": "Ente ecclesiastico",
    "2HC6": "Ente pubblico", "ZG6S": "Ente pubblico economico", "OQ8C": "Fondo pensione",
}

# Sole proprietorships: natural persons, never stored.
PERSON_ELF_CODES = {"LHBB"}

COLUMNS = {
    "lei": "LEI", "name": "Entity.LegalName", "address": "Entity.LegalAddress.FirstAddressLine",
    "city": "Entity.LegalAddress.City", "region": "Entity.LegalAddress.Region",
    "postcode": "Entity.LegalAddress.PostalCode", "country": "Entity.LegalAddress.Country",
    "ra": "Entity.RegistrationAuthority.RegistrationAuthorityID",
    "ra_id": "Entity.RegistrationAuthority.RegistrationAuthorityEntityID",
    "form": "Entity.LegalForm.EntityLegalFormCode", "form_other": "Entity.LegalForm.OtherLegalForm",
    "status": "Entity.EntityStatus", "created": "Entity.EntityCreationDate",
    "registration": "Registration.RegistrationStatus",
}


def gleif_legal_form(code: str, other: str = "") -> str:
    code = (code or "").strip().upper()
    if code in ELF_CODES:
        return ELF_CODES[code]
    if other:
        return legal_form(other)
    return ""


class GleifSource(RegisterSource):
    id = GLEIF
    licence = "CC0 1.0"
    attribution = "GLEIF Golden Copy, Global Legal Entity Identifier Foundation (CC0)"
    terms_url = "https://www.gleif.org/en/meta/lei-data-terms-of-use"
    homepage = "https://www.gleif.org/en/lei-data/gleif-golden-copy"
    update_frequency = "monthly"     # published 3 times a day; a monthly refresh is enough
    update_days = 30

    def latest(self, cancel=None) -> RemoteFile:
        reply = self.gateway.get(LATEST_URL, cancel=cancel)
        if reply.status != 200:
            raise ValueError(f"GLEIF API answered HTTP {reply.status}")
        data = reply.json().get("data") or {}
        info = ((data.get("full_file") or {}).get("csv")) or {}
        if not info.get("url"):
            raise ValueError("GLEIF API: no CSV file in the latest publication")
        size = info.get("size")
        rows = info.get("record_count")
        return RemoteFile(str(info["url"]), LOCAL_NAME, size=int(size) if str(size or "").isdigit() else None,
                          period=str(data.get("publish_date") or "")[:10],
                          rows=int(rows) if str(rows or "").isdigit() else None)

    def remote_files(self, manifest: Manifest | None = None, cancel=None) -> list[RemoteFile]:
        return [self.latest(cancel)]

    def check_update(self, dest: Path, cancel=None) -> UpdateCheck:
        """One small API call. The file changes 3 times a day: it counts as changed only
        when the local copy is older than `update_days`."""
        manifest = Manifest(dest)
        entry = manifest.entry(LOCAL_NAME)
        try:
            remote = self.latest(cancel)
        except Exception as error:  # noqa: BLE001
            return UpdateCheck(self.id, "failed", detail=f"{type(error).__name__}: {error}")
        if not entry:
            return UpdateCheck(self.id, "new", [LOCAL_NAME], remote.size)
        try:
            age = (date.today() - date.fromisoformat(str(entry.get("period") or entry.get("date", ""))[:10])).days
        except ValueError:
            age = self.update_days
        if entry.get("url") != remote.url and age >= self.update_days:
            return UpdateCheck(self.id, "changed", [LOCAL_NAME], remote.size, detail=f"local copy {age} days old")
        return UpdateCheck(self.id, "unchanged", detail=f"local copy {age} days old")

    def download(self, dest, progress=None, cancel=None, force=False):
        manifest = Manifest(dest)
        entry = manifest.entry(LOCAL_NAME)
        if entry and not force and (Path(dest) / LOCAL_NAME).is_file():
            try:
                age = (date.today() - date.fromisoformat(str(entry.get("period") or "")[:10])).days
            except ValueError:
                age = self.update_days
            if age < self.update_days:
                from .download import UNCHANGED, DownloadResult, FileResult
                return DownloadResult(self.id, "unchanged", [FileResult(LOCAL_NAME, entry.get("url", ""), UNCHANGED,
                                                                       path=str(Path(dest) / LOCAL_NAME))])
        return super().download(dest, progress, cancel, force)

    def parse(self, files, region_filter, progress=None, cancel=None) -> Iterator[RegisterRow]:
        wanted = set(region_filter) if region_filter else None
        italian = kept = no_cf = persons = 0
        for path in files:
            path = Path(path)
            last = [0]
            try:
                stream, total, _name = text_stream(path, (".csv",), on_bytes=None)
            except Exception as error:  # noqa: BLE001
                self.parse_error(path.name, f"{type(error).__name__}: {error}")
                continue
            counter = stream.buffer.raw  # CountingReader
            source_date = ""
            try:
                manifest_entry = Manifest(path.parent).entry(path.name)
                source_date = str(manifest_entry.get("period") or "")[:10]
            except Exception:  # noqa: BLE001
                source_date = ""
            if not source_date:
                source_date = datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
            try:
                reader = csv.reader(stream)
                header = next(reader)
                index = {key: header.index(col) for key, col in COLUMNS.items() if col in header}
                missing = [k for k in ("lei", "name", "country", "postcode", "ra_id") if k not in index]
                if missing:
                    self.parse_error(path.name, f"missing columns: {', '.join(missing)}")
                    continue
                i_country, i_postcode = index["country"], index["postcode"]
                width = max(index.values()) + 1
                for row in reader:
                    if len(row) < width or row[i_country] != "IT":
                        if counter.count - last[0] >= PROGRESS_EVERY:
                            check_cancel(cancel)
                            last[0] = counter.count
                            notify(progress, RegisterProgress(self.id, "parse", file=path.name,
                                                              done_bytes=counter.count, total_bytes=total, rows=kept))
                        continue
                    italian += 1
                    region, province = postcode_info(row[i_postcode])
                    if not province:
                        code = row[index["region"]] if "region" in index else ""
                        if code.startswith("IT-") and len(code) == 5 and region_of_province(code[3:]):
                            province = code[3:]
                            region = region or region_of_province(province)
                    if wanted is not None and region not in wanted:
                        continue
                    cf = company_cf(row[index["ra_id"]])
                    if not cf:
                        no_cf += 1
                        continue
                    form_code = row[index["form"]] if "form" in index else ""
                    other = row[index["form_other"]] if "form_other" in index else ""
                    if form_code in PERSON_ELF_CODES or (form_code in ("8888", "9999", "") and
                                                         is_natural_person_form(other)):
                        persons += 1
                        continue
                    kept += 1
                    yield self._row(row, index, cf, region, province, source_date)
                notify(progress, RegisterProgress(self.id, "parse", file=path.name, done_bytes=counter.count,
                                                  total_bytes=total, rows=kept))
            except CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                self.parse_error(path.name, f"{type(error).__name__}: {error}")
            finally:
                stream.close()
        self.stats = {"italian_records": italian, "kept": kept, "without_tax_code": no_cf,
                      "natural_persons": persons}

    @staticmethod
    def _row(row, index, cf, region, province, source_date) -> RegisterRow:
        def get(key: str) -> str:
            i = index.get(key)
            return clean_text(row[i]) if i is not None else ""

        status = get("status").upper()
        postcode = ""
        digits = "".join(c for c in get("postcode") if c.isdigit())
        if len(digits) == 5:
            postcode = digits
        fields = {
            "name": get("name"), "lei": get("lei"), "address": get("address"), "postcode": postcode,
            "municipality": get("city"), "province": province, "region": region,
            "legal_form": gleif_legal_form(get("form"), get("form_other")),
            "founded": iso_date(get("created")),
            "status": "active" if status == "ACTIVE" else ("inactive" if status == "INACTIVE" else ""),
        }
        return RegisterRow(GLEIF, cf, {k: v for k, v in fields.items() if v}, source_date=source_date)


__all__ = ["ELF_CODES", "GleifSource", "LATEST_URL", "gleif_legal_form"]
