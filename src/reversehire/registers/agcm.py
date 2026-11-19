"""AGCM "Elenco delle imprese con rating di legalità" (study A.1.4).

The whole register is republished weekly as one `;` CSV with a notice line before the
header. The file URL is read from the monthly dataset page
`https://dati.agcm.it/dataset/rating-legalita-YYYY-MM` (never through `/api/`, which
the site's robots.txt disallows), with the site's Crawl-Delay of 10 seconds.
The file states that it has statistical value only and that later revocations must be
checked on later lists: the rating is shown as a badge with its expiry, and a revoked,
annulled or suspended rating is flagged as such.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from .base import RegisterSource, open_member
from .download import Manifest, RemoteFile
from .italy_codes import region_of_province
from .model import AGCM, RegisterProgress, RegisterRow, clean_text, company_cf, iso_date, notify, now_iso

DATASET_URL = "https://dati.agcm.it/dataset/rating-legalita-{year:04d}-{month:02d}"
LOCAL_NAME = "agcm-rating-legalita.csv"
LISTING_MAX_AGE_S = 6 * 3600
MONTHS_BACK = 3
_CSV_LINK = re.compile(r'href="(https://dati\.agcm\.it/dataset/[^"]+/download/(elenco_rating_(\d{4})_(\d{2})_(\d{2})'
                       r'[^"/]*-geo\.csv))"')


def csv_links(html: str) -> list[tuple[str, str, str]]:
    """(url, file name, ISO date) of the "-geo.csv" files linked by a dataset page,
    newest first."""
    found = {}
    for match in _CSV_LINK.finditer(html or ""):
        url, name = match.group(1), match.group(2)
        found[url] = (url, name, f"{match.group(3)}-{match.group(4)}-{match.group(5)}")
    return sorted(found.values(), key=lambda item: item[2], reverse=True)


def _month_back(today: date, back: int) -> tuple[int, int]:
    index = today.year * 12 + today.month - 1 - back
    return index // 12, index % 12 + 1


class AgcmSource(RegisterSource):
    id = AGCM
    licence = "CC BY 4.0"
    attribution = "Autorità Garante della Concorrenza e del Mercato, rating di legalità (CC BY 4.0)"
    terms_url = "https://dati.agcm.it/"
    homepage = "https://dati.agcm.it/"
    update_frequency = "weekly"
    update_days = 7
    min_pause_s = 10.0

    def latest(self, manifest: Manifest | None, cancel=None, today: date | None = None) -> RemoteFile:
        cached = manifest.get("listing") if manifest is not None else None
        if isinstance(cached, dict) and cached.get("url"):
            try:
                age = (datetime.now() - datetime.fromisoformat(cached.get("date", ""))).total_seconds()
            except ValueError:
                age = None
            if age is not None and 0 <= age < LISTING_MAX_AGE_S:
                return RemoteFile(cached["url"], LOCAL_NAME, period=cached.get("period", ""))
        today = today or date.today()
        for back in range(MONTHS_BACK + 1):
            year, month = _month_back(today, back)
            reply = self.gateway.get(DATASET_URL.format(year=year, month=month), cancel=cancel)
            if reply.status != 200:
                continue
            links = csv_links(reply.text)
            if links:
                url, _name, day = links[0]
                if manifest is not None:
                    manifest.set("listing", {"date": now_iso(), "url": url, "period": day})
                return RemoteFile(url, LOCAL_NAME, period=day)
        raise ValueError("no rating list found on the AGCM dataset pages of the last months")

    def remote_files(self, manifest: Manifest | None = None, cancel=None) -> list[RemoteFile]:
        return [self.latest(manifest, cancel)]

    def cheap_check_needs_head(self) -> bool:
        return False   # a new weekly list has a new URL

    def parse(self, files, region_filter, progress=None, cancel=None) -> Iterator[RegisterRow]:
        wanted = set(region_filter) if region_filter else None
        best: dict[str, tuple[str, dict]] = {}
        today = date.today().isoformat()
        source_date = ""
        for path in files:
            path = Path(path)
            try:
                raw, total, _ = open_member(path, (".csv",))
                try:
                    data = raw.read()
                finally:
                    raw.close()
            except Exception as error:  # noqa: BLE001
                self.parse_error(path.name, f"{type(error).__name__}: {error}")
                continue
            text = data.decode("utf-8-sig", errors="replace")
            match = re.search(r"Aggiornamento del (\d{2}-\d{2}-\d{4})", text[:2000])
            if match:
                source_date = iso_date(match.group(1))
            elif not source_date:
                source_date = datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
            rows = list(csv.reader(io.StringIO(text), delimiter=";"))
            header_at = next((i for i, row in enumerate(rows[:10])
                              if any(clean_text(c).lower() == "codice fiscale" for c in row)), None)
            if header_at is None:
                self.parse_error(path.name, "no header with \"Codice fiscale\"")
                continue
            header = [clean_text(c).lower() for c in rows[header_at]]

            def col(name: str) -> int | None:
                return header.index(name) if name in header else None

            idx = {key: col(name) for key, name in (
                ("cf", "codice fiscale"), ("seat", "sede legale"), ("name", "società"), ("decided", "data decisione"),
                ("outcome", "esito"), ("rating", "rating"), ("until", "scadenza"), ("revoked", "revoca"),
                ("annulled", "annullamento"), ("suspended", "sospensione"), ("location", "location"))}
            for row in rows[header_at + 1:]:
                def get(key: str) -> str:
                    i = idx.get(key)
                    return clean_text(row[i]) if i is not None and i < len(row) else ""

                cf = company_cf(get("cf"))
                if not cf:
                    continue
                seat = get("seat")
                found = re.match(r"(.*?)\s*\(([A-Za-z]{2})\)\s*$", seat)
                municipality, province = (found.group(1), found.group(2).upper()) if found else (seat, "")
                region = region_of_province(province)
                if wanted is not None and region not in wanted:
                    continue
                state = "valid"
                if get("revoked"):
                    state = "revoked"
                elif get("annulled"):
                    state = "annulled"
                elif get("suspended"):
                    state = "suspended"
                until = iso_date(get("until"))
                if state == "valid" and until and until < today:
                    state = "expired"
                fields = {"name": get("name"), "municipality": municipality, "province": province, "region": region,
                          "legality_rating": get("rating"), "legality_until": until, "legality_state": state}
                loc = re.match(r"\(?\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)?", get("location"))
                if loc:
                    fields["lat"], fields["lon"] = loc.group(1), loc.group(2)
                decided = iso_date(get("decided"))
                previous = best.get(cf)
                if previous is None or decided >= previous[0]:
                    best[cf] = (decided, {k: v for k, v in fields.items() if v})
            notify(progress, RegisterProgress(self.id, "parse", file=path.name, done_bytes=total, total_bytes=total,
                                              rows=len(best)))
        self.stats = {"companies": len(best)}
        for cf, (_decided, fields) in best.items():
            yield RegisterRow(self.id, cf, fields, source_date=source_date)


__all__ = ["AgcmSource", "DATASET_URL", "csv_links"]
