"""Checks 6 and 7: the experience summary a parser computes (Textkernel-style
ExperienceSummary: months without double counting, months per employer, gaps,
no job in the last year, short stints) and, per skill, the months of the dated
jobs whose text names it and the date it was last used.

Everything is computed on the parsed text (`CvIndex` blocks), so what the
parser loses (dates in a dropped header, year-only dates) shows here too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..cv.sections import normalize
from .cvindex import TAXONOMY, CvIndex, ExperienceBlock, is_year_only, merged_months, month_text
from .match import RECENT_MONTHS, MatchResult, term_evidence
from .model import (CHECK_SKILL_RECENCY, CHECK_TIMELINE, COMPONENT_TIMELINE, FAIL, OK, SOURCE_CV, UNKNOWN,
                    WARN, Edit, EvidenceRef, Finding, msg)
from .parse_view import short

GAP_MONTHS = 6
NO_RECENT_MONTHS = 12
SHORT_STINT_MONTHS = 12
HOPPING_WINDOW_MONTHS = 60
HOPPING_MIN_SHORT = 2
HOPPING_AVERAGE_MONTHS = 18
HOPPING_MIN_EMPLOYERS = 3
PENALTY = {"no_dates": 100, "gap": 10, "gap_max": 30, "no_recent": 20, "hopping": 15, "year_only": 15}
MAX_LISTED = 8
_RE_INTERNSHIP = re.compile(
    r"\b(?:stage|stagista|tirocinio|tirocinante|intern|internship|trainee|apprendista|apprentice|"
    r"erasmus|summer job|lavoro estivo|stagionale|seasonal)\b", re.IGNORECASE)


@dataclass
class EmployerTenure:
    employer: str
    months: int
    blocks: list[ExperienceBlock]


def employers(blocks: list[ExperienceBlock]) -> list[EmployerTenure]:
    groups: dict[str, list[ExperienceBlock]] = {}
    for b in blocks:
        key = normalize(b.employer or b.title)
        groups.setdefault(key, []).append(b)
    return [EmployerTenure(bs[0].employer or bs[0].title, merged_months([(b.start, b.end) for b in bs]), bs)
            for bs in groups.values()]


def _evidence(block: ExperienceBlock) -> EvidenceRef:
    line = next((r for r in block.rows if block.period_text in r), block.rows[0] if block.rows else block.title)
    return EvidenceRef(SOURCE_CV, short(line))


def timeline_findings(index: CvIndex, profile) -> tuple[list[Finding], float | None, dict]:
    do = msg("rsim.timeline.do")
    blocks = index.blocks
    if not blocks:
        evidence = [EvidenceRef(SOURCE_CV, short(x)) for x in index.lines if re.search(r"(?:19|20)\d{2}", x)][:3]
        finding = Finding(CHECK_TIMELINE, FAIL, do, msg("rsim.timeline.no_dates"), evidence,
                          msg("rsim.timeline.no_dates.fix"), "timeline.no_dates",
                          [Edit("timeline.no_dates", msg("rsim.timeline.no_dates.fix"),
                                {COMPONENT_TIMELINE: PENALTY["no_dates"]}, evidence)])
        return [finding], 0.0, {}
    total = merged_months([(b.start, b.end) for b in blocks])
    tenures = employers(blocks)
    average = round(total / len(tenures), 1) if tenures else 0
    summary = {"months": total, "years": round(total / 12, 1), "employers": len(tenures), "average": average,
               "tenures": [{"employer": t.employer, "months": t.months} for t in tenures]}
    findings = [Finding(CHECK_TIMELINE, OK, do,
                        msg("rsim.timeline.summary", months=total, years=round(total / 12, 1),
                            employers=len(tenures), average=average),
                        [_evidence(b) for b in blocks[:4]], None, "timeline.summary", [], summary)]
    score = 100.0

    ordered = sorted(blocks, key=lambda b: (b.start, b.end))
    reach, reach_block = ordered[0].end, ordered[0]
    gap_penalty = 0
    for b in ordered[1:]:
        months = b.start - reach - 1
        if months > GAP_MONTHS:
            penalty = min(PENALTY["gap"], PENALTY["gap_max"] - gap_penalty)
            gap_penalty += penalty
            evidence = [_evidence(reach_block), _evidence(b)]
            findings.append(Finding(CHECK_TIMELINE, WARN, do,
                                    msg("rsim.timeline.gap", months=months, start=month_text(reach + 1),
                                        end=month_text(b.start - 1)), evidence, msg("rsim.timeline.gap.fix"),
                                    "timeline.gap", [Edit("timeline.gap", msg("rsim.timeline.gap.fix"),
                                                          {COMPONENT_TIMELINE: penalty}, evidence)]))
        if b.end >= reach:
            reach, reach_block = b.end, b
    score -= gap_penalty

    if not any(b.in_progress for b in blocks) and index.today_index - reach > NO_RECENT_MONTHS:
        evidence = [_evidence(reach_block)]
        findings.append(Finding(CHECK_TIMELINE, WARN, do, msg("rsim.timeline.no_recent", since=month_text(reach)),
                                evidence, msg("rsim.timeline.no_recent.fix"), "timeline.no_recent",
                                [Edit("timeline.no_recent", msg("rsim.timeline.no_recent.fix"),
                                      {COMPONENT_TIMELINE: PENALTY["no_recent"]}, evidence, conditional=True)]))
        score -= PENALTY["no_recent"]

    window = index.today_index - HOPPING_WINDOW_MONTHS
    short_jobs = [b for b in blocks if not b.in_progress and b.end >= window and b.months < SHORT_STINT_MONTHS
                  and not _RE_INTERNSHIP.search(" ".join(b.rows + [b.title]))]
    if len(short_jobs) >= HOPPING_MIN_SHORT or (len(tenures) >= HOPPING_MIN_EMPLOYERS
                                                 and average < HOPPING_AVERAGE_MONTHS):
        evidence = [_evidence(b) for b in short_jobs[:4]] or [_evidence(b) for b in blocks[:3]]
        findings.append(Finding(CHECK_TIMELINE, WARN, do,
                                msg("rsim.timeline.hopping", count=len(short_jobs), months=SHORT_STINT_MONTHS,
                                    years=HOPPING_WINDOW_MONTHS // 12, average=average), evidence,
                                msg("rsim.timeline.hopping.fix"), "timeline.hopping",
                                [Edit("timeline.hopping", msg("rsim.timeline.hopping.fix"),
                                      {COMPONENT_TIMELINE: PENALTY["hopping"] / 2}, evidence)]))
        score -= PENALTY["hopping"]

    year_only = [b for b in blocks if is_year_only(b.period_text)]
    if year_only:
        month_required = profile.parser.date_precision == "month_required"
        evidence = [_evidence(b) for b in year_only[:4]]
        edits = [Edit(f"timeline.year_only.{i}", msg("rsim.whatif.dates", line=e.text),
                      {COMPONENT_TIMELINE: PENALTY["year_only"] / len(year_only)}, [e])
                 for i, e in enumerate(evidence)] if month_required else []
        findings.append(Finding(CHECK_TIMELINE, WARN if month_required else OK, do,
                                msg("rsim.timeline.year_only", count=len(year_only)), evidence,
                                msg("rsim.timeline.year_only.fix"), "timeline.year_only", edits))
        if month_required:
            score -= PENALTY["year_only"]

    overlaps = [(a, b) for i, a in enumerate(ordered) for b in ordered[i + 1:]
                if min(a.end, b.end) - max(a.start, b.start) + 1 >= 2 and normalize(a.employer) != normalize(b.employer)]
    if overlaps:
        a, b = overlaps[0]
        findings.append(Finding(CHECK_TIMELINE, OK, do, msg("rsim.timeline.overlap", count=len(overlaps)),
                                [_evidence(a), _evidence(b)], None, "timeline.overlap"))
    return findings, max(0.0, score), summary


@dataclass
class SkillUse:
    skill: str
    months: int | None
    last_used: str | None
    dated: bool
    line: str | None
    required: bool = False

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def skill_uses(skill_names: list[str], index: CvIndex, required: set[str]) -> list[SkillUse]:
    result = []
    for name in skill_names:
        ev = term_evidence(name, index, TAXONOMY)
        result.append(SkillUse(name, ev.months, ev.last_used, ev.dated, ev.line, name.lower() in required))
    return result


def skill_recency_findings(skill_names: list[str], index: CvIndex, match: MatchResult | None,
                           match_gain=None) -> list[Finding]:
    do = msg("rsim.skill_recency.do")
    if not skill_names:
        return [Finding(CHECK_SKILL_RECENCY, UNKNOWN, do, msg("rsim.skill_recency.none"), [], None,
                        "skill_recency.none")]
    required_items = {i.text.lower(): i for i in (match.skill_items if match else []) if i.priority in ("must", "nice")}
    uses = skill_uses(skill_names, index, set(required_items))
    dated = [u for u in uses if u.dated]
    undated = [u for u in uses if not u.dated and u.line]
    details = {"skills": [u.to_dict() for u in uses]}
    status = WARN if undated and (len(undated) * 2 > len(uses) or any(u.required for u in undated)) else OK
    findings = [Finding(CHECK_SKILL_RECENCY, status, do,
                        msg("rsim.skill_recency.summary", dated=len(dated), total=len(uses), undated=len(undated)),
                        [EvidenceRef(SOURCE_CV, short(u.line)) for u in dated[:3] if u.line], None,
                        "skill_recency.summary", [], details)]
    if undated:
        undated.sort(key=lambda u: not u.required)
        edits = []
        for u in undated[:MAX_LISTED]:
            gain = match_gain(u.skill, dated=True) if (match_gain and u.required) else 0.0
            edits.append(Edit(f"skill_recency.undated.{u.skill}",
                              msg("rsim.whatif.skill_in_job", skill=u.skill, line=short(u.line or "", 80)),
                              {"match": gain} if gain else {}, [EvidenceRef(SOURCE_CV, short(u.line or ""))],
                              conditional=True))
        findings.append(Finding(CHECK_SKILL_RECENCY, WARN if any(u.required for u in undated) else OK, do,
                                msg("rsim.skill_recency.undated", skills=", ".join(u.skill for u in undated[:MAX_LISTED])),
                                [EvidenceRef(SOURCE_CV, short(u.line or "")) for u in undated[:4]],
                                msg("rsim.skill_recency.undated.fix", skill=undated[0].skill),
                                "skill_recency.undated", edits))
    stale = []
    if match is not None:
        for item in match.skill_items:
            if item.last_used and item.last_used != "present":
                year, month = (int(x) for x in item.last_used.split("-"))
                if index.today_index - (year * 12 + month - 1) > RECENT_MONTHS:
                    stale.append(item)
    if stale:
        findings.append(Finding(CHECK_SKILL_RECENCY, WARN, do,
                                msg("rsim.skill_recency.stale", skills=", ".join(i.text for i in stale)),
                                [EvidenceRef(SOURCE_CV, short(i.cv_line or "")) for i in stale[:4]], None,
                                "skill_recency.stale"))
    return findings
