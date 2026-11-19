"""Simulator profiles (`SystemProfile`), loaded from `data/profiles.json`.

The profiles are data, so they can be corrected without changing code; every
value is checked by `validate_profile` when loaded (a bad file raises
`ProfileError` naming the problem). See fonti/RECRUITER_STUDY.md §1.9.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import texts
from .model import Message, msg

DATA_FILE = Path(__file__).with_name("data") / "profiles.json"
SCHEMA_VERSION = 1
BASELINE_KEY = "baseline_literal"

READING_ORDERS = ("linear", "layout")
HEADER_FOOTER = ("drop", "keep")
IMAGES = ("lost", "ocr")
TABLES = ("flatten", "lost")
TEXT_BOXES = ("lost", "keep")
DATE_PRECISIONS = ("month_required", "year_ok")
HEADINGS = ("strict", "fuzzy")
KEYWORD_MODES = ("literal", "stem", "taxonomy", "semantic")
SCORE_KINDS = ("none", "percent", "grade_AD", "high_med_low", "criteria_met", "rating_points")
SCORE_METHODS = ("keyword_share", "taxonomy_match", "semantic")
KNOCKOUT_MODES = ("none", "manual_review", "auto_disqualify", "scored")
CONFIDENCE_LEVELS = ("low", "medium", "high")
PROFILE_KINDS = ("ats", "parser", "agency", "none", "baseline")
RECORD_FIELDS = ("name", "email", "phone", "town", "title", "employer", "dates", "degree",
                 "languages", "skills", "file", "area")
SEARCH_FIELDS = ("full_text", "title", "skills")
EVIDENCE_TAGS = ("V", "D", "M", "S")


class ProfileError(ValueError):
    """The profiles file is missing a value or has a value outside its enumeration."""


@dataclass(frozen=True)
class ParserSettings:
    reading_order: str = "linear"
    header_footer: str = "drop"
    images: str = "lost"
    tables: str = "flatten"
    text_boxes: str = "lost"
    date_precision: str = "month_required"
    headings: str = "strict"
    file_types: tuple[str, ...] = ()
    max_pages: int | None = None


@dataclass(frozen=True)
class SearchSettings:
    keyword_mode: str = "literal"
    boolean: bool | None = True  # None = not documented (both results are shown)
    fields_searched: tuple[str, ...] = ("full_text",)


@dataclass(frozen=True)
class ScoreSettings:
    kind: str = "percent"
    method: str = "keyword_share"
    recency: bool = False
    months_per_skill: bool = False
    weights: dict | None = None  # overrides of the default component weights


@dataclass(frozen=True)
class KnockoutSettings:
    mode: str = "manual_review"
    cap_on_fail: int | None = 20
    screening_weight: float | None = None  # "scored": share of the screening answers


@dataclass(frozen=True)
class Source:
    title: str
    url: str | None
    evidence: str


@dataclass(frozen=True)
class SystemProfile:
    """Parameters of one simulated screening system."""

    key: str
    name: str
    kind: str
    confidence: str
    confidence_note: Message
    parser: ParserSettings
    search: SearchSettings
    score: ScoreSettings
    knockout: KnockoutSettings
    record_fields: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()
    dedup: bool = False
    blind: bool = False
    auto_advance: bool = False
    form_fields: tuple[str, ...] = ()
    retention: Message | None = None
    notes: tuple[Message, ...] = ()
    sources: tuple[Source, ...] = ()
    reviewed: str = ""

    @property
    def approximation(self) -> Message:
        """The label every simulator carries (study §1.9)."""
        return msg("rsim.approximation", name=self.name, reviewed=self.reviewed)


# --- validation ---------------------------------------------------------------------


def _text_entry(value, where: str, errors: list[str]) -> None:
    if not (isinstance(value, dict) and isinstance(value.get("key"), str) and value["key"]
            and isinstance(value.get("en"), str) and value["en"].strip()):
        errors.append(f"{where}: expected {{\"key\": str, \"en\": str}}")


def _enum(value, allowed, where: str, errors: list[str]) -> None:
    if value not in allowed:
        errors.append(f"{where}: {value!r} not in {list(allowed)}")


def validate_profile(d: dict) -> list[str]:
    """Problems of one profile entry (empty list = valid)."""
    errors: list[str] = []
    if not isinstance(d, dict):
        return ["profile: expected an object"]
    key = d.get("key")
    where = f"profile {key!r}"
    for name in ("key", "name"):
        if not isinstance(d.get(name), str) or not d.get(name, "").strip():
            errors.append(f"{where}: missing '{name}'")
    _enum(d.get("kind"), PROFILE_KINDS, f"{where}.kind", errors)
    _enum(d.get("confidence"), CONFIDENCE_LEVELS, f"{where}.confidence", errors)
    _text_entry(d.get("confidence_note"), f"{where}.confidence_note", errors)
    parser = d.get("parser")
    if not isinstance(parser, dict):
        errors.append(f"{where}.parser: missing")
    else:
        for name, allowed in (("reading_order", READING_ORDERS), ("header_footer", HEADER_FOOTER),
                              ("images", IMAGES), ("tables", TABLES), ("text_boxes", TEXT_BOXES),
                              ("date_precision", DATE_PRECISIONS), ("headings", HEADINGS)):
            _enum(parser.get(name), allowed, f"{where}.parser.{name}", errors)
        types = parser.get("file_types")
        if not (isinstance(types, list) and types and all(isinstance(t, str) and t for t in types)):
            errors.append(f"{where}.parser.file_types: expected a non-empty list of extensions")
        pages = parser.get("max_pages")
        if pages is not None and not (isinstance(pages, int) and pages > 0):
            errors.append(f"{where}.parser.max_pages: expected a positive integer or null")
    fields = d.get("record_fields")
    if not isinstance(fields, list):
        errors.append(f"{where}.record_fields: expected a list")
    else:
        for f in fields:
            _enum(f, RECORD_FIELDS, f"{where}.record_fields", errors)
    search = d.get("search")
    if not isinstance(search, dict):
        errors.append(f"{where}.search: missing")
    else:
        _enum(search.get("keyword_mode"), KEYWORD_MODES, f"{where}.search.keyword_mode", errors)
        if search.get("boolean") not in (True, False, None):
            errors.append(f"{where}.search.boolean: expected true, false or null")
        searched = search.get("fields_searched")
        if not (isinstance(searched, list) and searched):
            errors.append(f"{where}.search.fields_searched: expected a non-empty list")
        else:
            for f in searched:
                _enum(f, SEARCH_FIELDS, f"{where}.search.fields_searched", errors)
    score = d.get("score")
    if not isinstance(score, dict):
        errors.append(f"{where}.score: missing")
    else:
        _enum(score.get("kind"), SCORE_KINDS, f"{where}.score.kind", errors)
        _enum(score.get("method"), SCORE_METHODS, f"{where}.score.method", errors)
        for flag in ("recency", "months_per_skill"):
            if not isinstance(score.get(flag), bool):
                errors.append(f"{where}.score.{flag}: expected true or false")
        weights = score.get("weights")
        if weights is not None:
            from .model import COMPONENTS

            if not isinstance(weights, dict) or not weights:
                errors.append(f"{where}.score.weights: expected an object or null")
            else:
                for name, w in weights.items():
                    _enum(name, COMPONENTS, f"{where}.score.weights", errors)
                    if not isinstance(w, (int, float)) or w < 0:
                        errors.append(f"{where}.score.weights.{name}: expected a number >= 0")
                if sum(w for w in weights.values() if isinstance(w, (int, float))) <= 0:
                    errors.append(f"{where}.score.weights: the weights sum to zero")
    knockout = d.get("knockout")
    if not isinstance(knockout, dict):
        errors.append(f"{where}.knockout: missing")
    else:
        _enum(knockout.get("mode"), KNOCKOUT_MODES, f"{where}.knockout.mode", errors)
        cap = knockout.get("cap_on_fail")
        if cap is not None and not (isinstance(cap, int) and 0 <= cap <= 100):
            errors.append(f"{where}.knockout.cap_on_fail: expected 0..100 or null")
        share = knockout.get("screening_weight")
        if knockout.get("mode") == "scored":
            if not (isinstance(share, (int, float)) and 0 < share < 1):
                errors.append(f"{where}.knockout.screening_weight: 'scored' needs a value in (0, 1)")
        elif share is not None:
            errors.append(f"{where}.knockout.screening_weight: only for mode 'scored'")
    for flag in ("dedup", "blind", "auto_advance"):
        if not isinstance(d.get(flag), bool):
            errors.append(f"{where}.{flag}: expected true or false")
    for name in ("platforms", "hosts", "form_fields"):
        value = d.get(name)
        if not (isinstance(value, list) and all(isinstance(v, str) and v for v in value)):
            errors.append(f"{where}.{name}: expected a list of strings")
    _text_entry(d.get("retention"), f"{where}.retention", errors)
    notes = d.get("notes")
    if not isinstance(notes, list):
        errors.append(f"{where}.notes: expected a list")
    else:
        for i, note in enumerate(notes):
            _text_entry(note, f"{where}.notes[{i}]", errors)
    sources = d.get("sources")
    if not (isinstance(sources, list) and sources):
        errors.append(f"{where}.sources: at least one source is required")
    else:
        for i, s in enumerate(sources):
            if not isinstance(s, dict) or not isinstance(s.get("title"), str) or not s["title"].strip():
                errors.append(f"{where}.sources[{i}]: missing 'title'")
                continue
            url = s.get("url")
            if url is not None and not (isinstance(url, str) and url.startswith("https://")):
                errors.append(f"{where}.sources[{i}].url: expected an https URL or null")
            _enum(s.get("evidence"), EVIDENCE_TAGS, f"{where}.sources[{i}].evidence", errors)
    return errors


def validate_document(data: dict) -> list[str]:
    """Problems of a whole profiles document."""
    if not isinstance(data, dict):
        return ["document: expected an object"]
    errors: list[str] = []
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version: expected {SCHEMA_VERSION}")
    if not isinstance(data.get("reviewed"), str) or not data.get("reviewed"):
        errors.append("reviewed: missing date")
    profiles = data.get("profiles")
    if not (isinstance(profiles, list) and profiles):
        return errors + ["profiles: expected a non-empty list"]
    seen: set[str] = set()
    for p in profiles:
        errors.extend(validate_profile(p))
        key = p.get("key") if isinstance(p, dict) else None
        if key in seen:
            errors.append(f"profile {key!r}: duplicate key")
        seen.add(key)
    if BASELINE_KEY not in seen:
        errors.append(f"profiles: the '{BASELINE_KEY}' profile is required (fallback)")
    return errors


# --- loading ------------------------------------------------------------------------


def _message(entry: dict) -> Message:
    texts.register_default(entry["key"], entry["en"])
    return Message(entry["key"], {})


def profile_from_dict(d: dict, reviewed: str = "") -> SystemProfile:
    """A validated `SystemProfile`; ProfileError if the entry is invalid."""
    errors = validate_profile(d)
    if errors:
        raise ProfileError("; ".join(errors))
    p, s, sc, k = d["parser"], d["search"], d["score"], d["knockout"]
    return SystemProfile(
        key=d["key"], name=d["name"], kind=d["kind"], confidence=d["confidence"],
        confidence_note=_message(d["confidence_note"]),
        parser=ParserSettings(p["reading_order"], p["header_footer"], p["images"], p["tables"],
                              p["text_boxes"], p["date_precision"], p["headings"],
                              tuple(t.lower().lstrip(".") for t in p["file_types"]), p.get("max_pages")),
        search=SearchSettings(s["keyword_mode"], s.get("boolean"), tuple(s["fields_searched"])),
        score=ScoreSettings(sc["kind"], sc["method"], sc["recency"], sc["months_per_skill"],
                            dict(sc["weights"]) if sc.get("weights") else None),
        knockout=KnockoutSettings(k["mode"], k.get("cap_on_fail"), k.get("screening_weight")),
        record_fields=tuple(d["record_fields"]), platforms=tuple(d["platforms"]),
        hosts=tuple(d["hosts"]), dedup=d["dedup"], blind=d["blind"], auto_advance=d["auto_advance"],
        form_fields=tuple(d["form_fields"]), retention=_message(d["retention"]),
        notes=tuple(_message(n) for n in d["notes"]),
        sources=tuple(Source(x["title"], x.get("url"), x["evidence"]) for x in d["sources"]),
        reviewed=reviewed,
    )


def load_profiles_from(path: str | Path) -> dict[str, SystemProfile]:
    """Profiles of a JSON file, by key, in file order; ProfileError if invalid."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ProfileError(f"cannot read {path}: {e}") from e
    errors = validate_document(data)
    if errors:
        raise ProfileError("; ".join(errors))
    reviewed = data["reviewed"]
    return {p["key"]: profile_from_dict(p, reviewed) for p in data["profiles"]}


@lru_cache(maxsize=1)
def _bundled() -> dict[str, SystemProfile]:
    return load_profiles_from(DATA_FILE)


def load_profiles() -> dict[str, SystemProfile]:
    """The bundled profiles by key (Allibo first, Baseline last)."""
    return dict(_bundled())


def get_profile(key: str) -> SystemProfile:
    """The profile with that key; KeyError if unknown."""
    return _bundled()[key]


def baseline() -> SystemProfile:
    return _bundled()[BASELINE_KEY]


def profile_for_platform(platform: str | None) -> SystemProfile:
    """The profile of an ATS platform key (`web.ats` / `web.screening` keys such as
    "workday", "allibo"); the Baseline profile when the platform is unknown."""
    if platform:
        p = platform.lower()
        for profile in _bundled().values():
            if p in profile.platforms or p == profile.key:
                return profile
    return baseline()


def profile_for_host(host: str | None) -> SystemProfile:
    """The profile whose hosts include the given host (or a parent domain of it)."""
    h = (host or "").lower().rstrip(".")
    if h:
        for profile in _bundled().values():
            if any(h == x or h.endswith("." + x) for x in profile.hosts):
                return profile
    return baseline()
