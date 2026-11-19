"""Helpers shared by the panels that need no Qt: error messages, links, number formatting."""

import logging
import webbrowser
from urllib.parse import urlsplit

from ..i18n import current_language, t
from .coordinates import parse_number

log = logging.getLogger(__name__)

SPACING = 6
COMMA_DECIMAL_LANGUAGES = ("it", "uk")

__all__ = ["SPACING", "is_openable_url", "open_url", "error_message", "format_number",
           "parse_number", "readable_duration", "enum_label", "cv_file_filter", "is_cv_file"]

# Values that have a label in the locale files ("<kind>.<value>").
CONTRACTS = ("permanent", "permanent_possible", "fixed_term", "internship", "apprenticeship",
             "freelance", "temporary_agency")
PERMANENT_CONTRACTS = ("permanent", "permanent_possible")
REMOTE_MODES = ("remote", "hybrid", "onsite")
TRAVEL_NEEDS = ("required", "occasional", "none")


def enum_label(kind: str, value: str | None) -> str:
    """Label of an enum value ("contract", "remote", "travel", "card_status", ...); "" for None."""
    if not value:
        return ""
    text = t(f"{kind}.{value}")
    return value.replace("_", " ") if text == f"{kind}.{value}" else text


def cv_file_filter() -> str:
    """File-dialog filter with every readable CV format (PDF, DOCX, images)."""
    from ..cv.reading import SUPPORTED_FORMATS

    # Qt matches case-sensitively on Linux: both cases ("cv.pdf", "CV.PDF").
    patterns = " ".join(f"*{ext} *{ext.upper()}" for ext in SUPPORTED_FORMATS)
    return f"{t('file.cv')} ({patterns});;{t('file.all')} (*)"


def is_cv_file(path: str) -> bool:
    from ..cv.reading import SUPPORTED_FORMATS

    return str(path).lower().endswith(tuple(SUPPORTED_FORMATS))


def is_openable_url(url: str | None) -> bool:
    """Only http/https URLs with a host."""
    if not url or not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


def open_url(url: str | None, opener=webbrowser.open) -> bool:
    """Opens the URL in the browser only if it is http/https; returns whether it did."""
    if not is_openable_url(url):
        return False
    try:
        opener(url.strip())
    except Exception:
        log.warning("Browser cannot be started", exc_info=True)
        return False
    return True


def run_job(run, work, on_success, on_error, **options) -> None:
    """Calls a `run_in_background(work, on_success, on_error)` callable, passing the
    window's extra options (name, timeout, cancel) when it accepts them (test runners
    often do not)."""
    import inspect

    try:
        parameters = inspect.signature(run).parameters
        accepted = {k: v for k, v in options.items()
                    if k in parameters or any(p.kind == p.VAR_KEYWORD for p in parameters.values())}
    except (TypeError, ValueError):
        accepted = {}
    run(work, on_success, on_error, **accepted)


class WorkerTimeout(TimeoutError):
    """A worker did not answer within its deadline: the window stopped waiting for it."""

    def __init__(self, name: str, seconds: float):
        super().__init__(f"{name}: no answer after {seconds:.0f} s")
        self.name = name
        self.seconds = seconds


def error_message(error: BaseException) -> str:
    """Text the user can understand (never a traceback)."""
    from ..cv.reading import CVReadError
    from ..pipeline import SearchError
    from ..geo.overpass import OverpassError
    from ..store import StoreBusyError

    if isinstance(error, WorkerTimeout):
        return t("error.timeout")
    if isinstance(error, StoreBusyError):
        return t("error.store_busy")
    if isinstance(error, SearchError):
        return t(error.key)
    if isinstance(error, CVReadError):
        text = str(error).lower()
        # CVReadError messages are matched by keyword ("OCR engine" before "scanned").
        if "ocr engine" in text:
            return t("error.cv_ocr_missing")
        if "not even with ocr" in text:
            return t("error.cv_ocr_empty")
        if "password" in text:
            return t("error.cv_protected")
        if "scanned" in text:
            return t("error.cv_scanned")
        if "unsupported format" in text:
            return t("error.cv_format")
        if "not found" in text:
            return t("error.cv_missing")
        return t("error.cv_unreadable")
    if isinstance(error, OverpassError):
        return t("error.overpass")
    try:
        import httpx

        if isinstance(error, httpx.HTTPError):
            return t("error.network")
    except ImportError:  # pragma: no cover
        pass
    if isinstance(error, (ConnectionError, TimeoutError)):
        return t("error.network")
    return t("error.generic")


def format_number(value: float | None, decimals: int = 0) -> str:
    """Number in the current language's style ("" when missing)."""
    if value is None:
        return ""
    text = f"{value:.{decimals}f}"
    return text.replace(".", ",") if current_language() in COMMA_DECIMAL_LANGUAGES else text


def format_km(value: float) -> str:
    """Kilometres without trailing zeros ("2.5", "15"), language decimal separator."""
    text = f"{value:g}"
    return text.replace(".", ",") if current_language() in COMMA_DECIMAL_LANGUAGES else text


def readable_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return t("time.seconds", n=seconds)
    minutes = round(seconds / 60)
    if minutes < 60:
        return t("time.minutes", n=minutes)
    hours, rest = divmod(minutes, 60)
    return t("time.hours", hours=hours, minutes=rest)
