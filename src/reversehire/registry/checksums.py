"""Italian company identifiers: normalisation and checksum validation.

- partita IVA: 11 digits, last one a Luhn-style check digit (the same check applies to
  the 11-digit codice fiscale of companies, which equals their Registro Imprese number);
- codice fiscale of a person (sole traders): 16 characters with a check letter
  (DM 23/12/1976), including the "omocodia" letters;
- REA: one number per province, written "BO-472965" (province code + number).

Pure functions, no network.
"""

from __future__ import annotations

import re

# Province codes (sigle automobilistiche) used in REA numbers.
PROVINCES = frozenset((
    "AG AL AN AO AP AQ AR AT AV BA BG BI BL BN BO BR BS BT BZ CA CB CE CH CL CN CO CR CS CT CZ "
    "EN FC FE FG FI FM FO FR GE GO GR IM IS KR LC LE LI LO LT LU MB MC ME MI MN MO MS MT NA NO NU "
    "OR PA PC PD PE PG PI PN PO PR PT PU PV PZ RA RC RE RG RI RM RN RO SA SI SO SP SR SS SU SV TA "
    "TE TN TO TP TR TS TV UD VA VB VC VE VI VR VT VV"
).split())

_ODD = {
    **{str(d): v for d, v in zip(range(10), (1, 0, 5, 7, 9, 13, 15, 17, 19, 21))},
    **dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
               (1, 0, 5, 7, 9, 13, 15, 17, 19, 21, 2, 4, 18, 20, 11, 3, 6, 8, 12, 14, 16, 10, 22,
                25, 24, 23))),
}
_EVEN = {**{str(d): d for d in range(10)}, **{c: i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")}}
_PERSONAL_CF = re.compile(
    r"[A-Z]{6}[0-9LMNPQRSTUV]{2}[A-EHLMPRST][0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{3}[A-Z]"
)


def digits_only(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def vat_check_digit(first_ten: str) -> int:
    """Check digit of an Italian partita IVA (or numeric codice fiscale) from its first 10 digits."""
    total = 0
    for i, c in enumerate(first_ten):
        d = int(c)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10


def piva_ok(value: str | None) -> bool:
    """True for 11 digits with a valid check digit (not all zeros)."""
    text = (value or "").strip()
    if not re.fullmatch(r"\d{11}", text) or text == "0" * 11:
        return False
    return vat_check_digit(text[:10]) == int(text[10])


def personal_cf_ok(value: str | None) -> bool:
    """True for a 16-character codice fiscale of a person with a valid check letter."""
    text = (value or "").strip().upper()
    if not _PERSONAL_CF.fullmatch(text):
        return False
    total = sum(_ODD[c] if i % 2 == 0 else _EVEN[c] for i, c in enumerate(text[:15]))
    return chr(ord("A") + total % 26) == text[15]


def cf_ok(value: str | None) -> bool:
    """Valid codice fiscale: 11 digits (companies) or 16 characters (persons)."""
    text = (value or "").strip().upper()
    return piva_ok(text) if len(text) == 11 else personal_cf_ok(text)


def is_personal_cf(value: str | None) -> bool:
    """A 16-character codice fiscale identifies a person (personal data)."""
    return len((value or "").strip()) == 16


def normalize_vat(value: str | None) -> str:
    """'IT 00159560366', 'it00159560366' -> '00159560366'; other text -> stripped digits."""
    text = re.sub(r"[\s.\-]", "", (value or "").strip().upper())
    if text.startswith("IT"):
        text = text[2:]
    return text if re.fullmatch(r"\d{11}", text) else digits_only(text)


def normalize_cf(value: str | None) -> str:
    return re.sub(r"\s", "", (value or "").strip().upper())


def normalize_rea(value: str | None, province: str | None = None) -> str:
    """'REA BO 472965', 'bo-0472965', ('472965', 'BO') -> 'BO-472965'; number only when
    the province is unknown. '' when no number is found."""
    text = (value or "").strip().upper()
    found = re.search(r"\b([A-Z]{2})\s*[-–/ ]?\s*(\d{3,7})\b", text)
    number = ""
    prov = (province or "").strip().upper()
    if found and found.group(1) in PROVINCES:
        prov, number = found.group(1), found.group(2)
    else:
        digits = re.search(r"(\d{3,7})", text)
        number = digits.group(1) if digits else ""
    if not number:
        return ""
    number = number.lstrip("0") or "0"
    return f"{prov}-{number}" if prov in PROVINCES else number


def rea_parts(value: str | None) -> tuple[str, str]:
    """('BO', '472965') from a normalised REA ('' province when unknown)."""
    text = normalize_rea(value)
    if "-" in text:
        prov, number = text.split("-", 1)
        return prov, number
    return "", text


def rea_equal(a: str | None, b: str | None) -> bool:
    """Same REA: same number and, when both state it, same province."""
    pa, na = rea_parts(a)
    pb, nb = rea_parts(b)
    if not na or not nb or na != nb:
        return False
    return not pa or not pb or pa == pb


_ATECO = re.compile(r"\b(\d{2}(?:\.\d{1,2}){0,2})\b")


def normalize_ateco(value: str | None) -> str:
    """First ATECO code in the text ('28.29.30 - Fabbricazione…' -> '28.29.30'); '' if none."""
    found = _ATECO.search((value or "").strip())
    return found.group(1) if found else ""


def ateco_ok(value: str | None) -> bool:
    return bool(re.fullmatch(r"\d{2}(?:\.\d{1,2}){0,2}", (value or "").strip()))
