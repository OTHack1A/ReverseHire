"""The shipped knowledge tables of the affinity engine (``data/knowledge.json``).

- product rules: words on the company's site or product list -> skills and roles the
  company PLAUSIBLY needs ("packaging machines -> PLC programming, motion control,
  HMI"), each with a low weight; the engine labels them "inferred" and never treats
  them as requirements;
- company types (machine builder, system integrator, manufacturer with its own
  automation/maintenance/IT staff, software house / IT consultancy): word patterns,
  ATECO prefixes and OSM tags with a weight each; a type is assigned when its
  summed weight reaches ``type_threshold`` and every contribution is quoted;
- type needs: roles and skills a company of each type plausibly hires for;
- sector adjacency: symmetric 0..1 closeness of two sector keys of ``geo/sectors.py``.

Patterns are matched on ``normalize_text`` output at the start of a word; patterns
of four characters or fewer must be whole words ("mes" never matches "messa").
Pure functions; the JSON is read once.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from ..scoring.relevance import normalize_text

DATA = Path(__file__).parent / "data"
KNOWLEDGE_FILE = DATA / "knowledge.json"
DIRECTIONS_FILE = DATA / "directions.json"

COMPANY_TYPES = ("machine_builder", "system_integrator", "manufacturer", "software_house")
OTHER_TYPE = "other"
SHORT_PATTERN = 4


@lru_cache(maxsize=1)
def knowledge() -> dict:
    return json.loads(KNOWLEDGE_FILE.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def default_directions_data() -> list[dict]:
    return list(json.loads(DIRECTIONS_FILE.read_text(encoding="utf-8"))["directions"])


@lru_cache(maxsize=4096)
def pattern_regex(pattern: str) -> re.Pattern:
    norm = normalize_text(pattern)
    tail = r"(?![a-z0-9])" if len(norm) <= SHORT_PATTERN else ""
    return re.compile(r"(?<![a-z0-9])" + re.escape(norm) + tail)


def find_pattern(pattern: str, normalized: str) -> re.Match | None:
    return pattern_regex(pattern).search(normalized)


# --- text with provenance -----------------------------------------------------------


@dataclass(frozen=True)
class SourceText:
    """A piece of company-side text with where it comes from."""

    text: str
    source: str  # URL, or "card:<field>" / "osm" for card fields
    field: str  # "products", "presentation", "posting", "site", "name", "sector", ...


def sentence_around(text: str, start: int, end: int, limit: int = 220) -> str:
    """The sentence of ``text`` containing [start, end) (positions in ``text``)."""
    left = max(text.rfind(c, 0, start) for c in ".!?\n;") + 1
    rights = [i for i in (text.find(c, end) for c in ".!?\n;") if i >= 0]
    right = min(rights) + 1 if rights else len(text)
    sentence = " ".join(text[left:right].split())
    if len(sentence) > limit:
        sentence = sentence[: limit - 1].rstrip() + "…"
    return sentence


def quote_for(text: str, pattern: str) -> str:
    """The sentence of the raw ``text`` that contains ``pattern`` (fallback: the start)."""
    norm = normalize_text(text)
    m = find_pattern(pattern, norm)
    if m is None:
        return " ".join(text.split())[:220]
    # map the hit back on the raw text by its first word
    first = normalize_text(pattern).split()[0]
    raw = re.search(r"(?<![a-z0-9])" + re.escape(first), _fold(text))
    if raw is None:
        return " ".join(text.split())[:220]
    return sentence_around(text, raw.start(), raw.end())


def _fold(text: str) -> str:
    from ..scoring.relevance import unaccented

    return unaccented(text).lower()


# --- product rules ------------------------------------------------------------------


@dataclass
class InferredNeed:
    rule: str  # rule id or "type:<type>"
    term: str  # skill name, or role title
    weight: float
    is_role: bool
    evidence: SourceText | None = None
    pattern: str = ""


def product_needs(texts: list[SourceText]) -> list[InferredNeed]:
    """Skills and roles the company plausibly needs from its products/services words."""
    result: list[InferredNeed] = []
    for rule in knowledge()["product_rules"]:
        hit = None
        for st in texts:
            norm = normalize_text(st.text)
            for pattern in rule["patterns"]:
                if find_pattern(pattern, norm):
                    hit = (st, pattern)
                    break
            if hit:
                break
        if hit is None:
            continue
        st, pattern = hit
        quoted = SourceText(quote_for(st.text, pattern), st.source, st.field)
        for term, weight in rule["skills"]:
            result.append(InferredNeed(rule["id"], term, float(weight), False, quoted, pattern))
        for role in rule.get("roles", []):
            result.append(InferredNeed(rule["id"], role, 1.0, True, quoted, pattern))
    return result


# --- company types ------------------------------------------------------------------


@dataclass
class TypeEvidence:
    kind: str  # "text" | "ateco" | "osm"
    detail: str  # pattern, code or tag
    weight: float
    evidence: SourceText


@dataclass
class CompanyType:
    key: str
    score: float
    confidence: str  # "low" | "medium" | "high"
    evidence: list[TypeEvidence] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"key": self.key, "score": round(self.score, 3), "confidence": self.confidence,
                "evidence": [{"kind": e.kind, "detail": e.detail, "weight": e.weight, "text": e.evidence.text,
                              "source": e.evidence.source, "field": e.evidence.field} for e in self.evidence]}

    @classmethod
    def from_dict(cls, d: dict) -> "CompanyType":
        return cls(key=d["key"], score=float(d.get("score", 0.0)), confidence=d.get("confidence", "low"),
                   evidence=[TypeEvidence(e.get("kind", ""), e.get("detail", ""), float(e.get("weight", 0.0)),
                                          SourceText(e.get("text", ""), e.get("source", ""), e.get("field", "")))
                             for e in d.get("evidence", [])])


def _ateco_division(code: str) -> int | None:
    m = re.match(r"\s*(\d{2})", code or "")
    return int(m.group(1)) if m else None


def _ateco_prefix(code: str, prefix: str) -> bool:
    code = (code or "").strip()
    return code == prefix or code.startswith(prefix + ".") or (
        len(prefix) > 2 and code.startswith(prefix))


def classify_company(texts: list[SourceText], ateco: str = "", ateco_source: str = "card:ateco",
                     osm_tags: dict | None = None) -> list[CompanyType]:
    """Company types with score, confidence and evidence, strongest first (may be empty).

    Confidence: "high" when at least two kinds of evidence agree (e.g. register ATECO
    and site text), "medium" with one strong kind (score >= 1.5 or the ATECO), else "low".
    """
    table = knowledge()["company_types"]
    threshold = float(knowledge().get("type_threshold", 1.0))
    found: list[CompanyType] = []
    for key in COMPANY_TYPES:
        rules = table.get(key) or {}
        evidence: list[TypeEvidence] = []
        for pattern, weight in rules.get("patterns", []):
            for st in texts:
                if find_pattern(pattern, normalize_text(st.text)):
                    evidence.append(TypeEvidence("text", pattern, float(weight),
                                                 SourceText(quote_for(st.text, pattern), st.source, st.field)))
                    break
        if ateco:
            ateco_hit = None
            for prefix, weight in rules.get("ateco_prefixes", []):
                if _ateco_prefix(ateco, prefix):
                    ateco_hit = (prefix, float(weight))
                    break
            division = _ateco_division(ateco)
            for low, high, weight in rules.get("ateco_divisions", []):
                if division is not None and low <= division <= high and (ateco_hit is None or weight > ateco_hit[1]):
                    ateco_hit = (f"{low:02d}-{high:02d}", float(weight))
            if ateco_hit:
                evidence.append(TypeEvidence("ateco", ateco_hit[0], ateco_hit[1],
                                             SourceText(f"ATECO {ateco}", ateco_source, "ateco")))
        for tag_key, tag_value, weight in rules.get("osm", []):
            present = (osm_tags or {}).get(tag_key)
            if present and (tag_value == "*" or present == tag_value):
                evidence.append(TypeEvidence("osm", f"{tag_key}={present}", float(weight),
                                             SourceText(f"{tag_key}={present}", "osm", "osm_tags")))
        # text patterns saturate: many synonyms on one page are one piece of evidence
        text_score = min(1.5, sum(e.weight for e in evidence if e.kind == "text"))
        score = text_score + sum(e.weight for e in evidence if e.kind != "text")
        if score + 1e-9 < threshold:
            continue
        kinds = {e.kind for e in evidence}
        if len(kinds) >= 2 and score >= threshold + 0.5:
            confidence = "high"
        elif score >= 1.5 or "ateco" in kinds:
            confidence = "medium"
        else:
            confidence = "low"
        found.append(CompanyType(key, round(score, 3), confidence, evidence))
    found.sort(key=lambda t: (-t.score, COMPANY_TYPES.index(t.key)))
    return found


def type_needs(type_key: str) -> tuple[list[str], list[tuple[str, float]]]:
    needs = knowledge()["type_needs"].get(type_key) or {}
    return list(needs.get("roles", [])), [(t, float(w)) for t, w in needs.get("skills", [])]


# --- sector adjacency -----------------------------------------------------------------


@lru_cache(maxsize=1)
def _adjacency() -> dict[tuple[str, str], float]:
    table: dict[tuple[str, str], float] = {}
    for a, b, value in knowledge()["sector_adjacency"]:
        table[(a, b)] = table[(b, a)] = float(value)
    return table


def sector_closeness(a: str, b: str) -> float:
    """1.0 for the same sector, the table value for adjacent ones, else 0."""
    if a == b:
        return 1.0
    return _adjacency().get((a, b), 0.0)
