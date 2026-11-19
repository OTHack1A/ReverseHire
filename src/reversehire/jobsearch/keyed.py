"""Job-search APIs that need the user's own key: Adzuna, Jooble, Careerjet.

Implemented against their documented request/response shapes and unit-tested with
fabricated responses only: NOT TESTED LIVE (no key exists). Used only when the user has
entered a key in Settings. Keys are sent only to the API's own host, never logged,
never stored in reports or caches (responses are not cached).

Documented shapes [M = from the providers' public docs, not verified live]:
- Adzuna: GET https://api.adzuna.com/v1/api/jobs/it/search/{page}?app_id&app_key&what&where
  &distance&results_per_page&what_exclude&content-type=application/json
  -> {"count", "results": [{"id", "title", "description", "created", "redirect_url",
  "company": {"display_name"}, "location": {"display_name", "area": [...]}, "latitude",
  "longitude", "salary_min", "salary_max", "salary_is_predicted", "contract_type",
  "contract_time", "category": {"label"}}]}. Attribution "Jobs by Adzuna" required.
- Jooble: POST https://jooble.org/api/{key} with JSON {"keywords", "location", "radius",
  "page"} -> {"totalCount", "jobs": [{"title", "location", "snippet", "salary", "source",
  "type", "link", "company", "updated", "id"}]}.
- Careerjet v4: GET https://search.api.careerjet.net/v4/query, the key as HTTP Basic user,
  params locale_code, keywords, location, page, page_size (+ user_ip / user_agent of the end
  user, which the API asks for) -> {"type": "JOBS" | "LOCATIONS", "hits", "jobs": [{"title",
  "company", "locations", "salary", "salary_min", "salary_max", "salary_type",
  "salary_currency_code", "date", "url", "description", "site"}]}.
"""

from __future__ import annotations

import base64
import json
from urllib.parse import quote

from ._util import compact, html_text, iso, parse_date
from .base import JobSource, SourceCancelled, SourceContext
from .models import Posting
from .normalize import finish

MAX_PAGES = 2
PAGE_SIZE = 50


def _what(query) -> str:
    return compact(" ".join([query.role] + [f'"{p}"' for p in query.phrases])) or " ".join(query.skills)


class _KeyedSource(JobSource):
    needs_key = True
    enabled_by_default = False
    terms_verdict = "api_terms"
    untested_live = True

    def estimate(self, query) -> int:
        return MAX_PAGES

    def _pages(self, ctx: SourceContext, fetch_page) -> list[Posting]:
        result: list[Posting] = []
        for page in range(1, MAX_PAGES + 1):
            ctx.check()
            try:
                postings, more = fetch_page(page)
            except SourceCancelled:
                raise
            except Exception as error:
                ctx.error(f"page {page}: {type(error).__name__}")
                break
            finally:
                ctx.step(self.label)
            result.extend(postings)
            ctx.add(postings)
            if not more:
                break
        return result


# --- Adzuna ------------------------------------------------------------------------------

ADZUNA_URL = "https://api.adzuna.com/v1/api/jobs/it/search/{page}"
_ADZUNA_CONTRACT = {"permanent": "permanent", "contract": "fixed_term"}
_ADZUNA_TIME = {"full_time": "full_time", "part_time": "part_time"}


def parse_adzuna(data: dict) -> list[Posting]:
    result = []
    for row in (data or {}).get("results") or []:
        if not isinstance(row, dict):
            continue
        title = compact(html_text(str(row.get("title") or ""), 300))
        url = str(row.get("redirect_url") or "")
        if not title or not url:
            continue
        company = compact(str((row.get("company") or {}).get("display_name") or "")) or None
        location = row.get("location") or {}
        area = [str(a) for a in location.get("area") or []]
        posting = Posting(
            source="adzuna", url=url, title=title, company=company,
            location=str(location.get("display_name") or ""),
            town=area[-1] if len(area) >= 3 else None, region=area[1] if len(area) >= 2 else None,
            country="IT", description=html_text(str(row.get("description") or ""), 4000),
            date_posted=iso(parse_date(row.get("created"))), source_id=str(row.get("id") or "") or None,
            contract=_ADZUNA_CONTRACT.get(str(row.get("contract_type") or "")),
            hours=_ADZUNA_TIME.get(str(row.get("contract_time") or "")),
            industry=str((row.get("category") or {}).get("label") or "") or None)
        try:
            lat, lon = float(row.get("latitude")), float(row.get("longitude"))
            posting.lat, posting.lon, posting.geo_quality = lat, lon, "town"
        except (TypeError, ValueError):
            pass
        predicted = str(row.get("salary_is_predicted", "0")).strip().lower() in ("1", "true")
        if not predicted and (row.get("salary_min") or row.get("salary_max")):
            posting.salary_min = float(row["salary_min"]) if row.get("salary_min") else None
            posting.salary_max = float(row["salary_max"]) if row.get("salary_max") else None
            posting.salary_currency, posting.salary_period = "EUR", "year"
            low, high = posting.salary_min, posting.salary_max
            posting.salary = f"{low:,.0f}–{high:,.0f} EUR/year" if low and high and low != high else \
                f"{(low or high):,.0f} EUR/year"
        result.append(finish(posting))
    return result


class AdzunaSource(_KeyedSource):
    id = "adzuna"
    label = "Adzuna"
    attribution = "Jobs by Adzuna"
    terms_url = "https://developer.adzuna.com/"
    terms_note = "Official API with your own free key (app_id + app_key); attribution shown."
    hosts = ("api.adzuna.com",)

    def __init__(self, app_id: str = "", app_key: str = ""):
        self.app_id = (app_id or "").strip()
        self.app_key = (app_key or "").strip()

    def has_key(self) -> bool:
        return bool(self.app_id and self.app_key)

    def params(self, query, page: int) -> dict:
        params = {"app_id": self.app_id, "app_key": self.app_key, "what": _what(query),
                  "results_per_page": PAGE_SIZE, "content-type": "application/json"}
        either = [w for group in (getattr(query, "alternatives", None) or []) for w in group]
        if either:  # "OT/Cybersecurity": ads with either word (Adzuna's what_or)
            rest = " ".join(w for w in query.role.split() if w not in either)
            params["what_or"] = " ".join(either)
            params["what"] = compact(" ".join([rest] + [f'"{p}"' for p in query.phrases]))
            if not params["what"]:
                del params["what"]
        if query.place:
            params["where"] = query.place
            if query.radius_km:
                params["distance"] = int(round(query.radius_km))
        if query.excluded:
            params["what_exclude"] = " ".join(query.excluded)
        if query.contract == "permanent":
            params["permanent"] = 1
        if query.hours == "full_time":
            params["full_time"] = 1
        elif query.hours == "part_time":
            params["part_time"] = 1
        return params

    def run(self, ctx: SourceContext) -> list[Posting]:
        def fetch(page: int):
            reply = ctx.get(ADZUNA_URL.format(page=page), params=self.params(ctx.query, page),
                            headers={"Accept": "application/json"})
            if reply.status != 200:
                ctx.error(f"HTTP {reply.status}")
                return [], False
            data = json.loads(reply.text)
            postings = parse_adzuna(data)
            return postings, len(data.get("results") or []) >= PAGE_SIZE
        return self._pages(ctx, fetch)


# --- Jooble ------------------------------------------------------------------------------

JOOBLE_URL = "https://jooble.org/api/{key}"
JOOBLE_RADII = (0, 4, 8, 16, 26, 40, 80)  # [M] values accepted by the radius field


def parse_jooble(data: dict) -> list[Posting]:
    result = []
    for row in (data or {}).get("jobs") or []:
        if not isinstance(row, dict):
            continue
        title = compact(html_text(str(row.get("title") or ""), 300))
        url = str(row.get("link") or "")
        if not title or not url:
            continue
        salary = compact(str(row.get("salary") or "")) or None
        posting = Posting(
            source="jooble", url=url, title=title, company=compact(str(row.get("company") or "")) or None,
            location=compact(str(row.get("location") or "")), description=html_text(str(row.get("snippet") or ""), 4000),
            date_posted=iso(parse_date(row.get("updated"))), source_id=str(row.get("id") or "") or None,
            salary=salary)
        posting.town = posting.location.split(",")[0].strip() or None
        kind = str(row.get("type") or "")
        result.append(finish(posting, posting.description + "\n" + kind + ("\n" + salary if salary else "")))
    return result


class JoobleSource(_KeyedSource):
    id = "jooble"
    label = "Jooble"
    terms_url = "https://jooble.org/api/about"
    terms_note = "Official API with your own key (request form on jooble.org)."
    hosts = ("jooble.org",)

    def __init__(self, key: str = ""):
        self.key = (key or "").strip()

    def has_key(self) -> bool:
        return bool(self.key)

    def body(self, query, page: int) -> dict:
        body = {"keywords": _what(query), "page": str(page)}
        if query.place:
            body["location"] = query.place
            if query.radius_km:
                body["radius"] = str(min(JOOBLE_RADII, key=lambda r: abs(r - query.radius_km)))
        return body

    def run(self, ctx: SourceContext) -> list[Posting]:
        def fetch(page: int):
            reply = ctx.request("POST", JOOBLE_URL.format(key=quote(self.key, safe="")),
                                content=json.dumps(self.body(ctx.query, page)),
                                headers={"Content-Type": "application/json", "Accept": "application/json"})
            if reply.status != 200:
                ctx.error(f"HTTP {reply.status}")
                return [], False
            data = json.loads(reply.text)
            postings = parse_jooble(data)
            return postings, len(data.get("jobs") or []) >= 20
        return self._pages(ctx, fetch)


# --- Careerjet ---------------------------------------------------------------------------

CAREERJET_URL = "https://search.api.careerjet.net/v4/query"
_CAREERJET_PERIOD = {"Y": "year", "M": "month", "W": "week", "D": "day", "H": "hour"}


def parse_careerjet(data: dict) -> tuple[list[Posting], str | None]:
    """(postings, problem) — problem "ambiguous_location" when the API answers LOCATIONS."""
    data = data or {}
    if str(data.get("type") or "").upper() == "LOCATIONS":
        return [], "ambiguous_location"
    result = []
    for row in data.get("jobs") or []:
        if not isinstance(row, dict):
            continue
        title = compact(html_text(str(row.get("title") or ""), 300))
        url = str(row.get("url") or "")
        if not title or not url:
            continue
        posting = Posting(
            source="careerjet", url=url, title=title, company=compact(str(row.get("company") or "")) or None,
            location=compact(str(row.get("locations") or "")), description=html_text(str(row.get("description") or ""), 4000),
            date_posted=iso(parse_date(row.get("date"))), salary=compact(str(row.get("salary") or "")) or None)
        posting.town = posting.location.split(",")[0].strip() or None
        try:
            if row.get("salary_min"):
                posting.salary_min = float(row["salary_min"])
            if row.get("salary_max"):
                posting.salary_max = float(row["salary_max"])
        except (TypeError, ValueError):
            pass
        posting.salary_period = _CAREERJET_PERIOD.get(str(row.get("salary_type") or "").upper())
        posting.salary_currency = str(row.get("salary_currency_code") or "") or None
        result.append(finish(posting))
    return result, None


class CareerjetSource(_KeyedSource):
    id = "careerjet"
    label = "Careerjet"
    terms_url = "https://www.careerjet.it/partners/api"
    terms_note = "Official API with your own partner key (sent as HTTP Basic user)."
    hosts = ("search.api.careerjet.net",)

    def __init__(self, key: str = "", *, user_ip: str = "", user_agent: str = ""):
        self.key = (key or "").strip()
        self.user_ip = user_ip
        self.user_agent = user_agent

    def has_key(self) -> bool:
        return bool(self.key)

    def params(self, query, page: int) -> dict:
        params = {"locale_code": "it_IT", "keywords": _what(query), "page": page, "page_size": PAGE_SIZE}
        if query.place:
            params["location"] = query.place
        if self.user_ip:
            params["user_ip"] = self.user_ip
        if self.user_agent:
            params["user_agent"] = self.user_agent
        if query.contract == "permanent":
            params["contract_type"] = "p"
        elif query.contract == "fixed_term":
            params["contract_type"] = "c"
        elif query.contract == "internship":
            params["contract_type"] = "i"
        if query.hours == "full_time":
            params["contract_period"] = "f"
        elif query.hours == "part_time":
            params["contract_period"] = "p"
        return params

    def auth_header(self) -> dict:
        token = base64.b64encode(f"{self.key}:".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}", "Accept": "application/json"}

    def run(self, ctx: SourceContext) -> list[Posting]:
        def fetch(page: int):
            reply = ctx.get(CAREERJET_URL, params=self.params(ctx.query, page), headers=self.auth_header())
            if reply.status != 200:
                ctx.error(f"HTTP {reply.status}")
                return [], False
            data = json.loads(reply.text)
            postings, problem = parse_careerjet(data)
            if problem:
                ctx.error(problem)
            return postings, len(data.get("jobs") or []) >= PAGE_SIZE
        return self._pages(ctx, fetch)
