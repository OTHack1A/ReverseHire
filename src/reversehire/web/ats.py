"""Recognition of ATS platforms and reading of their public APIs.

Public endpoints without authentication (checked live in November 2026):
- Greenhouse: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
  (EU boards: boards-api.eu.greenhouse.io, tried when the default host fails; in
  the Nov 2026 check that name did not resolve in DNS and the default host gave
  no answer for an EU board, so EU Greenhouse boards stay unreadable for now:
  the fallback then costs only a DNS lookup)
- Lever: https://api.lever.co/v0/postings/{slug}?mode=json  (EU: api.eu.lever.co,
  host reachable, not checked against a real EU board)
- Ashby: https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
- Workable: https://apply.workable.com/api/v1/widget/accounts/{slug}
- Recruitee: https://{slug}.recruitee.com/api/offers/
- SmartRecruiters: https://api.smartrecruiters.com/v1/companies/{slug}/postings
  (+ /postings/{id} for the full text of some postings, see below)
- Personio: https://{slug}.jobs.personio.{de|com}/xml  (XML "workzag-jobs")
- Workday CXS: POST https://{tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
  (pages of 20, total on the first page only; GET .../{site}{externalPath} for the detail)
- BambooHR: https://{slug}.bamboohr.com/careers/list (+ /careers/{id}/detail)

On the last three the list has no description: at most MAX_DETAIL_CALLS postings
get a detail call, those whose titles are closest to the candidate's roles
(`detail_order`), so the postings the fit will pick have their full text.

Workday and BambooHR endpoints, payload fields and host guards follow
ats-jobs-scraper-cli (MIT, src/adapters.js), re-written in Python.

Descriptions are kept as plain text with one line per block (up to
DESCRIPTION_LENGTH characters) so the requirement rules can read headings and
bullet lists; the attributes (contract, salary...) are read on FULL_TEXT_LENGTH.
"""

import json
import logging
import re
from dataclasses import dataclass, replace
from typing import Callable
from urllib.parse import quote, urlsplit

from lxml import etree

from ..models import Posting
from .posting_attrs import apply_attributes, detect_attributes
from .text import compact, fragment_lines, normalize, text_from_fragment

log = logging.getLogger(__name__)

MAX_POSTINGS = 200
DESCRIPTION_LENGTH = 6000  # plain text kept per posting (requirement analysis)
FULL_TEXT_LENGTH = 20000  # text read for the attributes (contract, salary...)
MAX_DETAIL_CALLS = 10  # detail requests per board (Workday, BambooHR, SmartRecruiters)
WORKDAY_PAGE_SIZE = 20  # hard cap of the Workday CXS API
WORKDAY_MAX_PAGES = 5
MAX_PROBE_CALLS = 3
PROBE_PLATFORMS = ("greenhouse", "lever", "ashby")  # never SmartRecruiters (200 for any slug)
MIN_PROBE_LABEL = 4
# Slug probing guesses a board from the site's domain. A guess is accepted only
# when the board's own data names the company's domain (a link or an e-mail on
# that domain): a board merely named like the brand is often another company's
# (live Nov 2026: "inter" on Greenhouse is a Brazilian bank, not inter.it).
PROBE_ENABLED = True
# Words of a posting title that say nothing about the job (ranking for the detail calls).
_GENERIC_TITLE_WORDS = {
    "senior", "junior", "mid", "lead", "head", "chief", "principal", "staff", "intern", "stage",
    "the", "and", "for", "with", "per", "con", "del", "della", "dei", "team", "specialist",
    "remote", "hybrid", "full", "time", "part", "all", "gender",
}

_SLUG = r"(?P<slug>[A-Za-z0-9][A-Za-z0-9_.-]*)"
_LABEL = r"(?P<slug>[A-Za-z0-9][A-Za-z0-9-]*)"
# Words that appear where the slug should be but are not slugs.
_NOT_SLUGS = {
    "embed", "api", "v0", "v1", "j", "jobs", "job", "www", "app", "apply", "help",
    "support", "resources", "careers", "login", "signup", "static", "assets",
    "cdn", "blog", "docs", "developers", "status", "oneclick-ui", "sr-jobs", "en",
    "wday", "cxs", "recruiting", "community", "marketplace",
}
_BAMBOO_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_WORKDAY_SITE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class ATSReference:
    platform: str
    slug: str
    domain: str = ""  # "de"/"com" for Personio, "eu" for Greenhouse/Lever, "wdN" for Workday
    site: str = ""  # Workday career site

    @property
    def _workday_host(self) -> str:
        return f"{self.slug.lower()}.{self.domain or 'wd1'}.myworkdayjobs.com"

    @property
    def board_url(self) -> str:
        """Public page of the postings, to show as the careers page."""
        s = self.slug
        eu = ".eu" if self.domain == "eu" else ""
        return {
            "greenhouse": f"https://job-boards{eu}.greenhouse.io/{s}",
            "lever": f"https://jobs{eu}.lever.co/{s}",
            "ashby": f"https://jobs.ashbyhq.com/{s}",
            "workable": f"https://apply.workable.com/{s}/",
            "recruitee": f"https://{s}.recruitee.com/",
            "smartrecruiters": f"https://careers.smartrecruiters.com/{s}",
            "personio": f"https://{s}.jobs.personio.{self.domain or 'de'}/",
            "workday": f"https://{self._workday_host}/{self.site}",
            "bamboohr": f"https://{s.lower()}.bamboohr.com/careers",
        }[self.platform]

    @property
    def url_api(self) -> str:
        s = self.slug
        eu = ".eu" if self.domain == "eu" else ""
        return {
            "greenhouse": f"https://boards-api{eu}.greenhouse.io/v1/boards/{s}/jobs?content=true",
            "lever": f"https://api{eu}.lever.co/v0/postings/{s}?mode=json",
            "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{s}?includeCompensation=true",
            "workable": f"https://apply.workable.com/api/v1/widget/accounts/{s}",
            "recruitee": f"https://{s}.recruitee.com/api/offers/",
            "smartrecruiters": f"https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=100",
            "personio": f"https://{s}.jobs.personio.{self.domain or 'de'}/xml",
            "workday": f"https://{self._workday_host}/wday/cxs/{s.lower()}/{self.site}/jobs",
            "bamboohr": f"https://{s.lower()}.bamboohr.com/careers/list",
        }[self.platform]

    def other_region(self) -> "ATSReference | None":
        """The same board on the other Greenhouse/Lever region (EU <-> default); None elsewhere."""
        if self.platform not in ("greenhouse", "lever"):
            return None
        return replace(self, domain="" if self.domain == "eu" else "eu")


# (platform, pattern) with the named groups "slug" and, optionally, "domain" and "site"
_PLATFORM_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(r"boards-api(?:\.(?P<domain>eu))?\.greenhouse\.io/v1/boards/" + _SLUG, re.I)),
    ("greenhouse", re.compile(r"(?:(?P<domain>eu)\.)?greenhouse\.io/embed/job_board(?:/js)?\?"
                              r"(?:[^\"'\s<>]*&(?:amp;)?)?for=" + _SLUG, re.I)),
    ("greenhouse", re.compile(r"(?:job-)?boards(?:\.(?P<domain>eu))?\.greenhouse\.io/" + _SLUG, re.I)),
    ("lever", re.compile(r"api(?:\.(?P<domain>eu))?\.lever\.co/v0/postings/" + _SLUG, re.I)),
    ("lever", re.compile(r"jobs(?:\.(?P<domain>eu))?\.lever\.co/" + _SLUG, re.I)),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/" + _SLUG, re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/" + _SLUG, re.I)),
    ("workable", re.compile(r"apply\.workable\.com/api/v[0-9]+/(?:widget/)?accounts/" + _SLUG, re.I)),
    ("workable", re.compile(r"apply\.workable\.com/" + _SLUG, re.I)),
    ("workable", re.compile(r"//" + _LABEL + r"\.workable\.com", re.I)),
    ("recruitee", re.compile(r"//" + _LABEL + r"\.recruitee\.com", re.I)),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/" + _SLUG, re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/" + _SLUG, re.I)),
    ("personio", re.compile(_LABEL + r"\.jobs\.personio\.(?P<domain>de|com)", re.I)),
    ("workday", re.compile(_LABEL + r"\.(?P<domain>wd\d+)\.myworkdayjobs\.com/wday/cxs/[A-Za-z0-9_-]+/"
                           r"(?P<site>[A-Za-z0-9_-]+)", re.I)),
    ("workday", re.compile(_LABEL + r"\.(?P<domain>wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[a-z]{2}/)?"
                           r"(?P<site>[A-Za-z0-9_-]+)", re.I)),
    ("bamboohr", re.compile(r"//" + _LABEL + r"\.bamboohr\.com", re.I)),
]

# Hosts of ATS platforms whose postings we cannot read: a careers link to them is
# still a hiring signal (shown as a link). From mr-jobs ATS_DOMAINS and erga-mcp
# _ATS_HOST_SUFFIXES (both MIT), minus the platforms supported above.
UNSUPPORTED_ATS_HOSTS = (
    "icims.com", "jobvite.com", "breezy.hr", "jazz.co", "applytojob.com", "taleo.net",
    "successfactors.com", "successfactors.eu", "ultipro.com", "paylocity.com", "avature.net",
    "phenom.com", "eightfold.ai", "teamtailor.com", "pinpointhq.com", "myworkdaysite.com",
    "myworkday.com", "inrecruiting.com", "altamirahrm.com", "join.com", "factorial.it",
    "factorialhr.com", "softgarden.io", "zohorecruit.com", "zohorecruit.eu", "jobs.personio.com",
    # Italian and other ATS of the recruiter simulators (recruiter_sim/data/profiles.json)
    "allibo.com", "intervieweb.it", "in-recruiting.com", "altamiraweb.net", "sapsf.eu", "sapsf.com",
    "arca24.com", "bullhornstaffing.com",
)

# Host -> platform key of every selection system the recruiter simulators know
# (recruiter_sim profiles "platforms"/"hosts"): the readable boards above, the
# recognised-only ATS and the staffing agencies (whose back office is not documented).
PLATFORM_HOSTS: dict[str, str] = {
    "joblink.allibo.com": "allibo", "allibo.com": "allibo",
    "intervieweb.it": "inrecruiting", "inrecruiting.com": "inrecruiting", "in-recruiting.com": "inrecruiting",
    "altamiraweb.net": "altamira", "altamirahrm.com": "altamira",
    "myworkdayjobs.com": "workday", "myworkdaysite.com": "workday", "myworkday.com": "workday",
    "successfactors.com": "successfactors", "successfactors.eu": "successfactors", "sapsf.eu": "successfactors",
    "sapsf.com": "successfactors",
    "taleo.net": "oracle", "oraclecloud.com": "oracle",
    "smartrecruiters.com": "smartrecruiters", "greenhouse.io": "greenhouse", "ashbyhq.com": "ashby",
    "workable.com": "workable", "jobs.personio.de": "personio", "jobs.personio.com": "personio",
    "teamtailor.com": "teamtailor", "icims.com": "icims", "recruitee.com": "recruitee",
    "bullhorn.com": "bullhorn", "bullhornstaffing.com": "bullhorn", "arca24.com": "arca24",
    "lever.co": "lever", "bamboohr.com": "bamboohr",
    "adecco.com": "adecco", "adecco.it": "adecco", "randstad.it": "randstad", "gigroup.it": "gigroup",
    "manpower.it": "manpower", "umana.it": "umana", "synergie-italia.it": "synergie",
    "openjobmetis.it": "openjobmetis", "orienta.net": "orienta", "etjca.it": "etjca",
}


def platform_for_host(url_or_host: str | None) -> str | None:
    """The platform key ("allibo", "workday", "randstad"...) of a URL or host name, the
    longest matching host first; None for hosts no simulator knows."""
    text = (url_or_host or "").strip()
    if not text:
        return None
    try:
        host = (urlsplit(text if "://" in text else "https://" + text).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if not host:
        return None
    for known in sorted(PLATFORM_HOSTS, key=len, reverse=True):
        if host == known or host.endswith("." + known):
            return PLATFORM_HOSTS[known]
    return None


def is_unsupported_ats(url: str) -> bool:
    """True if the URL is on an ATS host recognised but not readable through an API."""
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if not host or detect_ats(url):
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in UNSUPPORTED_ATS_HOSTS)


def _group(found: re.Match, name: str) -> str:
    try:
        return found.group(name) or ""
    except IndexError:
        return ""


def detect_ats(*texts: str) -> list[ATSReference]:
    """ATS platforms mentioned in HTML or URLs (links, iframes, embed scripts).

    Returns the references in the order they appear, without duplicates.
    """
    matches: list[tuple[int, ATSReference]] = []
    seen: set[tuple[str, str]] = set()
    for text_index, text in enumerate(texts):
        if not text:
            continue
        text = text.replace("\\/", "/")  # URLs inside JSON/JS strings
        for platform, pattern in _PLATFORM_PATTERNS:
            for found in pattern.finditer(text):
                slug = found.group("slug").strip(".-_")
                if slug.lower().endswith((".js", ".css", ".png", ".svg")):
                    continue
                if not slug or slug.lower() in _NOT_SLUGS:
                    continue
                domain = _group(found, "domain").lower()
                site = _group(found, "site")
                if platform == "workday" and (not site or site.lower() in _NOT_SLUGS):
                    continue
                key = (platform, slug.lower())
                if key in seen:
                    continue
                seen.add(key)
                position = text_index * 10_000_000 + found.start()
                matches.append((position, ATSReference(platform, slug, domain, site)))
    matches.sort(key=lambda pair: pair[0])
    return [ref for _, ref in matches]


# --- conversion of responses into Posting ----------------------------------


def _posting(title, url, location, description, platform: str, *, employment=None,
             workplace=None, salary=None, extra_text: str = "") -> Posting | None:
    """Posting with its attributes (contract, remote, salary, travel): the structured
    ATS fields first, then the full text (description + `extra_text`)."""
    title = compact(str(title or ""))
    url = str(url or "").strip()
    if not title or not url:
        return None
    location = compact(str(location or ""))
    body = "\n".join(t for t in (fragment_lines(str(description or ""), DESCRIPTION_LENGTH),
                                 fragment_lines(str(extra_text or ""), DESCRIPTION_LENGTH)) if t)
    posting = Posting(
        title=title,
        url=url,
        description=body[:DESCRIPTION_LENGTH].rstrip(),
        location=location,
        source=f"ats:{platform}",
    )
    full_text = "\n".join(
        t for t in (text_from_fragment(str(description or ""), FULL_TEXT_LENGTH),
                    text_from_fragment(str(extra_text or ""), FULL_TEXT_LENGTH)) if t
    )
    attributes = detect_attributes(full_text, title=title, location=location, employment_type=employment,
                                   workplace=workplace, salary=salary)
    return apply_attributes(posting, attributes)


def _join(*parts) -> str:
    return ", ".join(compact(str(p)) for p in parts if p and compact(str(p)))


def _get(data, key: str, default=None):
    """`data[key]` if `data` is a dict, otherwise `default` (APIs sometimes return null)."""
    return data.get(key, default) if isinstance(data, dict) else default


def _greenhouse_metadata(job: dict, *words: str):
    """Values of the custom fields ("metadata") whose name contains one of the words."""
    values = []
    for item in job.get("metadata") or []:
        name = str(_get(item, "name", "")).lower()
        if any(w in name for w in words) and _get(item, "value"):
            values.append(_get(item, "value"))
    return values or None


def _greenhouse_pay(job: dict):
    """First pay range (only present with ?pay_transparency=true), in currency units."""
    for item in job.get("pay_input_ranges") or []:
        low, high = _get(item, "min_cents"), _get(item, "max_cents")
        if isinstance(low, (int, float)) or isinstance(high, (int, float)):
            return {
                "min": low / 100 if isinstance(low, (int, float)) else None,
                "max": high / 100 if isinstance(high, (int, float)) else None,
                "currency": _get(item, "currency_type", ""),
            }
    return None


def _greenhouse(data, ref) -> list[Posting]:
    result = []
    for job in (data or {}).get("jobs", []) if isinstance(data, dict) else []:
        location = (job.get("location") or {}).get("name", "") if isinstance(job.get("location"), dict) else ""
        offices = " ".join(str(_get(o, "name", "")) for o in job.get("offices") or [])
        for word in ("Remote", "Hybrid"):  # an office named "Remote" is the job's arrangement
            if re.search(rf"\b{word}\b", offices, re.I) and not re.search(rf"\b{word}\b", location, re.I):
                location = _join(location, word)
        result.append(_posting(
            job.get("title"), job.get("absolute_url"), location, job.get("content"), "greenhouse",
            employment=_greenhouse_metadata(job, "employment", "contract"),
            workplace=_greenhouse_metadata(job, "workplace", "remote", "location type"),
            salary=_greenhouse_pay(job),
        ))
    return result


def _lever(data, ref) -> list[Posting]:
    result = []
    for job in data if isinstance(data, list) else []:
        categories = job.get("categories") or {}
        location = categories.get("location") or _join(*(categories.get("allLocations") or []))
        description = job.get("descriptionPlain") or job.get("openingPlain") or job.get("description")
        # Lever "lists" are the requirement blocks: one heading line, then one line per item.
        lists = "\n".join(f"{_get(x, 'text', '')}\n{_get(x, 'content', '')}" for x in job.get("lists") or [])
        extra = "\n".join(str(x) for x in (lists, job.get("additionalPlain"),
                                           job.get("salaryDescriptionPlain")) if x)
        result.append(_posting(
            job.get("text"), job.get("hostedUrl"), location, description, "lever",
            employment=categories.get("commitment"), workplace=job.get("workplaceType"),
            salary=job.get("salaryRange"), extra_text=extra,
        ))
    return result


def _ashby_salary(job: dict):
    compensation = job.get("compensation")
    for component in _get(compensation, "summaryComponents", []) or []:
        if str(_get(component, "compensationType", "")).lower() in ("salary", "basesalary", ""):
            return {"min": _get(component, "minValue"), "max": _get(component, "maxValue"),
                    "currency": _get(component, "currencyCode", ""),
                    "interval": _get(component, "interval", "")}
    return None


def _ashby(data, ref) -> list[Posting]:
    result = []
    for job in (data or {}).get("jobs", []) if isinstance(data, dict) else []:
        if job.get("isListed") is False:
            continue
        location = job.get("location") or ""
        if job.get("isRemote") and "remot" not in location.lower():
            location = _join(location, "Remote")
        description = job.get("descriptionHtml") or job.get("descriptionPlain")
        workplace = job.get("workplaceType") or ("remote" if job.get("isRemote") else None)
        posting = _posting(
            job.get("title"), job.get("jobUrl"), location, description, "ashby",
            employment=job.get("employmentType"), workplace=workplace, salary=_ashby_salary(job),
        )
        summary = _get(job.get("compensation"), "compensationTierSummary")
        if posting is not None and posting.salary and isinstance(summary, str) and summary.strip():
            posting.salary = compact(summary)  # as published
        result.append(posting)
    return result


def _workable(data, ref) -> list[Posting]:
    result = []
    for job in (data or {}).get("jobs", []) if isinstance(data, dict) else []:
        location = _join(job.get("city"), job.get("country"))
        if job.get("telecommuting"):
            location = _join(location, "Remote")
        url = job.get("url") or job.get("shortlink") or job.get("application_url")
        description = _join(job.get("department"), job.get("employment_type"))
        result.append(_posting(
            job.get("title"), url, location, description, "workable",
            employment=job.get("employment_type"),
            workplace="remote" if job.get("telecommuting") else None,
        ))
    return result


def _recruitee(data, ref) -> list[Posting]:
    result = []
    for job in (data or {}).get("offers", []) if isinstance(data, dict) else []:
        if job.get("status") and job.get("status") != "published":
            continue
        location = job.get("location") or _join(job.get("city"), job.get("country"))
        workplace = ("hybrid" if job.get("hybrid") else "remote" if job.get("remote")
                     else "onsite" if job.get("on_site") else None)
        result.append(_posting(
            job.get("title"), job.get("careers_url"), location, job.get("description"), "recruitee",
            employment=job.get("employment_type_code"), workplace=workplace,
            salary=job.get("salary"), extra_text=str(job.get("requirements") or ""),
        ))
    return result


def _smartrecruiters_job(job: dict, ref, detail: dict | None = None) -> Posting | None:
    place = job.get("location") or {}
    location = place.get("fullLocation") or _join(place.get("city"), place.get("country"))
    if place.get("remote"):
        location = _join(location, "Remote")
    company = (job.get("company") or {}).get("identifier") or ref.slug
    url = f"https://jobs.smartrecruiters.com/{company}/{job.get('id')}" if job.get("id") else ""
    labels = _join(
        (job.get("function") or {}).get("label"),
        (job.get("typeOfEmployment") or {}).get("label"),
        (job.get("experienceLevel") or {}).get("label"),
    )
    sections = _get(_get(detail, "jobAd"), "sections") or {}
    blocks = []
    for key in ("jobDescription", "qualifications", "additionalInformation"):
        section = _get(sections, key)
        if _get(section, "text"):
            blocks.append(f"<h3>{_get(section, 'title', '')}</h3>{_get(section, 'text', '')}")
    description = "\n".join([labels] + blocks) if blocks else labels
    workplace = "hybrid" if place.get("hybrid") else "remote" if place.get("remote") else None
    return _posting(
        job.get("name"), url, location, description, "smartrecruiters",
        employment=_get(job.get("typeOfEmployment"), "id"), workplace=workplace,
    )


def _smartrecruiters(data, ref) -> list[Posting]:
    return [_smartrecruiters_job(job, ref)
            for job in ((data or {}).get("content", []) if isinstance(data, dict) else [])]


def _workday_url(ref, path: str) -> str:
    return f"https://{ref._workday_host}/{ref.site}{path}"


def _workday_job(job: dict, ref, info: dict | None = None) -> Posting | None:
    path = str(job.get("externalPath") or "")
    if not path.startswith("/"):
        return None
    info = info if isinstance(info, dict) else {}
    location = job.get("locationsText") or info.get("location") or ""
    workplace = info.get("remoteType") or job.get("remoteType")
    if not workplace:  # list rows sometimes carry the arrangement as a bullet ("Remote", "Hybrid")
        workplace = next((b for b in job.get("bulletFields") or []
                          if isinstance(b, str) and normalize(b) in ("remote", "hybrid", "fully remote")), None)
    return _posting(
        job.get("title") or info.get("title"), _workday_url(ref, path), location,
        info.get("jobDescription") or "", "workday",
        employment=info.get("timeType"), workplace=workplace,
    )


def _workday(data, ref) -> list[Posting]:
    return [_workday_job(job, ref)
            for job in ((data or {}).get("jobPostings") or [] if isinstance(data, dict) else [])
            if isinstance(job, dict)]


def _bamboohr_location(job: dict) -> str:
    place = job.get("location") or {}
    ats = job.get("atsLocation") or {}
    return (_join(_get(place, "city"), _get(place, "state"))
            or _join(_get(ats, "city"), _get(ats, "state") or _get(ats, "province"), _get(ats, "country")))


def _bamboohr_job(job: dict, ref, info: dict | None = None) -> Posting | None:
    id_ = str(job.get("id") or "").strip()
    if not id_:
        return None
    info = info if isinstance(info, dict) else {}
    url = f"https://{ref.slug.lower()}.bamboohr.com/careers/{quote(id_, safe='')}"
    remote = job.get("isRemote") is True or str(info.get("locationType") or "") == "2"
    location = _bamboohr_location(job)
    if remote and "remot" not in location.lower():
        location = _join(location, "Remote")
    compensation = info.get("compensation")
    return _posting(
        job.get("jobOpeningName") or info.get("jobOpeningName"), url, location,
        info.get("description") or info.get("jobDescription") or "", "bamboohr",
        employment=info.get("employmentStatusLabel") or job.get("employmentStatusLabel"),
        workplace="remote" if remote else None,
        extra_text=f"Compensation: {compensation}" if compensation else "",
    )


def _bamboohr(data, ref) -> list[Posting]:
    return [_bamboohr_job(job, ref)
            for job in ((data or {}).get("result") or [] if isinstance(data, dict) else [])
            if isinstance(job, dict)]


def _personio(text: str, ref) -> list[Posting]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, recover=True)
    try:
        root = etree.fromstring(text.encode("utf-8"), parser)
    except (etree.XMLSyntaxError, ValueError):
        return []
    if root is None:
        return []
    base = f"https://{ref.slug}.jobs.personio.{ref.domain or 'de'}"
    result = []
    for position in root.iter("position"):
        id_ = (position.findtext("id") or "").strip()
        descriptions = [
            f"<h3>{d.findtext('name') or ''}</h3>{d.findtext('value') or ''}"
            for d in position.iter("jobDescription")
        ]
        result.append(
            _posting(
                position.findtext("name"),
                f"{base}/job/{id_}" if id_ else "",
                position.findtext("office"),
                "\n".join(descriptions),
                "personio",
                employment=(position.findtext("employmentType") or "").strip() or None,
            )
        )
    return result


_CONVERTERS: dict[str, Callable] = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "workable": _workable,
    "recruitee": _recruitee,
    "smartrecruiters": _smartrecruiters,
    "workday": _workday,
    "bamboohr": _bamboohr,
}


def _json(body: str | None):
    if body is None:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def parse_response(ref: ATSReference, body: str) -> list[Posting] | None:
    """Postings from the API response; None if the response cannot be interpreted.

    For Workday and BambooHR this is one list response without the detail texts.
    """
    if ref.platform == "personio":
        if "<workzag-jobs" not in body:
            return None
        postings = _personio(body, ref)
    else:
        data = _json(body)
        if data is None:
            return None
        try:
            postings = _CONVERTERS[ref.platform](data, ref)
        except (AttributeError, TypeError, KeyError) as error:
            log.info("Unexpected %s response (%s)", ref.platform, type(error).__name__)
            return None
    return [a for a in postings if a is not None][:MAX_POSTINGS]


# --- download (one or more requests per board) -----------------------------


# Words that name the same job family in titles (IT/EN): "Software Engineer" is close
# to the CV role "Software Developer".
_TITLE_FAMILIES = {
    "developer": "dev", "developers": "dev", "engineer": "dev", "engineers": "dev", "programmer": "dev",
    "sviluppatore": "dev", "sviluppatrice": "dev", "programmatore": "dev", "programmatrice": "dev",
    "ingegnere": "dev", "swe": "dev", "dev": "dev",  # not "development": "Business Development"
    "analyst": "analyst", "analista": "analyst", "architect": "architect", "architetto": "architect",
    "designer": "designer", "accountant": "accounting", "contabile": "accounting",
    "accounting": "accounting", "contabilita": "accounting", "sales": "sales", "commerciale": "sales",
    "vendite": "sales", "tecnico": "technician", "technician": "technician",
}


def _title_words(text: str) -> set[str]:
    return {_TITLE_FAMILIES.get(w, w) for w in re.findall(r"[a-z0-9+#]+", normalize(text or ""))
            if len(w) >= 3 and w not in _GENERIC_TITLE_WORDS}


def detail_order(titles: list[str], roles: list[str] | None) -> list[int]:
    """Indices of the postings in the order their detail should be fetched.

    Titles sharing more words with the candidate's roles come first (the
    postings whose fit will be computed); ties, and everything when the roles
    are unknown, keep the board's own order.
    """
    wanted = set().union(*(_title_words(r) for r in roles or [] if r)) if roles else set()
    scores = [len(_title_words(t) & wanted) if wanted else 0 for t in titles]
    return sorted(range(len(titles)), key=lambda i: (-scores[i], i))


def _detail_indices(titles: list[str], roles: list[str] | None, eligible: list[bool]) -> set[int]:
    """The (at most MAX_DETAIL_CALLS) postings worth a detail call."""
    order = [i for i in detail_order(titles, roles) if eligible[i]]
    return set(order[:max(0, MAX_DETAIL_CALLS)])


def _download_workday(ref: ATSReference, fetcher, roles: list[str] | None = None) -> list[Posting] | None:
    """List pages (POST), then the detail of the MAX_DETAIL_CALLS postings closest to `roles`."""
    if not _WORKDAY_SITE.match(ref.site or "") or not _BAMBOO_SLUG.match(ref.slug.lower()):
        return None
    jobs: list[dict] = []
    total = 0
    for page in range(WORKDAY_MAX_PAGES):
        payload = {"appliedFacets": {}, "limit": WORKDAY_PAGE_SIZE,
                   "offset": page * WORKDAY_PAGE_SIZE, "searchText": ""}
        data = _json(fetcher.download_data(ref.url_api, json_body=payload))
        if not isinstance(data, dict):
            if page == 0:
                return None
            break
        found = [j for j in data.get("jobPostings") or [] if isinstance(j, dict)]
        jobs.extend(found)
        if page == 0:  # the CXS API reports the total on the first page only (0 afterwards)
            total = data.get("total") if isinstance(data.get("total"), int) else 0
        if (len(found) < WORKDAY_PAGE_SIZE or len(jobs) >= MAX_POSTINGS
                or (total and len(jobs) >= total)):
            break
    jobs = jobs[:MAX_POSTINGS]
    base = f"https://{ref._workday_host}/wday/cxs/{ref.slug.lower()}/{ref.site}"
    paths = [str(job.get("externalPath") or "") for job in jobs]
    detailed = _detail_indices([str(job.get("title") or "") for job in jobs], roles,
                               [p.startswith("/") for p in paths])
    result = []
    for index, job in enumerate(jobs):
        info = None
        if index in detailed:
            info = _get(_json(fetcher.download_data(base + paths[index])), "jobPostingInfo")
        result.append(_workday_job(job, ref, info))
    return [p for p in result if p is not None]


def _download_bamboohr(ref: ATSReference, fetcher, roles: list[str] | None = None) -> list[Posting] | None:
    """Careers list, then the detail of the MAX_DETAIL_CALLS openings closest to `roles`."""
    slug = ref.slug.lower()
    if not _BAMBOO_SLUG.match(slug):
        return None
    data = _json(fetcher.download_data(ref.url_api))
    if not isinstance(data, dict):
        return None
    jobs = [j for j in data.get("result") or [] if isinstance(j, dict)][:MAX_POSTINGS]
    ids = [str(job.get("id") or "").strip() for job in jobs]
    detailed = _detail_indices([str(job.get("jobOpeningName") or "") for job in jobs], roles,
                               [bool(i) for i in ids])
    result = []
    for index, job in enumerate(jobs):
        info = None
        if index in detailed:
            detail = _json(fetcher.download_data(
                f"https://{slug}.bamboohr.com/careers/{quote(ids[index], safe='')}/detail"))
            info = _get(_get(detail, "result"), "jobOpening")
        result.append(_bamboohr_job(job, ref, info))
    return [p for p in result if p is not None]


def _download_smartrecruiters(ref: ATSReference, fetcher,
                              roles: list[str] | None = None) -> list[Posting] | None:
    """List, then the detail (job ad sections) of the MAX_DETAIL_CALLS postings closest to `roles`.

    An empty list is not a board: the API answers 200 with zero results for any slug.
    """
    data = _json(fetcher.download_data(ref.url_api))
    if not isinstance(data, dict):
        return None
    jobs = [j for j in data.get("content") or [] if isinstance(j, dict)][:MAX_POSTINGS]
    if not jobs:
        return None
    ids = [str(job.get("id") or "").strip() for job in jobs]
    detailed = _detail_indices([str(job.get("name") or "") for job in jobs], roles,
                               [bool(re.fullmatch(r"[A-Za-z0-9-]+", i)) for i in ids])
    result = []
    for index, job in enumerate(jobs):
        detail = None
        if index in detailed:
            detail = _json(fetcher.download_data(
                f"https://api.smartrecruiters.com/v1/companies/{quote(ref.slug, safe='')}/postings/{ids[index]}"))
        result.append(_smartrecruiters_job(job, ref, detail if isinstance(detail, dict) else None))
    return [p for p in result if p is not None]


_DOWNLOADERS: dict[str, Callable] = {
    "workday": _download_workday,
    "bamboohr": _download_bamboohr,
    "smartrecruiters": _download_smartrecruiters,
}


def fetch_board(ref: ATSReference, fetcher,
                roles: list[str] | None = None) -> tuple[ATSReference, list[Posting]] | None:
    """(reference actually answering, postings) from the ATS public API; None if no board.

    A Greenhouse or Lever board that does not answer on its host is tried once on
    the other region (EU <-> default); the returned reference says which one answered.
    An empty list = existing board without open positions. `roles` (the
    candidate's role names) choose which postings get a detail call on the
    platforms whose list has no description (Workday, BambooHR, SmartRecruiters).
    """
    downloader = _DOWNLOADERS.get(ref.platform)
    if downloader is not None:
        try:
            postings = downloader(ref, fetcher, roles)
        except (AttributeError, TypeError, KeyError, ValueError) as error:
            log.info("Unexpected %s response (%s)", ref.platform, type(error).__name__)
            return None
        return None if postings is None else (ref, postings)
    body = fetcher.download_data(ref.url_api)
    if body is None:
        other = ref.other_region()
        if other is None:
            return None
        body = fetcher.download_data(other.url_api)
        if body is None:
            return None
        ref = other
    postings = parse_response(ref, body)
    return None if postings is None else (ref, postings)


def download_postings(ref: ATSReference, fetcher, roles: list[str] | None = None) -> list[Posting] | None:
    """Postings from the ATS public API; None if the board does not respond.

    Empty list = existing board without open positions.
    """
    found = fetch_board(ref, fetcher, roles)
    return None if found is None else found[1]


# --- slug probing ------------------------------------------------------------


def probe_slugs(site_url: str) -> list[str]:
    """Board names worth trying for a company site: the host label, without hyphens too."""
    try:
        host = (urlsplit(site_url if "://" in site_url else "https://" + site_url).hostname or "").lower()
    except ValueError:
        return []
    labels = [p for p in host.split(".") if p]
    if len(labels) < 2:
        return []
    label = labels[-2]
    if label in ("co", "com", "org", "net", "gov", "ac") and len(labels) >= 3:
        label = labels[-3]  # acme.co.uk
    if len(label) < MIN_PROBE_LABEL or label in _NOT_SLUGS or not re.fullmatch(r"[a-z0-9-]+", label):
        return []
    result = [label]
    if "-" in label and label.replace("-", "") not in result:
        result.append(label.replace("-", ""))
    return result


_GENERIC_SECOND_LEVEL = ("co", "com", "org", "net", "gov", "ac", "edu")


def site_domain(site_url: str) -> str:
    """Registrable domain of a site ("www.acme.co.uk/x" -> "acme.co.uk"); "" if none."""
    try:
        host = (urlsplit(site_url if "://" in site_url else "https://" + site_url).hostname or "").lower()
    except ValueError:
        return ""
    labels = [p for p in host.rstrip(".").split(".") if p]
    if len(labels) < 2:
        return ""
    keep = 3 if labels[-2] in _GENERIC_SECOND_LEVEL and len(labels) >= 3 else 2
    return ".".join(labels[-keep:])


def names_domain(body: str, domain: str) -> bool:
    """The raw board data names the company's domain as a host or e-mail domain
    ("acme.it", "www.acme.it/x", "jobs@acme.it"), not as part of a longer name
    ("bancoacme.it", "acme.it.example.com", "acme.items")."""
    if not body or not domain:
        return False
    pattern = r"(?<![a-z0-9-])" + re.escape(domain) + r"(?![a-z0-9-])(?!\.[a-z0-9])"
    return re.search(pattern, body.replace("\\/", "/"), re.I) is not None


def probe_ats(site_url: str, fetcher, *, max_calls: int = MAX_PROBE_CALLS,
              platforms: tuple[str, ...] = PROBE_PLATFORMS) -> tuple[ATSReference, list[Posting]] | None:
    """Board guessed from the site's domain, for sites that show no ATS link.

    A guess counts only when the board's data names the company's own domain
    (`names_domain`): a board named like the brand, or postings that merely
    contain the brand word, are not evidence (short names such as "inter" are
    also other companies' boards). An empty board is never a hit.
    SmartRecruiters is never probed (it answers 200 with zero results for any name).
    Disabled entirely when PROBE_ENABLED is False.
    """
    domain = site_domain(site_url)
    if not PROBE_ENABLED or not domain:
        return None
    calls = 0
    for slug in probe_slugs(site_url):
        for platform in platforms:
            if platform == "smartrecruiters" or calls >= max_calls:
                continue
            calls += 1
            ref = ATSReference(platform, slug)
            # a guess: the fetcher remembers the misses so they are not asked again
            download = getattr(fetcher, "download_probe", None) or fetcher.download_data
            body = download(ref.url_api)
            postings = parse_response(ref, body) if body is not None else None
            if postings and names_domain(body, domain):
                return ref, postings
            if postings:
                log.info("Board %s/%s ignored: it does not name %s", platform, slug, domain)
    return None
