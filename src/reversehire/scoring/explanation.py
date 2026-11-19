"""Human-readable reasons and gaps (Italian/English) for an Assessment.

Lines of the CV quoted as evidence always have the form `` — CV: «…»``
(Italian) or `` — CV: “…”`` (English), so that ``strip_cv_quotes`` can remove
them where CV text must not leave the PC (MCP without personal data).
"""

from __future__ import annotations

import re

from ..models import Evidence, HiringStatus
from .fit import NOT_MET, UNVERIFIABLE, SATISFIED
from .relevance import sector_name

MAX_SENTENCE_CHARS = 240
MAX_LISTED_TERMS = 6
MAX_EXPLAINED_POSTINGS = 3  # best postings whose gaps are listed
MAX_REQUIREMENT_REASONS = 6  # met requirements (with CV quote) listed for the best posting
MAX_REQUIREMENT_GAPS = 8
MAX_QUOTE_CHARS = 140

TEXTS: dict[str, dict[str, str]] = {
    "it": {
        "site_sentence": "Testo del sito più vicino al profilo: «{sentence}»",
        "sector": "Categoria OpenStreetMap coerente con il settore «{sector}» ({tag})",
        "sector_name": "Nome o categoria dell'azienda coerenti con il settore «{sector}»",
        "terms": "Elementi del CV citati dal sito: {terms}",
        "posting": "Annuncio più adatto: «{title}» ({score:.0f}/100)",
        "posting_role": "Il titolo dell'annuncio «{title}» è vicino al ruolo del CV «{role}»",
        "posting_skills": "Competenze del CV presenti nell'annuncio «{title}»: {terms}",
        "req_ok": "Requisito soddisfatto in «{title}»: {requirement}",
        "req_not_met": "«{title}»: requisito non soddisfatto: {requirement}{cv}",
        "req_not_met_preferred": "«{title}»: requisito preferenziale non soddisfatto: {requirement}{cv}",
        "req_unverifiable": "«{title}»: requisito da verificare (non ricavabile dal CV): {requirement}",
        "years": "almeno {years} anni di esperienza",
        "language": "{language_name} livello {level}",
        "degree": "{title}",
        "cv_years": " (CV: {cv} anni)",
        "cv_language": " (CV: {cv})",
        "cv_language_missing": " (lingua non presente nel CV)",
        "cv_degree": " (CV: {cv})",
        "without_site": "Sito non disponibile: valutazione basata solo su nome e categoria OpenStreetMap",
        "unreachable": "Sito non raggiungibile: informazioni sull'azienda incomplete",
        "no_text": "Testo descrittivo del sito assente o troppo breve",
        "no_postings": "Nessun annuncio trovato: idoneità a un annuncio non calcolabile (vedi la candidatura spontanea)",
        "no_sectors": "Nessun settore indicato nel profilo: coerenza di categoria non valutata",
        "no_roles": "Nessun ruolo nel profilo: vicinanza dei titoli degli annunci non valutata",
        "cv_quote": " — CV: «{cv}»",
        "must_summary": "«{title}»: requisiti indispensabili soddisfatti {met} su {total}",
        "check_met": "«{title}»: requisito {priority}«{requirement}» soddisfatto{cv}",
        "check_partial": "«{title}»: requisito {priority}«{requirement}» soddisfatto in parte{cv}",
        "check_weak": "«{title}»: requisito {priority}«{requirement}» poco coperto dal CV{cv}",
        "check_missing": "«{title}»: requisito {priority}«{requirement}» non trovato nel CV (da verificare)",
        "check_contradicted": "«{title}»: requisito {priority}«{requirement}» in contrasto con il CV{cv}",
        "blocked": "«{title}»: bloccato dal requisito indispensabile «{requirement}»{cv}",
        "priority_must": "indispensabile ",
        "priority_nice": "preferenziale ",
        "priority_context": "",
        "role_coverage": "Competenze essenziali ESCO del ruolo citate nell'annuncio «{title}»: {terms}",
        "role_coverage_site": "Competenze essenziali ESCO del ruolo citate dal sito: {terms}",
        "pref_permanent": "«{title}»: contratto a tempo indeterminato",
        "pref_permanent_possible": "«{title}»: possibilità di tempo indeterminato",
        "pref_not_permanent": "«{title}»: contratto non a tempo indeterminato ({contract})",
        "pref_remote": "«{title}»: lavoro da remoto",
        "pref_hybrid": "«{title}»: lavoro ibrido (in parte da remoto)",
        "pref_salary": "«{title}»: retribuzione pubblicata: {salary}",
        "pref_salary_ok": "«{title}»: retribuzione pubblicata ({salary}) pari o superiore al minimo indicato ({minimum})",
        "pref_salary_low": "«{title}»: retribuzione pubblicata ({salary}) sotto il minimo indicato ({minimum})",
        "pref_distance": "Azienda vicina: {km:.1f} km (raggio di ricerca {radius:g} km)",
        "pref_travel_over": "«{title}»: trasferte oltre quanto accettato{days}",
        "travel_days": " (accettati {days} giorni l'anno)",
        "spontaneous": "Candidatura spontanea: {score:.0f}/100 ({why})",
        "sp_relevant": "azienda pertinente al profilo",
        "sp_roles": "ruoli del CV citati sul sito: {roles}",
        "sp_careers": "pagina «Lavora con noi» presente",
        "sp_invitation": "il sito invita a inviare il CV",
        "sp_size": "indizi di azienda strutturata",
        "sp_none": "nessun segnale particolare",
        "contract_permanent": "tempo indeterminato",
        "contract_permanent_possible": "possibilità di tempo indeterminato",
        "contract_fixed_term": "tempo determinato",
        "contract_internship": "stage/tirocinio",
        "contract_apprenticeship": "apprendistato",
        "contract_freelance": "collaborazione/partita IVA",
        "contract_temporary_agency": "somministrazione",
    },
    "en": {
        "site_sentence": "Website text closest to the profile: “{sentence}”",
        "sector": "OpenStreetMap category consistent with the “{sector}” sector ({tag})",
        "sector_name": "Company name or category consistent with the “{sector}” sector",
        "terms": "CV items mentioned on the website: {terms}",
        "posting": "Best matching job posting: “{title}” ({score:.0f}/100)",
        "posting_role": "The posting title “{title}” is close to the CV role “{role}”",
        "posting_skills": "CV skills found in the posting “{title}”: {terms}",
        "req_ok": "Requirement met in “{title}”: {requirement}",
        "req_not_met": "“{title}”: requirement not met: {requirement}{cv}",
        "req_not_met_preferred": "“{title}”: preferred requirement not met: {requirement}{cv}",
        "req_unverifiable": "“{title}”: requirement to check (not found in the CV): {requirement}",
        "years": "at least {years} years of experience",
        "language": "{language_name} level {level}",
        "degree": "{title}",
        "cv_years": " (CV: {cv} years)",
        "cv_language": " (CV: {cv})",
        "cv_language_missing": " (language not in the CV)",
        "cv_degree": " (CV: {cv})",
        "without_site": "No website: score based only on name and OpenStreetMap category",
        "unreachable": "Website unreachable: company information is incomplete",
        "no_text": "Descriptive website text missing or too short",
        "no_postings": "No job postings found: suitability for a posting cannot be computed (see the spontaneous application)",
        "no_sectors": "No sector in the profile: category consistency not assessed",
        "no_roles": "No role in the profile: posting titles not compared",
        "cv_quote": " — CV: “{cv}”",
        "must_summary": "“{title}”: must-have requirements met {met} of {total}",
        "check_met": "“{title}”: {priority}requirement “{requirement}” met{cv}",
        "check_partial": "“{title}”: {priority}requirement “{requirement}” partly met{cv}",
        "check_weak": "“{title}”: {priority}requirement “{requirement}” weakly covered by the CV{cv}",
        "check_missing": "“{title}”: {priority}requirement “{requirement}” not found in the CV (to check)",
        "check_contradicted": "“{title}”: {priority}requirement “{requirement}” contradicted by the CV{cv}",
        "blocked": "“{title}”: blocked by the must-have requirement “{requirement}”{cv}",
        "priority_must": "must-have ",
        "priority_nice": "preferred ",
        "priority_context": "",
        "role_coverage": "ESCO essential skills of the role named in the posting “{title}”: {terms}",
        "role_coverage_site": "ESCO essential skills of the role named on the website: {terms}",
        "pref_permanent": "“{title}”: permanent contract",
        "pref_permanent_possible": "“{title}”: possibility of a permanent contract",
        "pref_not_permanent": "“{title}”: not a permanent contract ({contract})",
        "pref_remote": "“{title}”: remote work",
        "pref_hybrid": "“{title}”: hybrid work (partly remote)",
        "pref_salary": "“{title}”: salary published: {salary}",
        "pref_salary_ok": "“{title}”: published salary ({salary}) at or above your minimum ({minimum})",
        "pref_salary_low": "“{title}”: published salary ({salary}) below your minimum ({minimum})",
        "pref_distance": "Nearby company: {km:.1f} km (search radius {radius:g} km)",
        "pref_travel_over": "“{title}”: more business travel than you accept{days}",
        "travel_days": " ({days} days a year accepted)",
        "spontaneous": "Spontaneous application: {score:.0f}/100 ({why})",
        "sp_relevant": "company relevant to the profile",
        "sp_roles": "CV roles named on the website: {roles}",
        "sp_careers": "careers page present",
        "sp_invitation": "the website invites you to send your CV",
        "sp_size": "hints of a structured company",
        "sp_none": "no particular signal",
        "contract_permanent": "permanent",
        "contract_permanent_possible": "possibly permanent",
        "contract_fixed_term": "fixed term",
        "contract_internship": "internship",
        "contract_apprenticeship": "apprenticeship",
        "contract_freelance": "freelance",
        "contract_temporary_agency": "temporary agency work",
    },
}

LANGUAGE_LABELS = {
    "en": ("inglese", "English"), "fr": ("francese", "French"), "de": ("tedesco", "German"),
    "es": ("spagnolo", "Spanish"), "it": ("italiano", "Italian"), "pt": ("portoghese", "Portuguese"),
    "ru": ("russo", "Russian"), "zh": ("cinese", "Chinese"), "ar": ("arabo", "Arabic"),
    "ja": ("giapponese", "Japanese"), "nl": ("olandese", "Dutch"),
}

DEGREE_NAMES = {
    1: ("diploma", "high school diploma"),
    2: ("laurea", "university degree"),
    3: ("laurea magistrale", "master's degree"),
    4: ("dottorato", "PhD"),
}


def _language(language: str) -> str:
    return language if language in TEXTS else "en"


def text(key: str, language: str = "it", **values) -> str:
    return TEXTS[_language(language)][key].format(**values)


def _name(table: dict, key, language: str) -> str:
    pair = table.get(key)
    if pair is None:
        return str(key)
    return pair[1] if _language(language) == "en" else pair[0]


def _shorten(sentence: str, n: int = MAX_SENTENCE_CHARS) -> str:
    sentence = " ".join(sentence.split())
    return sentence if len(sentence) <= n else sentence[: n - 1].rstrip() + "…"


def describe_requirement(r, language: str) -> tuple[str, str]:
    """(requirement description, CV value in parentheses or "")."""
    d = r.data
    if r.kind == "years":
        cv = d.get("cv")
        cv_txt = text("cv_years", language, cv=f"{cv:g}") if cv is not None else ""
        return text("years", language, years=d["years"]), cv_txt
    if r.kind == "language":
        name = _name(LANGUAGE_LABELS, d["language"], language)
        if d.get("cv"):
            cv_txt = text("cv_language", language, cv=d["cv"])
        elif r.status == NOT_MET:
            cv_txt = text("cv_language_missing", language)
        else:
            cv_txt = ""
        return text("language", language, language_name=name, level=d["level"]), cv_txt
    if r.kind == "degree":
        cv = d.get("cv")
        cv_txt = text("cv_degree", language, cv=_name(DEGREE_NAMES, cv, language)) if cv else ""
        return text("degree", language, title=_name(DEGREE_NAMES, d["level"], language)), cv_txt
    return r.kind, ""


def _typed_line(result, requirement) -> str | None:
    """CV line quoted by the requirement check of the same kind (years, language, degree)."""
    for c in getattr(result, "checks", None) or []:
        if c.kind == requirement.kind and c.evidence and (
            c.kind != "language" or requirement.sentence in c.text or c.text in requirement.sentence
        ):
            return c.evidence
    return None


def reasons_and_gaps(profile, company, outcome, r_rel, r_postings, language: str = "it"):
    """(reasons: list[Evidence], gaps: list[str])."""
    language = _language(language)
    reasons: list[Evidence] = []
    gaps: list[str] = []
    site_source = (company.site or "site") if outcome is not None else "osm"

    # --- relevance ---
    if r_rel.best_sentence:
        reasons.append(
            Evidence(text("site_sentence", language, sentence=_shorten(r_rel.best_sentence)), site_source)
        )
    for key in r_rel.matching_sectors:
        tag = ", ".join(f"{k}={v}" for k, v in (company.osm_tags or {}).items() if k in _CATEGORY_KEYS)
        name = sector_name(key, language)
        if tag:
            reasons.append(Evidence(text("sector", language, sector=name, tag=tag), "osm"))
        else:
            reasons.append(Evidence(text("sector_name", language, sector=name), "osm"))
    if r_rel.found_terms:
        terms = ", ".join(r_rel.found_terms[:MAX_LISTED_TERMS])
        reasons.append(Evidence(text("terms", language, terms=terms), site_source))

    # --- fit ---
    if r_postings:
        best = r_postings[0]
        a = best.posting
        source = a.url or site_source
        reasons.append(
            Evidence(text("posting", language, title=a.title, score=best.score), source)
        )
        if best.nearest_role and (best.title or 0) > 0:
            reasons.append(
                Evidence(text("posting_role", language, title=a.title, role=best.nearest_role), source)
            )
        if best.found_skills:
            terms = ", ".join(best.found_skills[:MAX_LISTED_TERMS])
            reasons.append(
                Evidence(text("posting_skills", language, title=a.title, terms=terms), source)
            )
        for r in best.requirements:
            if r.status == SATISFIED:
                descr, _ = describe_requirement(r, language)
                reasons.append(Evidence(text("req_ok", language, title=a.title, requirement=descr)
                                        + cv_quote(_typed_line(best, r), language), source))

        for ra in r_postings[:MAX_EXPLAINED_POSTINGS]:
            for r in ra.requirements:
                descr, cv_txt = describe_requirement(r, language)
                if r.status == NOT_MET:
                    key = "req_not_met" if r.blocking else "req_not_met_preferred"
                    gaps.append(text(key, language, title=ra.posting.title, requirement=descr, cv=cv_txt)
                                + cv_quote(_typed_line(ra, r), language))
                elif r.status == UNVERIFIABLE:
                    gaps.append(text("req_unverifiable", language, title=ra.posting.title, requirement=descr))
        if not profile.roles:
            gaps.append(text("no_roles", language))

    # --- missing data ---
    if outcome is None:
        gaps.append(text("without_site", language))
    else:
        if getattr(outcome, "status", None) == HiringStatus.UNREACHABLE:
            gaps.append(text("unreachable", language))
        if not r_rel.used_text:
            gaps.append(text("no_text", language))
        if not r_postings:
            gaps.append(text("no_postings", language))
    if not profile.sectors:
        gaps.append(text("no_sectors", language))
    return reasons, gaps


# --- requirements, preferences, spontaneous application ------------------------------

_CV_QUOTE = re.compile(r" — CV: (?:«[^»]*»|“[^”]*”)")


def strip_cv_quotes(sentence: str) -> str:
    """The sentence without the CV lines quoted as evidence (see the module docstring)."""
    return _CV_QUOTE.sub("", sentence or "")


def cv_quote(line: str | None, language: str) -> str:
    if not line:
        return ""
    return text("cv_quote", language, cv=_shorten(line, MAX_QUOTE_CHARS))


def _priority(check, language: str) -> str:
    return text("priority_" + (check.priority if check.priority in ("must", "nice") else "context"),
                language)


def describe_check(check, title: str, language: str) -> tuple[str, str]:
    """("reason" or "gap", sentence) for one RequirementCheck."""
    values = dict(title=title, priority=_priority(check, language),
                  requirement=_shorten(check.text, MAX_QUOTE_CHARS),
                  cv=cv_quote(check.evidence, language))
    if check.level is None:
        return "gap", text("check_missing", language, **values)
    if check.level >= 3:
        return "reason", text("check_met", language, **values)
    if check.level == 2:
        return "gap", text("check_partial", language, **values)
    if check.level == 1:
        return "gap", text("check_weak", language, **values)
    return "gap", text("check_contradicted", language, **values)


def requirement_reasons_and_gaps(r_postings, language: str = "it"):
    """Reasons and gaps of the requirement checks of the best postings, each with the
    requirement and the CV line quoted. Years, language and degree keep their
    older sentences (see ``reasons_and_gaps``) and are skipped here."""
    language = _language(language)
    reasons: list[Evidence] = []
    gaps: list[str] = []
    if not r_postings:
        return reasons, gaps
    best = r_postings[0]
    a = best.posting
    source = a.url or "posting"
    if best.blocked and best.blocked_by is not None:
        c = best.blocked_by
        gaps.append(text("blocked", language, title=a.title, requirement=_shorten(c.text, MAX_QUOTE_CHARS),
                         cv=cv_quote(c.evidence, language)))
    musts = [c for c in best.checks if c.priority == "must"]
    if musts:
        met = sum(1 for c in musts if (c.level or 0) >= 3)
        reasons.append(Evidence(text("must_summary", language, title=a.title, met=met, total=len(musts)),
                                source))
    if best.found_role_skills:
        reasons.append(Evidence(text("role_coverage", language, title=a.title,
                                     terms=", ".join(best.found_role_skills[:MAX_LISTED_TERMS])), source))
    n_reasons = 0
    for ra in r_postings[:MAX_EXPLAINED_POSTINGS]:
        for c in ra.checks:
            if c.kind in ("years", "language", "degree") or c.blocking:
                continue
            kind, sentence = describe_check(c, ra.posting.title, language)
            if kind == "reason":
                if ra is best and n_reasons < MAX_REQUIREMENT_REASONS:
                    reasons.append(Evidence(sentence, ra.posting.url or source))
                    n_reasons += 1
            elif len(gaps) < MAX_REQUIREMENT_GAPS and (c.priority != "context" or c.level == 0):
                gaps.append(sentence)
    return reasons, gaps


def preference_reasons_and_gaps(posting, desirability, language: str = "it"):
    """Reasons and gaps from the desirability (contract, remote, salary, distance, travel)."""
    language = _language(language)
    reasons: list[Evidence] = []
    gaps: list[str] = []
    if desirability is None:
        return reasons, gaps
    title = getattr(posting, "title", "") if posting is not None else ""
    source = (getattr(posting, "url", None) or "posting") if posting is not None else "osm"

    def sentence(key: str, values: dict) -> str:
        values = dict(values)
        if "contract" in values:
            values["contract"] = text("contract_" + values["contract"], language) \
                if "contract_" + values["contract"] in TEXTS[language] else values["contract"]
        if key == "pref_travel_over":
            days = values.get("days")
            values["days"] = text("travel_days", language, days=days) if days is not None else ""
        if "minimum" in values:
            values["minimum"] = f"{values['minimum']:,}".replace(",", ".")
        return text(key, language, title=title, **values)

    for key, values in desirability.pluses:
        reasons.append(Evidence(sentence(key, values), "osm" if key == "pref_distance" else source))
    for key, values in desirability.minuses:
        gaps.append(sentence(key, values))
    return reasons, gaps


def spontaneous_reason(spontaneous, r_rel, language: str = "it") -> Evidence | None:
    language = _language(language)
    if spontaneous is None:
        return None
    pieces = []
    for signal in spontaneous.signals:
        if signal == "roles":
            pieces.append(text("sp_roles", language, roles=", ".join(spontaneous.found_roles[:3])))
        else:
            pieces.append(text("sp_" + signal, language))
    why = "; ".join(pieces) if pieces else text("sp_none", language)
    sentence = text("spontaneous", language, score=spontaneous.score, why=why)
    if r_rel is not None and getattr(r_rel, "found_role_skills", None):
        sentence += ". " + text("role_coverage_site", language,
                                terms=", ".join(r_rel.found_role_skills[:MAX_LISTED_TERMS]))
    return Evidence(sentence, "site")


# OSM keys that describe a business category.
_CATEGORY_KEYS = {
    "office", "shop", "craft", "amenity", "industrial", "healthcare", "tourism", "leisure",
    "man_made", "building", "company", "landuse",
}
