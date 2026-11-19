"""RNA – Registro Nazionale Aiuti di Stato, open data "Aiuti" (study A.1.1).

Monthly ZIP files, each one XML `<LISTA_AIUTI><AIUTO>…`, declared UTF-8 but with stray
control bytes: the parser decodes incrementally with errors="replace" and strips
[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f] before feeding an XML pull parser, one AIUTO at a time
(files reach 600 MB of XML: never loaded whole).

Kept per company (11-digit tax code only; the 16-character codes of natural persons are
dropped): name, region of the aided unit, SME / large flag, NACE groups (a sector HINT,
not the company's ATECO), number of aids and latest concession date, the most frequent
measures, and every hiring-incentive record (measure title about hiring) as a dated
"hired recently" signal.

Default: the files of the last 24 months available; `options["rna_months"] = 0` takes
the whole archive (about 1-2 GB of downloads).
"""

from __future__ import annotations

import codecs
import re
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ..discovery.gateway import GatewayError
from .base import RegisterSource, UpdateCheck, open_member
from .download import Manifest, RemoteFile
from .model import (RNA, CancelledError, RegisterProgress, RegisterRow, Signal, check_cancel, clean_text, company_cf,
                    iso_date, nace_code, notify, now_iso)
from .italy_codes import region_keys

LISTING_URL = "https://www.incentivitalia.gov.it/open-data/aiuti"
FILE_BASE = "https://www.incentivitalia.gov.it/open-data/aiuti/"
NS = "{http://www.rna.it/RNA_aiuto/schema}"
MAX_PAGES = 40
LISTING_MAX_AGE_S = 12 * 3600
DEFAULT_MONTHS = 24
READ_CHUNK = 4 * 1024 * 1024
PROGRESS_EVERY = 16 * 1024 * 1024

_FILE_RE = re.compile(r"OpenData_Aiuti_(\d{4})_(\d{2})(?:_(\d{3}))?\.zip")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
HIRING = re.compile(r"assunzion|assumere|assunt[ie]\b|neoassunt|incremento occupazional|incentiv\w* occupazional|"
                    r"sost(?:egno|\.)? dell.{0,2}occup|occupazione (giovanile|femminile|stabile)|"
                    r"bonus (giovani|donne)", re.IGNORECASE)


def period_of(name: str) -> str:
    match = _FILE_RE.search(name)
    return f"{match.group(1)}-{match.group(2)}" if match else ""


def files_in_listing(html: str) -> list[str]:
    """ZIP file names linked by a listing page (each once, in page order)."""
    seen: list[str] = []
    for match in _FILE_RE.finditer(html or ""):
        name = match.group(0)
        if name not in seen:
            seen.append(name)
    return seen


def select_recent(names: list[str], months: int | None) -> list[str]:
    """The files of the last `months` periods available (all when months is 0/None),
    oldest first."""
    periods = sorted({period_of(n) for n in names if period_of(n)})
    if months and periods:
        latest = datetime.strptime(periods[-1], "%Y-%m")
        first_index = latest.year * 12 + latest.month - 1 - (int(months) - 1)
        first = f"{first_index // 12:04d}-{first_index % 12 + 1:02d}"
        names = [n for n in names if period_of(n) >= first]
    return sorted(set(names), key=lambda n: (period_of(n), n))


class RnaSource(RegisterSource):
    id = RNA
    licence = "CC BY 4.0"
    attribution = "Registro Nazionale Aiuti di Stato – Incentivi Italia, open data (CC BY 4.0)"
    terms_url = "https://www.incentivitalia.gov.it/open-data/aiuti"
    homepage = LISTING_URL
    update_frequency = "monthly"
    update_days = 30
    compare = ("size",)   # files are regenerated (new Last-Modified) without new content

    @property
    def months(self) -> int:
        value = self.options.get("rna_months", DEFAULT_MONTHS)
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return DEFAULT_MONTHS

    # --- listing

    def _read_listing(self, cancel, pages: int = MAX_PAGES) -> list[str]:
        names: list[str] = []
        for page in range(pages):
            reply = self.gateway.get(LISTING_URL, params={"page": page}, cancel=cancel)
            if reply.status != 200:
                if page == 0:
                    raise GatewayError(f"listing answered HTTP {reply.status}", reply.status)
                break
            new = [n for n in files_in_listing(reply.text) if n not in names]
            if not new:
                break
            names.extend(new)
        return names

    def listing(self, manifest: Manifest | None, cancel=None, max_age_s: float = LISTING_MAX_AGE_S) -> list[str]:
        cached = manifest.get("listing") if manifest is not None else None
        if isinstance(cached, dict) and cached.get("files"):
            try:
                age = (datetime.now() - datetime.fromisoformat(cached.get("date", ""))).total_seconds()
            except ValueError:
                age = None
            if age is not None and 0 <= age < max_age_s:
                return list(cached["files"])
        names = self._read_listing(cancel)
        if manifest is not None and names:
            manifest.set("listing", {"date": now_iso(), "files": names})
        return names

    def remote_files(self, manifest: Manifest | None = None, cancel=None) -> list[RemoteFile]:
        names = select_recent(self.listing(manifest, cancel), self.months)
        return [RemoteFile(FILE_BASE + name, name, period=period_of(name)) for name in names]

    def check_update(self, dest: Path, cancel=None) -> UpdateCheck:
        """One request: the first listing page (newest files first)."""
        manifest = Manifest(dest)
        try:
            newest = self._read_listing(cancel, pages=1)
        except Exception as error:  # noqa: BLE001
            return UpdateCheck(self.id, "failed", detail=f"{type(error).__name__}: {error}")
        known = set(manifest.files)
        if not known:
            return UpdateCheck(self.id, "new", newest)
        latest_known = max((period_of(n) for n in known), default="")
        fresh = [n for n in newest if n not in known and period_of(n) > latest_known]
        return UpdateCheck(self.id, "changed" if fresh else "unchanged", fresh)

    def after_download(self, manifest: Manifest, files: list[RemoteFile]) -> None:
        manifest.set("selected", [f.name for f in files])

    def local_files(self, dest: Path) -> list[Path]:
        manifest = Manifest(dest)
        selected = manifest.get("selected")
        paths = manifest.local_paths()
        if isinstance(selected, list) and selected:
            paths = [p for p in paths if p.name in selected]
        return sorted(paths, key=lambda p: (period_of(p.name), p.name))

    # --- parsing

    @staticmethod
    def iter_aids(path: Path, on_bytes=None, cancel=None) -> Iterator[ET.Element]:
        """AIUTO elements of one file (ZIP or XML), streamed; control bytes removed."""
        raw, _size, _name = open_member(path, (".xml",))
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        parser = ET.XMLPullParser(events=("start", "end"))
        root = None
        done = 0
        try:
            while True:
                check_cancel(cancel)
                chunk = raw.read(READ_CHUNK)
                text = decoder.decode(chunk, final=not chunk)
                if text:
                    parser.feed(_CONTROL.sub("", text))
                for event, elem in parser.read_events():
                    if event == "start":
                        if root is None:
                            root = elem
                        continue
                    if elem.tag == NS + "AIUTO":
                        yield elem
                        if root is not None:
                            root.clear()
                done += len(chunk)
                if on_bytes is not None:
                    on_bytes(done)
                if not chunk:
                    break
            parser.close()
        finally:
            raw.close()

    def parse(self, files, region_filter, progress=None, cancel=None) -> Iterator[RegisterRow]:
        wanted = set(region_filter) if region_filter else None
        companies: dict[str, dict] = {}
        sizes = []
        for path in files:
            try:
                _raw, size, _ = open_member(Path(path), (".xml",))
                _raw.close()
            except Exception:  # noqa: BLE001 - reported when parsed
                size = 0
            sizes.append(size)
        total = sum(sizes) or None
        before = 0
        seen_aids = kept_aids = 0
        latest_period = ""
        for path, size in zip(files, sizes):
            path = Path(path)
            latest_period = max(latest_period, period_of(path.name))
            last_report = [0]

            def on_bytes(done: int, _path=path, _before=before) -> None:
                if done - last_report[0] >= PROGRESS_EVERY:
                    last_report[0] = done
                    notify(progress, RegisterProgress(self.id, "parse", file=_path.name, done_bytes=_before + done,
                                                      total_bytes=total, rows=len(companies)))
            try:
                for aid in self.iter_aids(path, on_bytes, cancel):
                    seen_aids += 1
                    if self._add(aid, companies, wanted):
                        kept_aids += 1
            except CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - ParseError, BadZipFile, OSError...
                self.parse_error(path.name, f"{type(error).__name__}: {error}")
            before += size
        self.stats = {"aids_seen": seen_aids, "aids_kept": kept_aids, "companies": len(companies),
                      "files": len(files)}
        notify(progress, RegisterProgress(self.id, "parse", done_bytes=before, total_bytes=total, rows=len(companies)))
        source_date = latest_period
        for cf, agg in companies.items():
            measures = "; ".join(title if len(title) <= 80 else title[:79] + "…"
                                 for title, _ in agg["measures"].most_common(3))
            fields = {"name": agg["name"], "region": agg["region"], "size_class": agg["size"],
                      "nace_hints": ",".join(sorted(agg["nace"])), "aid_count": str(agg["n"]),
                      "last_aid_date": agg["last"], "aid_measures": measures}
            signals = [Signal("hiring", day, title) for day, title in sorted(agg["hiring"])]
            yield RegisterRow(self.id, cf, {k: v for k, v in fields.items() if v}, signals, source_date)

    @staticmethod
    def _add(aid: ET.Element, companies: dict, wanted: set[str] | None) -> bool:
        def text(tag: str) -> str:
            return (aid.findtext(NS + tag) or "").strip()

        cf = company_cf(text("CODICE_FISCALE_BENEFICIARIO"))
        if not cf:
            return False
        regions = region_keys(clean_text(text("REGIONE_BENEFICIARIO")))
        if wanted is not None:
            regions = [r for r in regions if r in wanted]
            if not regions:
                return False
        region = regions[0] if regions else ""
        day = iso_date(text("DATA_CONCESSIONE"))
        title = clean_text(text("TITOLO_MISURA"))
        agg = companies.get(cf)
        if agg is None:
            agg = companies[cf] = {"name": "", "region": "", "size": "", "nace": set(), "n": 0, "last": "",
                                   "hiring": set(), "measures": Counter()}
        agg["n"] += 1
        if day >= agg["last"]:
            agg["last"] = day
            agg["name"] = clean_text(text("DENOMINAZIONE_BENEFICIARIO")) or agg["name"]
            agg["region"] = region or agg["region"]
            agg["size"] = clean_text(text("DES_TIPO_BENEFICIARIO")) or agg["size"]
        for sector in aid.iter(NS + "SETTORE_ATTIVITA"):
            code = nace_code(sector.text or "")
            if code:
                agg["nace"].add(code)
        if title:
            agg["measures"][title] += 1
            if HIRING.search(title) and day:
                agg["hiring"].add((day, title[:160]))
        return True


__all__ = ["HIRING", "RnaSource", "files_in_listing", "period_of", "select_recent"]
