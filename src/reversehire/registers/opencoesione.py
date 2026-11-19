"""OpenCoesione "soggetti" (study A.1.3): recipients and implementers of cohesion-fund
projects, with the richest single row (municipality ISTAT code, street, postcode, full
6-digit ATECO, employee class, size class).

Read from the CSV ZIP with the standard library (the Parquet file would need pyarrow):
`soggetti.zip` holds one `;`-separated CSV of about 2.3 GB, one row per subject x project
x role, streamed and aggregated per tax code. Public bodies (rows with a public-body
code, or ATECO section O) are left out: the register lists companies.
Descriptions are double-encoded UTF-8 in places ("SocietÃ\xa0"): repaired on reading.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .base import RegisterSource, text_stream
from .download import Manifest, RemoteFile
from .italy_codes import comune_istat_info, postcode_info
from .model import (OPENCOESIONE, CancelledError, RegisterProgress, RegisterRow, check_cancel, clean_text, company_cf,
                    is_natural_person_form, legal_form, nace_code, notify)

SOGGETTI_URL = "https://opencoesione.gov.it/it/opendata/soggetti.zip"
LOCAL_NAME = "soggetti.zip"
PROGRESS_EVERY = 64 * 1024 * 1024

COLUMNS = {
    "cf": "OC_CODICE_FISCALE_SOGG", "name": "OC_DENOMINAZIONE_SOGG", "form": "DESCR_FORMA_GIURIDICA_SOGG",
    "comune": "COD_COMUNE_SEDE_SOGG", "address": "INDIRIZZO_SOGG", "postcode": "CAP_SOGG",
    "ateco": "COD_ATECO_SOGG", "ateco_text": "DESCRIZIONE_ATECO_SOGG", "employees": "DESCR_ADDETTI_SOGG",
    "size": "DESCR_DIMENSIONE_SOGG",
}
PUBLIC_BODY_COLUMNS = ("OC_COD_ENTE_BDAP", "OC_COD_ENTE_IPA", "OC_COD_ENTE_MIUR", "OC_COD_ENTE_SIOPE",
                       "OC_COD_ENTE_ISTAT_S13", "OC_COD_ENTE_SSN")
_AGGREGATED = ("name", "legal_form", "municipality_istat", "province", "region", "address", "postcode", "ateco",
               "ateco_text", "employees_class", "size_class")


class OpenCoesioneSource(RegisterSource):
    id = OPENCOESIONE
    licence = "CC BY 4.0"
    attribution = "OpenCoesione – Dipartimento per le politiche di coesione (CC BY 4.0)"
    terms_url = "https://opencoesione.gov.it/it/opendata/"
    homepage = "https://opencoesione.gov.it/it/opendata/"
    update_frequency = "twice_yearly"
    update_days = 60

    def remote_files(self, manifest: Manifest | None = None, cancel=None) -> list[RemoteFile]:
        return [RemoteFile(SOGGETTI_URL, LOCAL_NAME)]

    def parse(self, files, region_filter, progress=None, cancel=None) -> Iterator[RegisterRow]:
        wanted = set(region_filter) if region_filter else None
        companies: dict[str, dict] = {}
        public = rows_seen = persons = 0
        source_date = ""
        for path in files:
            path = Path(path)
            try:
                stream, total, member = text_stream(path, (".csv",))
            except Exception as error:  # noqa: BLE001
                self.parse_error(path.name, f"{type(error).__name__}: {error}")
                continue
            digits = "".join(c for c in member if c.isdigit())
            if len(digits) >= 8:
                source_date = f"{digits[-8:-4]}-{digits[-4:-2]}-{digits[-2:]}"
            elif not source_date:
                source_date = datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
            counter = stream.buffer.raw
            last = 0
            try:
                reader = csv.reader(stream, delimiter=";", quotechar='"')
                header = [h.strip().lstrip("﻿") for h in next(reader)]
                index = {key: header.index(col) for key, col in COLUMNS.items() if col in header}
                if "cf" not in index or "comune" not in index:
                    self.parse_error(path.name, "missing tax-code or municipality column")
                    continue
                public_idx = [header.index(c) for c in PUBLIC_BODY_COLUMNS if c in header]
                i_cf, i_com = index["cf"], index["comune"]
                width = max(index.values()) + 1
                for row in reader:
                    rows_seen += 1
                    if counter.count - last >= PROGRESS_EVERY:
                        last = counter.count
                        check_cancel(cancel)
                        notify(progress, RegisterProgress(self.id, "parse", file=path.name, done_bytes=counter.count,
                                                          total_bytes=total, rows=len(companies)))
                    if len(row) < width:
                        continue
                    comune = row[i_com]
                    if wanted is not None:
                        region = comune_istat_info(comune)[0] if comune else ""
                        if region not in wanted and comune:
                            continue
                    cf = company_cf(row[i_cf])
                    if not cf:
                        continue
                    if any(i < len(row) and row[i].strip() for i in public_idx) or row[index["ateco"]].startswith("O"):
                        public += 1
                        continue
                    if "form" in index and is_natural_person_form(row[index["form"]]):
                        persons += 1
                        continue
                    self._add(companies, cf, row, index, wanted)
            except CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                self.parse_error(path.name, f"{type(error).__name__}: {error}")
            finally:
                stream.close()
        self.stats = {"rows_seen": rows_seen, "public_body_rows": public, "natural_person_rows": persons,
                      "companies": len(companies)}
        notify(progress, RegisterProgress(self.id, "parse", rows=len(companies)))
        for cf, fields in companies.items():
            if wanted is not None and fields.get("region") not in wanted:
                continue
            yield RegisterRow(self.id, cf, {k: v for k, v in fields.items() if v}, source_date=source_date)

    @staticmethod
    def _add(companies: dict, cf: str, row: list[str], index: dict, wanted) -> None:
        def get(key: str) -> str:
            i = index.get(key)
            return clean_text(row[i]) if i is not None and i < len(row) else ""

        region, province, comune = comune_istat_info(get("comune"))
        postcode = "".join(c for c in get("postcode") if c.isdigit())
        postcode = postcode if len(postcode) == 5 else ""
        if not region and postcode:
            region, cap_province = postcode_info(postcode)
            province = province or cap_province
        values = {
            "name": get("name"), "legal_form": legal_form(get("form")), "municipality_istat": comune,
            "province": province, "region": region, "address": get("address"), "postcode": postcode,
            "ateco": nace_code(get("ateco")), "ateco_text": get("ateco_text"),
            "employees_class": get("employees"), "size_class": get("size"),
        }
        current = companies.get(cf)
        if current is None:
            companies[cf] = values
            return
        for key in _AGGREGATED:
            if not current.get(key) and values.get(key):
                current[key] = values[key]


__all__ = ["OpenCoesioneSource", "SOGGETTI_URL"]
