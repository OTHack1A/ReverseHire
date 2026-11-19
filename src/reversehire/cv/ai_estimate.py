"""Estimated probability that a CV text was generated with AI, with a confidence band.

READ THIS FIRST: this is an ESTIMATE FROM WRITING SIGNALS, NOT PROOF. The default
backend maps the style cues of `ai_signals` (stock phrases, uniform bullets,
generic lines, typography, file metadata, similarity to template sentences) to a
number. Those cues are not calibrated against a labelled set of CVs; texts by
non-native writers, translated texts, CV templates and career-service phrasing
raise them as well (false positives). The interface therefore always shows the
band and a fixed caption saying so, and the mapping never goes above
`PROBABILITY_CEILING` nor below `PROBABILITY_FLOOR`.

The interface is meant to be replaced by a better model later:

    estimate_ai_probability(text, language) -> AiEstimate(probability, low, high, signals)

A backend is any object with a `name` and an `estimate(text, language, *, path=None,
embedder=None, signals=None) -> AiEstimate` method; `set_backend` installs one for
the whole program (`reset_backend` goes back to the default). Every backend
returns probabilities in 0..1 with low <= probability <= high, and the driving
signals (`DrivingSignal`) with the sentences concerned and one rewrite suggestion
per sentence (i18n key and parameters).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Protocol

from .ai_signals import AISignals, Signal, analyse
from .sections import normalize

# --- the mapping from the signal score to a probability (signals backend) ------------------
#
# score: ai_signals' weighted score, 0..100 (see its module docstring).
# probability = FLOOR + (CEILING - FLOOR) * logistic((score - MIDPOINT) / SLOPE)
# A score of MIDPOINT gives the middle of the range; the logistic keeps a few
# strong cues from jumping to certainty.
PROBABILITY_FLOOR = 0.03
PROBABILITY_CEILING = 0.85
LOGISTIC_MIDPOINT = 45.0
LOGISTIC_SLOPE = 10.0
# Half-width of the confidence band: a base, widened when less is known.
BAND_BASE = 0.15
BAND_SHORT_TEXT = 0.12       # fewer words than ai_signals.MIN_WORDS
BAND_FEW_SIGNALS = 0.05      # fewer than BAND_FEW_SIGNALS_COUNT signals could be computed
BAND_FEW_SIGNALS_COUNT = 5
BAND_NOT_ENGLISH = 0.05      # the phrase lists and prototypes are best in English
TOP_SIGNALS = 3
SENTENCES_PER_SIGNAL = 3

SIGNALS_BACKEND = "writing-signals-v1"
CAPTION_KEY = "ai.estimate.caption"   # the fixed caption shown with the bar


@dataclass(frozen=True)
class SentenceAdvice:
    """One sentence of the CV that shows a signal, and how to rewrite it."""

    text: str
    suggestion_key: str
    params: dict = field(default_factory=dict)


@dataclass
class DrivingSignal:
    """A signal behind the estimate: its message (i18n key and parameters), its share
    of the estimate (0..1) and the sentences concerned."""

    key: str
    share: float
    params: dict = field(default_factory=dict)
    sentences: list[SentenceAdvice] = field(default_factory=list)


@dataclass
class AiEstimate:
    probability: float   # 0..1
    low: float           # lower edge of the confidence band, 0..1
    high: float          # upper edge, 0..1
    signals: list[DrivingSignal] = field(default_factory=list)
    reliable: bool = True    # False: too little text to say much
    words: int = 0
    backend: str = SIGNALS_BACKEND
    caption_key: str = CAPTION_KEY

    @property
    def percent(self) -> int:
        return round(self.probability * 100)

    @property
    def band_percent(self) -> tuple[int, int]:
        return round(self.low * 100), round(self.high * 100)


class AiProbabilityBackend(Protocol):
    name: str

    def estimate(self, text: str, language: str, *, path=None, embedder=None,
                 signals: AISignals | None = None) -> AiEstimate: ...


def probability_from_signals(signals: AISignals) -> tuple[float, float, float]:
    """THE mapping of the signals backend: (probability, low, high), all in 0..1.

    Monotonic in the signal score; never below PROBABILITY_FLOOR nor above
    PROBABILITY_CEILING; the band is BAND_BASE wide on each side, plus
    BAND_SHORT_TEXT for a short text and BAND_FEW_SIGNALS when few signals could
    be computed (no embedder, no file). Replace the whole backend, not this
    function, to plug in a trained model."""
    score = max(0.0, min(100.0, float(signals.score)))
    logistic = 1.0 / (1.0 + math.exp(-(score - LOGISTIC_MIDPOINT) / LOGISTIC_SLOPE))
    probability = PROBABILITY_FLOOR + (PROBABILITY_CEILING - PROBABILITY_FLOOR) * logistic
    half = BAND_BASE
    if not signals.reliable:
        half += BAND_SHORT_TEXT
    computed = getattr(signals, "computed", None)
    if computed is not None and computed < BAND_FEW_SIGNALS_COUNT:
        half += BAND_FEW_SIGNALS
    low = max(0.0, probability - half)
    high = min(1.0, probability + half)
    return round(probability, 3), round(low, 3), round(high, 3)


# --- rewrite suggestions -------------------------------------------------------------------

def _signal_name(key: str) -> str:
    """"ai.signal.metadata_ai_tool" -> "metadata"; "ai.signal.genericity" -> "genericity"."""
    name = key.rsplit(".", 1)[-1]
    return "metadata" if name.startswith("metadata") else name


def _phrases_in(sentence: str, signal: Signal) -> list[str]:
    phrases = [p.strip() for p in str(signal.params.get("phrases", "")).split(",") if p.strip()]
    norm = normalize(sentence)
    return [p for p in phrases if re.search(rf"(?<![\w]){re.escape(normalize(p))}(?![\w])", norm)]


def sentence_advice(signal: Signal, sentence: str) -> SentenceAdvice:
    """The rewrite suggestion for one sentence showing `signal`."""
    name = _signal_name(signal.key)
    params: dict = {}
    if name == "stock_phrases":
        found = _phrases_in(sentence, signal)
        if found:
            params["phrase"] = found[0]
        else:
            name = "stock_phrases_generic"
    elif name == "metadata":
        params["producer"] = str(signal.params.get("producer", "") or sentence)[:80]
    return SentenceAdvice(sentence, f"ai.rewrite.{name}", params)


def driving_signals(signals: AISignals, top: int = TOP_SIGNALS,
                    per_signal: int = SENTENCES_PER_SIGNAL) -> list[DrivingSignal]:
    """The strongest signals (by weight x strength) with their share of the total and
    the sentences concerned, each with its rewrite suggestion."""
    present = [s for s in signals.signals if s.strength > 0]
    total = sum(s.weight * s.strength for s in present)
    if total <= 0:
        return []
    ordered = sorted(present, key=lambda s: -s.weight * s.strength)[:top]
    return [DrivingSignal(key=s.key, share=round(s.weight * s.strength / total, 3), params=dict(s.params),
                          sentences=[sentence_advice(s, line) for line in s.evidence[:per_signal]])
            for s in ordered]


# --- backends ------------------------------------------------------------------------------

class SignalsBackend:
    """Default backend: the writing signals of `ai_signals`, mapped by
    `probability_from_signals`."""

    name = SIGNALS_BACKEND

    def estimate(self, text: str, language: str, *, path=None, embedder=None,
                 signals: AISignals | None = None) -> AiEstimate:
        if signals is None:
            signals = analyse(text, language, path, embedder=embedder)
        probability, low, high = probability_from_signals(signals)
        if language not in ("en",):
            low = round(max(0.0, low - BAND_NOT_ENGLISH), 3)
            high = round(min(1.0, high + BAND_NOT_ENGLISH), 3)
        return AiEstimate(probability, low, high, driving_signals(signals), reliable=signals.reliable,
                          words=signals.words, backend=self.name)


_DEFAULT = SignalsBackend()
_backend: AiProbabilityBackend = _DEFAULT


def set_backend(backend: AiProbabilityBackend) -> None:
    """Installs another estimator (for example a trained model) for the whole program."""
    global _backend
    _backend = backend


def reset_backend() -> None:
    set_backend(_DEFAULT)


def current_backend() -> AiProbabilityBackend:
    return _backend


def _checked(estimate: AiEstimate) -> AiEstimate:
    """Keeps every backend inside the contract: 0 <= low <= probability <= high <= 1."""
    p = max(0.0, min(1.0, float(estimate.probability)))
    estimate.probability = p
    estimate.low = max(0.0, min(p, float(estimate.low)))
    estimate.high = min(1.0, max(p, float(estimate.high)))
    return estimate


def estimate_ai_probability(text: str, language: str, *, path=None, embedder=None,
                            signals: AISignals | None = None) -> AiEstimate:
    """Probability (0..1) that `text` was generated with AI, with a confidence band and
    the signals behind it; computed by the installed backend. `signals`: already
    computed ai_signals (the default backend reuses them)."""
    return _checked(_backend.estimate(text, language, path=path, embedder=embedder, signals=signals))


def message_keys() -> dict[str, set[str]]:
    """Every i18n key this module can emit, with the parameter names it fills."""
    return {
        CAPTION_KEY: set(),
        "ai.rewrite.stock_phrases": {"phrase"},
        "ai.rewrite.stock_phrases_generic": set(),
        "ai.rewrite.uniformity": set(),
        "ai.rewrite.result_clauses": set(),
        "ai.rewrite.metric_endings": set(),
        "ai.rewrite.genericity": set(),
        "ai.rewrite.typography": set(),
        "ai.rewrite.embedding": set(),
        "ai.rewrite.metadata": {"producer"},
    }
