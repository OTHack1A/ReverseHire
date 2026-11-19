"""Small shared helpers of the job search package (text folding, lenient dates, cancel)."""

from __future__ import annotations

import html
import logging
import re
import unicodedata
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime

log = logging.getLogger("reversehire.jobsearch")

_SPACES = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9+#]+(?:[.'][a-z0-9+#]+)*")
_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'", "‘": "'", "`": "'", "´": "'"})

MONTHS = {
    "jan": 1, "gen": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "mag": 5, "jun": 6, "giu": 6,
    "jul": 7, "lug": 7, "aug": 8, "ago": 8, "sep": 9, "set": 9, "oct": 10, "ott": 10, "nov": 11,
    "dec": 12, "dic": 12,
}


def fold(text) -> str:
    """Lowercase, without accents, apostrophes unified, whitespace collapsed."""
    text = unicodedata.normalize("NFKD", str(text or "").translate(_APOSTROPHES))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return _SPACES.sub(" ", text.lower()).strip()


def compact(text) -> str:
    return _SPACES.sub(" ", str(text or "")).strip()


def words(text) -> list[str]:
    """Folded word tokens ("c#", "c++", "node.js" kept whole)."""
    return _WORD.findall(fold(text).replace("’", "'"))


def contains_phrase(haystack_folded: str, phrase: str) -> bool:
    """Whole-term match of a folded phrase in a folded text ("java" is not in "javascript")."""
    phrase = fold(phrase)
    if not phrase:
        return False
    pattern = r"(?<![a-z0-9+#])" + re.escape(phrase) + r"(?![a-z0-9+#])"
    return re.search(pattern, haystack_folded) is not None


def html_text(fragment: str, maximum: int = 8000) -> str:
    """Plain text of an HTML (or plain) fragment, one line per block."""
    if not fragment:
        return ""
    try:
        from ..web.text import fragment_lines
        return fragment_lines(str(fragment), maximum)
    except Exception:  # never fail on odd markup
        text = re.sub(r"<[^>]+>", "\n", html.unescape(str(fragment)))
        lines = [compact(line) for line in text.splitlines()]
        return "\n".join(line for line in lines if line)[:maximum]


def unescape_deep(value):
    """html.unescape on every string of a JSON value (done AFTER json.loads)."""
    if isinstance(value, str):
        out = value
        for _ in range(2):
            if "&" not in out:
                break
            out = html.unescape(out)
        return out
    if isinstance(value, list):
        return [unescape_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: unescape_deep(v) for k, v in value.items()}
    return value


_ISO_BASIC = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?)?")
_DMY = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})")
_DMONY = re.compile(r"^(\d{1,2})[\s\-/]([A-Za-z]{3,})[a-z]*[\s\-/,]+(\d{4})")
_YMD = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")


def parse_date(value) -> date | None:
    """A date from the many formats seen in job ads; None when not understood.

    ISO with or without separators ("2026-11-18T10:00:00+0000", "20260129T111110"),
    day first ("18/11/2026", "18-11-2026"), "31-Dec-2026", RFC 2822, epoch seconds or
    milliseconds. Leading/trailing spaces are ignored (Adecco adds them)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) / (1000.0 if value > 10**11 else 1.0)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit() and len(text) >= 9:
        return parse_date(int(text))
    found = _YMD.match(text)
    if found:
        try:
            return date(int(found.group(1)), int(found.group(2)), int(found.group(3)))
        except ValueError:
            return None
    found = _ISO_BASIC.match(text)
    if found:
        try:
            return date(int(found.group(1)), int(found.group(2)), int(found.group(3)))
        except ValueError:
            return None
    found = _DMY.match(text)
    if found:
        try:
            return date(int(found.group(3)), int(found.group(2)), int(found.group(1)))
        except ValueError:
            return None
    found = _DMONY.match(text)
    if found:
        month = MONTHS.get(found.group(2)[:3].lower())
        if month:
            try:
                return date(int(found.group(3)), month, int(found.group(1)))
            except ValueError:
                return None
    try:
        return parsedate_to_datetime(text).date()
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


class CancelCheck:
    """Uniform view of a cancel flag: a threading.Event, a callable or None."""

    def __init__(self, cancel=None):
        self._cancel = cancel

    def __call__(self) -> bool:
        cancel = self._cancel
        if cancel is None:
            return False
        try:
            if hasattr(cancel, "is_set"):
                return bool(cancel.is_set())
            return bool(cancel())
        except Exception:
            return False
