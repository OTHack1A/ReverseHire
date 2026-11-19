"""Static Italian codes used by the regional register (no network).

- ISTAT region codes (3 digits, "008" = Emilia-Romagna) and province codes (3 digits,
  "037" = Bologna; metropolitan cities use 200 + the province code, "237").
- Postcode (CAP) -> region and, where the first digits are unambiguous, the province.
  Emilia-Romagna is exact: 29 PC, 40 BO, 41 MO, 42 RE, 43 PR, 44 FE, 48 RA, and 47 is
  split into FC (470xx, 471xx, 475xx), RN (478xx, 479xx) and San Marino (4789x, not
  Italy). Elsewhere a few two-digit prefixes cover two provinces; they are split by the
  third or fourth digit where the split is well known, otherwise only the region is
  returned (the province then comes from another field or source).
- Tax office (digits 8-10 of an 11-digit company tax code / VAT number): the ISTAT code
  of the province where the company was first registered (study A.2: 89-96 % agreement
  with GLEIF in Emilia-Romagna). Provinces created later (RN from FC in 1992, LC/LO from
  CO/MI, BT, FM, MB...) share the old code, so the answer is a list of candidates and is
  always flagged as an approximation ("provincia presunta").
"""

from __future__ import annotations

import re

from ..geo import italy

# ISTAT region code -> region key of geo/regions.py
REGION_ISTAT: dict[str, str] = {
    "001": "piemonte", "002": "valle-d-aosta", "003": "lombardia", "004": "trentino-alto-adige",
    "005": "veneto", "006": "friuli-venezia-giulia", "007": "liguria", "008": "emilia-romagna",
    "009": "toscana", "010": "umbria", "011": "marche", "012": "lazio", "013": "abruzzo",
    "014": "molise", "015": "campania", "016": "puglia", "017": "basilicata", "018": "calabria",
    "019": "sicilia", "020": "sardegna",
}

# ISTAT province code -> sigla (2016 codes; old Sardinian codes kept for older data).
PROVINCE_ISTAT: dict[str, str] = {
    "001": "TO", "002": "VC", "003": "NO", "004": "CN", "005": "AT", "006": "AL", "007": "AO",
    "008": "IM", "009": "SV", "010": "GE", "011": "SP", "012": "VA", "013": "CO", "014": "SO",
    "015": "MI", "016": "BG", "017": "BS", "018": "PV", "019": "CR", "020": "MN", "021": "BZ",
    "022": "TN", "023": "VR", "024": "VI", "025": "BL", "026": "TV", "027": "VE", "028": "PD",
    "029": "RO", "030": "UD", "031": "GO", "032": "TS", "033": "PC", "034": "PR", "035": "RE",
    "036": "MO", "037": "BO", "038": "FE", "039": "RA", "040": "FC", "041": "PU", "042": "AN",
    "043": "MC", "044": "AP", "045": "MS", "046": "LU", "047": "PT", "048": "FI", "049": "LI",
    "050": "PI", "051": "AR", "052": "SI", "053": "GR", "054": "PG", "055": "TR", "056": "VT",
    "057": "RI", "058": "RM", "059": "LT", "060": "FR", "061": "CE", "062": "BN", "063": "NA",
    "064": "AV", "065": "SA", "066": "AQ", "067": "TE", "068": "PE", "069": "CH", "070": "CB",
    "071": "FG", "072": "BA", "073": "TA", "074": "BR", "075": "LE", "076": "PZ", "077": "MT",
    "078": "CS", "079": "CZ", "080": "RC", "081": "TP", "082": "PA", "083": "ME", "084": "AG",
    "085": "CL", "086": "EN", "087": "CT", "088": "RG", "089": "SR", "090": "SS", "091": "NU",
    "092": "CA", "093": "PN", "094": "IS", "095": "OR", "096": "BI", "097": "LC", "098": "LO",
    "099": "RN", "100": "PO", "101": "KR", "102": "VV", "103": "VB", "108": "MB", "109": "FM",
    "110": "BT", "111": "SU",
}

# Tax-office code -> candidate provinces (newer provinces carved out of an old one keep
# the old office code for companies registered before the split).
_OFFICE_SPLITS: dict[str, tuple[str, ...]] = {
    "002": ("VC", "BI"), "003": ("NO", "VB"), "013": ("CO", "LC"), "015": ("MI", "MB", "LO"),
    "040": ("FC", "RN"), "044": ("AP", "FM"), "048": ("FI", "PO"), "071": ("FG", "BT"),
    "072": ("BA", "BT"), "079": ("CZ", "KR", "VV"), "092": ("CA", "SU"), "095": ("OR", "SU"),
    "120": ("BO",),     # second Bologna office code, verified in the study (A.2)
}

# Two-digit postcode prefix -> (region key, province or "" when the prefix is shared).
_CAP2: dict[str, tuple[str, str]] = {
    "00": ("lazio", "RM"), "01": ("lazio", "VT"), "02": ("lazio", "RI"), "03": ("lazio", "FR"),
    "04": ("lazio", "LT"), "05": ("umbria", "TR"), "06": ("umbria", "PG"), "07": ("sardegna", ""),
    "08": ("sardegna", ""), "09": ("sardegna", ""), "10": ("piemonte", "TO"),
    "11": ("valle-d-aosta", "AO"), "12": ("piemonte", "CN"), "13": ("piemonte", ""),
    "14": ("piemonte", "AT"), "15": ("piemonte", "AL"), "16": ("liguria", "GE"),
    "17": ("liguria", "SV"), "18": ("liguria", "IM"), "19": ("liguria", "SP"),
    "20": ("lombardia", "MI"), "21": ("lombardia", "VA"), "22": ("lombardia", "CO"),
    "23": ("lombardia", ""), "24": ("lombardia", "BG"), "25": ("lombardia", "BS"),
    "26": ("lombardia", ""), "27": ("lombardia", "PV"), "28": ("piemonte", ""),
    "29": ("emilia-romagna", "PC"), "30": ("veneto", "VE"), "31": ("veneto", "TV"),
    "32": ("veneto", "BL"), "33": ("friuli-venezia-giulia", "UD"),
    "34": ("friuli-venezia-giulia", ""), "35": ("veneto", "PD"), "36": ("veneto", "VI"),
    "37": ("veneto", "VR"), "38": ("trentino-alto-adige", "TN"), "39": ("trentino-alto-adige", "BZ"),
    "40": ("emilia-romagna", "BO"), "41": ("emilia-romagna", "MO"), "42": ("emilia-romagna", "RE"),
    "43": ("emilia-romagna", "PR"), "44": ("emilia-romagna", "FE"), "45": ("veneto", "RO"),
    "46": ("lombardia", "MN"), "47": ("emilia-romagna", ""), "48": ("emilia-romagna", "RA"),
    "50": ("toscana", "FI"), "51": ("toscana", "PT"), "52": ("toscana", "AR"), "53": ("toscana", "SI"),
    "54": ("toscana", "MS"), "55": ("toscana", "LU"), "56": ("toscana", "PI"), "57": ("toscana", "LI"),
    "58": ("toscana", "GR"), "59": ("toscana", "PO"), "60": ("marche", "AN"), "61": ("marche", "PU"),
    "62": ("marche", "MC"), "63": ("marche", ""), "64": ("abruzzo", "TE"), "65": ("abruzzo", "PE"),
    "66": ("abruzzo", "CH"), "67": ("abruzzo", "AQ"), "70": ("puglia", "BA"), "71": ("puglia", "FG"),
    "72": ("puglia", "BR"), "73": ("puglia", "LE"), "74": ("puglia", "TA"), "75": ("basilicata", "MT"),
    "76": ("puglia", "BT"), "80": ("campania", "NA"), "81": ("campania", "CE"),
    "82": ("campania", "BN"), "83": ("campania", "AV"), "84": ("campania", "SA"),
    "85": ("basilicata", "PZ"), "86": ("molise", ""), "87": ("calabria", "CS"), "88": ("calabria", ""),
    "89": ("calabria", ""), "90": ("sicilia", "PA"), "91": ("sicilia", "TP"), "92": ("sicilia", "AG"),
    "93": ("sicilia", "CL"), "94": ("sicilia", "EN"), "95": ("sicilia", "CT"), "96": ("sicilia", "SR"),
    "97": ("sicilia", "RG"), "98": ("sicilia", "ME"),
}

# Longer prefixes that split a shared (or overridden) two-digit prefix; longest wins.
_CAP_SPLITS: dict[str, str] = {
    # Emilia-Romagna / San Marino
    "470": "FC", "471": "FC", "475": "FC", "478": "RN", "479": "RN", "4789": "SM",
    # Piemonte / Lombardia / Friuli
    "130": "VC", "131": "VC", "138": "BI", "139": "BI", "208": "MB", "209": "MB",
    "230": "SO", "231": "SO", "238": "LC", "239": "LC", "260": "CR", "261": "CR",
    "268": "LO", "269": "LO", "280": "NO", "281": "NO", "288": "VB", "289": "VB",
    "3307": "PN", "3308": "PN", "3309": "PN", "3317": "PN", "340": "TS", "341": "TS",
    "3407": "GO", "3417": "GO",
    # Marche / Calabria
    "630": "AP", "631": "AP", "638": "FM", "639": "FM", "888": "KR", "889": "KR",
    "880": "CZ", "881": "CZ", "890": "RC", "891": "RC", "898": "VV", "899": "VV",
}

_NON_ITALIAN = {"SM"}


def _digits(text) -> str:
    return re.sub(r"\D", "", str(text or ""))


def postcode_info(postcode) -> tuple[str, str]:
    """(region key, province sigla) of an Italian CAP; ("", "") when not a CAP or not
    Italian (San Marino); the province is "" when the CAP does not say it."""
    match = re.search(r"(?<!\d)(\d{5})(?!\d)", str(postcode or ""))
    if not match:
        return "", ""
    cap = match.group(1)
    region, province = _CAP2.get(cap[:2], ("", ""))
    if not region:
        return "", ""
    for length in (4, 3):
        split = _CAP_SPLITS.get(cap[:length])
        if split:
            if split in _NON_ITALIAN:
                return "", ""
            province = split
            break
    return region, province


def province_from_postcode(postcode) -> str:
    return postcode_info(postcode)[1]


def region_from_postcode(postcode) -> str:
    return postcode_info(postcode)[0]


def province_from_istat(code) -> str:
    """Sigla of an ISTAT province code ("037", "37", metropolitan "237"); "" if unknown."""
    digits = _digits(code)
    if not digits:
        return ""
    value = int(digits)
    for candidate in (value, value - 200 if value > 200 else -1):
        if candidate > 0:
            sigla = PROVINCE_ISTAT.get(f"{candidate:03d}")
            if sigla:
                return sigla
    return ""


def comune_istat_info(code) -> tuple[str, str, str]:
    """(region key, province sigla, 6-digit comune code) of an ISTAT municipality code
    written with the region (9 digits "008040007") or without it (6 digits "040007")."""
    digits = _digits(code)
    region = ""
    if len(digits) == 9:
        region = REGION_ISTAT.get(digits[:3], "")
        digits = digits[3:]
    if len(digits) != 6:
        return region, "", ""
    province = province_from_istat(digits[:3])
    if not region and province:
        region = region_of_province(province)
    return region, province, digits


def region_of_province(sigla: str) -> str:
    found = italy.province((sigla or "").upper())
    return found.region if found is not None else ""


def region_key(text: str) -> str:
    """Region key for a region name ("Emilia-Romagna", "EMILIA ROMAGNA", a key)."""
    value = (text or "").strip()
    if not value:
        return ""
    try:
        found = italy.region_key(value)
    except Exception:  # noqa: BLE001 - an odd name is just unknown
        found = None
    if found:
        return found
    folded = italy.fold(value).replace(" ", "-")
    for key in REGION_ISTAT.values():
        if key == folded or key.replace("-", "") == folded.replace("-", ""):
            return key
    if any(word in folded for word in ("trento", "bolzano", "bozen", "trentino", "sudtirol")):
        return "trentino-alto-adige"
    if "aosta" in folded or "aoste" in folded:
        return "valle-d-aosta"
    if "friuli" in folded:
        return "friuli-venezia-giulia"
    return ""


def region_keys(text: str) -> list[str]:
    """Region keys of a list such as "Emilia-Romagna, Lombardia" (RNA aids split over
    several regions); unknown parts (foreign regions) are left out."""
    keys = []
    for part in str(text or "").split(","):
        key = region_key(part)
        if key and key not in keys:
            keys.append(key)
    return keys


def tax_office_provinces(code) -> tuple[str, ...]:
    """Candidate provinces from the tax-office digits (8-10) of an 11-digit code; empty
    when unknown. Always an approximation (office of first registration)."""
    digits = _digits(code)
    if len(digits) != 11:
        return ()
    office = digits[7:10]
    if office in _OFFICE_SPLITS:
        return _OFFICE_SPLITS[office]
    sigla = PROVINCE_ISTAT.get(office)
    return (sigla,) if sigla else ()


def provinces_of_regions(regions) -> set[str]:
    result: set[str] = set()
    for key in regions or ():
        result.update(p.code for p in italy.provinces_of_region(key))
    return result


__all__ = ["PROVINCE_ISTAT", "REGION_ISTAT", "comune_istat_info", "postcode_info", "province_from_istat",
           "province_from_postcode", "provinces_of_regions", "region_from_postcode", "region_key",
           "region_of_province", "tax_office_provinces"]
