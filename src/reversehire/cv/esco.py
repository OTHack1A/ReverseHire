"""Normalisation of role titles on the ESCO classification (public EU API).

Endpoint verified live (2026-11-10):
    GET https://ec.europa.eu/esco/api/search?text=<titolo>&language=<it|en>&type=occupation&limit=5
JSON response: `_embedded.results[]` with, for each occupation,
    uri            "http://data.europa.eu/esco/occupation/<uuid>"
    title          preferred label in the requested language
    searchHit      label (possibly an alternative one) that matched the search
    code           ESCO/ISCO code, e.g. "2512.3" (not always unique)
    preferredLabel dict language -> preferred label (all EU languages)
The API sorts by "engine" relevance, not by exact similarity (for "contabile"
the first result is "responsabile della contabilità"): here the result most
similar to the title is chosen with rapidfuzz, above a threshold.

Privacy: ONLY the (cleaned) role title is sent to the API, never other text.

Use in the profile (see extraction.py): `ProfileItem.esco_code` = occupation
URI; two informative evidences are added,
`Evidence(text=<preferred IT label>, source="esco:it")` and
`Evidence(text=<preferred EN label>, source="esco:en")`, so the label in the
other language is available to later modules without changing the models.
"""

import logging
import re
from dataclasses import asdict, dataclass

import httpx
from rapidfuzz import fuzz

from ..store import Store
from ..config import Settings
from .sections import normalize

log = logging.getLogger(__name__)

SEARCH_URL = "https://ec.europa.eu/esco/api/search"
CACHE_NAMESPACE = "esco"
THRESHOLD = 75.0
TIMEOUT_S = 6.0

_FILLER_WORDS = re.compile(
    r"\b(senior|junior|sr|jr|lead|trainee|intern|stagista|tirocinante|apprendista|"
    r"part[- ]time|full[- ]time|freelance)\b\.?",
    re.IGNORECASE,
)


@dataclass
class EscoOccupation:
    uri: str
    code: str | None
    label: str  # in the search language
    label_it: str | None
    label_en: str | None
    score: float


def clean_title(title: str) -> str:
    """Title to send to the API: without parentheses, seniority levels, symbols."""
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", title)
    t = _FILLER_WORDS.sub(" ", t)
    t = re.sub(r"[^\w\s/&'+#-]", " ", t)
    return re.sub(r"\s+", " ", t).strip(" -/")


def _string_similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    # token_set rewards subsets ("infermiere" in "infermiere specialista"):
    # it is weighted less, so on a tie the closest label wins.
    return max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b), 0.9 * fuzz.token_set_ratio(a, b))


def choose_occupation(title: str, response: dict, language: str) -> EscoOccupation | None:
    """The search result most similar to the title, or None below the threshold."""
    results = (response.get("_embedded") or {}).get("results") or []
    best, best_score = None, 0.0
    for r in results:
        if not isinstance(r, dict) or not r.get("uri"):
            continue
        labels = [r.get("title") or ""]
        labels = [v for e in labels for v in e.split("/")]  # masculine/feminine forms
        scores = [_string_similarity(title, e) for e in labels if e]
        if r.get("searchHit"):
            scores.append(0.92 * _string_similarity(title, r["searchHit"]))
        p = max(scores, default=0.0)
        if p > best_score:
            best, best_score = r, p
    if best is None or best_score < THRESHOLD:
        return None
    pl = best.get("preferredLabel") or {}
    return EscoOccupation(
        uri=best["uri"],
        code=best.get("code"),
        label=best.get("title") or pl.get(language) or "",
        label_it=pl.get("it"),
        label_en=pl.get("en"),
        score=round(best_score, 1),
    )


# --- Essential / optional skills of an occupation ---------------------------------------
#
# Endpoint verified live (2026-11-12, 2 requests):
#     GET https://ec.europa.eu/esco/api/resource/occupation?uri=<occupation uri>&language=<it|en>
# JSON response: `_links.hasEssentialSkill[]` and `_links.hasOptionalSkill[]`, each item
#     {"href": ..., "uri": "http://data.europa.eu/esco/skill/<uuid>",
#      "title": <skill label in the requested language>, "skillType": ...}
# (accountant: 18 essential + 18 optional skills). Privacy: only the occupation URI is sent.

OCCUPATION_URL = "https://ec.europa.eu/esco/api/resource/occupation"
OCCUPATION_URI_PREFIX = "http://data.europa.eu/esco/occupation/"
SKILL_LANGUAGES = ("it", "en")


def skills_from_response(response: dict) -> dict[str, list[str]]:
    """{"essential": [...], "optional": [...]} skill labels of an occupation resource."""
    links = (response or {}).get("_links") or {}
    result: dict[str, list[str]] = {}
    for key, name in (("hasEssentialSkill", "essential"), ("hasOptionalSkill", "optional")):
        labels: list[str] = []
        for item in links.get(key) or []:
            title = (item.get("title") or "").strip() if isinstance(item, dict) else ""
            if title and title not in labels:
                labels.append(title)
        result[name] = labels
    return result


def occupation_skills(occupation_uri: str | None, language: str = "it",
                      store: Store | None = None, settings: Settings | None = None,
                      client: httpx.Client | None = None) -> dict[str, list[str]]:
    """Essential and optional skills of an ESCO occupation in the CV language.

    Cached forever in `Store` (namespace "esco"). Any network or format error
    gives empty lists and is not cached, so a later call can retry.
    """
    empty: dict[str, list[str]] = {"essential": [], "optional": []}
    if not occupation_uri or not str(occupation_uri).startswith(OCCUPATION_URI_PREFIX):
        return empty
    language = language if language in SKILL_LANGUAGES else "en"
    key = f"skills|{language}|{occupation_uri}"
    if store is not None:
        cached = store.cache_read(CACHE_NAMESPACE, key)
        if isinstance(cached, dict):
            return {"essential": list(cached.get("essential") or []),
                    "optional": list(cached.get("optional") or [])}
    settings = settings or Settings()
    owned = client is None
    http = client or httpx.Client(
        headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
        timeout=httpx.Timeout(TIMEOUT_S, connect=4.0),
        follow_redirects=True,
    )
    try:
        response = http.get(OCCUPATION_URL, params={"uri": occupation_uri, "language": language})
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as e:
        log.info("ESCO skills unavailable: %s", type(e).__name__)
        return empty
    finally:
        if owned:
            http.close()
    if not isinstance(data, dict):
        return empty
    result = skills_from_response(data)
    if store is not None:
        store.cache_write(CACHE_NAMESPACE, key, result)
    return result


def essential_skills(occupation_uri: str | None, language: str = "it",
                     store: Store | None = None, settings: Settings | None = None,
                     client: httpx.Client | None = None) -> list[str]:
    """Essential skills of the occupation (labels in `language`); [] when unavailable.

    Typical use: ``essential_skills(profile.roles[0].esco_code, profile.language,
    store, settings)``; pass the result as ``role_skills`` to ``scoring.assess``.
    """
    return occupation_skills(occupation_uri, language, store, settings, client)["essential"]


def profile_occupation(profile) -> str | None:
    """ESCO occupation URI of the most relevant role that has one, or None."""
    for item in getattr(profile, "roles", None) or []:
        if getattr(item, "esco_code", None):
            return item.esco_code
    return None


class EscoNormalizer:
    """Look up on ESCO the occupation matching a role title.

    Cached in `Store` (namespace "esco", no expiry), also for titles with no
    match. After the first network error it stops querying the API for the
    rest of the object's life (no repeated waits when offline).
    """

    def __init__(self, settings: Settings | None = None, store: Store | None = None,
                 client: httpx.Client | None = None):
        self.settings = settings or Settings()
        self.store = store
        self._client = client
        self._owned = client is None
        self._network_down = False

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                headers={"User-Agent": self.settings.user_agent, "Accept": "application/json"},
                timeout=httpx.Timeout(TIMEOUT_S, connect=4.0),
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        if self._owned and self._client is not None:
            self._client.close()
            self._client = None

    def normalize(self, title: str, language: str = "it") -> EscoOccupation | None:
        language = language if language in ("it", "en", "uk") else "it"
        text = clean_title(title)
        if len(text) < 2:
            return None
        key = f"occupation|{language}|{normalize(text)}"
        if self.store is not None:
            in_cache = self.store.cache_read(CACHE_NAMESPACE, key)
            if in_cache is not None:
                return EscoOccupation(**in_cache) if in_cache.get("uri") else None
        if self._network_down:
            return None
        try:
            response = self._http().get(
                SEARCH_URL,
                params={"text": text, "language": language, "type": "occupation", "limit": 5},
                headers={"User-Agent": self.settings.user_agent},
            )
            response.raise_for_status()
            data = response.json()
        except httpx.TransportError as e:  # no network, DNS, timeout
            log.info("ESCO unreachable (%s): profile without codes", e)
            self._network_down = True
            return None
        except (httpx.HTTPError, ValueError) as e:  # HTTP error or invalid JSON
            # The searched title comes from the CV: it must not be written to the log.
            log.info("Unusable ESCO response: %s", type(e).__name__)
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code >= 500:
                self._network_down = True
            return None
        if not isinstance(data, dict):
            return None
        occupation = choose_occupation(text, data, language)
        if self.store is not None:
            self.store.cache_write(
                CACHE_NAMESPACE, key, asdict(occupation) if occupation else {"uri": None}
            )
        return occupation
