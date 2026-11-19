"""Checks 5 (match with the role), 8 (languages, education, certifications),
9 (format notes for the system) and 10 (generated-text estimate, taken as input).
"""

from __future__ import annotations

import re

from ..cv.sections import rows_of, split_sections
from ..scoring.fit import _level_from_text
from ..scoring.relevance import normalize_text
from .cvindex import TAXONOMY, CvIndex
from .match import MatchResult, term_evidence
from .model import (CHECK_AI_TEXT, CHECK_CREDENTIALS, CHECK_FORMAT, CHECK_SEMANTIC, COMPONENT_CREDENTIALS,
                    COMPONENT_MATCH, FAIL, OK, SOURCE_CV, SOURCE_FILE, SOURCE_POSTING, SOURCE_PROFILE, UNKNOWN,
                    WARN, Edit, EvidenceRef, Finding, Target, msg)
from .parse_view import short

GRADES = ((0.75, "A"), (0.55, "B"), (0.35, "C"))
MAX_LISTED = 6
CREDENTIAL_PENALTY = {"no_languages": 30, "no_level": 20, "no_degree": 30, "cert": 15, "cert_max": 30}
AI_HUMAN_BELOW = 0.35  # whole band below: ordinary human writing
AI_GENERATED_ABOVE = 0.5  # whole band above: resembles generated text
_NATIVE = re.compile(r"madrelingua|native|mother tongue|lingua madre|bilingu|рідн", re.IGNORECASE)
_CERTS = re.compile(
    r"\b(?:PMP|PRINCE2|ITIL(?:\s?v?\d)?|CISSP|CISA|CISM|CCNA|CCNP|CCIE|AWS Certified[\w ]{0,30}|Azure[\w ]{0,20}"
    r"Certified|Microsoft Certified[\w: ]{0,30}|ISO\s?\d{4,5}(?::\d{4})?|Six Sigma(?: \w+ Belt)?|Scrum Master|PSM\s?I*"
    r"|CSM|ECDL|ICDL|IELTS|TOEFL|Cambridge (?:FCE|CAE|CPE|First|Advanced)|PES|PAV|PEI|F-?gas|CFA|ACCA"
    r"|patentino[\w ]{0,25}|abilitazione[\w ]{0,30})\b")


def grade(ad_fit: float) -> str:
    for threshold, letter in GRADES:
        if ad_fit >= threshold:
            return letter
    return "D"


def semantic_findings(match: MatchResult | None, semantic_used: bool, match_gain=None,
                      index: CvIndex | None = None) -> list[Finding]:
    do = msg("rsim.semantic.do")
    if match is None or not match.items:
        return [Finding(CHECK_SEMANTIC, UNKNOWN, do, msg("rsim.semantic.none"), [], None, "semantic.none")]
    letter = grade(match.ad_fit)
    musts = match.musts
    met = sum(1 for i in musts if i.status == "matched")
    if semantic_used:
        found = msg("rsim.semantic.grade", grade=letter, met=met, total=len(musts),
                    similarity=round(match.ad_fit * 100))
    else:
        found = msg("rsim.semantic.grade_lexical", grade=letter, met=met, total=len(musts))
    status = OK if letter in ("A", "B") else WARN if letter == "C" else FAIL
    evidence = [EvidenceRef(SOURCE_CV, short(i.cv_line)) for i in musts if i.status == "matched" and i.cv_line][:3]
    details = {"grade": letter, "items": [i.to_dict() for i in match.items]}
    findings = [Finding(CHECK_SEMANTIC, status, do, found, evidence,
                        msg("rsim.semantic.fix") if status != OK else None, "semantic.grade", [], details)]
    missing = [i for i in musts if i.status in ("missing", "partial", "uncertain") and i.kind in ("skill", "duty")]
    if missing:
        edits = []
        for i in missing:
            gain = match_gain(i.text) if match_gain else 0.0
            if index is not None and term_evidence(i.text, index, TAXONOMY).level >= 3:
                continue  # the CV shows it under another wording: the synonym edit covers it
            if i.is_skill:
                edits.append(Edit(f"semantic.missing.{i.text}", msg("rsim.whatif.missing_skill", skill=i.text),
                                  {COMPONENT_MATCH: gain} if gain else {},
                                  [EvidenceRef(SOURCE_POSTING, short(i.posting_line))], conditional=True))
        findings.append(Finding(CHECK_SEMANTIC, WARN, do,
                                msg("rsim.semantic.missing", items=", ".join(short(i.text, 40) for i in missing[:MAX_LISTED])),
                                [EvidenceRef(SOURCE_POSTING, short(i.posting_line)) for i in missing[:4]],
                                msg("rsim.semantic.fix"), "semantic.missing", edits))
    return findings


# --- credentials ---------------------------------------------------------------------


def _has_level(value: str) -> bool:
    return bool(_level_from_text(normalize_text(value)) or _NATIVE.search(value))


def credential_findings(parsed_profile, index: CvIndex, target: Target) -> tuple[list[Finding], float]:
    do = msg("rsim.credentials.do")
    findings: list[Finding] = []
    score = 100.0
    languages = list(getattr(parsed_profile, "spoken_languages", None) or [])
    if not languages:
        edit = Edit("credentials.no_languages", msg("rsim.credentials.no_languages.fix"),
                    {COMPONENT_CREDENTIALS: CREDENTIAL_PENALTY["no_languages"]}, [], conditional=True)
        findings.append(Finding(CHECK_CREDENTIALS, WARN, do, msg("rsim.credentials.no_languages"), [],
                                msg("rsim.credentials.no_languages.fix"), "credentials.no_languages", [edit]))
        score -= CREDENTIAL_PENALTY["no_languages"]
    else:
        with_level = [i for i in languages if any(_has_level(t) for t in (i.value, *(e.text for e in i.evidence)))]
        without = [i for i in languages if i not in with_level]
        if with_level:
            findings.append(Finding(CHECK_CREDENTIALS, OK, do,
                                    msg("rsim.credentials.languages_ok", count=len(with_level),
                                        items=", ".join(i.value for i in with_level)),
                                    [EvidenceRef(SOURCE_CV, short(i.evidence[0].text)) for i in with_level
                                     if i.evidence][:3], None, "credentials.languages"))
        if without:
            penalty = CREDENTIAL_PENALTY["no_level"]
            edits = [Edit(f"credentials.level.{i.value}",
                          msg("rsim.whatif.language_level", line=short(i.evidence[0].text if i.evidence else i.value, 80)),
                          {COMPONENT_CREDENTIALS: penalty / len(without)},
                          [EvidenceRef(SOURCE_CV, short(i.evidence[0].text))] if i.evidence else [])
                     for i in without]
            findings.append(Finding(CHECK_CREDENTIALS, WARN, do,
                                    msg("rsim.credentials.language_no_level", items=", ".join(i.value for i in without)),
                                    [e for x in edits for e in x.evidence], msg("rsim.credentials.language_no_level.fix"),
                                    "credentials.language_level", edits))
            score -= penalty
    degrees = list(getattr(parsed_profile, "degrees", None) or [])
    if degrees:
        findings.append(Finding(CHECK_CREDENTIALS, OK, do,
                                msg("rsim.credentials.degree_ok", items=", ".join(d.value for d in degrees[:3])),
                                [EvidenceRef(SOURCE_CV, short(d.evidence[0].text)) for d in degrees[:2] if d.evidence],
                                None, "credentials.degree"))
    else:
        edit = Edit("credentials.no_degree", msg("rsim.credentials.no_degree.fix"),
                    {COMPONENT_CREDENTIALS: CREDENTIAL_PENALTY["no_degree"]}, [], conditional=True)
        findings.append(Finding(CHECK_CREDENTIALS, WARN, do, msg("rsim.credentials.no_degree"), [],
                                msg("rsim.credentials.no_degree.fix"), "credentials.no_degree", [edit]))
        score -= CREDENTIAL_PENALTY["no_degree"]

    cv_certs = rows_of(split_sections(index.text()), "certifications")
    asked = []
    for m in _CERTS.finditer(target.requirement_text()):
        name = " ".join(m.group(0).split())
        if name.lower() not in (a.lower() for a in asked):
            asked.append(name)
    missing = [c for c in asked if not index.found(c, TAXONOMY)]
    if cv_certs:
        findings.append(Finding(CHECK_CREDENTIALS, OK, do,
                                msg("rsim.credentials.certs_ok", items=short(", ".join(cv_certs), 100)),
                                [EvidenceRef(SOURCE_CV, short(c)) for c in cv_certs[:3]], None, "credentials.certs"))
    elif not asked:
        findings.append(Finding(CHECK_CREDENTIALS, OK, do, msg("rsim.credentials.no_certs"), [], None,
                                "credentials.no_certs"))
    if missing:
        penalty = min(CREDENTIAL_PENALTY["cert_max"], CREDENTIAL_PENALTY["cert"] * len(missing))
        edits = [Edit(f"credentials.cert.{c}", msg("rsim.credentials.cert_missing.fix", item=c),
                      {COMPONENT_CREDENTIALS: penalty / len(missing)}, [], conditional=True) for c in missing]
        line = next((s for s in target.requirement_text().split("\n") if missing[0] in s), missing[0])
        findings.append(Finding(CHECK_CREDENTIALS, WARN, do,
                                msg("rsim.credentials.cert_missing", items=", ".join(missing)),
                                [EvidenceRef(SOURCE_POSTING, short(line))],
                                msg("rsim.credentials.cert_missing.fix", item=missing[0]),
                                "credentials.cert_missing", edits))
        score -= penalty
    return findings, max(0.0, score)


# --- format -----------------------------------------------------------------------------


def format_findings(cv_path: str | None, profile, layout_pages: int | None) -> list[Finding]:
    do = msg("rsim.format.do")
    types = list(profile.parser.file_types)
    findings: list[Finding] = []
    source = EvidenceRef(SOURCE_PROFILE, profile.name)
    ext = (cv_path or "").rsplit(".", 1)[-1].lower() if cv_path and "." in cv_path else ""
    if not ext:
        findings.append(Finding(CHECK_FORMAT, UNKNOWN, do, msg("rsim.format.type_unknown", types=", ".join(types)),
                                [source], None, "format.type"))
    elif ext in types:
        findings.append(Finding(CHECK_FORMAT, OK, do, msg("rsim.format.type_ok", ext=ext),
                                [EvidenceRef(SOURCE_FILE, short(cv_path or ""))], None, "format.type"))
    else:
        preferred = "PDF" if "pdf" in types else types[0].upper()
        findings.append(Finding(CHECK_FORMAT, FAIL, do, msg("rsim.format.type_bad", ext=ext, types=", ".join(types)),
                                [EvidenceRef(SOURCE_FILE, short(cv_path or ""))],
                                msg("rsim.format.type_bad.fix", preferred=preferred), "format.type"))
    if profile.dedup:
        findings.append(Finding(CHECK_FORMAT, OK, do, msg("rsim.format.dedup"), [source], None, "format.dedup"))
    if profile.blind:
        findings.append(Finding(CHECK_FORMAT, OK, do, msg("rsim.format.blind"), [source], None, "format.blind"))
    if profile.form_fields:
        findings.append(Finding(CHECK_FORMAT, OK, do, msg("rsim.format.form", fields=", ".join(profile.form_fields)),
                                [source], None, "format.form", [], {"form_fields": list(profile.form_fields)}))
    for i, note in enumerate(profile.notes):
        findings.append(Finding(CHECK_FORMAT, OK, do, note, [source], None, f"format.note.{i}"))
    if profile.retention is not None:
        findings.append(Finding(CHECK_FORMAT, OK, do, profile.retention, [source], None, "format.retention"))
    return findings


# --- AI-text estimate (input only) -------------------------------------------------------


def ai_findings(estimate) -> list[Finding]:
    """The caller's estimate (`cv.ai_estimate.AiEstimate` or any object with the same
    attributes) shown as a band; never a verdict, never computed here."""
    do = msg("rsim.ai_text.do")
    if estimate is None:
        return [Finding(CHECK_AI_TEXT, UNKNOWN, do, msg("rsim.ai_text.not_given"), [], None, "ai_text.none")]
    words = int(getattr(estimate, "words", 0) or 0)
    if not getattr(estimate, "reliable", True):
        return [Finding(CHECK_AI_TEXT, UNKNOWN, do, msg("rsim.ai_text.short", words=words), [], None, "ai_text.short")]
    low = float(getattr(estimate, "low", 0.0))
    high = float(getattr(estimate, "high", 1.0))
    params = {"low": round(low * 100), "high": round(high * 100)}
    sentences = []
    for signal in getattr(estimate, "signals", None) or []:
        for s in getattr(signal, "sentences", None) or []:
            text = getattr(s, "text", None) or (s if isinstance(s, str) else None)
            if text and text not in sentences:
                sentences.append(text)
    evidence = [EvidenceRef(SOURCE_CV, short(s)) for s in sentences[:4]]
    details = {"probability": float(getattr(estimate, "probability", 0.0)), "low": low, "high": high,
               "backend": getattr(estimate, "backend", ""), "caption_key": getattr(estimate, "caption_key", "")}
    if high < AI_HUMAN_BELOW:
        return [Finding(CHECK_AI_TEXT, OK, do, msg("rsim.ai_text.human", **params), [], None, "ai_text.band", [],
                        details)]
    if low > AI_GENERATED_ABOVE:
        return [Finding(CHECK_AI_TEXT, WARN, do, msg("rsim.ai_text.generated", **params), evidence,
                        msg("rsim.ai_text.fix"), "ai_text.band", [], details)]
    return [Finding(CHECK_AI_TEXT, UNKNOWN, do, msg("rsim.ai_text.inconclusive", **params), evidence,
                    msg("rsim.ai_text.fix") if evidence else None, "ai_text.band", [], details)]
