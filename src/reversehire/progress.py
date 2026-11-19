"""Progress models for the status-bar progress bar (no Qt here: tested directly).

Two producers feed the one bar of the main window (gui/progress_bar.py):

* `CVProgress`: the steps of reading and analysing a CV (PDF text, OCR page by
  page, sections and skills, ESCO, translation, recruiter review, AI-style
  signals, photo, display texts), each with a weight, giving a determinate
  fraction, the name of the running step and, during the OCR, "page 2 of 5".
* `SearchProgress`: the companies of a search. The ESTIMATED number of companies
  is known as soon as discovery has the candidate list; it is replaced by the
  final number once ranking, the company limit and the job boards are done.
  Then "analysed / total" advances, with the time remaining computed from the
  measured pace (only when there is enough data).

Both keep the bar from going backwards: the fraction shown is the highest one
reached so far, also when the total grows (the text shows the new denominator
at once, the bar waits until the real progress catches up).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable

# --- CV reading and analysis -------------------------------------------------------------

# Step keys in order, with their share of the whole bar (the sum is 1). "ocr" only
# runs for scanned pages: when it is skipped the bar simply jumps past its share.
CV_STEPS: tuple[tuple[str, float], ...] = (
    ("read", 0.08),        # text of the PDF / DOCX
    ("ocr", 0.29),         # text recognition of scanned pages or images, page by page
    ("ocr_advanced", 0.08),  # the advanced reader, page by page (only when the reading looked poor)
    ("sections", 0.10),    # sections, experiences, skills, languages
    ("esco", 0.10),        # occupation codes (ESCO)
    ("translate", 0.14),   # values shown in English (local translation model)
    ("recruiter", 0.11),   # recruiter review
    ("ai", 0.04),          # AI-writing signals
    ("photo", 0.03),       # portrait in the file
    ("display", 0.03),     # quoted CV lines pre-translated for the display
)
CV_STEP_KEYS = tuple(key for key, _ in CV_STEPS)
PAGED_STEPS = ("ocr", "ocr_advanced")  # steps reported page by page ("page 2 of 5")

# Called by the workers with (step key, done, total); `done`/`total` are the pages of
# the OCR step and 0/0 for the others.
StepCallback = Callable[[str, int, int], None]


def notify_step(callback: StepCallback | None, key: str, done: int = 0, total: int = 0) -> None:
    """Calls a step callback, if any; a failing callback never stops the work."""
    if callback is None:
        return
    try:
        callback(key, done, total)
    except Exception:  # noqa: BLE001 - progress is never worth an aborted analysis
        import logging

        logging.getLogger(__name__).debug("Progress callback failed", exc_info=True)


@dataclass
class CVProgress:
    """Where the CV analysis is: the current step, the OCR pages, the fraction."""

    step: str = "read"
    pages_done: int = 0
    pages_total: int = 0
    _fraction: float = 0.0

    def update(self, key: str, done: int = 0, total: int = 0) -> float:
        """A step started (or, for "ocr", a page done); returns the fraction to show."""
        if key not in CV_STEP_KEYS:
            return self._fraction
        self.step = key
        if key in PAGED_STEPS:
            self.pages_done, self.pages_total = max(0, done), max(0, total)
        start = 0.0
        for name, weight in CV_STEPS:
            if name == key:
                inside = 0.0
                if key in PAGED_STEPS and self.pages_total:
                    inside = weight * min(self.pages_done, self.pages_total) / self.pages_total
                self._fraction = max(self._fraction, min(1.0, start + inside))
                break
            start += weight
        return self._fraction

    @property
    def fraction(self) -> float:
        return self._fraction

    @property
    def index(self) -> int:
        """1-based number of the current step (for "step 3 of 9")."""
        return CV_STEP_KEYS.index(self.step) + 1

    @property
    def count(self) -> int:
        return len(CV_STEP_KEYS)

    @property
    def ocr_page(self) -> int | None:
        """The page being recognised (1-based), or None outside the OCR steps."""
        if self.step not in PAGED_STEPS or not self.pages_total:
            return None
        return min(self.pages_done + 1, self.pages_total)


# --- companies of a search ----------------------------------------------------------------

ETA_MIN_DONE = 3          # companies analysed since the total was known
ETA_MIN_SECONDS = 20.0    # seconds of measurement
ETA_SMOOTHING = 0.3       # weight of the newest pace in the moving average


@dataclass
class Revision:
    """The denominator changed: from `old` (estimated or final) to `new`."""

    old: int
    new: int
    estimated: bool  # the new value is still an estimate


@dataclass
class SearchProgress:
    """Analysed / total companies of a search, with an estimated time remaining."""

    clock: Callable[[], float] = time.monotonic
    total: int | None = None
    estimated: bool = False
    done: int = 0
    revisions: list[Revision] = field(default_factory=list)
    _fraction: float = 0.0
    _base_time: float | None = None
    _base_done: int = 0
    _pace: float | None = None       # seconds per company (smoothed)
    _last_time: float | None = None
    _last_done: int = 0

    def set_total(self, total: int, *, estimated: bool) -> Revision | None:
        """The estimated (as soon as discovery has its list) or final number of
        companies. Returns a Revision when a known value changed (for the log and
        the tooltip); the bar never goes backwards."""
        total = max(0, int(total))
        revision = None
        if self.total is not None and total != self.total:
            revision = Revision(self.total, total, estimated)
            self.revisions.append(revision)
        self.total, self.estimated = total, estimated
        if self._base_time is None:
            self._base_time = self._last_time = self.clock()
            self._base_done = self._last_done = self.done
        self._refresh()
        return revision

    def update(self, done: int) -> None:
        """Companies analysed (or failed) so far, the resumed ones included."""
        done = max(self.done, int(done))
        now = self.clock()
        if self._last_time is not None and done > self._last_done and now > self._last_time:
            pace = (now - self._last_time) / (done - self._last_done)
            self._pace = pace if self._pace is None else (
                ETA_SMOOTHING * pace + (1 - ETA_SMOOTHING) * self._pace)
            self._last_time, self._last_done = now, done
        self.done = done
        self._refresh()

    def _refresh(self) -> None:
        if self.total:
            self._fraction = max(self._fraction, min(1.0, self.done / self.total))
        elif self.total == 0 and not self.estimated:
            self._fraction = 1.0  # nothing to analyse: done

    @property
    def fraction(self) -> float | None:
        """0..1, or None while the total is not known yet (busy bar)."""
        return None if self.total is None else self._fraction

    @property
    def shown_total(self) -> int:
        """The denominator shown: never below the companies already analysed."""
        return max(self.total or 0, self.done)

    def remaining_seconds(self) -> float | None:
        """Estimated seconds left, from the measured pace; None until there is enough
        data (ETA_MIN_DONE companies over at least ETA_MIN_SECONDS)."""
        if self.total is None or self._base_time is None or self._pace is None:
            return None
        measured = self.done - self._base_done
        if measured < ETA_MIN_DONE or self.clock() - self._base_time < ETA_MIN_SECONDS:
            return None
        left = self.shown_total - self.done
        if left <= 0:
            return 0.0
        # The overall average keeps one slow or fast company from swinging the figure.
        average = (self._last_time - self._base_time) / measured if self._last_time else self._pace
        return left * (self._pace + average) / 2


def remaining_minutes(seconds: float | None) -> int | None:
    """Whole minutes for "about N min left" (rounded up); 0 means less than a minute."""
    if seconds is None:
        return None
    if seconds < 60:
        return 0
    return int(math.ceil(seconds / 60.0))
