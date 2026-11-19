"""A company of the regional register as returned by queries, convertible to the
`models.Company` candidate that the discovery sources yield."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Company
from . import texts
from .model import AGCM, FIELDS, RI_BOUGHT, USER

# Card provenance: the source name the company card knows for register data.
CARD_SOURCE = "register"
DISCOVERY_SOURCE = "regional_register"

_HOMEPAGES = {
    "rna": "https://www.incentivitalia.gov.it/open-data/aiuti",
    "gleif": "https://www.gleif.org/en/lei-data/gleif-golden-copy",
    "opencoesione": "https://opencoesione.gov.it/it/opendata/",
    "agcm": "https://dati.agcm.it/",
}
# Fields copied as card facts (with their source), in the card's naming.
_FACTS = {"name": "name", "cf": "tax_code", "vat": "vat", "lei": "lei", "ateco": "ateco", "pec": "pec",
          "rea": "rea", "municipality": "town", "province": "province", "postcode": "postcode",
          "employees_class": "employees", "legal_form": "legal_form", "website": "website", "email": "email",
          "phone": "phone"}


@dataclass
class RegisterCompany:
    key: str                         # the tax code, or "U-…" for a company added without one
    cf: str
    values: dict[str, object]
    provenance: dict[str, tuple[str, str]] = field(default_factory=dict)   # field -> (source, date)
    locked: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    user_added: bool = False
    hidden: bool = False
    province_src: str = ""
    province_guess: tuple[str, ...] = ()
    hiring_dates: tuple[str, ...] = ()

    def get(self, name: str, default=""):
        value = self.values.get(name)
        return default if value in (None, "") else value

    @property
    def name(self) -> str:
        return str(self.get("name"))

    @property
    def province(self) -> str:
        return str(self.get("province"))

    @property
    def province_approx(self) -> bool:
        return self.province_src == "vat_office" or (not self.province and bool(self.province_guess))

    @property
    def ateco_kind(self) -> str:
        if self.get("ateco"):
            return "exact"
        return "indicative" if self.get("nace_hints") else ""

    def badges(self) -> list[dict]:
        """[{"kind", "text", "date"}] for the card / table: legality rating, aid, hiring."""
        result = []
        rating = self.get("legality_rating")
        if rating:
            state = self.get("legality_state")
            until = self.get("legality_until")
            if state in ("revoked", "annulled", "suspended"):
                text = texts.text("registers.badge.legality_revoked", stars=rating)
            elif until:
                text = texts.text("registers.badge.legality_until", stars=rating, date=until)
            else:
                text = texts.text("registers.badge.legality", stars=rating)
            result.append({"kind": "legality", "text": text, "date": until, "state": state})
        if self.get("aid_count"):
            result.append({"kind": "aid", "date": self.get("last_aid_date"),
                           "text": texts.text("registers.badge.aid", count=self.get("aid_count"),
                                              date=self.get("last_aid_date") or "?")})
        if self.hiring_dates:
            latest = max(self.hiring_dates)
            result.append({"kind": "hired", "date": latest, "dates": list(self.hiring_dates),
                           "text": texts.text("registers.badge.hired", date=latest)})
        return result

    def to_dict(self) -> dict:
        data = {"key": self.key, "cf": self.cf}
        data.update({k: self.values.get(k) for k in FIELDS})
        data.update({"locked": list(self.locked), "sources": list(self.sources), "user_added": self.user_added,
                     "hidden": self.hidden, "province_src": self.province_src,
                     "province_guess": list(self.province_guess), "ateco_kind": self.ateco_kind,
                     "hiring_dates": list(self.hiring_dates)})
        return data

    def to_candidate(self, query_province: str = "") -> Company:
        """The discovery candidate (same shape as discovery/imports.to_company): register
        fields in `Company.fields`, legal ids in `Company.ids`, per-field provenance in
        `Company.sources`. The ATECO code goes to the ids only when it is exact (a hint
        from an aid stays in fields["nace_hint"]). `query_province`: when the province
        is only presumed (tax-office code shared by two provinces) and the search asks
        for one of them, that one is used and flagged "province_approx"."""
        from ..geo.overpass import normalize_site

        v = self.values
        municipality = str(v.get("municipality") or "")
        postcode = str(v.get("postcode") or "")
        address = str(v.get("address") or "")
        town = " ".join(p for p in (postcode, municipality) if p)
        if town and town.lower() not in address.lower():
            address = ", ".join(p for p in (address, town) if p)
        lat, lon = v.get("lat"), v.get("lon")
        try:
            lat = float(lat) if lat not in (None, "") else None
            lon = float(lon) if lon not in (None, "") else None
        except (TypeError, ValueError):
            lat = lon = None
        site = normalize_site(str(v.get("website") or "")) if v.get("website") else None
        company = Company(name=self.name, lat=lat, lon=lon, address=address, site=site or None, origin="register")
        ids = {"tax_code": self.cf, "vat": v.get("vat"), "lei": v.get("lei"), "pec": v.get("pec"),
               "rea": v.get("rea")}
        if self.ateco_kind == "exact":
            ids["ateco"] = v.get("ateco")
        company.ids = {k: str(val) for k, val in ids.items() if val}
        province = self.province
        approx = self.province_approx
        if not province and query_province and query_province.upper() in self.province_guess:
            province, approx = query_province.upper(), True
        extra = {
            "cf": self.cf, "vat": v.get("vat"), "town": municipality, "province": province, "postcode": postcode,
            "region": v.get("region"), "legal_form": v.get("legal_form"), "employees": v.get("employees_class"),
            "size_class": v.get("size_class"), "start_date": v.get("founded"), "status": v.get("status"),
            "lei": v.get("lei"), "pec": v.get("pec"), "rea": v.get("rea"), "email": v.get("email"),
            "phone": v.get("phone"),
            "ateco": v.get("ateco") if self.ateco_kind == "exact" else "", "ateco_text": v.get("ateco_text"),
            "nace_hint": v.get("nace_hints"), "legality_rating": v.get("legality_rating"),
            "legality_until": v.get("legality_until"), "legality_state": v.get("legality_state"),
            "aid_count": v.get("aid_count"), "last_aid": v.get("last_aid_date"),
            "hired_recently": max(self.hiring_dates) if self.hiring_dates else "",
            "hiring_count": str(len(self.hiring_dates)) if self.hiring_dates else "",
            "register_sources": ",".join(self.sources), "register_key": self.key,
            "province_approx": "1" if approx and province else "",
            "province_guess": ",".join(self.province_guess) if not province else "",
            "position": "town" if lat is not None and self.provenance.get("lat", ("",))[0] == AGCM else "",
        }
        company.fields = {k: str(val) for k, val in extra.items() if val not in (None, "")}
        facts = []
        for name, card_field in _FACTS.items():
            value = self.cf if name == "cf" else v.get(name)
            if not value:
                continue
            if name == "ateco" and self.ateco_kind != "exact":
                continue
            source, day = self.provenance.get(name, ("", ""))
            if name == "cf":
                source = self.sources[0] if self.sources else ""
            facts.append({"field": card_field, "value": str(value), "source": CARD_SOURCE,
                          "url": _HOMEPAGES.get(source, ""), "date": day or "",
                          "register_source": source or (USER if self.user_added else "")})
        company.sources = facts
        return company


def source_rank(source: str) -> int:
    """Lower is more trusted (for display ordering only)."""
    order = (USER, RI_BOUGHT, "gleif", "opencoesione", "agcm", "rna", "csv_import", "derived")
    return order.index(source) if source in order else len(order)


__all__ = ["CARD_SOURCE", "DISCOVERY_SOURCE", "RegisterCompany", "source_rank"]
