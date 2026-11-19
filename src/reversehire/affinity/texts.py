"""User-visible texts of the affinity engine: i18n keys with English defaults.

Results carry ``Note`` objects (key + parameters), never rendered sentences, so the
display layer translates them with ``text(key, **params)``: the locale files first
(``i18n.t``), the English default below until a key is added there.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..i18n import t

FALLBACK: dict[str, str] = {
    # verdicts
    "affinity.verdict.APPLY": "Apply",
    "affinity.verdict.SPONTANEOUS": "Spontaneous application",
    "affinity.verdict.WATCH": "Keep an eye on it",
    "affinity.verdict.NOT_A_FIT": "Not a fit",
    "affinity.verdict.UNKNOWN": "Not enough data",
    "affinity.verdict_help.APPLY": "There is an open posting that fits you: send your CV for it.",
    "affinity.verdict_help.SPONTANEOUS": "No fitting posting now, but the company plausibly needs your profile: an open application makes sense.",
    "affinity.verdict_help.WATCH": "It fits you, but there is nothing open and the signals are weak or the data thin: check again later.",
    "affinity.verdict_help.NOT_A_FIT": "Low affinity or a requirement you cannot meet.",
    "affinity.verdict_help.UNKNOWN": "Too little is known about what the company looks for to give a score.",
    # components
    "affinity.component.skills": "Skills and technologies",
    "affinity.component.role": "Role",
    "affinity.component.sector": "Sector experience",
    "affinity.component.seniority": "Seniority",
    "affinity.component.constraints": "Hard requirements",
    "affinity.component.practical": "Practical fit",
    "affinity.component.opportunity": "Opportunity",
    "affinity.confidence.low": "low confidence",
    "affinity.confidence.medium": "medium confidence",
    "affinity.confidence.high": "high confidence",
    "affinity.status.not_enough_data": "not enough data",
    "affinity.headline": "Fits you as: {direction}, {score} % ({confidence})",
    "affinity.headline.general": "Affinity {score} % ({confidence})",
    "affinity.headline.none": "Affinity: not enough data",
    "affinity.direction.general": "your profile as a whole",
    "affinity.direction.other": "Also as {direction}: {score} %",
    # company types
    "affinity.type.machine_builder": "Machine builder (OEM)",
    "affinity.type.system_integrator": "System integrator",
    "affinity.type.manufacturer": "Manufacturer with its own technical staff",
    "affinity.type.software_house": "Software house / IT consultancy",
    "affinity.type.other": "Other",
    "affinity.type.line": "Company type: {type} ({confidence})",
    # origins
    "affinity.origin.explicit_must": "required in a posting",
    "affinity.origin.explicit_nice": "a plus in a posting",
    "affinity.origin.explicit_context": "named in a posting",
    "affinity.origin.stated": "named on their site",
    "affinity.origin.inferred_product": "inferred from their products (not a requirement)",
    "affinity.origin.inferred_type": "inferred from the company type (not a requirement)",
    # reasons
    "affinity.reason.posting_fits": "Open posting that fits you: \"{title}\" ({score} %).",
    "affinity.reason.posting_not_fit": "Open postings, but none fits you well (best: \"{title}\", {score} %).",
    "affinity.reason.plausible_need": "They plausibly need your profile: {why}.",
    "affinity.reason.no_plausible_need": "Nothing shows they need your profile.",
    "affinity.reason.low_affinity": "Affinity {score} % is below {threshold} %.",
    "affinity.reason.hard_fail": "A requirement you do not meet: {requirement}.",
    "affinity.reason.capped": "Score capped at {cap} %: {requirement}.",
    "affinity.reason.entry_level": "\"{title}\" is an internship or entry-level position: your experience is well beyond it.",
    "affinity.reason.too_far": "{distance} km away, beyond your limit of {limit} km.",
    "affinity.reason.type_excluded": "Company type \"{type}\" is excluded in your preferences.",
    "affinity.reason.outside_directions": "It matches none of your career directions.",
    "affinity.reason.low_confidence": "Little is known about the company ({confidence}).",
    "affinity.reason.not_enough_data": "Not enough data on what the company looks for (coverage {coverage} %).",
    "affinity.reason.affinity": "Affinity {score} % ({confidence}).",
    # hints for "not enough data"
    "affinity.hint.build_card": "Build the full company card (site, products, presentation).",
    "affinity.hint.paste_posting": "Paste one of their postings, if you have one.",
    "affinity.hint.posting_text": "Their postings have no readable text: open one and paste its description.",
    "affinity.hint.ateco": "Add the ATECO code from registroimprese.it (free search).",
    "affinity.hint.site": "Add the company's website to the card.",
    # component evidence
    "affinity.ev.skill_matched": "{skill}: {origin}; in your CV: \"{cv_line}\"",
    "affinity.ev.skill_partial": "{skill}: {origin}; something close in your CV: \"{cv_line}\"",
    "affinity.ev.skill_missing": "{skill}: {origin}; not found in your CV",
    "affinity.ev.role": "Your role \"{cv_role}\" vs \"{company_role}\" ({origin}, {score} %)",
    "affinity.ev.role_none": "No role of the company is close to yours",
    "affinity.ev.sector": "Your experience in {cv_sector} ({cv_evidence}) vs their sector {company_sector} ({via}, {score} %)",
    "affinity.ev.sector_ateco": "Their ATECO {code} is a category of interest for your profile ({why})",
    "affinity.ev.seniority_years": "They ask {asked} years, you have {cv} years",
    "affinity.ev.seniority_level": "They ask a {asked} profile, yours is {cv}",
    "affinity.ev.seniority_over": "You may be over-qualified: they ask {asked} years, you have {cv}",
    "affinity.ev.constraint": "{topic}: {status} - \"{posting_line}\"",
    "affinity.ev.distance": "{distance} km from you (informational: no limit set)",
    "affinity.ev.distance_limit": "{distance} km from you, limit {limit} km",
    "affinity.ev.contract": "Contract: {contract}",
    "affinity.ev.remote": "Work mode: {remote}",
    "affinity.ev.salary": "Salary: {salary}",
    # opportunity
    "affinity.opp.hiring": "Hiring now: {count} open postings",
    "affinity.opp.hiring_status": "Hiring signals on the site",
    "affinity.opp.for_you": "Hiring for your role: \"{title}\"",
    "affinity.opp.invitation": "They invite open applications",
    "affinity.opp.careers": "They have a careers page",
    "affinity.opp.revenue_growing": "Revenue growing ({change} % a year, {first}-{last})",
    "affinity.opp.revenue_stable": "Revenue stable ({first}-{last})",
    "affinity.opp.revenue_shrinking": "Revenue shrinking ({change} % a year, {first}-{last})",
    "affinity.opp.size": "About {employees} employees",
    "affinity.opp.level.low": "weak signals",
    "affinity.opp.level.medium": "some signals",
    "affinity.opp.level.high": "strong signals",
    # strengths, gaps, extras, talking points
    "affinity.strength": "You have {skill} ({origin}): \"{cv_line}\"",
    "affinity.strength.role": "Your role \"{cv_role}\" matches \"{company_role}\"",
    "affinity.gap.state_it": "{skill} is required (\"{posting_line}\"): if you have it, state it in your CV.",
    "affinity.gap.stated": "{skill} is named on their site: if you have it, state it.",
    "affinity.extra": "Not asked but in your CV: {skill}",
    "affinity.talk.skill": "Your experience with {skill} (\"{cv_line}\") meets what they say: \"{company_text}\"",
    "affinity.talk.role": "As {cv_role} you match their \"{company_role}\" opening",
    "affinity.talk.sector": "Your background in {sector} (\"{cv_evidence}\") is their field: \"{company_text}\"",
    # calibration
    "affinity.calibration.too_few": "Not enough judgements yet ({count} of {needed}): default weights kept.",
    "affinity.calibration.agrees": "Your judgements mostly agree with the verdicts: weights kept.",
    "affinity.calibration.updated": "Weights adjusted from {count} judgements: {changed} past verdicts would change.",
    # constraint topics and statuses
    "affinity.topic.language": "Language",
    "affinity.topic.degree": "Degree",
    "affinity.topic.years": "Years of experience",
    "affinity.topic.licence": "Driving licence",
    "affinity.topic.car": "Own car",
    "affinity.topic.travel": "Business travel",
    "affinity.topic.right_to_work": "Right to work",
    "affinity.topic.relocation": "Relocation",
    "affinity.topic.location": "Location",
    "affinity.topic.availability": "Availability",
    "affinity.topic.salary": "Salary",
    "affinity.topic.shifts": "Shifts",
    "affinity.topic.must_skill": "Must-have skill",
    "affinity.topic.distance": "Distance",
    "affinity.status.ok": "met",
    "affinity.status.warn": "partly met",
    "affinity.status.unknown": "unknown",
    "affinity.status.fail": "not met",
}


def _nested(params: dict, translate: bool) -> dict:
    """Parameters that are themselves keys of this table ("affinity.confidence.high") rendered."""
    out = {}
    for k, v in params.items():
        if isinstance(v, str) and v in FALLBACK:
            out[k] = text(v) if translate else FALLBACK[v]
        else:
            out[k] = v
    return out


def text(key: str, **params) -> str:
    """Translated text of a key (locale files first, then the English default)."""
    params = _nested(params, True)
    translated = t(key, **params)
    if translated != key:
        return translated
    default = FALLBACK.get(key)
    if default is None:
        return key
    try:
        return default.format(**params)
    except (KeyError, IndexError, ValueError):
        return default


def render_default(key: str, params: dict) -> str:
    default = FALLBACK.get(key, key)
    try:
        return default.format(**_nested(params, False))
    except (KeyError, IndexError, ValueError):
        return default


@dataclass
class Note:
    """A user-visible text: i18n key, parameters and the English default."""

    key: str
    params: dict = field(default_factory=dict)

    @property
    def default(self) -> str:
        return render_default(self.key, self.params)

    def render(self) -> str:
        return text(self.key, **self.params)

    def to_dict(self) -> dict:
        return {"key": self.key, "params": dict(self.params), "default": self.default}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Note | None":
        if not d:
            return None
        return cls(str(d["key"]), dict(d.get("params") or {}))


def note(key: str, **params) -> Note:
    return Note(key, params)
