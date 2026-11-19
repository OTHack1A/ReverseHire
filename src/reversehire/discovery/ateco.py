"""ATECO 2025 (the Italian NACE) table and the profile -> "categories of interest" mapping.

The owner's method is territorial: identify the kind of companies of interest, find
their category in the register (REA / Registro Imprese uses ATECO), then look at the
companies of that category in the area. This module gives the categories:

- `describe(code)`, `search(text)`: the official ISTAT ATECO 2025 structure (codes and
  Italian/English titles), shipped as `data/ateco_2025.csv`. Source: ISTAT,
  "StrutturaATECO-2025-IT-EN-DE.xlsx", licence CC BY 4.0 (attribution "Fonte: ISTAT").
  `build_table_from_xlsx` rebuilds the CSV from that workbook.
- `ateco_for_profile(profile)`: ATECO codes proposed for a CV profile, from the
  app's sectors (`geo/sectors.py`) and from role/skill keywords, each with a weight
  0..1 and the reason. They are CATEGORIES OF INTEREST, chosen by the user: the
  ATECO code of a specific company is never guessed here; it only comes from a
  register (an imported Registro Imprese list).

Codes are compared by prefix on the dotted form: "28.29" covers "28.29.30".
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..geo.sectors import SECTORS, normalize_text, sector_scores

DATA_FILE = Path(__file__).parent / "data" / "ateco_2025.csv"
SOURCE_URL = "https://www.istat.it/wp-content/uploads/2025/02/StrutturaATECO-2025-IT-EN-DE.xlsx"
LICENCE_NOTE = "ATECO 2025 classification: Fonte ISTAT, licence CC BY 4.0"
MAX_PROPOSED = 15
MIN_WEIGHT = 0.15


@dataclass(frozen=True)
class AtecoCode:
    """An ATECO code with its titles, a weight 0..1 for the profile and why it is proposed."""

    code: str
    name_en: str
    name_it: str
    weight: float = 1.0
    why: str = ""

    def to_dict(self) -> dict:
        return {"code": self.code, "name_en": self.name_en, "name_it": self.name_it,
                "weight": self.weight, "why": self.why}

    @classmethod
    def from_dict(cls, d: dict) -> "AtecoCode":
        return cls(code=str(d.get("code", "")), name_en=str(d.get("name_en", "")),
                   name_it=str(d.get("name_it", "")), weight=float(d.get("weight", 1.0) or 0.0),
                   why=str(d.get("why", "")))


# --- the table ------------------------------------------------------------------------


@dataclass(frozen=True)
class _Row:
    code: str
    level: int
    parent: str
    title_it: str
    title_en: str


@lru_cache(maxsize=1)
def _table() -> dict[str, _Row]:
    rows: dict[str, _Row] = {}
    try:
        with DATA_FILE.open(encoding="utf-8", newline="") as handle:
            for record in csv.DictReader(handle):
                code = (record.get("code") or "").strip()
                if not code:
                    continue
                rows[code] = _Row(code, int(record.get("level") or 0), (record.get("parent") or "").strip(),
                                  (record.get("title_it") or "").strip(), (record.get("title_en") or "").strip())
    except OSError:
        return {}
    return rows


def table_size() -> int:
    return len(_table())


_CODE_RE = re.compile(r"^\s*([A-Ua-u]|\d{2}(?:\.\d{1,2}(?:\.\d{1,2})?)?)\s*$")


def normalize_code(text: str) -> str:
    """"2829.30" / "28.29.3" / " 28 29 30 " -> dotted ATECO form ("28.29.30"); "" if not a code.

    Register exports write codes in several ways ("282930", "28.29.30", "28.29.3").
    """
    raw = (text or "").strip().upper()
    if not raw:
        return ""
    if len(raw) == 1 and raw.isalpha():
        return raw
    digits = re.sub(r"[^0-9]", "", raw)
    if not digits or len(digits) < 2 or len(digits) > 6:
        return ""
    if "." in raw:
        parts = [p for p in re.split(r"[^0-9]+", raw) if p]
        if len(parts) >= 2 and len(parts[0]) == 2:
            first = parts[0]
            second = parts[1]
            if len(second) == 1:
                return f"{first}.{second}"
            code = f"{first}.{second[:2]}"
            rest = second[2:] + "".join(parts[2:])
            return f"{code}.{rest[:2]}" if rest else code
    # Undotted digits: 28 / 282 / 2829 / 28293 / 282930
    if len(digits) == 2:
        return digits
    if len(digits) == 3:
        return f"{digits[:2]}.{digits[2]}"
    if len(digits) == 4:
        return f"{digits[:2]}.{digits[2:]}"
    return f"{digits[:2]}.{digits[2:4]}.{digits[4:]}"


def describe(code: str) -> AtecoCode | None:
    """Titles of a code ("28.29.30", "282930", "28.29"); None when it is not in the table."""
    normalized = normalize_code(code)
    row = _table().get(normalized)
    if row is None:
        return None
    return AtecoCode(row.code, row.title_en, row.title_it)


def level(code: str) -> int:
    row = _table().get(normalize_code(code))
    return row.level if row else 0


def search(text: str, limit: int = 30) -> list[AtecoCode]:
    """Codes whose code starts with `text` or whose Italian/English title contains every
    word of `text` (accents and case ignored); classes and subcategories first."""
    query = (text or "").strip()
    if not query:
        return []
    results: list[tuple[int, str, _Row]] = []
    code_query = normalize_code(query) if re.fullmatch(r"[\d.\s]+", query) else ""
    words = normalize_text(query).split()
    for row in _table().values():
        if code_query and row.code.startswith(code_query):
            results.append((0, row.code, row))
            continue
        if not words:
            continue
        haystack = normalize_text(f"{row.title_it} {row.title_en}")
        if all(word in haystack for word in words):
            # Prefer classes (level 4) and subcategories (6) over sections/divisions.
            results.append((1 if row.level in (4, 6) else 2, row.code, row))
    results.sort(key=lambda item: (item[0], item[1]))
    return [AtecoCode(r.code, r.title_en, r.title_it) for _, _, r in results[:limit]]


def matches(company_code: str, chosen: list[str] | set[str]) -> bool:
    """True when a company's register ATECO code falls under one of the chosen codes (prefix)."""
    code = normalize_code(company_code)
    if not code:
        return False
    for wanted in chosen or ():
        prefix = normalize_code(wanted)
        if prefix and (code == prefix or code.startswith(prefix + ".") or
                       (len(prefix) >= 2 and code.startswith(prefix) and
                        (len(code) == len(prefix) or code[len(prefix)] in ".0123456789"))):
            return True
    return False


# --- profile -> categories of interest ------------------------------------------------
#
# Hand-made, reviewed against the ISTAT table (every code exists: see the tests).
# Weights: 1.0 = core employers of the sector, 0.7 = frequent, 0.4 = possible.

SECTOR_ATECO: dict[str, list[tuple[str, float]]] = {
    "administration_accounting": [("69.20", 1.0), ("82.10", 0.7), ("70.20", 0.5), ("64.19", 0.3)],
    "it_software": [("62.10", 1.0), ("62.20", 1.0), ("62.90", 0.8), ("58.29", 0.8), ("63.10", 0.7),
                    ("61.10", 0.4), ("26.20", 0.4)],
    "engineering_design": [("71.12", 1.0), ("72.10", 0.8), ("74.14", 0.7), ("71.20", 0.6),
                           ("74.11", 0.5), ("28.99", 0.4)],
    "manufacturing_metalworking": [("25.62", 0.6), ("25.53", 1.0), ("25.11", 0.8), ("25.99", 0.7),
                                   ("28.41", 0.8), ("28.99", 0.7), ("25.40", 0.6), ("24.10", 0.4),
                                   ("33.11", 0.5), ("33.12", 0.6)],
    "construction": [("41.00", 1.0), ("43.99", 0.8), ("42.99", 0.7), ("43.91", 0.7), ("43.34", 0.5),
                     ("43.41", 0.5), ("71.11", 0.3)],
    "wood_furniture": [("31.00", 1.0), ("16.23", 0.8), ("16.25", 0.7), ("16.11", 0.5),
                       ("43.32", 0.6), ("95.24", 0.4)],
    "electrical_plumbing": [("43.21", 1.0), ("43.22", 1.0), ("33.14", 0.6), ("27.12", 0.5),
                            ("43.24", 0.4)],
    "healthcare": [("86.10", 1.0), ("86.21", 0.8), ("86.22", 0.8), ("86.23", 0.7), ("86.91", 0.7),
                   ("86.94", 0.7), ("86.95", 0.7), ("87.10", 0.6), ("87.30", 0.6)],
    "pharmacy": [("47.73", 1.0), ("46.46", 0.6), ("21.20", 0.5), ("47.74", 0.4)],
    "education_training": [("85.31", 1.0), ("85.20", 0.8), ("85.10", 0.7), ("85.40", 0.7),
                           ("85.59", 0.8), ("85.32", 0.7), ("85.53", 0.4), ("88.91", 0.4)],
    "retail": [("47.11", 1.0), ("47.71", 0.8), ("47.12", 0.7), ("47.78", 0.6),
               ("47.52", 0.5), ("47.54", 0.5)],
    "food_hospitality": [("56.11", 1.0), ("56.30", 0.8), ("55.10", 0.9), ("56.21", 0.6),
                         ("56.22", 0.6), ("55.20", 0.5)],
    "logistics_transport": [("52.10", 1.0), ("52.25", 1.0), ("49.41", 1.0),
                            ("52.24", 0.7), ("53.20", 0.7), ("52.21", 0.5), ("82.92", 0.4)],
    "automotive_repair": [("95.31", 1.0), ("47.82", 0.6), ("46.72", 0.5),
                          ("95.32", 0.6), ("29.32", 0.4)],
    "banking_insurance": [("64.19", 1.0), ("65.12", 0.8), ("66.22", 0.9), ("65.11", 0.7),
                          ("66.19", 0.6), ("64.92", 0.5)],
    "law_firms": [("69.10", 1.0)],
    "architecture_design": [("71.11", 1.0), ("74.13", 0.8), ("74.11", 0.5), ("71.12", 0.4)],
    "marketing_communication": [("73.11", 1.0), ("73.30", 0.9), ("74.12", 0.8), ("73.20", 0.6),
                                ("59.11", 0.5), ("74.20", 0.5), ("58.13", 0.4)],
    "agriculture_food": [("10.51", 0.8), ("10.13", 0.8), ("10.71", 0.7), ("10.89", 0.6),
                         ("11.02", 0.7), ("01.50", 0.6), ("01.21", 0.5), ("01.13", 0.5),
                         ("10.73", 0.6), ("10.39", 0.5)],
    "chemicals_materials": [("20.59", 0.8), ("20.30", 0.8), ("20.16", 0.7),
                            ("22.26", 0.8), ("22.21", 0.7), ("21.20", 0.7), ("23.31", 0.8),
                            ("20.42", 0.6), ("71.20", 0.5)],
    "energy_environment": [("35.12", 1.0), ("35.11", 0.6), ("35.14", 0.6), ("38.11", 0.7),
                           ("38.21", 0.7), ("39.00", 0.6), ("43.21", 0.4), ("71.12", 0.4)],
    "real_estate": [("68.31", 1.0), ("68.32", 1.0), ("68.20", 0.7), ("68.12", 0.5)],
    "beauty_wellness": [("96.21", 1.0), ("96.22", 1.0), ("93.13", 0.8), ("96.23", 0.6),
                        ("93.11", 0.5)],
    "public_administration": [("84.11", 1.0), ("84.12", 0.6), ("84.13", 0.5), ("84.24", 0.4)],
    "non_profit": [("88.99", 1.0), ("88.10", 0.8), ("94.99", 0.8), ("87.99", 0.6), ("94.12", 0.4)],
    "hr_consulting": [("78.10", 1.0), ("78.20", 1.0), ("70.20", 0.9), ("82.99", 0.4)],
    "electronics_automation": [
        ("28.29.30", 1.0), ("28.99.20", 1.0), ("28.99.99", 0.9), ("33.20.09", 1.0),
        ("33.20.03", 0.9), ("33.20.01", 0.8), ("27.12", 1.0), ("71.12.10", 0.9),
        ("28.93", 0.9), ("28.95", 0.8), ("28.96", 0.8), ("28.41", 0.8), ("28.42", 0.7),
        ("28.22.09", 0.8), ("26.51", 0.7), ("26.11", 0.6), ("26.12", 0.7), ("26.20", 0.5),
        ("28.12", 0.6), ("28.15", 0.5), ("62.10", 0.6), ("33.12", 0.6), ("33.14", 0.6),
        ("27.90", 0.5), ("29.31", 0.5), ("72.10", 0.5), ("46.64", 0.3), ("43.21", 0.3),
    ],
    "textile_fashion": [("14.21", 1.0), ("14.29", 0.7), ("15.20", 0.8), ("15.12", 0.7), ("13.20", 0.6),
                        ("13.91", 0.6), ("74.11", 0.4), ("46.42", 0.4)],
    "tourism_travel": [("79.11", 1.0), ("79.12", 1.0), ("79.90", 0.7), ("55.10", 0.5)],
    "veterinary_animals": [("75.00", 1.0), ("47.76", 0.6), ("96.99", 0.3), ("01.48", 0.3)],
}

# Role / skill keywords -> codes (in addition to the sector table). The automation set
# follows the sources study (B.3). Keywords are matched on normalised phrases.
KEYWORD_ATECO: list[tuple[tuple[str, ...], list[tuple[str, float]], str]] = [
    (("plc", "scada", "hmi", "dcs", "automazione industriale", "industrial automation",
      "automation engineer", "ingegnere dell automazione", "tecnico automazione", "automation",
      "automazione", "siemens tia", "tia portal", "step 7", "codesys", "rockwell", "ladder"),
     [("28.29.30", 1.0), ("28.99.20", 1.0), ("33.20.09", 1.0), ("27.12", 0.9), ("71.12.10", 0.8),
      ("28.99.99", 0.8), ("33.20.03", 0.8), ("28.93", 0.7)],
     "automation / PLC keywords"),
    (("robot", "robotica", "robotics", "cobot", "robot programmer", "programmatore robot"),
     [("28.99.20", 1.0), ("28.22.09", 0.6), ("33.20.09", 0.8)], "robotics keywords"),
    (("packaging", "imballaggio", "confezionamento", "packaging machines", "macchine automatiche"),
     [("28.29.30", 1.0), ("33.12.54", 0.7)], "packaging machinery keywords"),
    (("quadri elettrici", "quadrista", "electrical panels", "control panel", "cablaggio"),
     [("27.12", 1.0), ("33.20.01", 0.7), ("43.21", 0.5)], "control panel keywords"),
    (("mes", "industry 4 0", "industria 4 0", "iiot", "opc ua", "manufacturing execution"),
     [("62.10", 0.9), ("62.20", 0.7), ("58.29", 0.6), ("28.99.99", 0.5)], "industrial software keywords"),
    (("embedded", "firmware", "microcontroller", "microcontrollori", "fpga", "pcb", "elettronica"),
     [("26.11", 0.9), ("26.12", 0.9), ("26.51", 0.7), ("29.31", 0.6), ("72.10", 0.5)],
     "electronics / embedded keywords"),
    (("cnc", "machine tool", "macchine utensili", "tornio", "fresa"),
     [("28.41", 0.9), ("28.42", 0.8), ("25.53", 0.8)], "machine-tool keywords"),
    (("agv", "magazzini automatici", "material handling", "warehouse automation", "intralogistica"),
     [("28.22.09", 1.0), ("52.10", 0.4)], "material handling keywords"),
    (("collaudo", "commissioning", "messa in servizio", "field service", "service engineer",
      "tecnico trasfertista", "trasfertista"),
     [("33.20.09", 0.9), ("33.12.54", 0.7), ("33.12.99", 0.6)], "commissioning / service keywords"),
    (("motorsport", "automotive", "autoveicoli", "veicoli"),
     [("29.10", 0.7), ("29.31", 0.7), ("29.32", 0.6)], "automotive keywords"),
    (("software", "developer", "sviluppatore", "software engineer", "python", "java", "c#"),
     [("62.10", 1.0), ("62.20", 0.7), ("58.29", 0.6)], "software keywords"),
    (("progettazione meccanica", "mechanical design", "solidworks", "inventor", "disegnatore"),
     [("71.12.10", 0.9), ("74.14", 0.7), ("28.99.99", 0.5)], "mechanical design keywords"),
]

_PHRASE_WORDS = 4


def _phrases(text: str) -> set[str]:
    words = normalize_text(text).split()
    found: set[str] = set()
    for n in range(1, _PHRASE_WORDS + 1):
        for i in range(len(words) - n + 1):
            found.add(" ".join(words[i:i + n]))
    return found


def _profile_phrases(profile) -> set[str]:
    texts: list[str] = []
    for attribute in ("roles", "skills", "degrees"):
        for item in getattr(profile, attribute, None) or []:
            texts.append(getattr(item, "value", "") or "")
            texts.append(getattr(item, "original", "") or "")
    for experience in getattr(profile, "experiences", None) or []:
        texts.append(getattr(experience, "title", "") or "")
    phrases: set[str] = set()
    for text in texts:
        if text:
            phrases |= _phrases(text)
    return phrases


def _sector_name(key: str) -> str:
    sector = SECTORS.get(key)
    return sector.name_en if sector else key


def ateco_for_profile(profile, sectors: list[str] | None = None,
                      limit: int = MAX_PROPOSED) -> list[AtecoCode]:
    """ATECO categories of interest for the profile, strongest first.

    From the sector scores of the profile (or the given `sectors`, all at full
    strength) and from role/skill keywords. Weight 0..1 = relative strength;
    `why` names the sector or the keywords. Codes missing from the table are skipped.
    """
    best: dict[str, tuple[float, str]] = {}

    def offer(code: str, weight: float, why: str) -> None:
        if weight <= 0:
            return
        row = _table().get(code)
        if row is None:
            return
        current = best.get(code)
        if current is None or weight > current[0] + 1e-9:
            best[code] = (weight, why)

    if sectors:
        strengths = {key: 1.0 for key in sectors if key in SECTORS}
    else:
        scores = sector_scores(profile) if profile is not None else {}
        top = max(scores.values(), default=0.0)
        strengths = {key: value / top for key, value in scores.items() if top > 0 and value / top >= 0.35}
    for key, strength in strengths.items():
        for code, weight in SECTOR_ATECO.get(key, []):
            offer(code, round(min(1.0, strength) * weight, 3), f"sector: {_sector_name(key)}")
    if profile is not None:
        phrases = _profile_phrases(profile)
        for keywords, codes, why in KEYWORD_ATECO:
            hit = next((k for k in keywords if normalize_text(k) in phrases), None)
            if hit is None:
                continue
            for code, weight in codes:
                offer(code, weight, f"{why} (\"{hit}\")")
    ranked = sorted(best.items(), key=lambda item: (-item[1][0], item[0]))
    result: list[AtecoCode] = []
    for code, (weight, why) in ranked:
        if weight < MIN_WEIGHT:
            continue
        row = _table()[code]
        result.append(AtecoCode(code, row.title_en, row.title_it, weight, why))
        if len(result) >= limit:
            break
    return result


def sectors_for_codes(codes: list[str]) -> list[str]:
    """Sector keys whose ATECO list covers one of `codes` (to widen the OSM tag search
    when the user adds a category by hand)."""
    result: list[str] = []
    for key, entries in SECTOR_ATECO.items():
        for code, weight in entries:
            if weight <= 0:
                continue
            if any(matches(code, [c]) or matches(c, [code]) for c in codes):
                if key not in result:
                    result.append(key)
                break
    return result


# --- rebuilding the CSV from the ISTAT workbook -------------------------------------


def build_table_from_xlsx(xlsx_path: str | Path, csv_path: str | Path = DATA_FILE) -> int:
    """Writes `csv_path` (code, level, parent, title_it, title_en) from the ISTAT
    "StrutturaATECO-2025-IT-EN-DE.xlsx" workbook (sheet "ATECO 2025 Struttura").
    The German titles and the ordering column are dropped. Returns the row count."""
    from .xlsx import read_rows

    rows = read_rows(xlsx_path, "ATECO 2025 Struttura")
    header = [h.strip().upper() for h in rows[0]]
    index = {name: header.index(name) for name in (
        "CODICE_ATECO_2025", "TITOLO_ITALIANO_ATECO_2025", "TITOLO_INGLESE_ATECO_2025",
        "GERARCHIA_ATECO_2025", "CODICE_PADRE_ATECO_2025")}
    written = 0
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["code", "level", "parent", "title_it", "title_en"])
        for row in rows[1:]:
            def cell(name: str) -> str:
                position = index[name]
                return " ".join((row[position] if position < len(row) else "").split())
            code = cell("CODICE_ATECO_2025")
            if not code:
                continue
            writer.writerow([code, cell("GERARCHIA_ATECO_2025"), cell("CODICE_PADRE_ATECO_2025"),
                             cell("TITOLO_ITALIANO_ATECO_2025"), cell("TITOLO_INGLESE_ATECO_2025")])
            written += 1
    _table.cache_clear()
    return written
