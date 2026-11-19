"""Everything the GUI shows after a CV is chosen, computed in ONE worker-thread call:
the text and how it was read, the profile, the recruiter review and the AI-style
signals. No Qt here (tested directly).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from ..models import CVProfile
from ..progress import StepCallback, notify_step

log = logging.getLogger(__name__)

# Called with (pages done, pages to OCR) while the OCR runs (see cv/reading.py).
Progress = Callable[[int, int], None]


@dataclass
class CVAnalysis:
    """Result of reading and analysing one CV.

    reading: `cv.reading.ReadResult` (None when an injected reader gave only a profile);
    review: `cv.recruiter.RecruiterReview`; signals: `cv.ai_signals.AISignals`
    (None when that analysis failed: the rest is still shown).
    """

    profile: CVProfile
    reading: object | None = None
    review: object | None = None
    signals: object | None = None
    photo: object | None = None   # cv.photo.Photo (kept in memory only)
    estimate: object | None = None  # cv.ai_estimate.AiEstimate (probability of AI writing)


def photo_of(path: str | None):
    """The portrait in the CV (`cv.photo.Photo`) or None; never raises."""
    if not path:
        return None
    try:
        from pathlib import Path

        if not Path(path).is_file():
            return None
        from ..cv.photo import extract_photo

        return extract_photo(path)
    except Exception:
        log.info("Photo extraction failed", exc_info=True)
        return None


def align_photo_finding(review, photo) -> None:
    """The recruiter view reports a photo as optional personal data: when the photo
    detection found one and the file inspection did not, the finding is added."""
    if review is None or photo is None:
        return
    findings = getattr(review, "findings", None)
    if findings is None or any(f.area == "personal" and f.key.endswith(".photo") for f in findings):
        return
    try:
        from ..cv.recruiter import Finding, _KEY_PREFIX

        key = f"{_KEY_PREFIX}.personal.photo"
        findings.append(Finding(area="personal", key=key, severity="info", params={"count": 1},
                                evidence=[], advice_key=f"{key}.advice", penalty=0))
    except Exception:
        log.debug("Photo finding not added", exc_info=True)


def essential_skills_for(profile: CVProfile, store=None, settings=None,
                         language: str | None = None) -> list[str] | None:
    """Essential skills of the profile's ESCO occupation, or None (no code, no network,
    function not available yet, any failure): the coverage area is then not assessed.
    `language`: language of the skill names (default: the CV language)."""
    uri = next((r.esco_code for r in profile.roles if r.esco_code), None)
    if not uri:
        return None
    try:
        from ..cv import esco

        function = getattr(esco, "essential_skills", None)
        if function is None:
            return None
        skills = function(uri, language or profile.language, store, settings)
    except Exception:
        log.info("ESCO essential skills not available", exc_info=True)
        return None
    return list(skills) or None


def english_skill_names(review, english: list[str] | None) -> None:
    """Shows the role coverage with the English ESCO names (interface in English).

    The coverage is computed with the names in the CV language (they match the CV
    text); ESCO returns the skills of an occupation in the same order in every
    language, so the English list replaces them one by one when the lengths agree.
    The lists of covered/missing skills in the findings are rebuilt from them."""
    coverage = list(getattr(review, "coverage", None) or [])
    if not coverage or not english or len(english) != len(coverage):
        return
    for item, name in zip(coverage, english):
        item.skill = name
    covered = [c.skill for c in coverage if c.covered]
    missing = [c.skill for c in coverage if not c.covered]
    try:
        from ..cv.recruiter import MAX_LISTED_SKILLS
    except Exception:  # pragma: no cover
        MAX_LISTED_SKILLS = 12
    for finding in getattr(review, "findings", None) or []:
        if finding.area != "coverage" or "skills" not in (finding.params or {}):
            continue
        names = covered if "covered" in finding.key else missing if "missing" in finding.key else None
        if names is not None:
            finding.params["skills"] = ", ".join(names[:MAX_LISTED_SKILLS])


def complete(result, store=None, settings=None, *, use_esco: bool = True, embedder=None,
             ui_language: str | None = None, use_photo: bool = True,
             step: StepCallback | None = None) -> CVAnalysis:
    """Adds the recruiter review and the AI-style signals to a profile (or an analysis).

    With the interface in English (`ui_language`, default the current one) the
    ESCO skill names are shown in English and the CV lines quoted as evidence are
    pre-translated for the display layer (`display_text`). `step` is told about each
    step that really runs (progress.CV_STEPS)."""
    from .. import display_text
    from ..i18n import current_language

    ui_language = ui_language or current_language()
    analysis = result if isinstance(result, CVAnalysis) else CVAnalysis(profile=result)
    profile = analysis.profile
    path = profile.cv_path or None
    if analysis.review is None:
        notify_step(step, "recruiter")
        try:
            from ..cv.recruiter import review

            skills = essential_skills_for(profile, store, settings) if use_esco else None
            analysis.review = review(profile, path, esco_skills=skills, embedder=embedder)
            if skills and ui_language == "en" and profile.language != "en":
                english_skill_names(analysis.review, essential_skills_for(profile, store, settings, "en"))
        except Exception:
            log.warning("Recruiter review failed", exc_info=True)
    if analysis.signals is None:
        notify_step(step, "ai")
        try:
            from ..cv.ai_signals import analyse

            analysis.signals = analyse(profile.text, profile.language, path, embedder=embedder)
        except Exception:
            log.warning("AI-style signals failed", exc_info=True)
    if analysis.estimate is None and analysis.signals is not None:
        try:
            from ..cv.ai_estimate import estimate_ai_probability

            analysis.estimate = estimate_ai_probability(profile.text, profile.language, path=path,
                                                        embedder=embedder, signals=analysis.signals)
        except Exception:
            log.warning("AI probability estimate failed", exc_info=True)
    if analysis.photo is None and use_photo:
        notify_step(step, "photo")
        analysis.photo = photo_of(path)
    align_photo_finding(analysis.review, analysis.photo)
    if ui_language == "en" and profile.language != "en":
        notify_step(step, "display")
        display_text.prewarm(display_text.analysis_texts(profile, analysis.review, analysis.signals),
                             store, ui_language=ui_language)
    return analysis


def advanced_mode(settings, requested: str | None = None) -> str:
    """The `advanced` argument of read_document: `requested` ("force", "off"...) when
    given, else "auto" unless the user chose "never" (no settings: "off")."""
    if requested:
        return requested
    if settings is None:
        return "off"
    return "off" if getattr(settings, "ocr_advanced", "ask") == "never" else "auto"


def analyse_cv(path: str, store=None, settings=None, *, use_esco: bool = True,
               progress: Progress | None = None, embedder=None,
               step: StepCallback | None = None, advanced: str | None = None,
               cancel=None) -> CVAnalysis:
    """Reads the CV (PDF, DOCX, image; OCR when needed) and analyses it.

    Blocking (OCR and ESCO take seconds): call it in a worker thread.
    CVReadError when the file cannot be read. `progress(done, total)`: OCR pages;
    `step(key, done, total)`: every step (progress.CV_STEPS), the OCR pages included.
    `advanced`: the advanced OCR reader ("auto", "force", "off"; default from the
    settings, see `advanced_mode`); `cancel` stops its pass (the normal reading stays).
    """
    from ..cv.extraction import profile_from_text
    from ..cv.reading import read_document

    def pages(done: int, total: int) -> None:
        notify_step(step, "ocr", done, total)
        if progress is not None:
            progress(done, total)

    def advanced_pages(done: int, total: int) -> None:
        notify_step(step, "ocr_advanced", done, total)

    notify_step(step, "read")
    mode = advanced_mode(settings, advanced)
    options = {} if mode == "off" else {"advanced": mode, "advanced_progress": advanced_pages, "cancel": cancel}
    reading = read_document(path, pages, **options)
    kwargs = {"step": step} if step is not None else {}
    profile = profile_from_text(reading.text, str(path), store, settings, use_esco, **kwargs)
    return complete(CVAnalysis(profile=profile, reading=reading), store, settings,
                    use_esco=use_esco, embedder=embedder, step=step)
