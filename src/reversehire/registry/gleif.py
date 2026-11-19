"""GLEIF LEI records (free, no key; data CC0): legal name, legal address and, for Italian
companies registered at the Registro Imprese (registration authority RA000407),
`registeredAs` = codice fiscale (not the REA).

Endpoints verified in the sources study (2026-11-17):
- GET https://api.gleif.org/api/v1/lei-records?filter[entity.registeredAs]={cf}&page[size]=5
- GET https://api.gleif.org/api/v1/lei-records/{LEI}
- GET https://api.gleif.org/api/v1/lei-records?filter[entity.legalName]={name}
      &filter[entity.legalAddress.country]=IT  (related entities come back too: only a
      record whose legal name matches the card name closely, and only one, is accepted)
Coverage: only companies that need an LEI (a minority of SMEs).

`entity.creationDate` (incorporation date) is midnight Italian time written in UTC
("2001-05-28T22:00:00Z" is 29 May 2001): it is converted to Europe/Rome before the date
is taken, otherwise every date would be one day early (study, section A.2).
Legal form: ELF codes P418 (S.p.A.) and OV32 (S.r.l.) seen in the study; other codes are
not translated (the legal form then comes from the name suffix), never guessed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .http import RegistryClient, RegistryError

API = "https://api.gleif.org/api/v1/lei-records"
RECORD_PAGE = "https://search.gleif.org/#/record/{lei}"
REGISTRO_IMPRESE = "RA000407"
SOURCE = "gleif"
ACCEPT = {"Accept": "application/vnd.api+json"}
_LEI = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")
log = logging.getLogger(__name__)
# ELF codes (ISO 20275) seen for Italian companies in the study; others are not mapped.
ELF_FORMS = {"P418": "S.p.A.", "OV32": "S.r.l."}
NAME_MATCH = 0.9


def _last_sunday(year: int, month: int) -> date:
    day = date(year, month, 31)
    return day - timedelta(days=(day.weekday() + 1) % 7)


def _rome_offset(moment: datetime) -> timedelta:
    """UTC offset of Italy at `moment` (UTC): +1 h, +2 h in summer time (EU rule:
    from 01:00 UTC of the last Sunday of March to 01:00 UTC of the last Sunday of October)."""
    start = datetime.combine(_last_sunday(moment.year, 3), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    end = datetime.combine(_last_sunday(moment.year, 10), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    return timedelta(hours=2 if start <= moment < end else 1)


def rome_date(value: str) -> str:
    """ISO date in Italy of a GLEIF timestamp ('2001-05-28T22:00:00Z' -> '2001-05-29');
    a plain date is returned as it is; '' when unreadable."""
    text = (value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return moment.astimezone(ZoneInfo("Europe/Rome")).date().isoformat()
    except Exception:  # no time-zone database (e.g. a frozen build): the EU rule
        return (moment + _rome_offset(moment)).date().isoformat()


@dataclass
class GleifRecord:
    lei: str
    legal_name: str = ""
    registered_as: str = ""
    registered_at: str = ""
    legal_form: str = ""
    entity_status: str = ""
    registration_status: str = ""
    street: str = ""
    postcode: str = ""
    town: str = ""
    region: str = ""  # "IT-BO"
    country: str = ""
    creation_date: str = ""  # ISO date in Italy (see rome_date)
    legal_form_other: str = ""
    url: str = ""
    date: str = field(default_factory=lambda: date.today().isoformat())

    @property
    def province(self) -> str:
        return self.region[3:] if self.region.startswith("IT-") and len(self.region) == 5 else ""

    @property
    def italian_register(self) -> bool:
        return self.registered_at == REGISTRO_IMPRESE

    @property
    def legal_form_name(self) -> str:
        return ELF_FORMS.get(self.legal_form, "") or self.legal_form_other

    def facts(self) -> list[dict]:
        values = [("lei", self.lei), ("name", self.legal_name), ("registered_office", self.street),
                  ("postcode", self.postcode), ("town", self.town), ("province", self.province),
                  ("founded", self.creation_date), ("legal_form", self.legal_form_name)]
        if self.italian_register and self.registered_as:
            values.append(("tax_code", self.registered_as))
        return [{"field": f, "value": v, "source": SOURCE, "url": self.url, "date": self.date}
                for f, v in values if v]


@dataclass
class GleifAnswer:
    state: str  # "found" | "not_found" | "unavailable"
    records: list[GleifRecord] = field(default_factory=list)
    url: str = ""
    detail: str = ""


def _record(item: dict) -> GleifRecord | None:
    attributes = item.get("attributes") if isinstance(item, dict) else None
    if not isinstance(attributes, dict):
        return None
    lei = str(attributes.get("lei") or item.get("id") or "")
    entity = attributes.get("entity") or {}
    registration = attributes.get("registration") or {}
    address = entity.get("legalAddress") or {}
    lines = [str(x) for x in address.get("addressLines") or [] if x]
    registered_at = entity.get("registeredAt") or {}
    legal_form = entity.get("legalForm") or {}
    return GleifRecord(
        lei=lei,
        legal_name=" ".join(str((entity.get("legalName") or {}).get("name") or "").split()),
        registered_as=str(entity.get("registeredAs") or "").strip(),
        registered_at=str(registered_at.get("id") or ""),
        legal_form=str(legal_form.get("id") or ""),
        entity_status=str(entity.get("status") or ""),
        registration_status=str(registration.get("status") or ""),
        street=", ".join(lines),
        postcode=str(address.get("postalCode") or ""),
        town=str(address.get("city") or ""),
        region=str(address.get("region") or ""),
        country=str(address.get("country") or ""),
        creation_date=rome_date(str(entity.get("creationDate") or "")),
        legal_form_other=" ".join(str(legal_form.get("other") or "").split()),
        url=RECORD_PAGE.format(lei=lei),
    )


def _answer(client: RegistryClient, url: str, params: dict | None) -> GleifAnswer:
    try:
        answer = client.get_json(url, params, headers=ACCEPT,
                                 cache=lambda status, _data: status in (200, 404))
    except RegistryError as error:
        if error.kind == "cancelled":
            raise
        return GleifAnswer("unavailable", url=url, detail=error.kind)
    if answer.status == 404:
        return GleifAnswer("not_found", url=answer.url)
    if answer.status != 200 or not isinstance(answer.data, dict):
        return GleifAnswer("unavailable", url=answer.url, detail=f"HTTP {answer.status}")
    items = answer.data.get("data")
    items = items if isinstance(items, list) else [items]
    records = [r for r in (_record(i) for i in items) if r is not None and r.lei]
    return GleifAnswer("found" if records else "not_found", records, answer.url)


def by_tax_code(client: RegistryClient, tax_code: str) -> GleifAnswer:
    """LEI records whose `registeredAs` equals the codice fiscale (Italian ones first)."""
    code = (tax_code or "").strip().upper()
    if not code:
        return GleifAnswer("not_found")
    result = _answer(client, API, {"filter[entity.registeredAs]": code, "page[size]": "5"})
    result.records = [r for r in result.records if r.registered_as.upper() == code]
    result.records.sort(key=lambda r: (r.country != "IT", not r.italian_register))
    if result.state == "found" and not result.records:
        result.state = "not_found"
    return result


def by_lei(client: RegistryClient, lei: str) -> GleifAnswer:
    code = (lei or "").strip().upper()
    if not _LEI.match(code):
        return GleifAnswer("not_found")
    return _answer(client, f"{API}/{code}", None)


def by_name(client: RegistryClient, name: str, *, country: str = "IT") -> GleifAnswer:
    """The ONE LEI record of `country` whose legal name matches `name` (token-set
    similarity >= NAME_MATCH, legal forms ignored); "not_found" when none or several
    match equally (related group companies are common)."""
    from .confirm import name_similarity

    wanted = " ".join((name or "").split())
    if len(wanted) < 3:
        return GleifAnswer("not_found")
    result = search_name(client, wanted, country=country)
    from .confirm import name_tokens

    def score(record) -> float:
        # the same words (legal forms ignored) beat a name that only contains them
        exact = set(name_tokens(wanted)) == set(name_tokens(record.legal_name))
        return name_similarity(wanted, record.legal_name) + (1.0 if exact else 0.0)

    scored = sorted(((score(r), r) for r in result.records), key=lambda item: -item[0])
    good = [(score, r) for score, r in scored if score >= NAME_MATCH]
    if len(good) > 1 and good[0][0] == good[1][0]:
        log.info("GLEIF by name %r: %d records match equally; none used", wanted, len(good))
        good = []
    result.records = [good[0][1]] if good else []
    if result.state == "found" and not result.records:
        result.state = "not_found"
    return result


def search_name(client: RegistryClient, name: str, *, country: str = "IT") -> GleifAnswer:
    """Every LEI record of `country` that GLEIF returns for a legal-name search (related
    group companies and namesakes included: the caller decides)."""
    wanted = " ".join((name or "").split())
    if len(wanted) < 3:
        return GleifAnswer("not_found")
    return _answer(client, API, {"filter[entity.legalName]": wanted,
                                 "filter[entity.legalAddress.country]": country, "page[size]": "10"})
