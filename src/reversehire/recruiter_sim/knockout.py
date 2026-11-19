"""Check 3, knock-out questions inferred from the target, with the user's answers.

Sources of the questions: the typed requirements of `scoring.requirements`
(years, language, degree, licence, car, travel) and the must-have skills, plus
topics recruiters ask in screening forms (study §1.4): right to work, location /
commute, relocation, availability, salary expectation, shifts. Every question
quotes the posting line it comes from; the outcome uses the user's saved answer
first, then the CV evidence. Silence is never a failure: it is "unknown" with
the advice to save an answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..scoring.preferences import yearly_salary
from ..scoring.relevance import normalize_text
from .match import CONTRADICTED, MATCHED, MISSING, PARTIAL, UNCERTAIN, MatchResult
from .model import (CHECK_KNOCKOUT, FAIL, OK, SOURCE_ANSWER, SOURCE_CV, SOURCE_POSTING, UNKNOWN, WARN,
                    EvidenceRef, Finding, KnockoutAnswers, Message, Target, msg)
from .parse_view import short
from .topics import TOPIC_PATTERNS

MAX_SKILL_QUESTIONS = 3
IMMEDIATE_MAX_WEEKS = 4

_TOPIC_PATTERNS = TOPIC_PATTERNS
_TYPED = ("years", "language", "degree", "licence", "car", "travel")


@dataclass
class KnockoutQuestion:
    topic: str
    status: str  # OK | WARN | FAIL | UNKNOWN
    posting_line: str
    reason: Message | None = None
    cv_line: str | None = None
    answer: str | None = None
    hint_key: str = ""

    def to_dict(self) -> dict:
        return {"topic": self.topic, "status": self.status, "posting_line": self.posting_line,
                "reason": self.reason.to_dict() if self.reason else None, "cv_line": self.cv_line,
                "answer": self.answer}


@dataclass
class KnockoutOutcome:
    questions: list[KnockoutQuestion] = field(default_factory=list)

    @property
    def failed(self) -> list[KnockoutQuestion]:
        return [q for q in self.questions if q.status == FAIL]

    @property
    def passed_share(self) -> float:
        """Screening answers as a 0..100 score (ok 1, warn/unknown 0.5, fail 0)."""
        if not self.questions:
            return 100.0
        value = {OK: 1.0, WARN: 0.5, UNKNOWN: 0.5, FAIL: 0.0}
        return 100.0 * sum(value[q.status] for q in self.questions) / len(self.questions)


def _sentences(text: str) -> list[str]:
    return [" ".join(s.split()) for s in re.split(r"(?<=[.!?;])\s+|\n+", text or "") if s.strip()]


def _line_for(pattern: re.Pattern, sentences: list[str]) -> str | None:
    return next((s for s in sentences if pattern.search(normalize_text(s))), None)


def _from_match(item, answers: KnockoutAnswers) -> KnockoutQuestion:
    """A typed requirement of the match turned into a screening question."""
    topic = item.kind
    status = {MATCHED: OK, PARTIAL: WARN, MISSING: WARN, UNCERTAIN: UNKNOWN, CONTRADICTED: FAIL}[item.status]
    reason = None
    answer = None
    if item.status == CONTRADICTED and item.priority != "must":
        status = WARN
    if topic == "licence" and answers.driving_licence is not None:
        held = {c.upper() for c in answers.driving_licence}
        answer = ", ".join(sorted(held)) or "none"
        status = OK if held else FAIL
        reason = None if held else msg("rsim.knockout.reason.answer_no")
    elif topic == "car" and answers.own_car is not None:
        answer = "yes" if answers.own_car else "no"
        status = OK if answers.own_car else FAIL
        reason = None if answers.own_car else msg("rsim.knockout.reason.answer_no")
    elif topic == "travel" and answers.max_travel_days is not None and item.status in (UNCERTAIN, PARTIAL):
        answer = f"{answers.max_travel_days} days/year"
        status = FAIL if answers.max_travel_days == 0 and item.priority == "must" else OK
    if status == FAIL and reason is None:
        reason = msg("rsim.knockout.reason.cv_contradicts", cv=short(item.cv_line or "", 80))
    elif status == UNKNOWN:
        reason = msg("rsim.knockout.reason.unanswered")
    if status == FAIL and item.priority != "must":
        status = WARN
    return KnockoutQuestion(topic, status, item.posting_line, reason, item.cv_line, answer, f"rsim.knockout.hint.{topic}")


def infer_knockouts(target: Target, match: MatchResult | None, answers: KnockoutAnswers | None,
                    cv_profile=None) -> KnockoutOutcome:
    answers = answers or KnockoutAnswers()
    outcome = KnockoutOutcome()
    sentences = _sentences(target.requirement_text())
    if match is not None:
        for item in match.items:
            if item.kind in _TYPED and item.priority in ("must", "nice"):
                outcome.questions.append(_from_match(item, answers))
        musts = [i for i in match.items if i.is_skill and i.priority == "must"]
        musts.sort(key=lambda i: (i.status == MATCHED, -i.weight))
        for item in musts[:MAX_SKILL_QUESTIONS]:
            if item.status == MATCHED:
                status, reason = OK, None
            elif item.status == PARTIAL:
                status, reason = WARN, msg("rsim.knockout.reason.missing_skill", skill=item.text)
            else:
                status, reason = WARN, msg("rsim.knockout.reason.missing_skill", skill=item.text)
            outcome.questions.append(KnockoutQuestion("must_skill", status, item.posting_line, reason,
                                                      item.cv_line, None, "rsim.knockout.hint.must_skill"))

    line = _line_for(_TOPIC_PATTERNS["right_to_work"], sentences)
    if line:
        if answers.right_to_work is None:
            q = KnockoutQuestion("right_to_work", UNKNOWN, line, msg("rsim.knockout.reason.unanswered"))
        elif answers.right_to_work:
            q = KnockoutQuestion("right_to_work", OK, line, answer="yes")
        else:
            sponsored = "sponsorship" in normalize_text(line) and answers.needs_sponsorship
            q = KnockoutQuestion("right_to_work", WARN if sponsored else FAIL, line,
                                 msg("rsim.knockout.reason.answer_no"), answer="no")
        q.hint_key = "rsim.knockout.hint.right_to_work"
        outcome.questions.append(q)

    line = _line_for(_TOPIC_PATTERNS["relocation"], sentences)
    if line:
        mobility = getattr(cv_profile, "mobility", None)
        cv_value = getattr(mobility, "relocation", "unknown") if mobility else "unknown"
        cv_line = next((e.text for e in getattr(mobility, "relocation_evidence", []) or []), None)
        if answers.willing_to_relocate is not None:
            ok = answers.willing_to_relocate
            q = KnockoutQuestion("relocation", OK if ok else FAIL, line,
                                 None if ok else msg("rsim.knockout.reason.answer_no"), cv_line,
                                 "yes" if ok else "no")
        elif cv_value == "excluded":
            q = KnockoutQuestion("relocation", FAIL, line,
                                 msg("rsim.knockout.reason.cv_contradicts", cv=short(cv_line or "", 80)), cv_line)
        elif cv_value == "declared":
            q = KnockoutQuestion("relocation", OK, line, None, cv_line)
        else:
            q = KnockoutQuestion("relocation", UNKNOWN, line, msg("rsim.knockout.reason.unanswered"))
        q.hint_key = "rsim.knockout.hint.relocation"
        outcome.questions.append(q)

    line = _line_for(_TOPIC_PATTERNS["location"], sentences) or (target.location or None)
    if line and (target.distance_km is not None or _TOPIC_PATTERNS["location"].search(normalize_text(line))):
        home = getattr(cv_profile, "home", None)
        if target.distance_km is not None and answers.max_commute_km is not None:
            near = target.distance_km <= answers.max_commute_km
            q = KnockoutQuestion("location", OK if near else FAIL, line,
                                 None if near else msg("rsim.knockout.reason.too_far",
                                                       distance=round(target.distance_km),
                                                       limit=round(answers.max_commute_km)),
                                 home, f"{answers.max_commute_km:g} km")
        else:
            q = KnockoutQuestion("location", UNKNOWN, line, msg("rsim.knockout.reason.unanswered"), home)
        q.hint_key = "rsim.knockout.hint.location"
        outcome.questions.append(q)

    line = _line_for(_TOPIC_PATTERNS["availability"], sentences)
    if line:
        immediate = re.search(r"immediat|da subito|asap|inizio immediato", normalize_text(line))
        if answers.notice_weeks is None:
            q = KnockoutQuestion("availability", UNKNOWN, line, msg("rsim.knockout.reason.unanswered"))
        elif immediate and answers.notice_weeks > IMMEDIATE_MAX_WEEKS:
            q = KnockoutQuestion("availability", WARN, line,
                                 msg("rsim.knockout.reason.notice", weeks=answers.notice_weeks),
                                 answer=f"{answers.notice_weeks} weeks")
        else:
            q = KnockoutQuestion("availability", OK, line, answer=f"{answers.notice_weeks} weeks")
        q.hint_key = "rsim.knockout.hint.availability"
        outcome.questions.append(q)

    posting = target.posting
    published = yearly_salary(posting) if posting is not None else None
    line = _line_for(_TOPIC_PATTERNS["salary"], sentences) or (getattr(posting, "salary", None) if published else None)
    if line:
        if answers.min_salary_year is not None and published:
            ok = answers.min_salary_year <= published
            q = KnockoutQuestion("salary", OK if ok else FAIL, line,
                                 None if ok else msg("rsim.knockout.reason.salary",
                                                     minimum=round(answers.min_salary_year),
                                                     maximum=round(published)),
                                 answer=f"{answers.min_salary_year:g}")
        else:
            q = KnockoutQuestion("salary", UNKNOWN, line, msg("rsim.knockout.reason.unanswered"))
        q.hint_key = "rsim.knockout.hint.salary"
        outcome.questions.append(q)

    line = _line_for(_TOPIC_PATTERNS["shifts"], sentences)
    if line:
        if answers.shifts_ok is None:
            q = KnockoutQuestion("shifts", UNKNOWN, line, msg("rsim.knockout.reason.unanswered"))
        else:
            q = KnockoutQuestion("shifts", OK if answers.shifts_ok else FAIL, line,
                                 None if answers.shifts_ok else msg("rsim.knockout.reason.answer_no"),
                                 answer="yes" if answers.shifts_ok else "no")
        q.hint_key = "rsim.knockout.hint.shifts"
        outcome.questions.append(q)
    return outcome


def knockout_findings(outcome: KnockoutOutcome, profile) -> list[Finding]:
    do = msg("rsim.knockout.do")
    mode = msg(f"rsim.knockout.mode.{profile.knockout.mode}")
    details = {"questions": [q.to_dict() for q in outcome.questions], "mode": mode.to_dict()}
    if not outcome.questions:
        return [Finding(CHECK_KNOCKOUT, OK, do, msg("rsim.knockout.none"), [], None, "knockout.none", [], details)]
    findings: list[Finding] = []
    for q in outcome.failed:
        evidence = [EvidenceRef(SOURCE_POSTING, short(q.posting_line))]
        if q.cv_line:
            evidence.append(EvidenceRef(SOURCE_CV, short(q.cv_line)))
        if q.answer:
            evidence.append(EvidenceRef(SOURCE_ANSWER, q.answer))
        topic = msg(f"rsim.knockout.topic.{q.topic}").default
        findings.append(Finding(CHECK_KNOCKOUT, FAIL, do,
                                msg("rsim.knockout.fail", topic=topic, reason=q.reason.default if q.reason else ""),
                                evidence, msg("rsim.knockout.fix.fail"), f"knockout.fail.{q.topic}", [], details))
    open_questions = [q for q in outcome.questions if q.status in (WARN, UNKNOWN)]
    summary_status = FAIL if outcome.failed else (WARN if open_questions else OK)
    evidence = [EvidenceRef(SOURCE_POSTING, short(q.posting_line)) for q in open_questions[:4]]
    fix = None
    if open_questions:
        q = open_questions[0]
        fix = (msg("rsim.knockout.fix.answer") if q.status == UNKNOWN
               else msg("rsim.knockout.fix.state", hint=msg(q.hint_key).default if q.hint_key else ""))
    findings.insert(0, Finding(CHECK_KNOCKOUT, summary_status, do,
                               msg("rsim.knockout.summary", count=len(outcome.questions),
                                   failed=len(outcome.failed), open=len(open_questions)),
                               evidence, fix, "knockout.summary", [], details))
    return findings
