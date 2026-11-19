"""Tolerant reading of schema.org `JobPosting` JSON-LD as published by Italian agencies.

Quirks handled (RECRUITER_STUDY §2.1/§2.4, checked on live pages 2026-11-18):
- JavaScript `//` and `/* */` comments inside the JSON (Umana), trailing commas;
- HTML comments / CDATA around the block;
- HTML entities inside strings (Adecco, Manpower): the JSON is parsed FIRST and the
  strings are unescaped afterwards (unescaping first breaks the JSON);
- dates: "2026-11-05T16:28:17+0000", "20260129T111110" (Manpower), "18/11/2026"
  (Gi Group), trailing spaces (Adecco); a validThrough before datePosted or before
  2000 ("1970-04-01", Gi Group) is ignored;
- address: addressLocality holding the region and streetAddress the town (Manpower),
  streetAddress holding the agency branch (Umana), addressRegion as a province code;
- baseSalary with "currency": "€" and a free-text value (Manpower).
"""

from __future__ import annotations

import json
import re
from datetime import date

from . import lexicon
from ._util import compact, fold, html_text, iso, log, parse_date, unescape_deep
from .models import Posting
from .normalize import finish

_SCRIPT = re.compile(r"<script[^>]*type\s*=\s*[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script>", re.S | re.I)
MAX_DEPTH = 12
DESCRIPTION_CHARS = 8000


def strip_js_comments(text: str) -> str:
    """Remove // and /* */ comments outside JSON strings, and trailing commas."""
    out = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_lenient(block: str):
    """Parse one JSON-LD block; None when it cannot be read even leniently."""
    text = (block or "").strip()
    text = re.sub(r"^\s*(<!--|/\*\s*<!\[CDATA\[\s*\*/|<!\[CDATA\[)", "", text)
    text = re.sub(r"(-->|/\*\s*\]\]>\s*\*/|\]\]>)\s*$", "", text).strip()
    if not text:
        return None
    for candidate in (text, strip_js_comments(text)):
        try:
            return unescape_deep(json.loads(candidate, strict=False))
        except ValueError:
            continue
    try:  # last resort: some pages entity-encode the whole block
        import html

        return unescape_deep(json.loads(strip_js_comments(html.unescape(text)), strict=False))
    except ValueError:
        return None


def _is_jobposting(item: dict) -> bool:
    kind = item.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(isinstance(k, str) and k.split("/")[-1].lower() == "jobposting" for k in kinds)


def _collect(item, found: list[dict], depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        return
    if isinstance(item, list):
        for element in item:
            _collect(element, found, depth + 1)
    elif isinstance(item, dict):
        if _is_jobposting(item):
            found.append(item)
            return
        for value in item.values():
            if isinstance(value, (dict, list)):
                _collect(value, found, depth + 1)


def jobposting_items(html: str) -> list[dict]:
    """The JobPosting objects of a page (malformed blocks skipped)."""
    found: list[dict] = []
    for block in _SCRIPT.findall(html or ""):
        data = load_lenient(block)
        if data is not None:
            _collect(data, found)
    return found


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("name") or value.get("@value") or value.get("value") or "")
    if isinstance(value, list):
        return ", ".join(t for t in (_text(v) for v in value) if t)
    return "" if value is None else str(value)


def _first_place(item: dict) -> dict:
    places = item.get("jobLocation")
    places = places if isinstance(places, list) else [places] if places else []
    for place in places:
        if isinstance(place, dict):
            return place
    return {}


def _float(value) -> float | None:
    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return number


def _location(item: dict, branch_in_street: bool) -> dict:
    place = _first_place(item)
    address = place.get("address") if isinstance(place.get("address"), dict) else {}
    locality = compact(_text(address.get("addressLocality")))
    region = compact(_text(address.get("addressRegion")))
    street = compact(_text(address.get("streetAddress")))
    country = compact(_text(address.get("addressCountry")))
    postcode = compact(_text(address.get("postalCode")))
    result = {"town": None, "province": None, "region": None, "country": country or None,
              "branch": None, "lat": None, "lon": None, "quality": None}
    province = None
    if re.fullmatch(r"[A-Za-z]{2}", region or ""):
        province = region.upper()
        region = ""
    if fold(locality) in lexicon.REGIONS or fold(locality).replace("-", " ") in lexicon.REGIONS:
        # Manpower: locality = region, street = town
        result["region"] = locality
        locality = street if street and not re.search(r"\d", street) else ""
        street = ""
    if branch_in_street and street:
        result["branch"] = street
    result["town"] = locality or None
    result["province"] = province
    if region and not result["region"]:
        result["region"] = region
    geo = place.get("geo") if isinstance(place.get("geo"), dict) else {}
    lat, lon = _float(geo.get("latitude")), _float(geo.get("longitude"))
    if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat or lon):
        result["lat"], result["lon"] = lat, lon
        result["quality"] = "exact"
    elif result["town"]:
        result["quality"] = "town"
    elif province:
        result["quality"] = "province"
    elif result["region"]:
        result["quality"] = "region"
    head = " ".join(p for p in (result["town"], f"({province})" if province else None) if p)
    result["text"] = ", ".join(p for p in (head, result["region"]) if p) or \
        ", ".join(p for p in (postcode, country) if p)
    if _text(item.get("jobLocationType")).upper().find("TELECOMMUTE") >= 0:
        result["text"] = (result["text"] + "; Remote").strip("; ")
    return result


def _workplace(item: dict):
    kind = _text(item.get("jobLocationType"))
    if kind:
        return kind
    remote = str(item.get("isRemote") or "").strip().lower()
    if remote in ("true", "1", "yes"):
        return "remote"
    if item.get("applicantLocationRequirements") and not item.get("jobLocation"):
        return "remote"
    return None


_UNITS = {"annuale": "YEAR", "annuo": "YEAR", "anno": "YEAR", "year": "YEAR", "yearly": "YEAR",
          "mensile": "MONTH", "mese": "MONTH", "month": "MONTH", "monthly": "MONTH", "orari": "HOUR",
          "orario": "HOUR", "ora": "HOUR", "hour": "HOUR", "hourly": "HOUR", "settimanale": "WEEK", "week": "WEEK",
          "giornaliero": "DAY", "day": "DAY"}


def _amount(text: str) -> float | None:
    text = text.strip().replace(" ", "").rstrip(".,")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", text):
        text = text.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+,\d{1,2}", text):
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _structured_salary_text(salary: dict) -> dict:
    """Manpower/Adecco style: "value": "€ 30000 - € 40000 Annuale" or unitText "orari"."""
    value = salary.get("value")
    if not isinstance(value, dict):
        return salary
    value = dict(value)
    unit = fold(str(value.get("unitText") or ""))
    if unit in _UNITS:
        value["unitText"] = _UNITS[unit]
    raw = value.get("value")
    if isinstance(raw, str) and not value.get("minValue"):
        numbers = [n for n in (_amount(x) for x in re.findall(r"\d[\d.,]*", raw)) if n]
        words_found = [u for u in _UNITS if re.search(r"\b" + u + r"\b", fold(raw))]
        if words_found and unit not in _UNITS:
            value["unitText"] = _UNITS[words_found[0]]
        if len(numbers) >= 2:
            value["minValue"], value["maxValue"] = min(numbers[:2]), max(numbers[:2])
            value.pop("value", None)
        elif len(numbers) == 1:
            value["value"] = numbers[0]
    salary["value"] = value
    return salary


def _salary(item: dict):
    salary = item.get("baseSalary") or item.get("estimatedSalary")
    if isinstance(salary, dict):
        salary = dict(salary)
        currency = str(salary.get("currency") or "").strip()
        if currency in ("€", "eur", "Euro", "euro"):
            salary["currency"] = "EUR"
        salary = _structured_salary_text(salary)
    if isinstance(item.get("estimatedSalary"), dict) and not item.get("baseSalary"):
        return None  # estimated by the publisher: never shown as published
    return salary


def posting_from_item(item: dict, page_url: str, *, source: str, agency: str | None = None,
                      branch_in_street: bool = False, today: date | None = None) -> Posting | None:
    """A Posting from one JobPosting object; None without a title or when expired."""
    try:
        title = compact(_text(item.get("title") or item.get("name")))
        if not title:
            return None
        posted = parse_date(item.get("datePosted"))
        valid = parse_date(item.get("validThrough"))
        if valid is not None and (valid.year < 2000 or (posted is not None and valid < posted)):
            valid = None  # bogus (Gi Group "1970-04-01")
        if valid is not None and today is not None and valid < today:
            return None
        location = _location(item, branch_in_street)
        organization = item.get("hiringOrganization")
        org_name = compact(_text(organization)) if organization else ""
        identifier = item.get("identifier")
        source_id = compact(_text(identifier.get("value") if isinstance(identifier, dict) else identifier)) or None
        parts = []
        for key in ("description", "responsibilities", "qualifications", "skills", "jobBenefits",
                    "educationRequirements", "experienceRequirements", "incentiveCompensation"):
            value = item.get(key)
            if isinstance(value, dict):
                value = _text(value.get("credentialCategory") or value.get("monthsOfExperience") or value)
            text = html_text(_text(value), DESCRIPTION_CHARS)
            if text and text not in parts:
                parts.append(text)
        full_text = "\n".join(parts)
        url = compact(_text(item.get("url"))) or page_url
        if agency:
            is_agency = True
            company = None
            if org_name and not lexicon.is_agency_name(org_name) and fold(org_name) != fold(agency):
                company = org_name  # the agency named the client
        else:
            is_agency = lexicon.is_agency_name(org_name)
            company = None if is_agency else (org_name or None)
            agency = org_name if is_agency else None
        posting = Posting(
            source=source, url=url, title=title, company=company, is_agency=is_agency, agency=agency,
            agency_branch=location["branch"], location=location["text"], town=location["town"],
            province=location["province"], region=location["region"], country=location["country"],
            lat=location["lat"], lon=location["lon"], geo_quality=location["quality"],
            description=html_text(_text(item.get("description")), DESCRIPTION_CHARS) or full_text[:DESCRIPTION_CHARS],
            date_posted=iso(posted), valid_through=iso(valid), industry=compact(_text(item.get("industry"))) or None,
            source_id=source_id)
        return finish(posting, full_text, employment_type=item.get("employmentType"),
                      workplace=_workplace(item), salary=_salary(item), hours_text=_text(item.get("workHours")))
    except Exception as error:  # one odd posting never breaks a source
        log.info("JobPosting not read (%s): %s", type(error).__name__, page_url)
        return None


def postings_from_html(html: str, page_url: str, **kwargs) -> list[Posting]:
    result = []
    for item in jobposting_items(html):
        posting = posting_from_item(item, page_url, **kwargs)
        if posting is not None:
            result.append(posting)
    return result
