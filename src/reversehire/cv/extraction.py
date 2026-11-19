"""Profile extraction from a CV with rules only (no LLM).

Extracted data: roles (with period and employer), years of experience without
double counting, degrees, languages with level, skills (`skills.py`: skills
sections + IT/EN/UK dictionary in `data/skills.json`), work history with
employment gaps, seniority (`data/seniority.json`), mobility and home town
(`mobility.py`). Each item carries at least one `Evidence(text=<original row>,
source="cv")`. Sectors are NOT computed here (the pipeline adds them with
`geo.sectors.sectors_for_profile`).

CVs in Italian, English and Ukrainian are read; the profile values are then
shown in English (`english.py`: tables first, local translation model for the
rest), each translated item keeping the CV wording in `original`.

Period conventions: endpoints are inclusive; a year without month counts from
January (start) to December (end), so "2015-2018" is 48 months;
"oggi/present" is the current date (parameter `today` for tests).
"""

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz

from ..store import Store
from ..config import Settings
from ..models import Evidence, CVProfile, EmploymentGap, ProfileItem, WorkExperience
from .mobility import find_home, find_mobility
from .reading import read_text, detect_language
from .sections import (DATA_FOLDER, HEAD, IGNORED_SECTIONS, Section, split_sections, has_section, normalize,
                       rows_of)
from .skills import extract_skills  # noqa: F401 - public name of this module too

SOURCE = "cv"
MAX_ESCO_ROLES = 5
GAP_MONTHS = 6  # longer pauses between experiences are reported as gaps
HOME_ROWS = 25  # rows at the top of the CV where the contact block is looked for

# --- dates -------------------------------------------------------------------

_MONTHS = {
    "gennaio": 1, "gen": 1, "january": 1, "jan": 1,
    "febbraio": 2, "feb": 2, "february": 2,
    "marzo": 3, "mar": 3, "march": 3,
    "aprile": 4, "apr": 4, "april": 4,
    "maggio": 5, "mag": 5, "may": 5,
    "giugno": 6, "giu": 6, "june": 6, "jun": 6,
    "luglio": 7, "lug": 7, "july": 7, "jul": 7,
    "agosto": 8, "ago": 8, "august": 8, "aug": 8,
    "settembre": 9, "sett": 9, "set": 9, "september": 9, "sept": 9, "sep": 9,
    "ottobre": 10, "ott": 10, "october": 10, "oct": 10,
    "novembre": 11, "nov": 11, "november": 11,
    "dicembre": 12, "dic": 12, "december": 12, "dec": 12,
    # Ukrainian: nominative, genitive and short forms.
    "січень": 1, "січня": 1, "січ": 1, "лютий": 2, "лютого": 2, "лют": 2,
    "березень": 3, "березня": 3, "бер": 3, "квітень": 4, "квітня": 4, "квіт": 4,
    "травень": 5, "травня": 5, "трав": 5, "червень": 6, "червня": 6, "черв": 6,
    "липень": 7, "липня": 7, "лип": 7, "серпень": 8, "серпня": 8, "серп": 8,
    "вересень": 9, "вересня": 9, "вер": 9, "жовтень": 10, "жовтня": 10, "жовт": 10,
    "листопад": 11, "листопада": 11, "лист": 11, "грудень": 12, "грудня": 12, "груд": 12,
}
_MONTHS_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_POINT = (
    r"(?:\d{1,2}[/.\-]\d{1,2}[/.\-](?:19|20)\d{2}"  # 15/03/2019
    r"|\d{1,2}\s?[/.\-]\s?(?:19|20)\d{2}"  # 03/2019, 3-2019
    rf"|(?:{_MONTHS_RE})\.?\s*'?\s*(?:19|20)\d{{2}}"  # gen 2018, March 2020
    r"|(?:19|20)\d{2})"  # 2015
)
_PRESENT = (
    r"(?:ad\s+oggi|a\s+oggi|oggi|in\s+corso|tuttora|attuale|presente|"
    r"present|current|currently|now|today|ongoing|date|"
    r"дотепер|по\s+теперішній\s+час|теперішній\s+час|до\s+тепер|досі|сьогодні|наразі|тепер)"
)
_YEAR_MARK = r"(?:\s*рр?\.)?"  # Ukrainian "2019 р."
_SEPARATOR = (r"\s*(?:-|–|—|−|~|→|>|\bto\b|\buntil\b|\btill\b|\bal\b|\ba\b|\bfino\s+al?\b|/"
              r"|\bпо\b|\bдо\b)\s*")
_RE_RANGE = re.compile(
    rf"(?<![\w/.\-])(?:(?:dal|da|from|з|із|від)\s+)?(?P<start>{_DATE_POINT})(?![\d]){_YEAR_MARK}"
    rf"{_SEPARATOR}(?P<end>{_DATE_POINT}(?![\d])|{_PRESENT}(?!\w)){_YEAR_MARK}",
    re.IGNORECASE,
)
_RE_OPEN_ENDED = re.compile(
    rf"(?<![\w/.\-])(?:dal|da|since|from|з|із|від)\s+(?P<start>{_DATE_POINT})(?![\d\w])", re.IGNORECASE
)


@dataclass
class Period:
    start: int  # month index: year*12 + (month-1)
    end: int
    in_progress: bool
    text: str  # part of the row recognised as the period

    @property
    def months(self) -> int:
        return self.end - self.start + 1


def _date_point(text: str, as_end: bool) -> int | None:
    t = text.strip().lower()
    m = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})", t)
    if m:
        month, year = int(m.group(2)), int(m.group(3))
    elif m := re.fullmatch(r"(\d{1,2})\s?[/.\-]\s?(\d{4})", t):
        month, year = int(m.group(1)), int(m.group(2))
    elif m := re.fullmatch(rf"({_MONTHS_RE})\.?\s*'?\s*(\d{{4}})", t):
        month, year = _MONTHS[m.group(1)], int(m.group(2))
    elif m := re.fullmatch(r"(\d{4})", t):
        month, year = (12 if as_end else 1), int(m.group(1))
    else:
        return None
    if not 1 <= month <= 12 or not 1950 <= year <= date.today().year + 1:
        return None
    return year * 12 + month - 1


def find_period(row: str, today: date | None = None) -> Period | None:
    """The first period (date range) in the row, or None."""
    today = today or date.today()
    today_index = today.year * 12 + today.month - 1
    # "system integrator07/2014 – 09/2022": a PDF text layer may glue the date to the word
    row = re.sub(r"(?<=[^\W\d_])(?=\d{1,2}[/.\-](?:19|20)\d{2}\b)", " ", row)
    for m in _RE_RANGE.finditer(row):
        start = _date_point(m.group("start"), as_end=False)
        end_text = m.group("end")
        if re.fullmatch(_PRESENT, end_text.strip(), re.IGNORECASE):
            end, in_progress = today_index, True
        else:
            end, in_progress = _date_point(end_text, as_end=True), False
        if start is None or end is None or end < start or start > today_index:
            continue
        return Period(start, min(end, today_index), in_progress, m.group(0).strip())
    m = _RE_OPEN_ENDED.search(row)
    if m:
        start = _date_point(m.group("start"), as_end=False)
        if start is not None and start <= today_index:
            return Period(start, today_index, True, m.group(0).strip())
    return None


def total_months(periods: list[Period]) -> int:
    """Months covered by the union of the periods (overlaps count once)."""
    intervals = sorted((p.start, p.end) for p in periods)
    total, current = 0, None
    for start, end in intervals:
        if current is None or start > current[1] + 1:
            if current:
                total += current[1] - current[0] + 1
            current = [start, end]
        else:
            current[1] = max(current[1], end)
    if current:
        total += current[1] - current[0] + 1
    return total


def years_from_periods(periods: list[Period]) -> float | None:
    if not periods:
        return None
    return round(total_months(periods) / 12, 1)


# --- experiences ---------------------------------------------------------------

_RE_BULLET = re.compile(r"^\s*(?:[-•*·▪◦●○■□➢►>»–—✓✔]|\d+[.)]\s)\s*")
_TITLE_SEPARATORS = (" | ", " presso ", " at ", " @ ", " – ", " — ", " - ", " · ", ", ")


@dataclass
class Experience:
    title: str
    employer: str
    period: Period
    evidence: list[str] = field(default_factory=list)


def _is_bullet(row: str) -> bool:
    return bool(_RE_BULLET.match(row))


def _has_letters(text: str, minimum: int = 2) -> bool:
    return len(re.findall(r"[^\W\d_]", text)) >= minimum


def _tidy(text: str) -> str:
    text = re.sub(r"\(\s*\)", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t|,;:-–—·/").strip()


_RE_COMPANY = re.compile(
    r"(?:(?<!\w)(?:s\.?r\.?l\.?s?|s\.?p\.?a\.?|s\.?n\.?c\.?|s\.?a\.?s\.?|ltd|limited|llc|inc|corp|gmbh|"
    r"ag|kg|bv|nv|oy|sarl|onlus|coop|cooperativa|group|gruppo|holding|tov|тов|пп|прат|пат|ат|тзов|дп|фоп)"
    r"(?!\w)\.?|«[^»]+»)",
    re.IGNORECASE,
)


def _company_like(text: str) -> bool:
    return bool(_RE_COMPANY.search(text))


# Words that make a phrase a job title (IT/EN/UK), used to tell "Employer · Title" from
# "Title · Employer" when neither part carries a legal form ("Prova Impianti · Engineer").
_RE_JOB_WORD = re.compile(
    r"(?<!\w)(?:engineer|developer|programmer|manager|technician|analyst|consultant|specialist|designer|"
    r"architect|operator|assistant|director|lead|leader|head|officer|administrator|integrator|intern|"
    r"trainee|accountant|clerk|coordinator|supervisor|worker|scientist|researcher|teacher|nurse|driver|"
    r"ingegnere|sviluppatore|sviluppatrice|programmatore|programmatrice|tecnico|tecnica|progettista|"
    r"responsabile|addetto|addetta|impiegato|impiegata|operaio|operaia|consulente|analista|direttore|"
    r"capo|stagista|apprendista|collaudatore|manutentore|disegnatore|commerciale|contabile|"
    r"інженер|розробник|програміст|менеджер|технік|аналітик|консультант|оператор|керівник|спеціаліст)"
    r"(?!\w)",
    re.IGNORECASE,
)


def _job_like(text: str) -> bool:
    return bool(_RE_JOB_WORD.search(text or ""))


def split_title_employer(text: str) -> tuple[str, str]:
    """'Contabile presso Studio X' -> ('Contabile', 'Studio X');
    'Alfa srl · Developer' -> ('Developer', 'Alfa srl');
    'Prova Impianti · Senior Software Engineer' -> ('Senior Software Engineer', 'Prova Impianti')."""
    text = _tidy(re.sub(r"\s*·\s*", " · ", text or ""))
    for sep in _TITLE_SEPARATORS:
        if sep.strip() in ("presso", "at"):
            parts = re.split(rf"\s+{sep.strip()}\s+", text, maxsplit=1, flags=re.IGNORECASE)
        else:
            parts = text.split(sep, 1)
        if len(parts) == 2 and _has_letters(parts[0]) and _has_letters(parts[1]):
            first, second = _tidy(parts[0]), _tidy(parts[1])
            if sep.strip() not in ("presso", "at", "@"):
                if _company_like(first) and not _company_like(second):
                    return second, first
                if _job_like(second) and not _job_like(first) and not _company_like(second):
                    return second, first
            return first, second
    return text, ""


def _valid_title(title: str) -> bool:
    words = title.split()
    return (
        _has_letters(title, 3)
        and 1 <= len(words) <= 8
        and not title.endswith(".")
        and "@" not in title
        and not re.search(r"https?://|www\.", title)
    )


def extract_experiences(rows: list[str], today: date | None = None) -> tuple[list[Experience], list[Period]]:
    """Experiences (with a valid title) and all periods found in the rows."""
    periods = [find_period(r, today) for r in rows]
    indexes = [i for i, p in enumerate(periods) if p]
    if not indexes:
        return [], []

    def rest(i: int) -> str:
        return _tidy(rows[i].replace(periods[i].text, " ", 1))

    # Style: title before the date row or after it. Decided on the first
    # "bare" date row (no text besides the period).
    layout = "after"
    for i in indexes:
        if not _has_letters(rest(i), 3):
            j = i - 1
            if j >= 0 and not _is_bullet(rows[j]) and not periods[j]:
                layout = "before"
            break

    experiences: list[Experience] = []
    for pos, i in enumerate(indexes):
        period = periods[i]
        previous = indexes[pos - 1] if pos > 0 else -1
        following = indexes[pos + 1] if pos + 1 < len(indexes) else len(rows)
        rest_text = rest(i)
        evidence = [rows[i]]
        title = employer = ""

        def useful(j: int, lower_limit: int, upper_limit: int) -> bool:
            return lower_limit < j < upper_limit and not _is_bullet(rows[j]) and not periods[j]

        if _has_letters(rest_text, 3):
            title, employer = split_title_employer(rest_text)
            if (not employer and _company_like(title) and not _job_like(title)
                    and useful(i + 1, i, following) and _job_like(rows[i + 1])):
                # "Alfa srl (Beta group)  03/2025 – present" then the title on the next row
                employer = title
                title, _extra = split_title_employer(rows[i + 1])
                if _company_like(title) and _extra:
                    title = _extra
                evidence.append(rows[i + 1])
            elif not employer and useful(i + 1, i, following) and layout == "after":
                employer = _tidy(rows[i + 1])
        elif layout == "after":
            if useful(i + 1, i, following):
                title, employer = split_title_employer(rows[i + 1])
                evidence.insert(0, rows[i + 1])
                if not employer and useful(i + 2, i, following):
                    employer = _tidy(rows[i + 2])
        else:  # before
            if useful(i - 1, previous, i):
                title, employer = split_title_employer(rows[i - 1])
                evidence.insert(0, rows[i - 1])
                if (not employer and useful(i - 2, previous, i)
                        and len(rows[i - 2].split()) <= 6):
                    employer = title
                    title = _tidy(rows[i - 2])
                    evidence.insert(0, rows[i - 2])
        if title and _valid_title(title):
            experiences.append(Experience(title, employer, period, evidence))
    return experiences, [p for p in periods if p]


def _same_roles(a: str, b: str) -> bool:
    na, nb = normalize(a), normalize(b)
    return na == nb or fuzz.token_set_ratio(na, nb) >= 95


def sort_roles(experiences: list[Experience]) -> list[Experience]:
    """Most relevant first: most recent (end), on a tie the longest; duplicates merged."""
    by_recency = sorted(experiences, key=lambda e: (-e.period.end, -e.period.months))
    unique_items: list[Experience] = []
    for e in by_recency:
        duplicate = next((u for u in unique_items if normalize(u.title) == normalize(e.title)), None)
        if duplicate:
            duplicate.evidence.extend(x for x in e.evidence if x not in duplicate.evidence)
        else:
            unique_items.append(e)
    return unique_items


def _month_text(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _year_only(period: Period) -> tuple[bool, bool]:
    """(start, end) written as a bare year in the CV ("2015 - 2018")."""
    m = _RE_RANGE.search(period.text) or _RE_OPEN_ENDED.search(period.text)
    if not m:
        return False, False
    start = bool(re.fullmatch(r"\d{4}", m.group("start").strip()))
    end_text = m.groupdict().get("end") or ""
    return start, bool(re.fullmatch(r"\d{4}", end_text.strip()))


def work_history(experiences: list[Experience]) -> list[WorkExperience]:
    """Serialisable work history, most recent first (by end, then by start)."""
    result = []
    for e in sorted(experiences, key=lambda x: (-x.period.end, -x.period.start)):
        p = e.period
        start_year, end_year = _year_only(p)
        start = str(p.start // 12) if start_year else _month_text(p.start)
        if p.in_progress:
            end = "present"
        else:
            end = str(p.end // 12) if end_year else _month_text(p.end)
        rows = list(dict.fromkeys(e.evidence))
        result.append(WorkExperience(e.title, e.employer, start, end, p.months,
                                     [Evidence(r, SOURCE) for r in rows]))
    return result


def employment_gaps(experiences: list[Experience], minimum_months: int = GAP_MONTHS) -> list[EmploymentGap]:
    """Months without any experience between consecutive (merged) periods, most recent first."""
    intervals = sorted((e.period.start, e.period.end) for e in experiences)
    merged: list[list[int]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    gaps = []
    for previous, following in zip(merged, merged[1:]):
        months = following[0] - previous[1] - 1
        if months > minimum_months:
            gaps.append(EmploymentGap(_month_text(previous[1] + 1), _month_text(following[0] - 1), months))
    return list(reversed(gaps))


# --- seniority ----------------------------------------------------------------

@lru_cache(maxsize=1)
def _seniority_data() -> dict:
    return json.loads((DATA_FOLDER / "seniority.json").read_text(encoding="utf-8"))


def _has_word(norm: str, words: list[str]) -> bool:
    return any(re.search(r"(?<![\w])" + re.escape(normalize(w)) + r"(?![\w])", norm) for w in words)


_LEVELS = ["junior", "mid", "senior"]


def estimate_seniority(years: float | None, title_item: ProfileItem | None) -> tuple[str | None, list[Evidence]]:
    """("junior"|"mid"|"senior"|None, evidence) from the years of experience and the
    words of the most recent role title (senior/lead/responsabile... one level up,
    junior/stagista/trainee... one level down)."""
    data = _seniority_data()
    evidence: list[Evidence] = []
    level: int | None = None
    if years is not None:
        level = 0 if years < data["junior_below"] else 1 if years < data["senior_from"] else 2
        evidence.append(Evidence(f"{years:g} years of dated work experience", "computed"))
    if title_item is not None:
        norm = normalize(title_item.value)
        senior = _has_word(norm, data["senior_words"])
        junior = _has_word(norm, data["junior_words"])
        if senior != junior:
            if level is None:
                level = 2 if senior else 0
            elif senior:
                level = min(2, level + 1)
            elif years is None or years < 5:
                level = max(0, level - 1)
            evidence.extend(e for e in title_item.evidence[:1] if e.source == SOURCE)
    if level is None:
        return None, []
    return _LEVELS[level], evidence


# --- degrees ---------------------------------------------------------

_RE_DEGREE = re.compile(
    r"\b(laurea|diploma|master|dottorato|phd|ph\.\s?d|doctorate|doctor of|bachelor|"
    r"b\.?\s?sc|m\.?\s?sc|bsc|msc|b\.?eng|m\.?eng|beng|meng|mba|b\.a\.|m\.a\.|"
    r"licenza media|licenza|qualifica professionale|qualifica|maturit[aà]|"
    r"high school|a-levels?|gcse|associate degree|degree|specializzazione|"
    r"abilitazione professionale|istituto tecnico superiore|"
    r"бакалавр\w*|магістр\w*|спеціаліст|молодший спеціаліст|кандидат\s+\w+\s+наук|кандидат наук|"
    r"доктор філософії|доктор наук|диплом\w*|атестат\w*|ступінь\s+\w+|вища освіта)(?!\w)",
    re.IGNORECASE,
)


def extract_degrees(rows: list[str], today: date | None = None) -> list[ProfileItem]:
    items: list[ProfileItem] = []
    seen: set[str] = set()
    for row in rows:
        if not _RE_DEGREE.search(row):
            continue
        text = row
        period = find_period(row, today)
        if period:
            text = text.replace(period.text, " ", 1)
        text = _RE_BULLET.sub("", _tidy(text))
        # Keep the segment with the keyword ("BSc in X, University of Y" -> "BSc in X").
        for sep in (" | ", " – ", " — ", " - ", ", ", " presso ", " at "):
            parts = text.split(sep)
            if len(parts) > 1:
                with_keyword = [p for p in parts if _RE_DEGREE.search(p)]
                if with_keyword:
                    text = with_keyword[0]
                    break
        text = _tidy(re.sub(r"\(\s*(?:19|20)\d{2}\s*\)", " ", text))
        if not _has_letters(text, 4) or len(text.split()) > 14:
            continue
        key = normalize(text)
        if key in seen:
            continue
        seen.add(key)
        items.append(ProfileItem(text, [Evidence(row, SOURCE)]))
    return items


# --- languages -------------------------------------------------------------------

@lru_cache(maxsize=1)
def _language_data() -> dict:
    return json.loads((DATA_FOLDER / "languages.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _language_regexes() -> tuple[re.Pattern, dict[str, str], re.Pattern, dict[str, dict]]:
    data = _language_data()
    language_variant = {}
    for key, item in data["languages"].items():
        for v in item["variants"]:
            language_variant[normalize(v)] = key
    re_languages = re.compile(
        r"(?<![\w])(" + "|".join(re.escape(v) for v in sorted(language_variant, key=len, reverse=True))
        + r")(?![\w])"
    )
    level_variant = {}
    for item in data["levels"]:
        for v in item["variants"]:
            level_variant[normalize(v)] = item
    re_levels = re.compile(
        r"(?<![\w])([abc][12]|" + "|".join(
            re.escape(v) for v in sorted(level_variant, key=len, reverse=True)) + r")(?![\w])"
    )
    return re_languages, language_variant, re_levels, level_variant


def _level(segment_norm: str, cv_language: str) -> str | None:
    _, _, re_levels, level_variant = _language_regexes()
    matches = re_levels.findall(segment_norm)
    if not matches:
        return None
    cefr = [t.upper() for t in matches if re.fullmatch(r"[abc][12]", t)]
    if cefr:
        count = Counter(cefr)
        maximum = max(count.values())
        return max(c for c, n in count.items() if n == maximum)  # mode, on a tie the highest
    item = level_variant[matches[0]]
    return item.get(cv_language) or item["en"]


def _is_native_section(section: Section) -> bool:
    e = section.label
    return any(p in e for p in ("madre", "mother", "native", "madrelingua", "рідн"))


_NATIVE_WORDS = {"it": "madrelingua", "en": "native", "uk": "рідна"}
# Statements of a mother tongue; the nationality or the CV language are never one.
_RE_NATIVE_STATEMENT = re.compile(r"madre|mother tongue|native|рідн")


def extract_languages(sections: list[Section], cv_language: str, display: str | None = None) -> list[ProfileItem]:
    """Languages with level from the language sections (or, failing that, rows with language+level).

    `display`: language of the names and level words ("en" for the English
    profile); defaults to the CV language. The mother tongue is set only from an
    explicit statement ("Lingua madre", "native", "рідна"), never inferred from
    the nationality or from the language of the CV."""
    display = display or cv_language
    re_languages, language_variant, re_levels, _ = _language_regexes()
    data = _language_data()["languages"]
    found_items: dict[str, tuple[str | None, str]] = {}  # key -> (level, row)

    def record_entry(key: str, level: str | None, row: str) -> None:
        if key not in found_items or (found_items[key][0] is None and level):
            found_items[key] = (level, row)

    blocks = [(s, s.rows) for s in sections if s.key == "languages"]
    only_with_level = not blocks
    if only_with_level:
        blocks = [(s, s.rows) for s in sections if s.key not in (HEAD, *IGNORED_SECTIONS)]
    native = _NATIVE_WORDS.get(display, "native")
    for section, rows in blocks:
        default = native if _is_native_section(section) else None
        last_without_level: tuple[str, str] | None = None
        for row in rows:
            norm = normalize(row)
            matches = list(re_languages.finditer(norm))
            if not matches:
                level = _level(norm, cv_language)
                if level and last_without_level and not only_with_level:
                    key, language_row = last_without_level
                    record_entry(key, level, f"{language_row} {row}")
                    last_without_level = None
                elif _RE_NATIVE_STATEMENT.search(norm):
                    default = native
                continue
            for k, m in enumerate(matches):
                key = language_variant[m.group(1)]
                segment_end = matches[k + 1].start() if k + 1 < len(matches) else len(norm)
                level = _level(norm[m.end():segment_end], display)
                if level is None and k == 0:
                    level = _level(norm[:m.start()], display)  # "madrelingua italiano"
                if level is None and only_with_level:
                    continue
                level = level or default
                record_entry(key, level, row)
                last_without_level = (key, row) if level is None else None
    items = []
    for key, (level, row) in found_items.items():
        name = data[key].get(display) or data[key]["en"]
        value = f"{name} ({level})" if level else name
        original = _tidy(row) if display != cv_language and len(row) <= 80 else None
        items.append(ProfileItem(value, [Evidence(row, SOURCE)], original=original))
    return items


# --- roles --------------------------------------------------------------------

def _role_item(e: Experience) -> ProfileItem:
    rows = list(dict.fromkeys(e.evidence))  # without duplicates, in order
    return ProfileItem(e.title, [Evidence(r, SOURCE) for r in rows])


def _fallback_roles(sections: list[Section]) -> list[ProfileItem]:
    """Without dated experiences: position held/desired or title under the name."""
    items = []
    for row in rows_of(sections, "position")[:2]:
        title, _ = split_title_employer(row)
        if _valid_title(title):
            items.append(ProfileItem(title, [Evidence(row, SOURCE)]))
    if items:
        return items
    head = rows_of(sections, HEAD)
    for row in head[1:5]:
        if re.search(r"[\d@:/]|www\.", row):
            continue
        if _valid_title(row) and len(row.split()) <= 5:
            return [ProfileItem(_tidy(row), [Evidence(row, SOURCE)])]
    return []


def _experience_rows(sections: list[Section]) -> list[str]:
    if has_section(sections, "experience"):
        return rows_of(sections, "experience")
    excluded = {"education", "certifications", "languages", "skills", "projects", "other", *IGNORED_SECTIONS}
    return [r for s in sections if s.key not in excluded for r in s.rows]


def _apply_esco(roles: list[ProfileItem], language: str, store, settings, esco) -> None:
    from .esco import EscoNormalizer

    normalizer = esco or EscoNormalizer(settings or Settings(), store)
    try:
        for item in roles[:MAX_ESCO_ROLES]:
            occupation = normalizer.normalize(item.value, language)
            if occupation is None:
                continue
            item.esco_code = occupation.uri
            for language_code, label in (("it", occupation.label_it),
                                             ("en", occupation.label_en)):
                if label:
                    item.evidence.append(Evidence(label, f"esco:{language_code}"))
    finally:
        if esco is None:
            normalizer.close()


# --- public API -------------------------------------------------------------

def profile_from_text(text: str, path: str = "", store: Store | None = None,
                     settings: Settings | None = None, use_esco: bool = True,
                     *, esco=None, today: date | None = None, english: bool = True,
                     translation_models=None, use_mt: bool = True, step=None) -> CVProfile:
    """Profile from the CV text.

    Extra parameters beyond the contract (keyword-only, optional):
    `esco`: a ready `EscoNormalizer` (tests, client reuse);
    `today`: reference date for "oggi/present" (tests);
    `english`: show the values in English (default; False keeps the CV language);
    `translation_models`: folder of the local translation models (default: see
    `translate.models_folder`); `use_mt`: False uses the tables only;
    `step`: progress callback (key, done, total) called as each step starts
    ("sections", "esco", "translate": see progress.CV_STEPS).
    """
    from ..progress import notify_step

    notify_step(step, "sections")
    language = detect_language(text)
    sections = split_sections(text)
    display = "en" if english else language

    experience_rows = _experience_rows(sections)
    experiences, periods = extract_experiences(experience_rows, today)
    history = work_history(experiences)  # before sort_roles, which merges evidence
    gaps = employment_gaps(experiences)
    roles = [_role_item(e) for e in sort_roles(experiences)]
    for fallback in _fallback_roles(sections) if roles else []:
        # The position held/desired (Europass) is appended at the end if new.
        duplicate = next((r for r in roles if _same_roles(r.value, fallback.value)), None)
        if duplicate:
            texts = {e.text for e in duplicate.evidence}
            duplicate.evidence.extend(e for e in fallback.evidence if e.text not in texts)
        elif rows_of(sections, "position"):
            roles.append(fallback)
    if not roles:
        roles = _fallback_roles(sections)

    education_rows = rows_of(sections, "education") if has_section(sections, "education") else [
        r for s in sections if s.key not in ("experience", *IGNORED_SECTIONS) for r in s.rows
    ]
    all_rows = [r.strip() for r in text.split("\n") if r.strip()]
    head_rows = rows_of(sections, HEAD)[:HOME_ROWS]
    years = years_from_periods(periods)
    seniority, seniority_evidence = estimate_seniority(years, roles[0] if roles else None)
    profile = CVProfile(
        cv_path=str(path),
        language=language,
        roles=roles,
        skills=extract_skills(sections, language, display),
        spoken_languages=extract_languages(sections, language, display),
        degrees=extract_degrees(education_rows, today),
        sectors=[],
        years_experience=years,
        text=text,
        experiences=history,
        gaps=gaps,
        mobility=find_mobility(all_rows, experience_rows),
        home=find_home(head_rows),
        seniority=seniority,
        seniority_evidence=seniority_evidence,
    )
    if use_esco and profile.roles:
        notify_step(step, "esco")
        _apply_esco(profile.roles, language, store, settings, esco)
    if english:
        from .english import to_english

        notify_step(step, "translate")
        to_english(profile, folder=translation_models, use_mt=use_mt)
    return profile


def build_profile(path: str | Path, store: Store | None = None,
                       settings: Settings | None = None, use_esco: bool = True,
                       *, esco=None, today: date | None = None, english: bool = True,
                       translation_models=None, use_mt: bool = True) -> CVProfile:
    """Read the CV (PDF/DOCX) and build its profile; CVReadError if unreadable."""
    text = read_text(path)
    return profile_from_text(text, str(path), store, settings, use_esco,
                            esco=esco, today=today, english=english,
                            translation_models=translation_models, use_mt=use_mt)
