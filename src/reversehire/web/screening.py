"""Selection system and CV screening of an employer (company card, sections 5 and 6).

Three kinds of information, never mixed up:
(a) what the applicant-tracking PLATFORM offers, per its vendor (knowledge table
    ATS_KNOWLEDGE, from the sources study section D, with source URLs and the date of
    the last review). Worded "The platform offers ...", never as a fact about the employer;
(b) EXPLICIT statements found on the employer's own careers / privacy pages (automated
    decision-making, profiling, art. 22 GDPR, AI Act notices), quoted verbatim with
    their URL (keyword lists IT/EN from the study, plus a recruitment-context check:
    a cookie-policy "profilazione" is not about candidates);
(c) a three-level indicator: automatic parsing "likely" / "unknown"; AI screening
    "declared" (only with (b)) / "platform_capable" (only (a)) / "no_evidence".

Pure functions over already fetched pages; no network here.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

from .ats import detect_ats, is_unsupported_ats
from .text import analyze_html, extract_links

REVIEWED = "2026-11-17"  # date of the vendor review in the sources study


@dataclass(frozen=True)
class Vendor:
    key: str
    name: str
    hosts: tuple[str, ...]
    parsing: str  # "yes" | "basic" | "unknown"
    ranking: str  # what the platform offers for ranking / knock-out questions ("" = not documented)
    ai: str  # "yes" | "assistive" | "unclear"
    ai_note: str
    sources: tuple[str, ...]
    evidence: str  # "S" (vendor/third-party pages via search) | "M" (from memory) | "V"
    reviewed: str = REVIEWED

    def offer_text(self) -> str:
        """English sentence: what THE PLATFORM offers (never what the employer does)."""
        parts = []
        if self.parsing in ("yes", "basic"):
            parts.append("resume parsing" + (" (basic)" if self.parsing == "basic" else ""))
        if self.ranking:
            parts.append(self.ranking)
        if self.ai in ("yes", "assistive"):
            parts.append("AI features" + (f" ({self.ai_note})" if self.ai_note else ""))
        if not parts:
            return f"The platform ({self.name}): capabilities not documented by the vendor."
        return f"The platform ({self.name}) offers " + "; ".join(parts) + "."


ATS_KNOWLEDGE: dict[str, Vendor] = {v.key: v for v in (
    Vendor("greenhouse", "Greenhouse", ("greenhouse.io",), "yes",
           "Talent Matching match scores (add-on), custom application questions", "assistive",
           "vendor says it does not auto-advance or auto-reject; optional opt-out to manual review",
           ("https://support.greenhouse.io/hc/en-us/articles/41396009937307",
            "https://www.greenhouse.com/ai-recruiting"), "S"),
    Vendor("lever", "Lever", ("lever.co",), "yes", "", "unclear", "",
           (), "M"),
    Vendor("ashby", "Ashby", ("ashbyhq.com",), "yes",
           "AI-Assisted Application Review: AI marks recruiter criteria as met / not met", "assistive",
           "decisions by humans per the vendor",
           ("https://www.ashbyhq.com/blog/product/ai-assisted-application-review",
            "https://docs.ashbyhq.com/ai-assisted-application-review"), "S"),
    Vendor("workable", "Workable", ("workable.com",), "yes",
           "match score and requirement checklist per candidate", "yes", "AI Recruiting Agent",
           ("https://resources.workable.com",), "S"),
    Vendor("recruitee", "Recruitee (Tellent)", ("recruitee.com",), "yes", "Matching Assistant",
           "assistive", "", (), "S"),
    Vendor("personio", "Personio", ("jobs.personio.de", "jobs.personio.com"), "yes",
           "Talent Screening Agent with recruiter 'AI Signals' in screening stages", "yes",
           "needs the CV as PDF, image or Word",
           ("https://support.personio.de/hc/en-us/articles/37951608749981",
            "https://www.personio.com/feature/talent-screening-agent/"), "S"),
    Vendor("smartrecruiters", "SmartRecruiters (SAP)", ("smartrecruiters.com",), "yes",
           "Winston Match score (High/Medium/Low) in the applicant list; Winston Screen", "yes", "",
           ("https://www.smartrecruiters.com/de/recruiting-software/winston-match/",), "S"),
    Vendor("workday", "Workday Recruiting", ("myworkdayjobs.com", "myworkdaysite.com", "myworkday.com"), "yes",
           "HiredScore AI grading A-D with fit and gap per requirement; can fast-track top grades", "yes", "",
           ("https://doc.workday.com", "https://www.workday.com"), "S"),
    Vendor("bamboohr", "BambooHR", ("bamboohr.com",), "basic", "", "unclear", "", (), "M"),
    Vendor("teamtailor", "Teamtailor", ("teamtailor.com",), "yes",
           "Co-pilot: resume summary matched against the job description, screening by criteria",
           "assistive", "",
           ("https://support.teamtailor.com/en/articles/8403177",
            "https://www.teamtailor.com/en/ai-recruiting-software/"), "S"),
    Vendor("icims", "iCIMS", ("icims.com",), "yes",
           "Talent Cloud AI: candidate ranking with Job Fit scores (add-on)", "yes", "",
           ("https://community.icims.com",), "S"),
    Vendor("oracle", "Oracle Taleo / Oracle Recruiting Cloud", ("taleo.net", "oraclecloud.com"), "yes",
           "prescreening questions with knock-out and weighting; AI matching (Recruiting Booster)", "yes",
           "", (), "M"),
    Vendor("successfactors", "SAP SuccessFactors Recruiting",
           ("successfactors.com", "successfactors.eu", "sapsf.eu", "sapsf.com"), "yes",
           "AI-assisted screening against requirements; prescreen questions", "yes", "",
           ("https://www.sap.com/use-cases/reduce-time-spent-assessing-candidate-applications",
            "https://www.textkernel.com/integrations/sap-successfactors/"), "S"),
    Vendor("inrecruiting", "Zucchetti InRecruiting (ex Intervieweb)",
           ("intervieweb.it", "inrecruiting.com", "in-recruiting.com"), "yes",
           "semantic search, scoring and job matching (Inda engine)", "yes", "",
           ("https://www.in-recruiting.com",), "S"),
    Vendor("altamira", "Altamira", ("altamiraweb.net", "altamirahrm.com"), "unknown",
           "", "unclear", "", (), "M"),
    Vendor("cornerstone", "Cornerstone", ("csod.com",), "yes", "skills-based AI matching", "yes", "",
           (), "S"),
    Vendor("jobvite", "Jobvite (Employ)", ("jobvite.com",), "yes", "prescreen / knock-out questions",
           "unclear", "", (), "M"),
    Vendor("factorial", "Factorial", ("factorialhr.com", "factorial.it", "factorialhr.it"), "unknown",
           "", "unclear", "", (), "M"),
    Vendor("softgarden", "softgarden", ("softgarden.io",), "unknown", "", "unclear", "", (), "M"),
    Vendor("join", "JOIN", ("join.com",), "unknown", "", "unclear", "", (), "M"),
    # RECRUITER_STUDY §1.1: vendor pages read (documented) plus a live application form (verified).
    Vendor("allibo", "Allibo Recruit", ("joblink.allibo.com", "allibo.com"), "yes",
           "screening support against the requirements, automatic sorting of applications into "
           "per-search folders, pre-qualification, questionnaires and tests, CV deduplication",
           "unclear", "AI or semantic matching not documented publicly",
           ("https://www.allibo.com/ats-recruiting-software/",), "S"),
)}

# Channels that are not a platform.
CHANNEL_EMAIL = "email"
CHANNEL_FORM = "form"

AI_SCREENING_IT = [
    r"decisi\w* (?:basat\w+ (?:unicamente|esclusivamente|solo) )?su (?:un )?trattamento automatizzato",
    r"processo decisionale automatizzato", r"\bprofilazion\w+", r"art(?:icolo|\.)?\s*22\b.{0,40}(?:GDPR|Regolamento|2016/679)",
    r"intelligenza artificiale", r"\bIA\b|\bAI\b", r"sistem\w+ di (?:IA|intelligenza artificiale)",
    r"(?:screening|selezione|valutazione|analisi|filtr\w+) (?:automatic\w+|automatizzat\w+) (?:dei|del) (?:CV|curricul\w+|candidat\w+)",
    r"punteggio di (?:compatibilit|corrispondenz|affinit|matching)", r"\bmatching\b", r"\branking\b",
    r"algoritm\w+", r"AI Act|Regolamento \(UE\) 2024/1689", r"alto rischio",
]
AI_SCREENING_EN = [
    r"automated decision[- ]making", r"\bprofiling\b", r"article 22\b.{0,40}GDPR",
    r"artificial intelligence|\bAI\b|machine learning", r"automated employment decision tool|\bAEDT\b",
    r"bias audit", r"Local Law 144", r"(?:automatically|automated) (?:screen|rank|score|assess|filter)\w*",
    r"(?:match|fit|compatibility) score", r"knock[- ]?out questions?", r"AI Act|Regulation \(EU\) 2024/1689",
    r"human (?:review|oversight|intervention)", r"opt[- ]out of (?:automated|AI)",
]
NEGATION_HINTS = [
    r"non (?:viene|vengono|sono|e|è|sara|sarà) (?:effettuat|utilizzat|adottat|previst|attuat)\w* .{0,60}(?:automatizzat|profilazion)",
    r"no automated decision", r"does not use .{0,30}(?:automated|AI)",
    # added: the usual Italian wording of art. 13(2)(f) GDPR notices
    r"non (?:e|è) previst\w+ (?:alcun |un )?(?:processo decisionale automatizzato|trattamento automatizzato|profilazion)",
    r"nessun (?:processo decisionale automatizzato|trattamento automatizzato|processo di profilazione)",
    r"non (?:sara|sarà|saranno|verra|verrà|verranno) (?:sottopost|soggett)\w* a (?:una |alcuna )?(?:decision|profilazion|processo decisionale)",
    r"(?:is|are|will) not (?:be )?(?:subject|subjected) to (?:a |any )?(?:decision based solely on )?automated",
    r"no profiling", r"not used for profiling",
]
# A statement is about candidates only when the sentence (or its neighbours) talks about
# recruitment; this keeps the many cookie-policy "profilazione" sentences out.
_RECRUITMENT_CONTEXT = re.compile(
    r"candidat|curricul|\bcv\b|selezion|recruit|assunzion|lavora con noi|posizion\w* aperte|"
    r"applicant|job application|hiring|recruitment|resume|vacanc|personale",
    re.I,
)
_COOKIE_CONTEXT = re.compile(r"cookie|pubblicit|advertis|marketing|newsletter|navigazione|browsing", re.I)
# Strong patterns: enough on their own (in recruitment context) to say "declared".
_STRONG = {
    r"processo decisionale automatizzato", r"automated decision[- ]making",
    r"decisi\w* (?:basat\w+ (?:unicamente|esclusivamente|solo) )?su (?:un )?trattamento automatizzato",
    r"(?:screening|selezione|valutazione|analisi|filtr\w+) (?:automatic\w+|automatizzat\w+) (?:dei|del) (?:CV|curricul\w+|candidat\w+)",
    r"(?:automatically|automated) (?:screen|rank|score|assess|filter)\w*",
    r"automated employment decision tool|\bAEDT\b", r"bias audit", r"Local Law 144",
    r"art(?:icolo|\.)?\s*22\b.{0,40}(?:GDPR|Regolamento|2016/679)", r"article 22\b.{0,40}GDPR",
    r"punteggio di (?:compatibilit|corrispondenz|affinit|matching)", r"(?:match|fit|compatibility) score",
}
# AI words: "declared" only when the same sentence speaks of processing applications.
_AI_TERMS = {
    r"intelligenza artificiale", r"artificial intelligence|\bAI\b|machine learning",
    r"AI Act|Regolamento \(UE\) 2024/1689", r"AI Act|Regulation \(EU\) 2024/1689",
    r"sistem\w+ di (?:IA|intelligenza artificiale)", r"\bIA\b|\bAI\b",
}
_PROCESSING = re.compile(r"\bcv\b|curricul|candidat|application|selezion|screening|valutazion|recruit|"
                         r"applicant|resume", re.I)
# The list of a data subject's rights ("diritto di non essere sottoposto a una decisione
# basata unicamente sul trattamento automatizzato") is not a statement of use.
_RIGHTS = re.compile(r"diritt\w* (?:a |di )?(?:non essere|opporsi|ottenere l.intervento)|"
                     r"right (?:not to be subject|to object|to obtain human)", re.I)
MAX_QUOTE = 400
MAX_STATEMENTS = 6


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _compile(pattern: str) -> re.Pattern:
    # "AI"/"IA" only in capitals: with re.I they would match the Italian preposition "ai".
    text = _fold(pattern).replace(r"\bAI\b", r"(?-i:\bAI\b)").replace(r"\bIA\b", r"(?-i:\bIA\b)")
    return re.compile(text, re.I)


_POSITIVE = [(p, _compile(p)) for p in AI_SCREENING_IT + AI_SCREENING_EN]
_NEGATIVE = [re.compile(_fold(p), re.I) for p in NEGATION_HINTS]
_SENTENCE_END = re.compile(r"(?<=[.;!?])\s+(?=[A-ZÀ-Ý0-9«\"(])")


@dataclass
class Statement:
    kind: str  # "states_ai" | "states_no_automated" | "mentions" (weak, shown as context)
    quote: str
    url: str
    patterns: list[str] = field(default_factory=list)


@dataclass
class PlatformHit:
    key: str  # ATS_KNOWLEDGE key, or "email" / "form"
    name: str
    url: str  # where it was seen (link, iframe or page)


@dataclass
class ScreeningReport:
    platforms: list[PlatformHit] = field(default_factory=list)
    statements: list[Statement] = field(default_factory=list)
    parsing: str = "unknown"  # "likely" | "unknown"
    ai: str = "no_evidence"  # "declared" | "platform_capable" | "no_evidence"
    advice: list[str] = field(default_factory=list)  # keys, English texts in ADVICE_TEXTS
    pages_checked: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


ADVICE_TEXTS = {
    "text_pdf": "Send a text-based PDF (selectable text) or DOCX, never a scan or a photo of the CV.",
    "single_column": "This platform reads the CV automatically: use one column, no tables, text boxes, "
                     "icons or text in headers/footers for key data.",
    "standard_headings": "Use standard section titles (Experience / Esperienza, Education / Istruzione, "
                         "Skills / Competenze) and dates as MM/YYYY.",
    "knockout_truthful": "Answer the application questions truthfully: they are set by the employer and "
                         "may exclude applications automatically.",
    "keywords": "Repeat the exact wording of the posting's key requirements where they are true for you.",
    "email_subject": "By e-mail: a clear subject (position + your name), the CV as PDF named "
                     "Surname_Name_CV.pdf, a short message in the body.",
    "ai_declared": "The employer states automated or AI processing: you may ask for human review "
                   "(art. 22 GDPR) and keep the CV factual and verifiable.",
}


def vendor_for_url(url: str) -> Vendor | None:
    """Vendor of the knowledge table whose host appears in the URL."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    for vendor in ATS_KNOWLEDGE.values():
        if any(host == h or host.endswith("." + h) for h in vendor.hosts):
            return vendor
    if "zucchetti" in host and "recruit" in url.lower():
        return ATS_KNOWLEDGE["inrecruiting"]
    return None


_EMAIL_INVITE = re.compile(
    r"(?:invia\w*|manda\w*|inoltra\w*|send|e-?mail)\b.{0,60}(?:cv|curricul\w*|candidatur\w*|resume|application)",
    re.I,
)


def detect_platforms(pages: list[dict], postings_source: str = "", careers_url: str | None = None) -> list[PlatformHit]:
    """Selection systems seen on the careers pages: ATS (links, iframes, scripts, the
    postings source), else the plain channels "email" (a mailto + an invitation to send
    the CV) and "form" (an upload form on the site)."""
    hits: list[PlatformHit] = []
    seen: set[str] = set()

    def add(key: str, name: str, url: str) -> None:
        if key not in seen:
            seen.add(key)
            hits.append(PlatformHit(key, name, url))

    if postings_source.startswith("ats:"):
        key = postings_source[4:]
        vendor = ATS_KNOWLEDGE.get(key)
        add(key, vendor.name if vendor else key, careers_url or "")
    if careers_url:
        vendor = vendor_for_url(careers_url)
        if vendor:
            add(vendor.key, vendor.name, careers_url)
    for page in pages:
        html, url = page.get("html") or "", page.get("url") or ""
        for ref in detect_ats(html, url):
            vendor = ATS_KNOWLEDGE.get(ref.platform)
            add(ref.platform, vendor.name if vendor else ref.platform, getattr(ref, "board_url", url))
        tree = analyze_html(html)
        sources = [link for link, _text in extract_links(html, url, tree)]
        if tree is not None:
            sources += [el.get("src") or "" for el in tree.iter("iframe", "script")]
        for link in sources:
            if not link:
                continue
            vendor = vendor_for_url(link if "://" in link else "https:" + link if link.startswith("//") else "")
            if vendor is None and is_unsupported_ats(link):
                host = (urlsplit(link).hostname or "").lower()
                add(host, host, link)
            elif vendor is not None:
                add(vendor.key, vendor.name, link)
    if not any(h.key in ATS_KNOWLEDGE for h in hits):
        for page in pages:
            html, url = page.get("html") or "", page.get("url") or ""
            text = page.get("text") or ""
            lower = html.lower()
            if "mailto:" in lower and _EMAIL_INVITE.search(text):
                add(CHANNEL_EMAIL, "E-mail", url)
            if re.search(r"<input[^>]+type=[\"']?file", lower) or re.search(r"<form[^>]*>(?:(?!</form>).)*(?:cv|curriculum)", lower, re.S):
                add(CHANNEL_FORM, "Form on the website", url)
    return hits


def _sentences(text: str) -> list[str]:
    text = " ".join((text or "").split())
    return [s.strip() for s in _SENTENCE_END.split(text) if s.strip()]


def scan_statements(pages: list[dict]) -> list[Statement]:
    """Explicit statements about automated decisions / AI on careers and privacy pages.

    Every quote is verbatim (whitespace collapsed, cut at MAX_QUOTE) with its page URL.
    """
    found: list[Statement] = []
    quotes: set[str] = set()
    for page in pages:
        url = page.get("url") or ""
        sentences = _sentences(page.get("text") or "")
        for i, sentence in enumerate(sentences):
            folded = _fold(sentence)
            negative = any(p.search(folded) for p in _NEGATIVE)
            matched = [p for p, rx in _POSITIVE if rx.search(folded)]
            if not matched and not negative:
                continue
            window = " ".join(sentences[max(0, i - 1): i + 2])
            if not _RECRUITMENT_CONTEXT.search(_fold(window)):
                continue
            if _COOKIE_CONTEXT.search(folded) and not _RECRUITMENT_CONTEXT.search(folded):
                continue
            if negative:
                kind = "states_no_automated"
            elif _RIGHTS.search(folded):
                kind = "mentions"
            elif any(p in _STRONG for p in matched):
                kind = "states_ai"
            elif any(p in _AI_TERMS for p in matched) and _PROCESSING.search(folded):
                kind = "states_ai"
            else:
                kind = "mentions"
            quote = sentence if len(sentence) <= MAX_QUOTE else sentence[:MAX_QUOTE].rsplit(" ", 1)[0] + " …"
            if quote in quotes:
                continue
            quotes.add(quote)
            found.append(Statement(kind, quote, url, matched))
    order = {"states_ai": 0, "states_no_automated": 1, "mentions": 2}
    found.sort(key=lambda s: order[s.kind])
    return found[:MAX_STATEMENTS]


def assess(platforms: list[PlatformHit], statements: list[Statement], pages_checked: list[str] | None = None
           ) -> ScreeningReport:
    """Indicators and advice from the platform hits (a) and the employer's statements (b)."""
    report = ScreeningReport(platforms=list(platforms), statements=list(statements),
                             pages_checked=list(pages_checked or []))
    vendors = [ATS_KNOWLEDGE[h.key] for h in platforms if h.key in ATS_KNOWLEDGE]
    if any(v.parsing in ("yes", "basic") for v in vendors):
        report.parsing = "likely"
    if any(s.kind == "states_ai" for s in statements):
        report.ai = "declared"
    elif any(v.ai in ("yes", "assistive") for v in vendors):
        report.ai = "platform_capable"
    else:
        report.ai = "no_evidence"
    advice = ["text_pdf", "standard_headings"]
    if vendors:
        advice.insert(1, "single_column")
        advice += ["knockout_truthful", "keywords"]
    if any(h.key == CHANNEL_EMAIL for h in platforms) and not vendors:
        advice.append("email_subject")
    if report.ai == "declared":
        advice.append("ai_declared")
    report.advice = advice
    return report


def privacy_links(html: str, base_url: str, maximum: int = 1) -> list[str]:
    """Links of a careers page to the candidates' privacy notice ("informativa candidati")."""
    scored: list[tuple[int, str]] = []
    for url, text in extract_links(html, base_url):
        probe = _fold(f"{text} {urlsplit(url).path}").lower()
        if not re.search(r"privacy|informativa|gdpr|data protection|trattamento dei dati", probe):
            continue
        score = 2 if re.search(r"candidat|recruit|selezion|applicant|curricul|lavora", probe) else 1
        if urlsplit(url).path.lower().endswith(".pdf"):
            continue
        scored.append((score, url))
    scored.sort(key=lambda s: -s[0])
    result: list[str] = []
    for _score, url in scored:
        if url not in result:
            result.append(url)
    return result[:maximum]
