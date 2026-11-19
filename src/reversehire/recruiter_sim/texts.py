"""English default texts of the recruiter simulators, by i18n key.

All keys start with "rsim.". The GUI translates them with the locale files;
`render` gives the English default with the parameters filled in. Profile
texts (notes, retention, confidence notes) live in `data/profiles.json` with
their English default and are registered here when the profiles are loaded.
"""

from __future__ import annotations

import string

_EXTRA: dict[str, str] = {}  # defaults registered at run time (profile texts)


def register_default(key: str, text: str) -> None:
    """Record the English default of a key defined outside this module (profiles)."""
    _EXTRA.setdefault(key, text)


def default(key: str) -> str | None:
    return TEXTS.get(key) or _EXTRA.get(key)


class _SafeDict(dict):
    def __missing__(self, key):  # an unknown placeholder stays visible
        return "{" + key + "}"


def render(key: str, params: dict | None = None) -> str:
    """The English default with its placeholders filled ("{name}" style)."""
    template = default(key)
    if template is None:
        return key
    values = {k: _plain(v) for k, v in (params or {}).items()}
    return string.Formatter().vformat(template, (), _SafeDict(values))


def _plain(value) -> str:
    if hasattr(value, "key") and hasattr(value, "params"):  # a nested Message
        return render(value.key, value.params)
    if isinstance(value, float):
        return f"{value:.1f}".rstrip("0").rstrip(".")
    if isinstance(value, (list, tuple)):
        return ", ".join(_plain(v) for v in value)
    return str(value)


def placeholders(key: str) -> set[str]:
    template = default(key) or ""
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def all_keys() -> list[str]:
    """Every key with a default (the profiles' keys included once they are loaded)."""
    from . import profiles

    profiles.load_profiles()
    return sorted(set(TEXTS) | set(_EXTRA))


TEXTS: dict[str, str] = {
    # --- general ---------------------------------------------------------------------
    "rsim.approximation": "Approximate simulation of {name}, built from the vendor's public documentation "
                          "(reviewed {reviewed}). The real system can be configured differently by each employer.",
    "rsim.status.ok": "OK",
    "rsim.status.warn": "Check",
    "rsim.status.fail": "Problem",
    "rsim.status.unknown": "Not determinable",
    "rsim.check.parse": "Reading the file",
    "rsim.check.record": "Fields filled automatically",
    "rsim.check.knockout": "Knock-out questions",
    "rsim.check.findability": "Findability in keyword searches",
    "rsim.check.semantic": "Match with the role",
    "rsim.check.timeline": "Timeline",
    "rsim.check.skill_recency": "Seniority and recency of skills",
    "rsim.check.credentials": "Languages, degrees, certifications",
    "rsim.check.format": "Format for this system",
    "rsim.check.ai_text": "Generated-text estimate",
    "rsim.check.score": "Simulated ATS score",
    "rsim.confidence.low": "low",
    "rsim.confidence.medium": "medium",
    "rsim.confidence.high": "high",
    "rsim.degraded.semantic": "No embedding model available: the semantic match was replaced by a "
                              "lexical match with synonyms.",

    # --- 1. parse view ----------------------------------------------------------------
    "rsim.parse.do": "The ATS turns your file into plain text before anyone reads it; columns, headers, "
                     "images, tables and icons are where text gets lost or scrambled.",
    "rsim.parse.ok": "The parser reads {lines} lines of text from {pages} page(s); nothing was lost.",
    "rsim.parse.lost_summary": "The parser would lose {count} piece(s) of text ({kinds}).",
    "rsim.parse.no_text": "The parser finds no text layer: the file is an image ({method}).",
    "rsim.parse.no_text.fix": "Export the CV as a text PDF or DOCX from your word processor; do not scan "
                              "or photograph it.",
    "rsim.parse.ocr_lost": "{pages} page(s) are images: this system reads no text from them "
                           "(the app read them with OCR, the ATS will not).",
    "rsim.parse.ocr_kept": "{pages} page(s) are images: this system runs OCR, which may misread words.",
    "rsim.parse.ocr.fix": "Replace scanned pages with real text.",
    "rsim.parse.columns": "Page {pages} has {columns} columns: a linear parser reads across them and mixes "
                          "lines from both sides.",
    "rsim.parse.columns_layout": "Page {pages} has {columns} columns: this parser is column-aware, but "
                                 "columnar layouts are a known parsing risk (Textkernel code 433).",
    "rsim.parse.columns.fix": "Use a single-column layout; put dates on the same line as the job title.",
    "rsim.parse.header_footer_lost": "Text in the page header or footer is dropped by this parser: "
                                     "{text}",
    "rsim.parse.header_footer_contacts": "Your contact details are only in the header/footer, which this "
                                         "parser drops: {text}",
    "rsim.parse.header_footer_kept": "Text in the page header or footer is read, but often out of place: "
                                     "{text}",
    "rsim.parse.header_footer.fix": "Move the text out of the page header/footer into the body of page one.",
    "rsim.parse.tables_lost": "{count} table(s) hold {chars} characters that this parser loses.",
    "rsim.parse.tables_flat": "{count} table(s) are flattened: cells are read one after the other.",
    "rsim.parse.tables.fix": "Replace tables with plain lines (title, employer, dates on one line).",
    "rsim.parse.text_boxes_lost": "{count} text box(es) hold text that this parser loses: {text}",
    "rsim.parse.text_boxes.fix": "Move the text out of text boxes into normal paragraphs.",
    "rsim.parse.glyphs": "{count} character(s) cannot be decoded (icons or fonts without a Unicode map): {text}",
    "rsim.parse.glyphs.fix": "Write the words ('Email', 'Phone') instead of icons and use a standard font.",
    "rsim.parse.fonts": "{count} font(s) in the PDF have no Unicode map: some words may come out garbled.",
    "rsim.parse.fonts.fix": "Re-export the PDF with standard fonts embedded (e.g. from Word or LibreOffice).",
    "rsim.parse.headings_strict": "{count} heading(s) are not standard and this parser uses a fixed list: "
                                  "{text}",
    "rsim.parse.headings_fuzzy": "{count} heading(s) are not standard: {text}",
    "rsim.parse.headings.fix": "Use standard headings: Experience, Education, Skills, Languages "
                               "(Esperienze lavorative, Istruzione, Competenze, Lingue).",
    "rsim.parse.headings_missing": "No standard section headings were found (Textkernel code 412).",
    "rsim.parse.headings_spaced": "{count} heading(s) are letter-spaced and read as single letters: {text}",
    "rsim.parse.headings_spaced.fix": "Write headings as normal words, not spaced letters.",
    "rsim.parse.contacts_not_top": "Your contact details start at line {line} of {lines}, not at the top "
                                   "(Textkernel code 311).",
    "rsim.parse.contacts_not_top.fix": "Put name, e-mail, phone and town in the first lines.",
    "rsim.parse.vertical_dates": "{count} date range(s) are split over two lines (Textkernel code 418): {text}",
    "rsim.parse.vertical_dates.fix": "Write each date range on one line: '03/2019 - 06/2022'.",
    "rsim.parse.pages": "The file has {pages} pages; this system expects at most {maximum}.",
    "rsim.parse.pages.fix": "Shorten the CV to {maximum} page(s).",

    # --- 2. record round trip ---------------------------------------------------------
    "rsim.record.do": "The ATS fills a candidate record from the parsed text; the recruiter often sees "
                      "the record, not your file.",
    "rsim.record.ok": "All {count} record fields this system fills were found.",
    "rsim.record.missing": "{count} of {total} record fields would be empty: {fields}.",
    "rsim.record.garbled": "{count} record field(s) would be filled with garbled text: {fields}.",
    "rsim.record.lost": "{count} field(s) are in your file but lost by this parser: {fields}.",
    "rsim.record.fix.email": "Write your e-mail address as plain text in the first lines.",
    "rsim.record.fix.phone": "Write your phone number as plain text in the first lines (not in an icon "
                             "or the page header).",
    "rsim.record.fix.town": "Write your town (and province) in the first lines.",
    "rsim.record.fix.name": "Put your full name alone on the first line.",
    "rsim.record.fix.title": "Write each job title on its own line with the employer and the dates.",
    "rsim.record.fix.employer": "Write the employer next to each job title.",
    "rsim.record.fix.dates": "Give month and year for every job (MM/YYYY - MM/YYYY).",
    "rsim.record.fix.degree": "Write your degree with its standard name (e.g. 'Laurea in ...', 'BSc in ...').",
    "rsim.record.fix.languages": "Add a Languages section with a CEFR level for each language.",
    "rsim.record.fix.skills": "Add a Skills section listing your tools and technologies.",
    "rsim.record.fix.generic": "Make the field '{field}' readable as plain text.",
    "rsim.record.field.name": "Name",
    "rsim.record.field.email": "E-mail",
    "rsim.record.field.phone": "Phone",
    "rsim.record.field.town": "Town",
    "rsim.record.field.title": "Current title",
    "rsim.record.field.employer": "Current employer",
    "rsim.record.field.dates": "Job dates",
    "rsim.record.field.degree": "Degree",
    "rsim.record.field.languages": "Languages",
    "rsim.record.field.skills": "Skills",
    "rsim.record.field.file": "CV file",
    "rsim.record.field.area": "Professional area (chosen in the form)",

    # --- 3. knock-out questions -------------------------------------------------------
    "rsim.knockout.do": "Screening questions are asked before any score: in some systems a wrong answer "
                        "to a 'disqualifier' question rejects the application automatically.",
    "rsim.knockout.none": "No knock-out requirement could be inferred from the target.",
    "rsim.knockout.summary": "{count} likely screening question(s): {failed} fail, {open} need your answer.",
    "rsim.knockout.fail": "Knock-out failed: {topic}. {reason}",
    "rsim.knockout.fix.answer": "Save your answer to this question in Settings so the check is complete; "
                                "answer the real form truthfully.",
    "rsim.knockout.fix.state": "If it is true, state it in the CV: {hint}.",
    "rsim.knockout.fix.fail": "This requirement is not met: the application will likely be rejected "
                              "before scoring. Apply only if the posting is flexible on it.",
    "rsim.knockout.topic.years": "years of experience",
    "rsim.knockout.topic.language": "language level",
    "rsim.knockout.topic.degree": "degree",
    "rsim.knockout.topic.licence": "driving licence",
    "rsim.knockout.topic.car": "own car",
    "rsim.knockout.topic.travel": "business travel",
    "rsim.knockout.topic.right_to_work": "right to work",
    "rsim.knockout.topic.location": "location / commute",
    "rsim.knockout.topic.relocation": "relocation",
    "rsim.knockout.topic.availability": "availability / notice period",
    "rsim.knockout.topic.salary": "salary expectation",
    "rsim.knockout.topic.shifts": "shifts",
    "rsim.knockout.topic.must_skill": "must-have skill",
    "rsim.knockout.reason.cv_contradicts": "Your CV says otherwise: {cv}",
    "rsim.knockout.reason.answer_no": "Your saved answer is no.",
    "rsim.knockout.reason.too_far": "The posting is {distance} km away; your limit is {limit} km.",
    "rsim.knockout.reason.salary": "Your minimum ({minimum}) is above the published maximum ({maximum}).",
    "rsim.knockout.reason.notice": "Immediate start asked; your notice is {weeks} weeks.",
    "rsim.knockout.reason.missing_skill": "Not found in your CV: {skill}.",
    "rsim.knockout.reason.unanswered": "The CV is silent and no answer is saved.",
    "rsim.knockout.hint.licence": "'Patente B' / 'Driving licence B'",
    "rsim.knockout.hint.car": "'Automunito' / 'Own car'",
    "rsim.knockout.hint.travel": "your availability to travel",
    "rsim.knockout.hint.right_to_work": "your right to work in the country",
    "rsim.knockout.hint.relocation": "your availability to relocate",
    "rsim.knockout.hint.availability": "your availability date or notice period",
    "rsim.knockout.hint.language": "the language with its CEFR level",
    "rsim.knockout.hint.degree": "your degree",
    "rsim.knockout.hint.years": "the dates of each job",
    "rsim.knockout.hint.must_skill": "the skill in the job where you used it",
    "rsim.knockout.hint.location": "your town",
    "rsim.knockout.hint.salary": "nothing in the CV; answer the form question",
    "rsim.knockout.hint.shifts": "your availability for shifts",
    "rsim.knockout.mode.none": "This system's form asks no screening questions; a recruiter may still "
                               "check these points by hand.",
    "rsim.knockout.mode.manual_review": "A recruiter reviews the answers by hand; a failed point usually "
                                        "means rejection.",
    "rsim.knockout.mode.auto_disqualify": "A wrong answer can reject the application automatically.",
    "rsim.knockout.mode.scored": "The answers are scored and weighted together with the CV match.",

    # --- 4. findability ---------------------------------------------------------------
    "rsim.findability.do": "Recruiters search their CV database with job titles, 2-4 hard skills and a "
                           "town; many systems match whole words literally.",
    "rsim.findability.none": "No search could be built: the target has no title or skills.",
    "rsim.findability.summary": "{hits} of {total} likely searches find your CV with this system "
                                "({mode} matching).",
    "rsim.findability.miss_synonym": "The search {query} misses you: you wrote {cv_term} but not {term}.",
    "rsim.findability.miss_synonym.fix": "Write '{term}' next to '{cv_term}' (both forms are searched): "
                                         "{line}",
    "rsim.findability.miss_absent": "The search {query} misses you: {term} is not in your CV.",
    "rsim.findability.miss_absent.fix": "If you have '{term}', state it with these exact words in the job "
                                        "where you used it.",
    "rsim.findability.miss_title.fix": "If '{term}' describes your work, use it as the title under your name "
                                       "and in the job where it applies.",
    "rsim.findability.mode.literal": "literal whole-word",
    "rsim.findability.mode.stem": "word-stem",
    "rsim.findability.mode.taxonomy": "synonym/taxonomy",
    "rsim.findability.mode.semantic": "semantic",
    "rsim.findability.boolean_unknown": "This system's boolean syntax is not documented: both the literal "
                                        "and the normalised result are shown.",

    # --- 5. semantic match ------------------------------------------------------------
    "rsim.semantic.do": "Many systems compute a match score or grade between the CV and the job "
                        "(e.g. a percentage, A-D, High/Medium/Low).",
    "rsim.semantic.grade": "Grade {grade}: {met} of {total} must-have requirements evidenced; "
                           "similarity {similarity}%.",
    "rsim.semantic.grade_lexical": "Grade {grade}: {met} of {total} must-have requirements evidenced "
                                   "(lexical match, no embedding model).",
    "rsim.semantic.none": "The target has no requirements to compare with.",
    "rsim.semantic.missing": "Missing must-haves: {items}.",
    "rsim.semantic.fix": "For each must-have you meet, write one line in the relevant job that names it "
                         "and shows a result.",

    # --- 6. timeline ------------------------------------------------------------------
    "rsim.timeline.do": "Parsers compute months of experience without double counting, average months per "
                        "employer, gaps and whether you had a job in the last year.",
    "rsim.timeline.summary": "{months} months of experience ({years} years) over {employers} employer(s); "
                             "average {average} months per employer.",
    "rsim.timeline.no_dates": "No dated job was found: the parser cannot compute your experience "
                              "(Textkernel code 419).",
    "rsim.timeline.no_dates.fix": "Give month and year for the start and end of every job.",
    "rsim.timeline.gap": "Gap of {months} months between {start} and {end}.",
    "rsim.timeline.gap.fix": "Explain the gap in one line (training, care, job search, freelance work).",
    "rsim.timeline.no_recent": "No job in the last 12 months: the last one ended {since} "
                               "(Textkernel code 226).",
    "rsim.timeline.no_recent.fix": "If you did courses, projects or freelance work since then, list them "
                                   "with dates.",
    "rsim.timeline.hopping": "{count} jobs shorter than {months} months in the last {years} years "
                             "(average {average} months per employer).",
    "rsim.timeline.hopping.fix": "State the reason for short jobs in one line (fixed-term contract, project "
                                 "end, company closure).",
    "rsim.timeline.year_only": "{count} job(s) have only years, no months: this system needs months.",
    "rsim.timeline.year_only.fix": "Write month and year (MM/YYYY) for every job.",
    "rsim.timeline.overlap": "{count} overlapping job(s) (counted once in the total).",

    # --- 7. skill recency -------------------------------------------------------------
    "rsim.skill_recency.do": "Parsers give each skill the months of the jobs whose text names it and the "
                             "date it was last used; a skill only in the Skills list gets no months.",
    "rsim.skill_recency.summary": "{dated} of {total} skills have dated use in a job; {undated} appear only "
                                  "in the skills list or summary.",
    "rsim.skill_recency.undated": "No dated use for: {skills}.",
    "rsim.skill_recency.undated.fix": "If you used {skill} in one of your jobs, name it in that job's "
                                      "description.",
    "rsim.skill_recency.stale": "Required skills last used more than 2 years ago: {skills}.",
    "rsim.skill_recency.none": "No skills were found in the CV.",

    # --- 8. credentials ---------------------------------------------------------------
    "rsim.credentials.do": "Languages with a CEFR level, degrees and certifications are typical filters "
                           "and knock-out questions.",
    "rsim.credentials.languages_ok": "{count} language(s) with a level: {items}.",
    "rsim.credentials.language_no_level": "Language(s) without a level: {items}.",
    "rsim.credentials.language_no_level.fix": "Add the CEFR level (A1-C2) to each language, e.g. "
                                              "'English B2'.",
    "rsim.credentials.no_languages": "No Languages section was found.",
    "rsim.credentials.no_languages.fix": "Add a Languages section with a CEFR level for each language.",
    "rsim.credentials.degree_ok": "Degree found: {items}.",
    "rsim.credentials.no_degree": "No degree or diploma was recognised.",
    "rsim.credentials.no_degree.fix": "Write your highest qualification with its standard name and year.",
    "rsim.credentials.certs_ok": "Certifications found: {items}.",
    "rsim.credentials.cert_missing": "The target names certifications not found in your CV: {items}.",
    "rsim.credentials.cert_missing.fix": "If you hold {item}, list it under Certifications with the year.",
    "rsim.credentials.no_certs": "No Certifications section (none is asked by the target).",

    # --- 9. format --------------------------------------------------------------------
    "rsim.format.do": "Each system accepts some file types and has its own pitfalls.",
    "rsim.format.type_ok": "The file type .{ext} is accepted by this system.",
    "rsim.format.type_bad": "The file type .{ext} is not among the types this system accepts ({types}).",
    "rsim.format.type_bad.fix": "Send the CV as {preferred}.",
    "rsim.format.type_unknown": "No CV file was given: the accepted types are {types}.",
    "rsim.format.note": "{note}",
    "rsim.format.dedup": "This system merges duplicate CVs of the same person: send one up-to-date file "
                         "per company.",
    "rsim.format.blind": "This system can hide personal data from the recruiter (blind screening).",
    "rsim.format.form": "The application form asks: {fields}.",

    # --- 10. AI-text estimate ---------------------------------------------------------
    "rsim.ai_text.do": "No major ATS documents an AI-writing detector; some recruiters use third-party "
                       "detectors as a signal, not a verdict.",
    "rsim.ai_text.not_given": "No estimate was computed for this CV.",
    "rsim.ai_text.short": "The text is too short ({words} words) to estimate anything.",
    "rsim.ai_text.human": "The style looks like ordinary human writing (estimate {low}-{high}%).",
    "rsim.ai_text.inconclusive": "Not determinable (estimate {low}-{high}%): a statistical estimate of "
                                 "style, not proof.",
    "rsim.ai_text.generated": "Parts of the style resemble generated text (estimate {low}-{high}%); this "
                              "is a statistical estimate, not proof.",
    "rsim.ai_text.fix": "Rewrite the flagged sentences with concrete facts: tools, numbers, results.",

    # --- 11. score ----------------------------------------------------------------------
    "rsim.score.do": "Systems that score CVs combine parsing quality, keyword hits, the match with the "
                     "requirements and the timeline; a knock-out rejects before any score.",
    "rsim.score.value": "Simulated score {score}/100 ({display}).",
    "rsim.score.capped": "A failed knock-out caps the score at {cap}: {reason}",
    "rsim.score.component": "{component}: {value}/100 (weight {weight}%)",
    "rsim.score.component.parse": "Parsing and record",
    "rsim.score.component.findability": "Keyword searches that find you",
    "rsim.score.component.match": "Requirements covered",
    "rsim.score.component.timeline": "Timeline",
    "rsim.score.component.credentials": "Languages and education",
    "rsim.score.screening": "Screening answers weigh {share}% of the score.",
    "rsim.score.display.none": "this system shows no score; the recruiter sees which requirements are met: "
                               "{met} of {total}",
    "rsim.score.display.percent": "{value}%",
    "rsim.score.display.grade_AD": "grade {grade}",
    "rsim.score.display.high_med_low": "{label}",
    "rsim.score.display.criteria_met": "{met} of {total} criteria met",
    "rsim.score.display.no_criteria": "no score: the posting states no must-have requirement",
    "rsim.score.display.rating_points": "rating {points} of 5",
    "rsim.score.label.high": "High",
    "rsim.score.label.medium": "Medium",
    "rsim.score.label.low": "Low",
    "rsim.score.band.top": "Likely near the top of the list",
    "rsim.score.band.middle": "Likely in the middle of the list",
    "rsim.score.band.bottom": "Likely near the bottom of the list",
    "rsim.score.band.rejected": "Likely rejected automatically by a knock-out question",
    "rsim.score.band.at_risk": "At risk: a recruiter checking the screening points would likely reject",

    # --- match score (§2.7) -----------------------------------------------------------
    "rsim.match.summary": "{common} skills in common out of {total} asked ({musts_met} must-haves out of "
                          "{musts}).",
    "rsim.match.score": "Match score {score}/100 (ad covered {ad_fit}%, your strengths used {cv_use}%).",
    "rsim.match.capped": "Capped at {cap}: a must-have requirement is contradicted by the CV ({reason}).",
    "rsim.match.confidence": "Confidence {label} ({value}%): {parsed}% of the posting could be read as "
                             "requirements.",
    "rsim.match.status.matched": "in common",
    "rsim.match.status.partial": "partly",
    "rsim.match.status.missing": "missing",
    "rsim.match.status.uncertain": "not stated",
    "rsim.match.status.contradicted": "contradicted",

    # --- what-if ------------------------------------------------------------------------
    "rsim.whatif.synonym": "Write '{term}' next to '{cv_term}' in: {line}",
    "rsim.whatif.skill_in_job": "If you used {skill} in a job, name it in that job's description (it is now "
                                "only in: {line}).",
    "rsim.whatif.missing_skill": "If you have {skill}, state it in the job where you used it.",
    "rsim.whatif.title": "If '{title}' describes your work, write it as your headline title.",
    "rsim.whatif.columns": "Rebuild the CV in a single column.",
    "rsim.whatif.header_footer": "Move the header/footer text into the body: {text}",
    "rsim.whatif.dates": "Add months to the job dates: {line}",
    "rsim.whatif.headings": "Rename the heading '{heading}' to a standard one.",
    "rsim.whatif.language_level": "Add the CEFR level to: {line}",
    "rsim.whatif.contacts_top": "Move your contact details to the first lines.",
    "rsim.whatif.record_field": "Make the record field '{field}' readable: {fix}",
    "rsim.whatif.generic": "{fix}",
}
