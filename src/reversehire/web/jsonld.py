"""Extraction of schema.org/JobPosting from the JSON-LD blocks of pages."""

import json
import logging
import re
from datetime import date, datetime
from urllib.parse import urljoin

from ..models import Posting
from .posting_attrs import apply_attributes, detect_attributes
from .text import analyze_html, compact, text_from_fragment

log = logging.getLogger(__name__)

DESCRIPTION_LENGTH = 500
FULL_TEXT_LENGTH = 20000  # text read for the attributes (contract, salary...)
MAX_DEPTH = 12


def _jsonld_blocks(html: str, tree=None) -> list[str]:
    if tree is None:
        tree = analyze_html(html)
    if tree is None:
        return []
    blocks = []
    for script in tree.iter("script"):
        kind = (script.get("type") or "").split(";")[0].strip().lower()
        if kind == "application/ld+json":
            blocks.append(script.text or "")
    return blocks


def _load(block: str):
    text = block.strip()
    # Some CMSs wrap the JSON in HTML comments or CDATA.
    text = re.sub(r"^\s*(<!--|/\*\s*<!\[CDATA\[\s*\*/)", "", text)
    text = re.sub(r"(-->|/\*\s*\]\]>\s*\*/)\s*$", "", text)
    if not text:
        return None
    try:
        return json.loads(text, strict=False)
    except ValueError:
        return None


def _is_jobposting(item: dict) -> bool:
    kind = item.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(isinstance(t, str) and t.split("/")[-1].lower() == "jobposting" for t in kinds)


def _collect(item, matches: list[dict], depth: int = 0) -> None:
    """Recursive visit: JobPosting at the top level, in lists, @graph, ItemList..."""
    if depth > MAX_DEPTH:
        return
    if isinstance(item, list):
        for element in item:
            _collect(element, matches, depth + 1)
    elif isinstance(item, dict):
        if _is_jobposting(item):
            matches.append(item)
            return
        for value in item.values():
            if isinstance(value, (dict, list)):
                _collect(value, matches, depth + 1)


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("name") or value.get("@value") or "")
    if isinstance(value, list):
        return ", ".join(filter(None, (_text(v) for v in value)))
    return "" if value is None else str(value)


def _location(posting: dict) -> str:
    places = posting.get("jobLocation")
    places = places if isinstance(places, list) else [places] if places else []
    locations: list[str] = []
    for place in places:
        if isinstance(place, str):
            parts = [place]
        elif isinstance(place, dict):
            address = place.get("address", place)
            if isinstance(address, str):
                parts = [address]
            elif isinstance(address, dict):
                parts = [
                    _text(address.get("addressLocality")),
                    _text(address.get("addressRegion")),
                    _text(address.get("addressCountry")),
                ]
                if not any(parts):
                    parts = [_text(place.get("name"))]
            else:
                parts = [_text(place.get("name"))]
        else:
            continue
        location = ", ".join(compact(p) for p in parts if p and compact(p))
        if location and location not in locations:
            locations.append(location)
    place_type = _text(posting.get("jobLocationType")).upper()
    if "TELECOMMUTE" in place_type:
        locations.append("Remote")
    return "; ".join(locations)


def _expired(posting: dict, today: date) -> bool:
    valid_through = posting.get("validThrough")
    if not isinstance(valid_through, str) or not valid_through.strip():
        return False
    text = valid_through.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date() < today
    except ValueError:
        try:
            return date.fromisoformat(text[:10]) < today
        except ValueError:
            return False


def extract_jobposting(html: str, page_url: str, today: date | None = None, tree=None) -> list[Posting]:
    """Postings from the JSON-LD JobPostings of the page (malformed blocks ignored)."""
    today = today or date.today()
    matches: list[dict] = []
    for block in _jsonld_blocks(html, tree):
        data = _load(block)
        if data is not None:
            _collect(data, matches)
    postings: list[Posting] = []
    for posting in matches:
        title = compact(_text(posting.get("title") or posting.get("name")))
        if not title or _expired(posting, today):
            continue
        url = _text(posting.get("url") or posting.get("sameAs") or "").strip()
        url = urljoin(page_url, url) if url else page_url
        location = _location(posting)
        result = Posting(
            title=title,
            url=url,
            description=text_from_fragment(_text(posting.get("description")), DESCRIPTION_LENGTH),
            location=location,
            source="jsonld",
        )
        apply_attributes(result, _attributes(posting, title, location))
        postings.append(result)
    return postings


def _workplace(posting: dict):
    """jobLocationType TELECOMMUTE, or applicantLocationRequirements alone (remote job)."""
    place_type = _text(posting.get("jobLocationType"))
    if place_type:
        return place_type
    if posting.get("applicantLocationRequirements") and not posting.get("jobLocation"):
        return "remote"
    return None


def _attributes(posting: dict, title: str, location: str):
    """Contract, remote work, salary and travel from the JobPosting fields and its full text."""
    parts = [text_from_fragment(_text(posting.get(key)), FULL_TEXT_LENGTH)
             for key in ("description", "jobBenefits", "responsibilities", "qualifications")]
    text = "\n".join(p for p in parts if p)
    return detect_attributes(
        text,
        title=title,
        location=location,
        employment_type=posting.get("employmentType"),
        workplace=_workplace(posting),
        salary=posting.get("baseSalary") or posting.get("estimatedSalary"),
    )
