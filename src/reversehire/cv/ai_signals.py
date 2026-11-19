"""AI-style signals in a CV: style cues that often appear in CVs written or
polished with a language model, each with the CV lines that show it.

LIMITS, stated honestly: detectors of "AI writing" are unreliable. They
misjudge texts by non-native writers, by people who follow CV templates or
career-service advice, and texts that were only proofread with a tool. This
module therefore never says that a CV "was written by AI": it lists style cues
(stock phrases, very uniform bullets, generic lines without names or numbers,
typographic habits, file metadata) so that the candidate can make the CV more
personal and concrete. `level` only summarises how many cues were found ("low",
"medium", "high"). The estimated probability shown in the recruiter view, always
with a confidence band and a caption, is computed from these signals in
`ai_estimate.py` (a replaceable backend).

Scoring (internal, 0..100): every signal has a strength 0..1 and a weight
(`WEIGHTS`); score = 100 * sum(weight * strength) / sum(weight of the signals
that could be computed). Signals that need an embedder or a file are left out
of the denominator when unavailable. `level` comes from `LEVEL_THRESHOLDS`.
Texts shorter than `MIN_WORDS` give `reliable=False` and level "low".
"""

from __future__ import annotations

import json
import re
import statistics
import zipfile
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

from .sections import DATA_FOLDER, detect_heading, normalize

# --- weights and thresholds ----------------------------------------------------

WEIGHTS = {
    "stock_phrases": 0.35,
    "uniformity": 0.15,
    "result_clauses": 0.12,
    "metric_endings": 0.05,
    "genericity": 0.15,
    "typography": 0.05,
    "embedding": 0.10,
    "metadata": 0.05,
}
LEVEL_THRESHOLDS = (25, 50)  # score < 25 low, < 50 medium, otherwise high
MIN_WORDS = 80
# stock phrases: hits per 100 words giving full strength
STOCK_FULL_PER_100 = 2.0
# uniformity: coefficient of variation of the bullet/sentence length
UNIFORM_CV_HIGH = 0.40  # at or above: no signal
UNIFORM_CV_LOW = 0.12  # at or below: full strength
UNIFORM_MIN_ITEMS = 6
UNIFORM_SHORT_MEAN = 8  # very short bullets (<8 words) are naturally uniform: halve
# result clauses and metric endings: share of bullets
CLAUSE_SHARE_START, CLAUSE_SHARE_FULL = 0.15, 0.50
METRIC_SHARE_START, METRIC_SHARE_FULL = 0.40, 0.80
METRIC_MIN_ITEMS = 5
# genericity: share of content lines without any concrete anchor
GENERIC_SHARE_START, GENERIC_SHARE_FULL = 0.35, 0.80
GENERIC_MIN_WORDS = 6
GENERIC_MIN_LINES = 4
# typography: number of tells giving full strength
TYPOGRAPHY_FULL = 6
# embedding: position inside the embedder's similarity range counting as "close"
EMBED_POSITION = 0.85
EMBED_SHARE_FULL = 0.40
EMBED_MAX_LINES = 120
# metadata strengths
METADATA_STRENGTH = {"ai_tool": 0.8, "cv_builder": 0.3}
MAX_EVIDENCE = 5
EVIDENCE_CHARS = 140

_KEY_PREFIX = "ai"


@dataclass
class Signal:
    key: str  # i18n key, e.g. "ai.signal.stock_phrases"
    strength: float  # 0..1
    weight: float
    params: dict = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    advice_key: str | None = None


@dataclass
class AISignals:
    level: str  # "low" | "medium" | "high"
    score: int  # 0..100, internal
    signals: list[Signal]
    words: int = 0
    reliable: bool = True
    notice_key: str = "ai.notice.limits"
    computed: int | None = None  # how many signals could be computed (None: not recorded)

    @property
    def level_key(self) -> str:
        return f"{_KEY_PREFIX}.level.{self.level}"

    def to_dict(self) -> dict:
        return asdict(self)


# --- helpers ---------------------------------------------------------------------

@lru_cache(maxsize=1)
def _data() -> dict:
    return json.loads((DATA_FOLDER / "ai_phrases.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _skill_terms() -> re.Pattern | None:
    try:
        data = json.loads((DATA_FOLDER / "skills.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    forms = set()
    for item in data.get("items", []):
        for f in [item.get("it"), item.get("en"), *item.get("variants", [])]:
            if f and len(f) >= 2:
                forms.add(normalize(f))
    return re.compile(r"(?<![\w])(?:" + "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
                      + r")(?![\w])")


@lru_cache(maxsize=4)
def _phrases_regex(name: str, language: str) -> re.Pattern | None:
    data = _data()[name]
    first = language if language in ("it", "en") else "en"
    other = "en" if first == "it" else "it"
    forms = {normalize(p) for p in data.get(first, []) + data.get(other, []) if p.strip()}
    if not forms:
        return None
    return re.compile(r"(?<![\w])(?:" + "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
                      + r")(?![\w])")


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _ramp(value: float, start: float, full: float) -> float:
    """0 at `start`, 1 at `full` (linear), clamped."""
    if full == start:
        return 1.0 if value >= full else 0.0
    return _clamp((value - start) / (full - start))


def _words_of(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", text)


def _short(line: str) -> str:
    line = re.sub(r"\s+", " ", line).strip()
    return line if len(line) <= EVIDENCE_CHARS else line[:EVIDENCE_CHARS - 1].rstrip() + "…"


def _evidence(lines) -> list[str]:
    result: list[str] = []
    for line in lines:
        s = _short(line)
        if s and s not in result:
            result.append(s)
        if len(result) >= MAX_EVIDENCE:
            break
    return result


_RE_BULLET = re.compile(r"^\s*(?:[-•*·▪◦●○■□➢►>»–—✓✔❖◆♦→]|\d+[.)]\s)\s*")
_RE_CONTACT = re.compile(r"@|https?://|www\.|\+?\d[\d\s./\-]{7,}\d")
_RE_DATE = re.compile(r"\b(?:19|20)\d{2}\b")


def _strip_bullet(line: str) -> str:
    return _RE_BULLET.sub("", line).strip()


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?;])\s+|\n", text)
    return [p.strip() for p in parts if len(_words_of(p)) >= 4]


def _signal(name: str, strength: float, params: dict | None = None, evidence=None,
            variant: str | None = None) -> Signal:
    key = f"{_KEY_PREFIX}.signal.{variant or name}"
    return Signal(key=key, strength=round(_clamp(strength), 3), weight=WEIGHTS[name],
                  params=dict(params or {}), evidence=_evidence(evidence or []),
                  advice_key=f"{key}.advice")


# --- signals ------------------------------------------------------------------------

def _stock_phrases(lines: list[str], words: int, language: str) -> Signal:
    regex = _phrases_regex("stock_phrases", language)
    hits: list[str] = []
    hit_lines: list[str] = []
    for line in lines:
        found = regex.findall(normalize(line)) if regex else []
        if found:
            hits.extend(found)
            hit_lines.append(line)
    per_100 = len(hits) * 100 / max(1, words)
    distinct = list(dict.fromkeys(hits))
    return _signal("stock_phrases", per_100 / STOCK_FULL_PER_100,
                   {"count": len(hits), "per_100": round(per_100, 1), "phrases": ", ".join(distinct[:8])},
                   hit_lines)


def _items(lines: list[str]) -> list[str]:
    """Bullets if there are enough, otherwise the sentences of the text."""
    bullets = [_strip_bullet(ln) for ln in lines if _RE_BULLET.match(ln)]
    bullets = [b for b in bullets if len(_words_of(b)) >= 3]
    if len(bullets) >= UNIFORM_MIN_ITEMS:
        return bullets
    return [s for s in _sentences("\n".join(ln for ln in lines if detect_heading(ln) is None))
            if not _RE_CONTACT.search(s)]


def _uniformity(items: list[str]) -> Signal | None:
    if len(items) < UNIFORM_MIN_ITEMS:
        return None
    lengths = [len(_words_of(i)) for i in items]
    mean = statistics.fmean(lengths)
    cv = statistics.pstdev(lengths) / mean if mean else 1.0
    strength = _ramp(UNIFORM_CV_HIGH - cv, 0.0, UNIFORM_CV_HIGH - UNIFORM_CV_LOW)
    if mean < UNIFORM_SHORT_MEAN:
        strength /= 2
    return _signal("uniformity", strength,
                   {"count": len(items), "mean_words": round(mean, 1), "variation": round(cv, 2)},
                   items[:3] if strength > 0 else [])


def _result_clauses(items: list[str], language: str) -> Signal | None:
    if len(items) < METRIC_MIN_ITEMS:
        return None
    regex = _phrases_regex("result_clauses", language)
    hits = []
    for item in items:
        norm = normalize(item)
        if regex is None:
            break
        # A clause after a comma (", resulting in ...", ", garantendo ...") or a
        # strong outcome connector anywhere.
        m = regex.search(norm)
        if m and (m.start() > 0 and norm[:m.start()].rstrip().endswith(",")
                  or m.group(0) in ("resulting in", "which resulted in", "con conseguente", "che ha portato")):
            hits.append(item)
    share = len(hits) / len(items)
    return _signal("result_clauses", _ramp(share, CLAUSE_SHARE_START, CLAUSE_SHARE_FULL),
                   {"count": len(hits), "total": len(items), "share": round(share * 100)}, hits)


_RE_METRIC = re.compile(r"\d+(?:[.,]\d+)?\s*(?:%|percent|per cento|x\b|k\b)|[€$£]\s?\d|\d\s?(?:€|eur|euro)\b|\b\d+\b",
                        re.IGNORECASE)


def _metric_endings(items: list[str]) -> Signal | None:
    if len(items) < METRIC_MIN_ITEMS:
        return None
    hits = []
    for item in items:
        tail = " ".join(item.split()[-6:])
        tail = _RE_DATE.sub(" ", tail)
        if _RE_METRIC.search(tail):
            hits.append(item)
    share = len(hits) / len(items)
    return _signal("metric_endings", _ramp(share, METRIC_SHARE_START, METRIC_SHARE_FULL),
                   {"count": len(hits), "total": len(items), "share": round(share * 100)}, hits)


def _is_concrete(line: str) -> bool:
    """True if the line has a name, number, acronym, tool, place or date."""
    if re.search(r"\d", line):
        return True
    words = line.split()
    for i, w in enumerate(words):
        core = w.strip("()[]{}.,;:!?\"'“”‘’«»")
        if not core:
            continue
        if re.fullmatch(r"[A-Z][A-Z0-9&+#./\-]{1,}", core):  # acronym: SQL, IVA, CNC
            return True
        if re.search(r"[a-z][A-Z]", core):  # CamelCase: TeamSystem, JavaScript
            return True
        if i > 0 and core[:1].isupper() and not words[i - 1].endswith((".", ":", "!", "?")):
            return True  # proper noun inside the sentence
    terms = _skill_terms()
    return bool(terms and terms.search(normalize(line)))


def _genericity(lines: list[str]) -> Signal | None:
    content = [_strip_bullet(ln) for ln in lines
               if len(_words_of(ln)) >= GENERIC_MIN_WORDS and detect_heading(ln) is None
               and not _RE_CONTACT.search(ln)]
    if len(content) < GENERIC_MIN_LINES:
        return None
    generic = [ln for ln in content if not _is_concrete(ln)]
    share = len(generic) / len(content)
    return _signal("genericity", _ramp(share, GENERIC_SHARE_START, GENERIC_SHARE_FULL),
                   {"count": len(generic), "total": len(content), "share": round(share * 100)}, generic)


_RE_EM_DASH = re.compile(r"[^\W\d_]\s?—\s?[^\W\d_]")
_RE_CURLY_DOUBLE = re.compile(r"[“”]")


def _typography(lines: list[str], language: str) -> Signal:
    dash_lines = [ln for ln in lines if _RE_EM_DASH.search(ln)]
    dashes = sum(len(_RE_EM_DASH.findall(ln)) for ln in dash_lines)
    quote_lines = [ln for ln in lines if _RE_CURLY_DOUBLE.search(ln)]
    quotes = sum(len(_RE_CURLY_DOUBLE.findall(ln)) for ln in quote_lines) // 2
    title_case = []
    if language == "it":
        for ln in lines:
            words = ln.split()
            long_words = [w for w in words if len(w) >= 4]
            if (2 <= len(words) <= 6 and len(long_words) >= 2 and not ln.isupper()
                    and all(w[:1].isupper() for w in long_words) and not re.search(r"[\d@|,]", ln)
                    and detect_heading(ln) is not None):
                title_case.append(ln)
    tells = dashes + quotes + len(title_case)
    return _signal("typography", tells / TYPOGRAPHY_FULL,
                   {"em_dashes": dashes, "curly_quotes": quotes, "title_case_headings": len(title_case)},
                   dash_lines + quote_lines + title_case)


def _embedding(lines: list[str], language: str, embedder) -> Signal | None:
    import numpy as np

    candidates = [_strip_bullet(ln) for ln in lines
                  if len(_words_of(ln)) >= 6 and detect_heading(ln) is None and not _RE_CONTACT.search(ln)]
    candidates = candidates[:EMBED_MAX_LINES]
    if len(candidates) < GENERIC_MIN_LINES:
        return None
    data = _data()["prototypes"]
    prototypes = data.get(language if language in data else "en", []) + data.get(
        "en" if language == "it" else "it", [])
    try:
        low, high = tuple(getattr(embedder, "similarity_range", (0.0, 1.0)))
        threshold = low + EMBED_POSITION * (high - low)
        a = np.asarray(embedder.embed(candidates, kind="passage"))
        b = np.asarray(embedder.embed(prototypes, kind="passage"))
        best = (a @ b.T).max(axis=1)
    except Exception:  # noqa: BLE001 - optional signal
        return None
    close = [candidates[i] for i in range(len(candidates)) if float(best[i]) >= threshold]
    share = len(close) / len(candidates)
    return _signal("embedding", share / EMBED_SHARE_FULL,
                   {"count": len(close), "total": len(candidates), "share": round(share * 100)}, close)


def document_producer(path: str | Path | None) -> str:
    """Producer/Creator of a PDF, Application of a DOCX ("" if unknown)."""
    if not path:
        return ""
    p = Path(path)
    try:
        if p.suffix.lower() == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(str(p))
            metadata = reader.metadata or {}
            values = [str(metadata.get(k) or "") for k in ("/Producer", "/Creator")]
            return " | ".join(v for v in values if v)
        if p.suffix.lower() == ".docx":
            with zipfile.ZipFile(p) as archive:
                app = archive.read("docProps/app.xml").decode("utf-8", "replace")
            m = re.search(r"<Application>([^<]*)</Application>", app)
            return m.group(1) if m else ""
    except Exception:  # noqa: BLE001 - metadata is optional
        return ""
    return ""


def _metadata(path) -> Signal | None:
    if not path or Path(path).suffix.lower() not in (".pdf", ".docx"):
        return None
    producer = document_producer(path)
    norm = normalize(producer)
    for kind in ("ai_tool", "cv_builder"):
        for name in _data()["producers"][kind]:
            if re.search(rf"(?<![\w]){re.escape(normalize(name))}(?![\w])", norm):
                return _signal("metadata", METADATA_STRENGTH[kind], {"producer": producer[:80]},
                               [producer], variant=f"metadata_{kind}")
    return _signal("metadata", 0.0, {"producer": producer[:80]}, variant="metadata_other")


# --- public API ---------------------------------------------------------------------

def analyse(text: str, language: str, path: str | Path | None = None, embedder=None) -> AISignals:
    """AI-style signals of a CV text (`language` "it" or "en").

    `path` (PDF/DOCX) adds the metadata signal; `embedder` (interface of
    `scoring.embedding.Embedder`) adds the similarity to prototypical AI-style
    sentences. Never a verdict: see the module docstring.
    """
    language = language if language in ("it", "en") else "en"
    lines = [ln.strip() for ln in (text or "").split("\n") if ln.strip()]
    words = len(_words_of(text or ""))
    items = _items(lines)
    computed: list[Signal] = [_stock_phrases(lines, words, language), _typography(lines, language)]
    for signal in (_uniformity(items), _result_clauses(items, language), _metric_endings(items),
                   _genericity(lines)):
        if signal is not None:
            computed.append(signal)
    if embedder is not None:
        signal = _embedding(lines, language, embedder)
        if signal is not None:
            computed.append(signal)
    signal = _metadata(path)
    if signal is not None:
        computed.append(signal)

    total_weight = sum(s.weight for s in computed)
    score = round(100 * sum(s.weight * s.strength for s in computed) / total_weight) if total_weight else 0
    reliable = words >= MIN_WORDS
    if not reliable:
        level = "low"
    elif score < LEVEL_THRESHOLDS[0]:
        level = "low"
    elif score < LEVEL_THRESHOLDS[1]:
        level = "medium"
    else:
        level = "high"
    signals = sorted((s for s in computed if s.strength > 0), key=lambda s: -s.weight * s.strength)
    return AISignals(level=level, score=score, signals=signals, words=words, reliable=reliable,
                     computed=len(computed))


def message_keys() -> dict[str, set[str]]:
    """Every i18n key this module can emit, with the parameter names it fills."""
    return dict(_MESSAGE_KEYS)


_MESSAGE_KEYS: dict[str, set[str]] = {
    "ai.signal.stock_phrases": {"count", "per_100", "phrases"},
    "ai.signal.uniformity": {"count", "mean_words", "variation"},
    "ai.signal.result_clauses": {"count", "total", "share"},
    "ai.signal.metric_endings": {"count", "total", "share"},
    "ai.signal.genericity": {"count", "total", "share"},
    "ai.signal.typography": {"em_dashes", "curly_quotes", "title_case_headings"},
    "ai.signal.embedding": {"count", "total", "share"},
    "ai.signal.metadata_ai_tool": {"producer"},
    "ai.signal.metadata_cv_builder": {"producer"},
}

# Key -> placeholders the GUI must fill.
UI_KEYS: dict[str, set[str]] = {
    "ai.title": set(),
    "ai.notice.limits": set(),
    "ai.notice.unreliable": {"words", "minimum"},
    "ai.level.low": set(),
    "ai.level.medium": set(),
    "ai.level.high": set(),
    "ai.none": set(),
    "ai.evidence": set(),
    "ai.strength": {"strength"},
}
