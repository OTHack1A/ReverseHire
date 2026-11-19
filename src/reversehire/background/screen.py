"""Fast offline pre-screen of the whole candidate list: it ORDERS the deep-analysis queue,
it never hides a company.

Only data already in memory is used (no network, no file reads after the first call):
the name words, OpenStreetMap tags, Wikidata industry/description, imported register
fields (ATECO, employees), and previous card data (description, sectors, products).

Per company:
- the discovery pre-crawl ranking (`discovery.ranking.context_for` + `rank`, read-only
  reuse of its public functions) as the general signal;
- a likely company type from the affinity knowledge table (word patterns, ATECO
  prefixes, OSM tags of ``affinity/data/knowledge.json``, matched once on one
  normalised text; skipped when that package is not available);
- per enabled direction: the direction's role and skill words found in the text, and
  how well the company type suits the direction;
- the user's company-type weight (0 = "not wanted": the company stays in the list,
  ordered last, with the reason).

Cost: one text normalisation and a few set/regex lookups per company; designed for well
under a second per thousand companies (a test checks it).
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from typing import Callable

from ..models import Company
from .texts import text

log = logging.getLogger(__name__)

# How well each company type suits each shipped direction (unknown pairs: 0.5).
DIRECTION_TYPES: dict[str, dict[str, float]] = {
    "industrial_automation": {"machine_builder": 1.0, "system_integrator": 1.0, "manufacturer": 0.75,
                              "software_house": 0.35},
    "software_development": {"software_house": 1.0, "system_integrator": 0.65, "machine_builder": 0.55,
                             "manufacturer": 0.35},
    "cybersecurity_networks": {"system_integrator": 1.0, "software_house": 0.9, "manufacturer": 0.45,
                               "machine_builder": 0.35},
    "technical_coordination": {"machine_builder": 0.8, "system_integrator": 0.8, "manufacturer": 0.8,
                               "software_house": 0.7},
}
TYPE_NAMES = {"machine_builder": "machine builder", "system_integrator": "system integrator",
              "manufacturer": "manufacturer", "software_house": "software / IT"}
UNKNOWN_TYPE_FIT = 0.45
EXCLUDED_FACTOR = 0.05
W_TERMS, W_TYPE, W_RANK = 0.45, 0.25, 0.30
MAX_TEXT_CHARS = 1500


@dataclass
class ScreenResult:
    key: str
    name: str
    score: float              # ordering only (higher first), not an affinity
    direction: str = ""       # best direction id ("" when no directions)
    direction_name: str = ""
    company_type: str = ""    # type key or ""
    reason: str = ""          # one line
    index: int = 0            # position in the input list

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Direction:
    id: str
    name: str
    terms: frozenset


def _normalize(value: str) -> str:
    from ..geo.sectors import normalize_text

    return normalize_text(value)


def _phrases(norm: str, longest: int = 3) -> set[str]:
    words = norm.split()
    found: set[str] = set()
    for n in range(1, longest + 1):
        for i in range(len(words) - n + 1):
            found.add(" ".join(words[i:i + n]))
    return found


def _directions(preferences) -> list[_Direction]:
    if preferences is None:
        return []
    enabled = getattr(preferences, "enabled_directions", None)
    items = enabled() if callable(enabled) else [d for d in getattr(preferences, "directions", None) or []
                                                if getattr(d, "enabled", True)]
    out = []
    for d in items:
        words = set()
        for value in list(getattr(d, "roles", None) or []) + list(getattr(d, "skills", None) or []):
            norm = _normalize(value)
            if norm and len(norm) >= 3 and len(norm.split()) <= 3:
                words.add(norm)
        out.append(_Direction(str(d.id), str(getattr(d, "name", d.id)), frozenset(words)))
    return out


def _type_table():
    """(rules per type, threshold, find_pattern) from the affinity knowledge, or None."""
    try:
        from ..affinity.knowledge import COMPANY_TYPES, find_pattern, knowledge

        table = knowledge()["company_types"]
        return [(key, table.get(key) or {}) for key in COMPANY_TYPES], \
            float(knowledge().get("type_threshold", 1.0)), find_pattern
    except Exception:  # noqa: BLE001 - the pre-screen works without company types
        log.info("Pre-screen: company-type knowledge not available")
        return None


def _type_of(norm: str, ateco: str, tags: dict, table) -> tuple[str, str]:
    """(type key, evidence) of the strongest type over the threshold, or ("", "")."""
    if table is None:
        return "", ""
    rules_by_type, threshold, find_pattern = table
    best, best_score, best_evidence = "", 0.0, ""
    division = int(ateco[:2]) if re.match(r"\d{2}", ateco or "") else None
    for key, rules in rules_by_type:
        score, evidence = 0.0, ""
        text_score = 0.0
        for pattern, weight in rules.get("patterns", []):
            if find_pattern(pattern, norm):
                text_score += float(weight)
                evidence = evidence or pattern
        score += min(1.5, text_score)
        if ateco:
            for prefix, weight in rules.get("ateco_prefixes", []):
                if ateco == prefix or ateco.startswith(prefix + ".") or (len(prefix) > 2 and ateco.startswith(prefix)):
                    score += float(weight)
                    evidence = evidence or f"ATECO {ateco}"
                    break
            else:
                for low, high, weight in rules.get("ateco_divisions", []):
                    if division is not None and low <= division <= high:
                        score += float(weight)
                        evidence = evidence or f"ATECO {ateco}"
                        break
        for tag_key, tag_value, weight in rules.get("osm", []):
            present = tags.get(tag_key)
            if present and (tag_value == "*" or present == tag_value):
                score += float(weight)
                evidence = evidence or f"{tag_key}={present}"
        if score + 1e-9 >= threshold and score > best_score:
            best, best_score, best_evidence = key, score, evidence
    return best, best_evidence


def _flatten(value, limit: int = 400) -> str:
    out: list[str] = []

    def visit(v, depth=0):
        if sum(len(x) for x in out) > limit or depth > 3:
            return
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                visit(x, depth + 1)
        elif isinstance(v, (list, tuple)):
            for x in v:
                visit(x, depth + 1)

    visit(value)
    return " ".join(out)[:limit]


def _view(candidate, index: int, key_of) -> tuple[str, Company, str, str]:
    """(key, Company for the ranking, extra text, ATECO) of a Company, a CompanyCard or a dict."""
    extra_text, ateco = "", ""
    if isinstance(candidate, Company):
        company = candidate
    elif hasattr(candidate, "to_company"):
        company = candidate.to_company()
        extra_text = " ".join([str(getattr(candidate, "description", "") or ""),
                               " ".join(getattr(candidate, "sectors", None) or []),
                               _flatten(getattr(candidate, "products", None))])
        ateco = str(getattr(candidate, "ateco", "") or "")
    elif isinstance(candidate, dict):
        company = Company(name=str(candidate.get("name") or ""), lat=candidate.get("lat"), lon=candidate.get("lon"),
                          site=candidate.get("site"), osm_tags=dict(candidate.get("osm_tags") or {}),
                          fields={str(k): str(v) for k, v in (candidate.get("fields") or {}).items()})
        extra_text = " ".join([str(candidate.get("description") or ""), " ".join(candidate.get("sectors") or [])])
        ateco = str(candidate.get("ateco") or "")
    else:
        company = Company(name=str(getattr(candidate, "name", "") or ""), lat=None, lon=None)
    fields = getattr(company, "fields", None) or {}
    ateco = ateco or str((getattr(company, "ids", None) or {}).get("ateco") or fields.get("ateco") or "")
    if key_of is not None:
        key = str(key_of(candidate))
    else:
        key = str(getattr(candidate, "key", "") or "") or (company.site or "") or f"{company.name}#{index}"
    tags = company.osm_tags or {}
    parts = [company.name, extra_text, fields.get("industry", ""), fields.get("about", ""),
             fields.get("description", ""), str(tags.get("product", "")), str(tags.get("description", "")),
             fields.get("brand", ""), fields.get("operator", "")]
    return key, company, " ".join(p for p in parts if p)[:MAX_TEXT_CHARS], ateco


def _shown_direction(direction) -> str:
    """The direction's name in the interface language (shipped directions are translated)."""
    from ..i18n import t

    key = f"affinity.ui.direction.{direction.id}"
    shown = t(key)
    return direction.name if shown == key else shown


def _shown_type(ctype: str) -> str:
    from ..i18n import current_language, t

    key = f"affinity.type.{ctype}"
    shown = t(key)
    if shown == key or current_language() == "en":
        return TYPE_NAMES.get(ctype, ctype)
    return shown


# The discovery ranking writes its reasons in English for the log; the list shows them in
# the interface language (an unknown one is left out rather than shown in English).
_RANKING_REASONS = (
    (re.compile(r"^industrial site \((?P<what>[^)]*)\)$"), "background.screen.rank.industrial"),
    (re.compile(r"^has a website$"), "background.screen.rank.website"),
    (re.compile(r"^name: (?P<what>.+)$"), "background.screen.rank.name"),
    (re.compile(r"^map/name matches (?P<what>.+)$"), "background.screen.rank.map_name"),
    (re.compile(r"^name matches the CV: (?P<what>.+)$"), "background.screen.rank.name_cv"),
    (re.compile(r"^name matches an ATECO category: (?P<what>.+)$"), "background.screen.rank.name_ateco"),
    (re.compile(r"^established company \(Wikidata\)$"), "background.screen.rank.wikidata"),
    (re.compile(r"^large employer \((?P<what>\d+) employees\)$"), "background.screen.rank.employees"),
    (re.compile(r"^(?P<what>\d+) employees$"), "background.screen.rank.employees"),
    (re.compile(r"^register ATECO (?P<what>\S+) in your categories$"), "background.screen.rank.ateco"),
)


def ranking_reason_text(reason: str) -> str:
    """The discovery ranking's first reason in the interface language; "" when it has no
    translation (never an English phrase in an Italian list) or says nothing specific."""
    for pattern, key in _RANKING_REASONS:
        match = pattern.match(str(reason or "").strip())
        if match:
            return text(key, what=match.groupdict().get("what", ""))
    return ""


def screen_companies(candidates, cv_profile=None, preferences=None, *, sectors: list[str] | None = None,
                     ateco: list[str] | None = None,
                     key_of: Callable[[object], str] | None = None) -> list[ScreenResult]:
    """All candidates, best first, each with a cheap score, direction, type and one-line
    reason. `sectors` defaults to the profile's sectors. Never raises: a candidate that
    cannot be read keeps its place at the end with score 0."""
    from ..discovery.ranking import context_for, rank

    try:
        context = context_for(cv_profile, sectors if sectors is not None else list(
            getattr(cv_profile, "sectors", None) or []), ateco)
    except Exception:  # noqa: BLE001
        log.exception("Pre-screen: ranking context not built")
        context = None
    directions = _directions(preferences)
    table = _type_table()
    type_weight = getattr(preferences, "type_weight", None) if preferences is not None else None
    results: list[ScreenResult] = []
    for index, candidate in enumerate(candidates or []):
        try:
            results.append(_screen_one(candidate, index, key_of, context, rank, directions, table, type_weight))
        except Exception:  # noqa: BLE001 - one bad record never stops the screen
            log.debug("Pre-screen: candidate %d not readable", index, exc_info=True)
            results.append(ScreenResult(key=f"#{index}", name=str(getattr(candidate, "name", "") or ""),
                                        score=0.0, reason=text("background.screen.no_signal"), index=index))
    results.sort(key=lambda r: (-r.score, r.index))
    return results


def _screen_one(candidate, index, key_of, context, rank, directions, table, type_weight) -> ScreenResult:
    key, company, blob, ateco = _view(candidate, index, key_of)
    ranking = rank(company, context) if context is not None else None
    base = min(1.0, (ranking.score if ranking is not None else 0.0) / 1.5)
    norm = _normalize(blob)
    phrases = _phrases(norm)
    ctype, type_evidence = _type_of(norm, ateco, company.osm_tags or {}, table)
    best_dir, best_score, best_terms = None, base, []
    for direction in directions:
        hits = sorted((t for t in direction.terms if t in phrases), key=lambda t: (-len(t), t))
        fit = DIRECTION_TYPES.get(direction.id, {}).get(ctype, 0.5) if ctype else UNKNOWN_TYPE_FIT
        score = W_TERMS * min(1.0, 0.4 * len(hits)) + W_TYPE * fit + W_RANK * base
        if best_dir is None or score > best_score:
            best_dir, best_score, best_terms = direction, score, hits
    parts = []
    if best_dir is not None:
        # without a matching word or a known company type the direction is only a guess from
        # the name and the map tags (Yoroi, a cybersecurity company, showed "fits industrial
        # automation"): said so until the deep analysis has read the site
        direction_key = "background.screen.direction" if (best_terms or ctype) else "background.screen.direction_guess"
        parts.append(text(direction_key, direction=_shown_direction(best_dir)))
    if ctype:
        parts.append(text("background.screen.type", type=_shown_type(ctype)))
    if best_terms:
        parts.append(text("background.screen.terms", terms=", ".join(best_terms[:3])))
    if ranking is not None and ranking.reasons:
        shown = ranking_reason_text(ranking.reasons[0])
        if shown:
            parts.append(shown)
    score = best_score
    if ctype and callable(type_weight):
        weight = float(type_weight(ctype))
        if weight <= 0:
            score *= EXCLUDED_FACTOR
            parts.append(text("background.screen.type_excluded"))
        else:
            score *= weight
    reason = "; ".join(parts) or text("background.screen.no_signal")
    return ScreenResult(key=key, name=company.name, score=round(score, 4),
                        direction=best_dir.id if best_dir is not None else "",
                        direction_name=best_dir.name if best_dir is not None else "",
                        company_type=ctype, reason=reason[:200], index=index)
