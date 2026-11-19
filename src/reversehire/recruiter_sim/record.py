"""Check 2, the candidate-record round trip: the standard fields an ATS fills from
the parsed text (name, e-mail, phone, town, current title, employer, dates,
degree, languages, skills), each found / garbled / missing / lost.

"lost" = present in the app's own reading of the file but not in what this
parser extracts (header dropped, image not read...). Statuses follow the idea of
Resume-Matcher `services/ats_parse/roundtrip.py` (found/garbled/missing/hidden;
Apache-2.0, idea only); the values come from the app's own extractors run on
the parsed text.
"""

from __future__ import annotations

import re
from datetime import date

from ..cv.extraction import find_period, profile_from_text
from ..cv.mobility import find_home
from ..cv.sections import HEAD, detect_heading, rows_of, split_sections
from .cvindex import CvIndex, is_year_only, month_text
from .model import (CHECK_RECORD, COMPONENT_PARSE, FAIL, OK, SOURCE_CV, WARN, Edit, EvidenceRef, Finding,
                    RecordField, RecordPreview, msg)
from .parse_view import RE_EMAIL, RE_GARBLED, RE_PHONE, RECORD_SHARE, ParsedView, short

FOUND, GARBLED, MISSING, LOST, FORM = "found", "garbled", "missing", "lost", "form"
PREVIEW_LINES = 25
_NAME_WORD = re.compile(r"^[^\W\d_][^\W\d_'’.\-]*$")
_NOT_NAME = re.compile(r"\b(?:curriculum|vitae|resume|cv|profile|profilo|engineer|developer|manager|"
                       r"tecnico|ingegner\w*|sviluppat\w*|responsabile|addett\w*)\b", re.IGNORECASE)


def parsed_profile(text: str, today: date | None):
    """The app's extractors on the parser's text (no ESCO, no translation, offline)."""
    return profile_from_text(text, use_esco=False, english=False, today=today)


def _name(head: list[str]) -> tuple[str, int] | None:
    for i, row in enumerate(head[:6]):
        words = row.split()
        if (2 <= len(words) <= 4 and all(_NAME_WORD.match(w) for w in words)
                and all(w[:1].isupper() for w in words) and not _NOT_NAME.search(row)
                and detect_heading(row) is None):
            return row, i
    return None


def _phone(text: str) -> str | None:
    for m in RE_PHONE.finditer(text):
        value = m.group(0)
        if len(re.sub(r"\D", "", value)) >= 8 and not find_period(value):
            return value.strip()
    return None


def _status(value: str | None) -> str:
    if not value:
        return MISSING
    return GARBLED if RE_GARBLED.search(value) else FOUND


def _line_no(lines: list[str], text: str | None) -> int | None:
    if not text:
        return None
    for i, x in enumerate(lines):
        if text in x:
            return i + 1
    return None


def _field(name: str, value: str | None, lines: list[str], line: str | None = None) -> RecordField:
    status = _status(value)
    evidence = None
    if value:
        source_line = line or next((x for x in lines if value in x), value)
        evidence = EvidenceRef(SOURCE_CV, short(source_line), _line_no(lines, source_line))
    return RecordField(name, value or "", status, evidence)


def build_record(view: ParsedView, index: CvIndex, app_text: str, profile_fields, today: date | None,
                 app_profile=None) -> tuple[RecordPreview, object]:
    """The record preview and the profile extracted from the parsed text."""
    lines = view.lines
    parsed = parsed_profile(view.text, today) if view.text.strip() else None
    head = rows_of(split_sections(view.text), HEAD) if view.text.strip() else []
    fields: list[RecordField] = []
    blocks = sorted(index.blocks, key=lambda b: (-b.end, -b.start))
    latest = blocks[0] if blocks else None
    for name in profile_fields:
        if name == "name":
            found = _name(head)
            fields.append(_field("name", found[0] if found else None, lines))
        elif name == "email":
            m = RE_EMAIL.search(view.text)
            fields.append(_field("email", m.group(0) if m else None, lines))
        elif name == "phone":
            fields.append(_field("phone", _phone(view.text), lines))
        elif name == "town":
            town = find_home(head[:25]) if head else None
            fields.append(_field("town", town, lines))
        elif name == "title":
            line = next((r for r in latest.rows if latest.title in r), None) if latest else None
            fields.append(_field("title", latest.title if latest else None, lines, line))
        elif name == "employer":
            value = latest.employer if latest and latest.employer else None
            line = next((r for r in latest.rows if value and value in r), None) if latest else None
            fields.append(_field("employer", value, lines, line))
        elif name == "dates":
            if not blocks:
                fields.append(RecordField("dates", "", MISSING))
            else:
                year_only = [b for b in blocks if is_year_only(b.period_text)]
                value = f"{len(blocks)} job(s), {len(year_only)} without months"
                line = next((r for r in latest.rows if latest.period_text in r), latest.rows[0])
                status = GARBLED if year_only else FOUND
                fields.append(RecordField("dates", value, status,
                                          EvidenceRef(SOURCE_CV, short(line), _line_no(lines, line))))
        elif name == "degree":
            item = parsed.degrees[0] if parsed and parsed.degrees else None
            fields.append(_field("degree", item.value if item else None, lines,
                                 item.evidence[0].text if item and item.evidence else None))
        elif name == "languages":
            items = parsed.spoken_languages if parsed else []
            value = ", ".join(i.value for i in items)
            line = items[0].evidence[0].text if items and items[0].evidence else None
            fields.append(_field("languages", value or None, lines, line))
        elif name == "skills":
            items = parsed.skills if parsed else []
            value = ", ".join(i.value for i in items[:12])
            line = items[0].evidence[0].text if items and items[0].evidence else None
            fields.append(_field("skills", value or None, lines, line))
        elif name == "file":
            fields.append(RecordField("file", "attached", FOUND))
        elif name == "area":
            fields.append(RecordField("area", "", FORM))
    _mark_lost(fields, app_text, view)
    positions = [{"title": b.title, "employer": b.employer, "start": month_text(b.start),
                  "end": "present" if b.in_progress else month_text(b.end),
                  "months_known": not is_year_only(b.period_text)} for b in blocks]
    skills = [i.value for i in (parsed.skills if parsed else [])]
    return RecordPreview(fields, positions, skills, lines[:PREVIEW_LINES]), parsed


def _mark_lost(fields: list[RecordField], app_text: str, view: ParsedView) -> None:
    """A missing field whose value the app finds in the full text was lost by the parser."""
    if app_text == view.text:
        return
    for f in fields:
        if f.status != MISSING:
            continue
        value = None
        if f.name == "email":
            m = RE_EMAIL.search(app_text)
            value = m.group(0) if m else None
        elif f.name == "phone":
            value = _phone(app_text)
        elif f.name == "name":
            found = _name(rows_of(split_sections(app_text), HEAD))
            value = found[0] if found else None
        elif f.name == "town":
            value = find_home(rows_of(split_sections(app_text), HEAD)[:25])
        if value:
            lost_from = next((x.kind for x in view.lost if value in x.text), "parser")
            f.status = LOST
            f.value = value
            f.evidence = EvidenceRef(SOURCE_CV, short(next((x for x in app_text.split("\n") if value in x), value)))
            f.evidence = EvidenceRef(SOURCE_CV, f.evidence.text + f"  [{lost_from}]")


def record_findings(record: RecordPreview) -> tuple[list[Finding], float | None]:
    """Findings of the round trip and the share (0..100) of fields filled correctly."""
    scored = [f for f in record.fields if f.status != FORM and f.name != "file"]
    if not scored:
        return [], None
    do = msg("rsim.record.do")
    labels = lambda fs: ", ".join(msg(f"rsim.record.field.{f.name}").default for f in fs)  # noqa: E731
    missing = [f for f in scored if f.status == MISSING]
    garbled = [f for f in scored if f.status == GARBLED]
    lost = [f for f in scored if f.status == LOST]
    share = 100.0 * sum(1 for f in scored if f.status == FOUND) / len(scored)
    per_field = RECORD_SHARE * 100.0 / len(scored)
    findings: list[Finding] = []

    def edits(fs) -> list[Edit]:
        return [Edit(f"record.{f.name}", msg(f"rsim.record.fix.{f.name}"), {COMPONENT_PARSE: per_field},
                     [f.evidence] if f.evidence else []) for f in fs]

    details = {"fields": [f.to_dict() for f in record.fields]}
    if lost:
        findings.append(Finding(CHECK_RECORD, FAIL, do, msg("rsim.record.lost", count=len(lost), fields=labels(lost)),
                                [f.evidence for f in lost if f.evidence], msg(f"rsim.record.fix.{lost[0].name}"),
                                "record.lost", edits(lost), details))
    if missing:
        status = FAIL if any(f.name in ("email", "phone", "name") for f in missing) else WARN
        findings.append(Finding(CHECK_RECORD, status, do,
                                msg("rsim.record.missing", count=len(missing), total=len(scored),
                                    fields=labels(missing)), [], msg(f"rsim.record.fix.{missing[0].name}"),
                                "record.missing", edits(missing), details))
    if garbled:
        findings.append(Finding(CHECK_RECORD, WARN, do, msg("rsim.record.garbled", count=len(garbled),
                                                            fields=labels(garbled)),
                                [f.evidence for f in garbled if f.evidence], msg(f"rsim.record.fix.{garbled[0].name}"),
                                "record.garbled", edits(garbled), details))
    if not findings:
        findings.append(Finding(CHECK_RECORD, OK, do, msg("rsim.record.ok", count=len(scored)),
                                [f.evidence for f in scored if f.evidence][:4], None, "record.ok", [], details))
    return findings, share
