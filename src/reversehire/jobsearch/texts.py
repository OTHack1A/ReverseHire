"""User-visible texts of the job search, as i18n keys with English defaults.

The locale files are maintained separately: until a key is added there, `text()` shows
the English default below instead of the bare key (same pattern as discovery/texts.py).
"""

from __future__ import annotations

from ..i18n import t

FALLBACK: dict[str, str] = {
    # menu, search box, progress
    "jobsearch.menu.title": "Job search",
    "jobsearch.menu.search": "Search postings…",
    "jobsearch.menu.paste": "Paste a job ad or company page…",
    "jobsearch.search.placeholder": "Job title, skills, town (e.g. plc programmer bologna 30 km)",
    "jobsearch.search.button": "Search",
    "jobsearch.search.cancel": "Stop",
    "jobsearch.search.help": "Write as on a job site. \"Quotes\" = exact words, -word = exclude. "
                             "Contract and remote words (tempo indeterminato, smart working) become filters.",
    "jobsearch.progress.source": "{source}: {done} of about {total}",
    "jobsearch.progress.ranking": "Ranking {count} postings…",
    "jobsearch.progress.done": "Search finished in {seconds} s",
    # sources
    "jobsearch.source.local": "Postings already found by company searches",
    "jobsearch.source.my_companies": "My companies (job boards and careers pages)",
    "jobsearch.source.agency.randstad": "Randstad",
    "jobsearch.source.agency.manpower": "Manpower",
    "jobsearch.source.agency.synergie": "Synergie",
    "jobsearch.source.agency.adecco": "Adecco",
    "jobsearch.source.agency.gigroup": "Gi Group",
    "jobsearch.source.agency.umana": "Umana",
    "jobsearch.source.adzuna": "Adzuna (your key)",
    "jobsearch.source.jooble": "Jooble (your key)",
    "jobsearch.source.careerjet": "Careerjet (your key)",
    # terms verdicts and notes
    "jobsearch.verdict.allowed": "Allowed",
    "jobsearch.verdict.not_addressed": "Terms do not address automated reading",
    "jobsearch.verdict.harmful_automation_only": "Terms forbid only harmful automation",
    "jobsearch.verdict.forbidden": "Terms forbid automated reading: never used",
    "jobsearch.verdict.api_terms": "Official API: its own terms apply",
    "jobsearch.terms.agency.randstad": "Terms cover the candidate account only; automated reading is not addressed.",
    "jobsearch.terms.agency.manpower": "Terms forbid automated software that compromises the services and "
                                       "unauthorised reproduction; slow reading for your own search is neither. "
                                       "Results are shown as links.",
    "jobsearch.terms.agency.synergie": "Portal terms cover registered users only; automated reading is not "
                                       "addressed.",
    "jobsearch.terms.agency.adecco": "Terms forbid extracting or reusing any part of the site by any means or "
                                     "procedure.",
    "jobsearch.terms.agency.gigroup": "Terms forbid extracting content by any means or procedure beyond personal "
                                      "use.",
    "jobsearch.terms.agency.umana": "Terms forbid extraction by any means, including automatic, without written "
                                    "consent.",
    "jobsearch.terms.checked": "Terms of use read on {date}",
    "jobsearch.untested_live": "Not yet tested with a real key",
    "jobsearch.attribution.adzuna": "Jobs by Adzuna",
    # per-source status
    "jobsearch.state.ok": "{source}: {found} found, {kept} kept",
    "jobsearch.state.empty": "{source}: nothing found",
    "jobsearch.state.disabled": "{source}: off",
    "jobsearch.state.no_key": "{source}: needs your API key (Settings)",
    "jobsearch.state.forbidden_by_terms": "{source}: not used, its terms of use forbid automated reading",
    "jobsearch.state.failed": "{source}: failed ({detail})",
    "jobsearch.state.timeout": "{source}: too slow, stopped after {seconds} s ({found} found)",
    "jobsearch.state.cancelled": "{source}: cancelled ({found} found)",
    "jobsearch.state.partial": "{source}: {found} found, {kept} kept, with errors ({detail})",
    # result list
    "jobsearch.result.summary": "{count} postings ({merged} duplicates merged, {far} outside the radius)",
    "jobsearch.result.none": "No postings found. Try fewer words or a larger radius.",
    "jobsearch.result.unknown_distance": "{n} postings without a known place are listed after the others.",
    "jobsearch.result.open": "Open the ad",
    "jobsearch.result.create_card": "Create company card",
    "jobsearch.result.possible_employers": "Who might be behind this ad?",
    "jobsearch.result.hidden_employer": "Employer not named (agency ad)",
    "jobsearch.result.column.score": "Match",
    "jobsearch.result.column.title": "Position",
    "jobsearch.result.column.company": "Company",
    "jobsearch.result.column.place": "Place",
    "jobsearch.result.column.distance": "km",
    "jobsearch.result.column.contract": "Contract",
    "jobsearch.result.column.date": "Posted",
    "jobsearch.result.column.source": "Source",
    # why lines
    "jobsearch.why.skills": "{n} skills in common out of {m} requested: {list}",
    "jobsearch.why.skills_cv": "{n} of your skills appear in the ad: {list}",
    "jobsearch.why.no_skills": "No skill of the ad matched your CV",
    "jobsearch.why.no_cv": "No CV loaded: sorted by how well the title matches your search",
    "jobsearch.why.title_match": "Title matches your search",
    "jobsearch.why.distance": "{km} km from {place}",
    "jobsearch.why.distance_unknown": "Distance unknown",
    "jobsearch.why.remote": "Remote work",
    "jobsearch.why.recent": "Posted {days} days ago",
    "jobsearch.why.today": "Posted today",
    "jobsearch.why.salary": "Salary stated: {salary}",
    "jobsearch.why.also_on": "Also on: {sources}",
    "jobsearch.why.meaning": "Close in meaning to your search (estimate of the language model): “{snippet}”",
    "jobsearch.why.agency": "Published by {agency}; the employer is not named",
    "jobsearch.why.blocked": "Blocking requirement: {reason}",
    # possible employers
    "jobsearch.employers.header": "Companies that might match: a hypothesis from public data, not verified. "
                                  "The agency did not name the client.",
    "jobsearch.employers.unverified": "Unverified hypothesis",
    "jobsearch.employers.other": "Another company not in the list: {percent}%",
    "jobsearch.employers.none": "No company of the local list stands out (each below 10%).",
    "jobsearch.employers.no_location": "The ad has no usable location: no estimate.",
    "jobsearch.employers.label.strong": "strong hint",
    "jobsearch.employers.label.plausible": "plausible",
    "jobsearch.employers.label.possible": "possible",
    "jobsearch.employers.chip.distance": "{km} km",
    "jobsearch.employers.chip.ateco": "ATECO {code}",
    "jobsearch.employers.chip.sector_section": "same sector group ({code})",
    "jobsearch.employers.chip.sector_compatible": "compatible sector",
    "jobsearch.employers.chip.tech": "uses {tech} (from its site)",
    "jobsearch.employers.chip.role": "posted \"{title}\" ({when})",
    "jobsearch.employers.chip.role_old": "posted \"{title}\" some time ago",
    "jobsearch.employers.chip.agency_client": "named client of {agency}",
    "jobsearch.employers.chip.ccnl": "CCNL {ccnl} consistent",
    "jobsearch.employers.chip.ccnl_conflict": "CCNL {ccnl} unlikely here",
    "jobsearch.employers.chip.size": "size matches ({size})",
    "jobsearch.employers.chip.wording": "wording similar to its own ads",
    # paste and review
    "jobsearch.paste.title": "Paste a job ad or company page",
    "jobsearch.paste.help": "Copy the page in your browser (Ctrl+A, Ctrl+C) and paste it here. Nothing is "
                            "downloaded from the site.",
    "jobsearch.paste.url": "Page address (optional, not opened):",
    "jobsearch.paste.parse": "Read",
    "jobsearch.paste.empty": "Nothing to read: paste the page text first.",
    "jobsearch.paste.review_note": "Check every field: guesses are marked.",
    "jobsearch.paste.removed_people": "{n} lines naming people were removed and are not stored.",
    "jobsearch.paste.save_posting": "Save as posting",
    "jobsearch.paste.save_company": "Save to company card",
    "jobsearch.paste.kind.linkedin_job": "LinkedIn job ad",
    "jobsearch.paste.kind.linkedin_company": "LinkedIn company page",
    "jobsearch.paste.kind.indeed_job": "Indeed job ad",
    "jobsearch.paste.kind.generic_job": "Job ad",
    "jobsearch.paste.confidence.high": "read from a label",
    "jobsearch.paste.confidence.medium": "probable",
    "jobsearch.paste.confidence.low": "guess, please check",
    "jobsearch.paste.field.title": "Position",
    "jobsearch.paste.field.company": "Company",
    "jobsearch.paste.field.location": "Place",
    "jobsearch.paste.field.posted": "Posted",
    "jobsearch.paste.field.applicants": "Applicants",
    "jobsearch.paste.field.workplace": "Workplace",
    "jobsearch.paste.field.contract": "Contract",
    "jobsearch.paste.field.hours": "Working time",
    "jobsearch.paste.field.salary": "Salary",
    "jobsearch.paste.field.description": "Description",
    "jobsearch.paste.field.industry": "Industry",
    "jobsearch.paste.field.size": "Company size",
    "jobsearch.paste.field.website": "Website",
    "jobsearch.paste.field.phone": "Phone",
    "jobsearch.paste.field.headquarters": "Headquarters",
    "jobsearch.paste.field.founded": "Founded",
    "jobsearch.paste.field.specialties": "Specialties",
    "jobsearch.paste.field.overview": "Overview",
    "jobsearch.paste.field.followers": "Followers",
    "jobsearch.paste.field.locations": "Locations",
    "jobsearch.paste.field.rating": "Rating",
    "jobsearch.paste.field.benefits": "Benefits",
    "jobsearch.paste.field.experience": "Experience",
    "jobsearch.paste.field.language": "Language",
    "jobsearch.paste.field.workplace_address": "Work location",
    # settings
    "jobsearch.settings.title": "Job search sources",
    "jobsearch.settings.source_enabled": "Use this source",
    "jobsearch.settings.adzuna_id": "Adzuna app_id:",
    "jobsearch.settings.adzuna_key": "Adzuna app_key:",
    "jobsearch.settings.jooble_key": "Jooble key:",
    "jobsearch.settings.careerjet_key": "Careerjet key:",
    "jobsearch.settings.keys_note": "Keys stay on this computer (system keyring) and are sent only to that service.",
    "jobsearch.settings.timeout": "Stop a source after (s):",
    "jobsearch.settings.max_pages": "Posting pages read per agency and search:",
}


def text(key: str, **params) -> str:
    value = t(key, **params)
    if value != key:
        return value
    template = FALLBACK.get(key, key)
    try:
        return template.format(**params) if params else template
    except (KeyError, IndexError, ValueError):
        return template


def source_key(source_id: str) -> str:
    return "jobsearch.source." + source_id.replace(":", ".")


def source_label(source_id: str, default: str = "") -> str:
    key = source_key(source_id)
    value = text(key)
    return default if value == key and default else value


def state_line(report, label: str | None = None) -> str:
    """One status line for a SourceReport."""
    label = label or source_label(report.source_id, report.source_id)
    detail = "; ".join(report.errors[:2]) or "-"
    return text(f"jobsearch.state.{report.state}", source=label, found=report.found, kept=report.kept,
                detail=detail, seconds=f"{report.seconds:.0f}")
