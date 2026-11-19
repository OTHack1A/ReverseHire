""""Paste and review" for pages the app must not read automatically (LinkedIn job,
LinkedIn company "About", Indeed job, any other page): the user copies the page in the
browser and pastes it; this module only reads the pasted text (or the clipboard HTML).
Nothing is ever fetched, not even the optional URL.

    draft = parse_pasted(text, html=clipboard_html, hint_url=url)
    draft.kind        "linkedin_job" | "linkedin_company" | "indeed_job" | "generic_job"
    draft.fields      {"title": DraftField(value, confidence, line), ...}
    draft.to_posting()          -> jobsearch Posting (source "paste:<site>")
    draft.to_company_fields()   -> dict for the company card form

Confidence per field: "high" (read next to a label or a fixed pattern), "medium" (by
position in a recognised layout), "low" (a guess). The review form must show "low"
and "medium" fields as to be checked.

CAVEAT: the page layouts here are written from memory of the 2025-26 interfaces
(RECRUITER_STUDY §2.6 [M]); the parser works on heading/label heuristics in Italian and
English and is tolerant, but it must be tuned on real pastes from the owner's browser.

Privacy: pasted pages contain other people's names ("People you can reach out to",
"Meet the hiring team", connection lines "· 2nd"): those lines are dropped and never
stored (`removed_people` counts them).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from urllib.parse import urlsplit

from . import lexicon
from ._util import compact, fold, iso, log
from .models import Posting
from .normalize import finish

HIGH, MEDIUM, LOW = "high", "medium", "low"
MAX_TITLE_WORDS = 16


@dataclass
class DraftField:
    value: str
    confidence: str = LOW
    line: int | None = None   # index in `PastedDraft.lines` the value was read from

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PastedDraft:
    kind: str
    site: str
    language: str | None
    fields: dict[str, DraftField] = field(default_factory=dict)
    original_text: str = ""
    lines: list[str] = field(default_factory=list)
    url: str = ""
    removed_people: int = 0
    warnings: list[str] = field(default_factory=list)

    def value(self, name: str, default: str = "") -> str:
        item = self.fields.get(name)
        return item.value if item else default

    def to_dict(self) -> dict:
        data = asdict(self)
        data["fields"] = {k: v.to_dict() for k, v in self.fields.items()}
        return data

    def to_posting(self, today: date | None = None) -> Posting:
        """The reviewed draft as a posting (source "paste:<site>")."""
        location = self.value("location") or self.value("workplace_address")
        town = location.split(",")[0].split("(")[0].strip() if location else None
        workplace = fold(self.value("workplace"))
        posting = Posting(
            source=f"paste:{self.site}", url=self.url, title=self.value("title"),
            company=self.value("company") or None, location=location, town=town or None,
            description=self.value("description"), date_posted=self.value("posted") or None,
            salary=self.value("salary") or None, industry=self.value("industry") or None)
        for words, value in (("remote", "remote"), ("hybrid", "hybrid"), ("onsite", "onsite")):
            if workplace == words:
                posting.remote = value
        contract = self.value("contract")
        if contract in lexicon.CONTRACT_ACCEPTS or contract in ("permanent_possible", "temporary_agency"):
            posting.contract = contract
        hours = self.value("hours")
        if hours in ("full_time", "part_time"):
            posting.hours = hours
        extra = "\n".join(x for x in (self.value("contract_text"), self.value("salary"), self.value("hours_text"),
                                      self.value("benefits"), self.value("experience")) if x)
        size = self.value("size")
        if size:
            posting.employer_hints = {"size_text": size}
        return finish(posting, posting.description + ("\n" + extra if extra else ""))

    def to_company_fields(self) -> dict:
        """Fields for the company card form (all to be reviewed by the user)."""
        names = ("company", "website", "phone", "industry", "size", "headquarters", "founded", "specialties",
                 "overview", "followers", "locations")
        result = {n: self.value(n) for n in names if self.value(n)}
        result["source"] = f"paste:{self.site}"
        if self.url:
            result["source_url"] = self.url
        return result


# --- markers ----------------------------------------------------------------------------

_LI_JOB = ["informazioni sull'offerta di lavoro", "about the job", "candidatura semplice", "easy apply",
           "le candidature sono gestite fuori da linkedin", "responses managed off linkedin",
           "persone che puoi contattare", "people you can reach out to", "incontra il team di selezione",
           "meet the hiring team", "informazioni sull'azienda", "about the company"]
_LI_COMPANY = ["panoramica", "overview", "sito web", "website", "dimensioni dell'azienda", "company size",
               "sede principale", "headquarters", "specializzazioni", "specialties", "anno di fondazione",
               "founded", "membri associati", "associated members"]
_INDEED = ["descrizione completa dell'offerta di lavoro", "full job description", "tipo di offerta di lavoro",
           "job type", "su 5 stelle", "out of 5 stars", "segnala offerta", "report job", "dettagli dell'offerta",
           "job details", "indeed"]

_NOISE = {
    "candidati", "candidati ora", "apply", "apply now", "candidatura semplice", "easy apply", "salva", "save",
    "salvato", "saved", "mostra di piu", "show more", "mostra meno", "show less", "altro", "more", "see more",
    "vedi altro", "segnala offerta", "report job", "imposta un avviso", "set alert", "crea un avviso",
    "condividi", "share", "segui", "follow", "+ segui", "+ follow", "messaggio", "message", "promossa",
    "promoted", "attivamente alla ricerca di candidati", "actively recruiting", "prova premium", "try premium",
    "home", "rete", "my network", "lavoro", "jobs", "messaggistica", "messaging", "notifiche", "notifications",
    "tu", "me", "per le aziende", "for business", "visualizza pagina", "view page", "visita il sito web",
    "visit website", "mostra tutti", "show all", "indietro", "back", "chiudi", "close", "...", "…",
    "cerca", "search", "accedi", "sign in", "iscriviti", "join now", "copia link", "copy link",
    "le candidature sono gestite fuori da linkedin", "responses managed off linkedin",
    "visualizza il profilo", "view profile", "invia messaggio", "send message", "salva l'offerta",
}
_NOISE_PATTERNS = [
    re.compile(r"^(?:…|\.\.\.)?\s*(mostra di piu|show more|altro|more|see more)$"),
    re.compile(r"^\d+\s+(notifiche|notifications)$"),
    re.compile(r"^(ha risposto|responds?) .*%"),
    re.compile(r"^(scopri|see) (come|how) .*premium"),
    re.compile(r"^\W+$"),
]
_PEOPLE_HEADINGS = re.compile(
    r"^(persone che puoi contattare|people you can reach out to|incontra il team di selezione|"
    r"meet the hiring team|persone che conosci|people you know|dipendenti che conosci|"
    r"connessioni in comune|mutual connections|chi ha pubblicato l'offerta|job poster|"
    r"recruiter dell'offerta|hiring team)\b")
_CONNECTION = re.compile(r"·\s*(?:1°|2°|3°|3°\+|1st|2nd|3rd|3rd\+)\b|\b(?:1°|2°|3°)\s*(?:grado|livello)\b")
_PERSON_ROLE = re.compile(r"\b(?:è il recruiter|is hiring|sta assumendo|ha pubblicato|posted this job|"
                          r"job poster|autore dell'offerta)\b", re.I)

_HEAD_DESCRIPTION = re.compile(r"^(informazioni sull'offerta di lavoro|about the job|descrizione completa "
                               r"dell'offerta di lavoro|full job description|descrizione dell'offerta|"
                               r"job description|descrizione)$")
_HEAD_TAIL = re.compile(r"^(informazioni sull'azienda|about the company|altre offerte di lavoro.*|more jobs.*|"
                        r"offerte di lavoro simili.*|similar jobs.*|assunzioni|hiring insights|"
                        r"insight sulle assunzioni.*|le persone hanno visualizzato anche.*|people also viewed.*|"
                        r"cerca altre offerte.*|esplora .*|segnala offerta|report job|"
                        r"come ti posizioni.*|how you match.*|valutazione dei requisiti.*)$")
_RELATIVE = re.compile(r"(\d+)\s*(minut\w*|or[ae]\b|ore\b|hours?|giorn\w*|days?|settiman\w*|weeks?|mes[ei]|"
                       r"months?|ann[oi]|years?)\s*(?:fa|ago)?")
_APPLICANTS = re.compile(r"candidat|applicant|clic su candidati|clicked apply|persone hanno cliccato")
_SALARY = re.compile(r"(?:€|\beur\b|euro|\bral\b|\$|£)\s*\d|\d[\d.,\s]*(?:k\b|€|\beur\b|euro)", re.I)
_LEGAL_FORM = re.compile(r"\b(s\.?\s?p\.?\s?a\.?|s\.?\s?r\.?\s?l\.?s?|s\.?\s?n\.?\s?c\.?|s\.?\s?a\.?\s?s\.?|"
                         r"gmbh|ltd|inc\.?|llc|ag|scarl|soc\.? coop\.?)$|\b(spa|srl)\b", re.I)
_RATING = re.compile(r"(\d[,.]\d)\s*(?:su 5 stelle|out of 5 stars)|^\d[,.]\d$")

_WORKPLACE = [("in sede", "onsite"), ("on-site", "onsite"), ("on site", "onsite"), ("ibrido", "hybrid"),
              ("hybrid", "hybrid"), ("da remoto", "remote"), ("remoto", "remote"), ("remote", "remote")]
_TYPE = [("tempo pieno", "hours", "full_time"), ("full-time", "hours", "full_time"), ("full time", "hours", "full_time"),
         ("part-time", "hours", "part_time"), ("part time", "hours", "part_time"),
         ("tempo parziale", "hours", "part_time"),
         ("tempo indeterminato", "contract", "permanent"), ("permanent", "contract", "permanent"),
         ("tempo determinato", "contract", "fixed_term"), ("temporaneo", "contract", "fixed_term"),
         ("temporary", "contract", "fixed_term"), ("contratto", "contract", "fixed_term"),
         ("contract", "contract", "fixed_term"), ("stage", "contract", "internship"),
         ("tirocinio", "contract", "internship"), ("internship", "contract", "internship"),
         ("apprendistato", "contract", "apprenticeship"), ("apprenticeship", "contract", "apprenticeship"),
         ("somministrazione", "contract", "temporary_agency"), ("freelance", "contract", "freelance"),
         ("partita iva", "contract", "freelance")]

# label (folded) -> field, for "label: value" lines and label-alone lines
_LABELS = {
    "sito web": "website", "website": "website", "telefono": "phone", "phone": "phone",
    "settore": "industry", "industry": "industry", "dimensioni dell'azienda": "size", "company size": "size",
    "sede principale": "headquarters", "headquarters": "headquarters", "anno di fondazione": "founded",
    "fondata nel": "founded", "founded": "founded", "specializzazioni": "specialties", "specialties": "specialties",
    "tipo": "company_type", "type": "company_type",
    "retribuzione": "salary", "pay": "salary", "salary": "salary", "stipendio": "salary", "ral": "salary",
    "tipo di offerta di lavoro": "contract_text", "job type": "contract_text", "tipo di contratto": "contract_text",
    "contratto": "contract_text", "orario": "hours_text", "schedule": "hours_text", "turni": "hours_text",
    "shift and schedule": "hours_text", "benefit": "benefits", "benefits": "benefits",
    "esperienza": "experience", "experience": "experience", "lingua": "language", "language": "language",
    "sede di lavoro": "workplace_address", "luogo di lavoro": "workplace_address", "work location": "workplace_address",
    "location": "location", "sede": "location", "localita": "location", "luogo": "location",
}


def _clean_lines(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", " ").replace("​", "")
    lines = []
    previous = None
    for raw in text.split("\n"):
        line = compact(raw)
        if not line:
            continue
        if line == previous:  # LinkedIn repeats the title (sticky header)
            continue
        lines.append(line)
        previous = line
    return lines


def _html_lines(html: str) -> str:
    try:
        from ..web.text import fragment_lines

        return fragment_lines(html, 60000)
    except Exception:
        return re.sub(r"<[^>]+>", "\n", html or "")


def _is_noise(line: str) -> bool:
    folded = fold(line).strip(" .:·•")
    if folded in _NOISE:
        return True
    if not re.search(r"[a-z0-9]", folded):
        return True  # emoji / symbols only
    return any(p.search(folded) for p in _NOISE_PATTERNS)


def _drop_people(lines: list[str]) -> tuple[list[str], list[str]]:
    """Remove people blocks and connection lines (never stored): (kept, removed)."""
    kept: list[str] = []
    removed: list[str] = []
    skipping = 0
    for line in lines:
        folded = fold(line)
        if _PEOPLE_HEADINGS.search(folded):
            skipping = 8
            removed.append(line)
            continue
        if skipping:
            if _HEAD_DESCRIPTION.match(folded) or _HEAD_TAIL.match(folded) or folded in ("panoramica", "overview"):
                skipping = 0
            else:
                skipping -= 1
                removed.append(line)
                continue
        if _CONNECTION.search(line) or _PERSON_ROLE.search(line):
            removed.append(line)
            continue
        kept.append(line)
    return kept, removed


def _without(original: str, removed: list[str]) -> str:
    """The pasted text verbatim, minus the removed people lines."""
    drop = {compact(unicodedata.normalize("NFKC", r)) for r in removed}
    if not drop:
        return original
    return "\n".join(raw for raw in original.splitlines()
                     if compact(unicodedata.normalize("NFKC", raw).replace(" ", " ")) not in drop)


def _site(hint_url: str | None) -> str | None:
    host = (urlsplit(hint_url or "").hostname or "").lower() if hint_url and "://" in hint_url else \
        (urlsplit("https://" + (hint_url or "")).hostname or "").lower() if hint_url else ""
    if "linkedin." in host:
        return "linkedin"
    if "indeed." in host:
        return "indeed"
    if "infojobs." in host:
        return "infojobs"
    return None


def detect_kind(lines: list[str], hint_url: str | None = None) -> tuple[str, str]:
    """(kind, site) from the URL host or the marker lines."""
    folded = [fold(line) for line in lines]
    joined = "\n".join(folded)

    def hits(markers):
        return sum(1 for m in markers if any(line == m or line.startswith(m) for line in folded) or
                   (len(m) > 12 and m in joined))
    site = _site(hint_url)
    li_job, li_company, indeed = hits(_LI_JOB), hits(_LI_COMPANY), hits(_INDEED)
    if site == "linkedin" or (site is None and max(li_job, li_company) >= 2 and max(li_job, li_company) >= indeed):
        path = urlsplit(hint_url or "").path if hint_url else ""
        if "/company/" in path or (li_company >= 3 and li_company > li_job):
            return "linkedin_company", "linkedin"
        return "linkedin_job", "linkedin"
    if site == "indeed" or (site is None and indeed >= 2):
        return "indeed_job", "indeed"
    return "generic_job", site or "generic"


def _relative_date(text: str, today: date) -> date | None:
    folded = fold(text)
    if re.search(r"\b(ieri|yesterday)\b", folded):
        return today - timedelta(days=1)
    if re.search(r"\b(oggi|today|ora|just now|adesso)\b", folded):
        return today
    found = _RELATIVE.search(folded)
    if not found:
        return None
    n, unit = int(found.group(1)), found.group(2)
    if unit.startswith(("minut", "or", "hour")):
        return today
    if unit.startswith(("giorn", "day")):
        return today - timedelta(days=n)
    if unit.startswith(("settiman", "week")):
        return today - timedelta(weeks=n)
    if unit.startswith(("mes", "month")):
        return today - timedelta(days=30 * n)
    return today - timedelta(days=365 * n)


def _chips(line: str) -> dict[str, str]:
    """Workplace / contract / hours values named in a chip line."""
    folded = fold(line)
    found: dict[str, str] = {}
    for word, value in _WORKPLACE:
        if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", folded):
            found.setdefault("workplace", value)
    for word, name, value in _TYPE:
        if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", folded):
            found.setdefault(name, value)
    return found


def _chip_line(line: str) -> bool:
    """A short line made only of chips (no sentence)."""
    if len(line) > 80 or line.endswith("."):
        return False
    chips = _chips(line)
    if not chips:
        return False
    rest = fold(line)
    for word, _ in _WORKPLACE:
        rest = rest.replace(word, " ")
    for word, _, _ in _TYPE:
        rest = rest.replace(word, " ")
    rest = re.sub(r"[·•|,/\-–]|\b(e|and|corrisponde|matches|le tue preferenze|your preferences)\b", " ", rest)
    return len(rest.split()) <= 2


def _segment_chips(line: str) -> dict[str, str]:
    """Chips of the "·"-separated segments of a line that are chips alone: "Bologna, Italia ·
    2 weeks ago · Full-time · Hybrid" or "Full-time · Hybrid · Mid-Senior level" (the place,
    the date and the seniority are not chips and do not hide the chips beside them)."""
    found: dict[str, str] = {}
    for segment in re.split(r"[·•|]", line):
        if segment.strip() and _chip_line(segment.strip()):
            for name, value in _chips(segment).items():
                found.setdefault(name, value)
    return found


def _label_value(lines: list[str], start: int = 0) -> dict[str, DraftField]:
    """"Label: value" lines, and label-alone lines followed by the value."""
    result: dict[str, DraftField] = {}
    for index in range(start, len(lines)):
        line = lines[index]
        folded = fold(line)
        match = re.match(r"^([^:]{2,40}):\s*(.+)$", line)
        if match and fold(match.group(1)) in _LABELS:
            name = _LABELS[fold(match.group(1))]
            result.setdefault(name, DraftField(compact(match.group(2)), HIGH, index))
            continue
        if folded in _LABELS and index + 1 < len(lines):
            name = _LABELS[folded]
            value = lines[index + 1]
            if fold(value) not in _LABELS:
                result.setdefault(name, DraftField(value, HIGH, index + 1))
    return result


def _description(lines: list[str], start: int) -> tuple[str, int]:
    """Lines from `start` to the next tail heading; (text, end index)."""
    end = len(lines)
    for index in range(start, len(lines)):
        if _HEAD_TAIL.match(fold(lines[index])):
            end = index
            break
    return "\n".join(lines[start:end]).strip(), end


# Fields whose line breaks are kept (a description's requirement list must stay one item
# per line for the match with the CV).
MULTILINE_FIELDS = ("description", "overview", "benefits", "specialties")


def _set(fields: dict, name: str, value, confidence: str, line: int | None) -> None:
    if name in MULTILINE_FIELDS:
        lines = (compact(part) for part in str(value or "").splitlines())
        value = "\n".join(part for part in lines if part)
    else:
        value = compact(str(value or ""))
    if not value:
        return
    current = fields.get(name)
    order = {HIGH: 3, MEDIUM: 2, LOW: 1}
    if current is None or order[confidence] > order[current.confidence]:
        fields[name] = DraftField(value, confidence, line)


def _title_like(line: str) -> bool:
    sentence_end = line.endswith(".") and not _LEGAL_FORM.search(line)
    return 2 <= len(line) <= 140 and len(line.split()) <= MAX_TITLE_WORDS and not sentence_end and \
        not _SALARY.search(line) and "·" not in line and not _chip_line(line)


def _parse_linkedin_job(lines: list[str], fields: dict, today: date) -> None:
    folded = [fold(line) for line in lines]
    head = next((i for i, f in enumerate(folded) if f in ("informazioni sull'offerta di lavoro", "about the job")), None)
    header_end = head if head is not None else min(len(lines), 12)
    dot_line = None
    for index in range(header_end):
        line = lines[index]
        if "·" in line and (_RELATIVE.search(folded[index]) or _APPLICANTS.search(folded[index]) or
                            re.search(r"\b(fa|ago|ripubblicat\w*|reposted)\b", folded[index])):
            dot_line = index
            parts = [compact(p) for p in line.split("·") if compact(p)]
            if parts and not _RELATIVE.search(fold(parts[0])) and not _APPLICANTS.search(fold(parts[0])):
                _set(fields, "location", parts[0], HIGH, index)
            for name, value in _segment_chips(line).items():
                _set(fields, name, value, HIGH, index)
            for part in parts[1:]:
                if _APPLICANTS.search(fold(part)):
                    _set(fields, "applicants", part, HIGH, index)
                else:
                    posted = _relative_date(part, today)
                    if posted:
                        _set(fields, "posted", iso(posted), MEDIUM, index)
            break
    candidates = [i for i in range(dot_line if dot_line is not None else header_end)
                  if _title_like(lines[i]) and not _RATING.search(folded[i])]
    if candidates:
        title_index = candidates[-1]
        _set(fields, "title", lines[title_index], MEDIUM if dot_line is not None else LOW, title_index)
        before = [i for i in candidates if i < title_index]
        if before:
            _set(fields, "company", lines[before[-1]], MEDIUM if dot_line is not None else LOW, before[-1])
    for index in range((dot_line or 0), header_end):
        chips = _chips(lines[index]) if _chip_line(lines[index]) else _segment_chips(lines[index])
        if chips:
            for name, value in chips.items():
                _set(fields, name, value, HIGH, index)
        elif _SALARY.search(lines[index]) and len(lines[index]) < 80:
            _set(fields, "salary", lines[index], MEDIUM, index)
    if head is not None:
        text, end = _description(lines, head + 1)
        _set(fields, "description", text, HIGH, head + 1)
        tail = lines[end:]
        if tail and fold(tail[0]) in ("informazioni sull'azienda", "about the company"):
            for offset, line in enumerate(tail[1:8], start=end + 1):
                folded_line = fold(line)
                if re.search(r"\b\d[\d.,]*\s*follower", folded_line):
                    _set(fields, "followers", line, HIGH, offset)
                elif re.search(r"dipendenti|employees", folded_line):
                    parts = [compact(p) for p in line.split("·") if compact(p)]
                    for part in parts:
                        if re.search(r"dipendenti|employees", fold(part)):
                            _set(fields, "size", part, HIGH, offset)
                        else:
                            _set(fields, "industry", part, MEDIUM, offset)
                elif "company" in fields and fold(line) == fold(fields["company"].value):
                    fields["company"].confidence = HIGH
    else:
        start = (dot_line + 1) if dot_line is not None else header_end
        text, _ = _description(lines, start)
        _set(fields, "description", text, LOW, start)


def _parse_linkedin_company(lines: list[str], fields: dict) -> None:
    folded = [fold(line) for line in lines]
    overview = next((i for i, f in enumerate(folded) if f in ("panoramica", "overview")), None)
    first = next((i for i in range(overview if overview is not None else len(lines)) if _title_like(lines[i])), None)
    if first is not None:
        _set(fields, "company", lines[first], MEDIUM, first)
    for index, line in enumerate(lines[: overview or len(lines)]):
        if re.search(r"\b\d[\d.,]*\s*follower", folded[index]):
            parts = [compact(p) for p in line.split("·") if compact(p)]
            for part in parts:
                if re.search(r"follower", fold(part)):
                    _set(fields, "followers", part, HIGH, index)
                elif re.search(r"dipendenti|employees", fold(part)):
                    _set(fields, "size", part, MEDIUM, index)
    labels = _label_value(lines)
    for name, item in labels.items():
        if name in ("website", "phone", "industry", "size", "headquarters", "founded", "specialties"):
            _set(fields, name, item.value, item.confidence, item.line)
    if overview is not None:
        stop = next((i for i in range(overview + 1, len(lines)) if folded[i] in _LABELS), len(lines))
        _set(fields, "overview", "\n".join(lines[overview + 1: stop]), HIGH, overview + 1)
    locations = next((i for i, f in enumerate(folded) if re.match(r"^(sedi|locations)\b", f)), None)
    if locations is not None:
        addresses = [line for line in lines[locations + 1: locations + 12]
                     if not _HEAD_TAIL.match(fold(line)) and not re.match(r"^(indicazioni|get directions)", fold(line))]
        if addresses:
            _set(fields, "locations", "; ".join(addresses[:8]), MEDIUM, locations + 1)
    for index, line in enumerate(lines):
        if re.search(r"membri associati|associated members", folded[index]):
            _set(fields, "members", line, HIGH, index)


def _parse_indeed(lines: list[str], fields: dict) -> None:
    folded = [fold(line) for line in lines]
    head = next((i for i, f in enumerate(folded)
                 if f in ("descrizione completa dell'offerta di lavoro", "full job description")), None)
    header_end = head if head is not None else min(len(lines), 10)
    titles = [i for i in range(header_end) if _title_like(lines[i]) and not _RATING.search(folded[i])
              and fold(lines[i]) not in _LABELS]
    if titles:
        _set(fields, "title", lines[titles[0]], MEDIUM, titles[0])
        rating = next((i for i in range(titles[0] + 1, header_end) if _RATING.search(folded[i])), None)
        after = [i for i in titles[1:]]
        if after:
            company_index = after[0]
            confidence = HIGH if rating is not None and rating in (company_index + 1, company_index + 2) else MEDIUM
            _set(fields, "company", re.sub(r"\s*\d[,.]\d.*$", "", lines[company_index]), confidence, company_index)
            following = [i for i in after[1:] if i != rating]
            if following and not lexicon.is_agency_name(lines[following[0]]):
                _set(fields, "location", lines[following[0]], MEDIUM, following[0])
    for index in range(header_end):
        if _SALARY.search(lines[index]) and len(lines[index]) < 90:
            _set(fields, "salary", lines[index], MEDIUM, index)
        if _chip_line(lines[index]):
            for name, value in _chips(lines[index]).items():
                _set(fields, name, value, MEDIUM, index)
    labels = _label_value(lines)
    for name, item in labels.items():
        _set(fields, name, item.value, item.confidence, item.line)
    if "contract_text" in fields:
        for name, value in _chips(fields["contract_text"].value).items():
            _set(fields, name, value, HIGH, fields["contract_text"].line)
    if head is not None:
        text, _ = _description(lines, head + 1)
        _set(fields, "description", text, HIGH, head + 1)
    else:
        start = (titles[-1] + 1) if titles else 0
        text, _ = _description(lines, start)
        _set(fields, "description", text, LOW, start)


def _parse_generic(lines: list[str], fields: dict) -> None:
    folded = [fold(line) for line in lines]
    titles = [i for i in range(min(len(lines), 8)) if _title_like(lines[i]) and fold(lines[i]) not in _LABELS]
    if titles:
        _set(fields, "title", lines[titles[0]], LOW, titles[0])
    for index in range(min(len(lines), 15)):
        line = lines[index]
        if "company" not in fields and _LEGAL_FORM.search(line) and len(line) < 90 and index != (titles[0] if titles
                                                                                                     else -1):
            _set(fields, "company", line, MEDIUM, index)
        if "location" not in fields and len(line) < 80:
            town = next((t for t in lexicon.TOWNS if re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])",
                                                               folded[index])), None)
            if town or re.search(r"\(([A-Z]{2})\)", line):
                _set(fields, "location", line, LOW, index)
    labels = _label_value(lines)
    for name, item in labels.items():
        _set(fields, name, item.value, item.confidence, item.line)
    used = [f.line for name, f in fields.items() if name in ("title", "company", "location") and f.line is not None]
    start = (max(used) + 1) if used else 0
    head = next((i for i, f in enumerate(folded) if _HEAD_DESCRIPTION.match(f)), None)
    if head is not None:
        start = head + 1
    text, _ = _description(lines, start)
    _set(fields, "description", text, LOW if head is None else MEDIUM, start)


def parse_pasted(text: str, html: str | None = None, hint_url: str | None = None, *,
                 today: date | None = None) -> PastedDraft:
    """Read a pasted page. Never fetches anything, never raises."""
    today = today or date.today()
    original = text or ""
    try:
        source_text = original
        if html:
            from_html = _html_lines(html)
            if len(compact(from_html)) >= 0.5 * len(compact(original)):
                source_text = from_html
        lines = _clean_lines(source_text)
        lines, removed = _drop_people(lines)
        lines = [line for line in lines if not _is_noise(line)]
        kind, site = detect_kind(lines, hint_url)
        draft = PastedDraft(kind=kind, site=site, language=lexicon.detect_language("\n".join(lines)),
                            original_text=_without(original, removed), lines=lines, url=(hint_url or "").strip(),
                            removed_people=len(removed))
        if not lines:
            draft.warnings.append("empty")
            return draft
        if kind == "linkedin_job":
            _parse_linkedin_job(lines, draft.fields, today)
        elif kind == "linkedin_company":
            _parse_linkedin_company(lines, draft.fields)
        elif kind == "indeed_job":
            _parse_indeed(lines, draft.fields)
        else:
            _parse_generic(lines, draft.fields)
        if kind != "linkedin_company" and "description" in draft.fields:
            description = draft.fields["description"].value
            if "salary" not in draft.fields:
                try:
                    from ..web.posting_attrs import salary_from_text

                    found = salary_from_text(description)
                    if found is not None and found.salary:
                        _set(draft.fields, "salary", found.salary, MEDIUM, None)
                except Exception:
                    pass
        return draft
    except Exception as error:  # a paste must never crash the dialog
        log.warning("Pasted text not parsed: %s", type(error).__name__)
        return PastedDraft(kind="generic_job", site="generic", language=None, original_text=original,
                           warnings=[type(error).__name__])
