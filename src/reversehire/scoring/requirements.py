"""Posting requirements without an LLM: units, must/nice, types, anchored 0..4 levels, gate.

1. ``extract_items``: the posting text is split into lines, bullets and
   sentences ("units"). Each unit is classified by the heading it sits under
   first ("Requisiti", "Requirements" -> must; "Costituirà titolo
   preferenziale", "Nice to have" -> nice; "Mansioni", "What you will do" ->
   duty; "Cosa offriamo", "We offer" -> offer, never scored), then by cue words
   in its own clause ("gradito", "preferibile", "plus" -> nice; "richiesto",
   "indispensabile", "required", "must" -> must). Typed requirements found
   outside any heading (years, language, degree, licence, car, travel) are
   must-haves, as in the older ``fit.extract_requirements``.
2. Types: ``years``, ``language``, ``degree`` (the rules of ``fit.py``),
   ``licence`` (category, default B), ``car`` (own car), ``travel`` (business
   trips, frequency and days a year), ``skill`` (anything else under a
   must/nice heading or with a cue), ``duty`` and plain ``skill`` context.
3. ``check_items``: each item gets an anchored level with the CV line quoted:
   4 explicit strong evidence, 3 clear evidence, 2 partial or adjacent,
   1 weak, 0 explicit contradiction, ``None`` = no evidence ("uncertain",
   never a failure). Skills: a CV skill/role/job title found in the
   requirement (rapidfuzz via ``TextIndex``) is a lexical anchor; the
   embedding similarity with the CV evidence lines is calibrated on the
   model's ``requirement_range`` and cut at ``SKILL_LEVEL_THRESHOLDS``.
4. Gate: an item is ``blocking`` only when it is a must-have of a hard type
   (``GATE_KINDS``) and the CV explicitly contradicts it (level 0): e.g.
   frequent travel required and travel "excluded" in the CV, licence B
   required and the CV lists only other categories, own car required and the
   CV says "non automunito". Silence is uncertain.
5. ``coverage``: weighted share of the items met, weights must 5 / nice 3 /
   context 1 (+1 for duties).

Portions adapted from erga-mcp ``src/erga_mcp/applications/role_profile.py``
(the required/preferred/context weights, the cue regexes ``_REQUIRED`` /
``_PREFERRED`` extended with Italian, and the light ``_stem`` token overlap),
Copyright (c) 2026 Erga MCP contributors, MIT License; modified for ReverseHire.
The anchored 0..4 scale and the "uncertain passes" gate are ideas from
resume-job-matcher (no licence: ideas only, no text or code).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from ..cv import mobility as cv_mobility
from ..cv.sections import normalize as lower_text
from ..models import RequirementCheck, item_variants
from .relevance import TextIndex, calibrate_similarity, normalize_text

# --- weights and thresholds -----------------------------------------------------------

PRIORITY_WEIGHTS = {"must": 5, "nice": 3, "context": 1}  # erga-mcp role_profile
DUTY_BONUS = 1  # erga-mcp: responsibilities weigh one more
UNCERTAIN_CREDIT = 0.25  # skill/duty with no evidence: counted as a weak level 1
MAX_ITEMS = 40
MAX_ITEM_CHARS = 220
MIN_UNIT_WORDS = 2
MAX_HEADING_WORDS = 7
MAX_CV_LINES = 150
MAX_EVIDENCE_CHARS = 220

# Calibrated similarity (0..1) -> level, for skills without a lexical anchor.
SKILL_LEVEL_THRESHOLDS = ((0.80, 3), (0.55, 2), (0.30, 1))
ANCHOR_STRONG_SIMILARITY = 0.45  # anchor + similarity at least this -> level 4
MIN_STEM_OVERLAP = 2  # shared stems that make a CV line a weak anchor (level >= 2)
MIN_SHARED_STEM_CHARS = 5  # one shared content stem this long = tangential evidence (level 1)
CONTEXT_MIN_LEVEL = 2  # context items count only when they are evidence (bonus only)

YEARS_PARTIAL_SHARE = 0.8  # CV years >= 80% of the request -> level 3
LANGUAGE_PARTIAL_STEPS = 1  # one CEFR step below -> level 2; more -> contradiction
LICENCE_IMPLIES_B = {"B", "BE", "C1", "C1E", "C", "CE", "D1", "D1E", "D", "DE"}
TRAVEL_DAYS_PARTIAL = 1.5  # posting days up to 1.5 x the accepted days -> partial

GATE_KINDS = {"language", "licence", "car", "travel"}
TYPED_KINDS = {"years", "language", "degree", "licence", "car", "travel"}

MET, PARTIAL, UNCERTAIN, NOT_MET, CONTRADICTED = "met", "partial", "uncertain", "not_met", "contradicted"

# --- headings and cues (normalized text: lowercase, no accents, no punctuation) -------

_HEAD_NICE = re.compile(
    r"^(?:costituira|costituiscono|costituisce|titol[oi] preferenzial|preferenzial|gradit|"
    r"preferibil|sara un plus|e un plus|plus|nice to have|preferred|bonus|desirable|ideally|"
    r"it s a plus|would be a plus|good to have|extra)"
)
_HEAD_MUST = re.compile(
    r"^(?:requisit|competenze (?:richieste|necessarie|tecniche)|conoscenze richieste|"
    r"cosa cerchiamo|chi cerchiamo|cerchiamo|il candidato ideale|la candidata ideale|"
    r"candidato ideale|profilo (?:ricercato|richiesto)|il profilo|profilo|"
    r"requirements?|qualifications?|minimum qualifications|required|must have|"
    r"what you (?:bring|need|have|ll bring|will bring)|you have|you bring|who you are|"
    r"what we re looking for|what we are looking for|about you|your profile|skills)"
)
_HEAD_DUTY = re.compile(
    r"^(?:responsabilita|mansioni|attivita|compiti|principali attivita|di cosa ti occuperai|"
    r"cosa farai|il ruolo|ruolo|la posizione|descrizione (?:del ruolo|della posizione|"
    r"dell offerta)|responsibilities|duties|what you ll do|what you will do|your role|"
    r"the role|key tasks|your tasks|tasks|job description|your mission|in this role)"
)
_HEAD_OFFER = re.compile(
    r"^(?:cosa offriamo|offriamo|si offre|offerta|benefit|benefits|we offer|what we offer|"
    r"perks|contratto|inquadramento|retribuzione|ral|sede|sede di lavoro|luogo di lavoro|"
    r"orario|orario di lavoro|location|salary|compensation|why join|perche|chi siamo|"
    r"about us|company|azienda|l azienda|la societa|altre informazioni|ulteriori informazioni|"
    r"informazioni aggiuntive|other information|additional information)"
)
# One-word section headings of a posting ("Formazione", "Esperienza"): requirements follow.
# Matched only as the whole unit, so that "Esperienza di 3 anni con PLC" stays a requirement.
_HEAD_MUST_WORD = re.compile(r"^(?:formazione|istruzione|esperienza|esperienze|competenze|conoscenze|"
                             r"education|experience|skills required)$")

# Cue words inside a clause (lowercase, no accents; adapted from erga-mcp + Italian).
NICE_CUE = re.compile(
    r"preferibil|gradit|preferenzial|costituisce (?:un )?(?:titolo|plus)|\bun plus\b|\bplus\b"
    r"|nice to have|nice-to-have|preferred|preferably|is a plus|desirable|ideally|bonus"
    r"|advantage|vantaggio|apprezzat"
)
MUST_CUE = re.compile(
    r"richiest|indispensabil|obbligatori|necessari|imprescindibil|essenzial|fondamental"
    r"|\bsi richiede|\bmust\b|\brequired\b|\brequirement|mandatory|\bessential\b"
    r"|\bneed(?:s)? to\b|\byou have\b|\bminimum\b|\bminimo\b"
)
_OFFER_CUE = re.compile(
    r"\bcontratto\b|tempo (?:in)?determinato|\bral\b|retribuzion|stipendio|\bsalary\b"
    r"|compensation|buoni pasto|ticket restaurant|welfare aziendale|\bwe offer\b|offriamo"
    r"|inquadramento|\bccnl\b|full[- ]time|part[- ]time|smart working|\bhybrid\b|\bibrid"
    r"|lavoro da remoto|remote work|\bbenefit"
)
_LICENCE = re.compile(r"\bpatente\b|\blicenza di guida\b|driv(?:ing|er\W?s?) licen[cs]e")
_TRAVEL = re.compile(
    r"trasfert|viaggi(?:are)? (?:di lavoro|per lavoro)|business trip|business travel"
    r"|\btravel(?:l?ing)?\b|spostament\w* (?:sul territorio|frequenti)|missioni (?:in italia|all estero)"
)
_TRAVEL_CONTEXT = re.compile(
    r"disponibil|available|availability|willing|richiest|required|frequent|costant|continu"
    r"|occasional|saltuari|sporadic|eventual|\d{1,3}\s?%|giorni|days|settiman|week|must|need"
)
_TRAVEL_FREQUENT = re.compile(
    r"frequent|costant|continu|regolar|regular|extensive|spesso|often|\bfino al\b|up to|\d{1,3}\s?%"
)
_TRAVEL_OCCASIONAL = re.compile(r"occasional|saltuari|sporadic|eventual|rare|limitat|some travel")

_UNIT_SPLIT = re.compile(r"(?<=[.!?;:])\s+|\n+|\s*[•·▪●\-–*]\s+(?=[A-ZÀ-Üa-z])")

# erga-mcp role_profile: stop words and light stemming for the token overlap.
_STOP_WORDS = frozenset(
    "a about an and are as at be by candidate company for from have in is it job of on or our "
    "role that the their this to using we will with work you your "
    "di del della dei delle e ed in per con da il la lo le gli un una uno nel nella sul sulla "
    "che si come anche ai al alla alle agli tra fra non piu molto ottima ottimo buona buono "
    "conoscenza esperienza capacita competenze lavoro ruolo azienda candidato candidata".split()
)


def _stem(token: str) -> str:
    """Light suffix stripping (erga-mcp `_stem`, with a few Italian endings added)."""
    if len(token) > 5 and token.endswith("ies"):
        return token[:-3] + "y"
    for suffix in ("ments", "ment", "ations", "ation", "zione", "zioni", "ing", "ers", "er",
                   "ed", "s", "i", "e", "o", "a"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            token = token[: -len(suffix)]
            break
    # Italian plurals/singulars: "bilancio" -> "bilanci" -> "bilanc", like "bilanci".
    if len(token) > 4 and token[-1] in "aeio":
        token = token[:-1]
    return token


def stems(text: str) -> frozenset[str]:
    return frozenset(
        _stem(t) for t in normalize_text(text).split()
        if len(t) > 2 and t not in _STOP_WORDS and not t.isdigit()
    )


# --- data -------------------------------------------------------------------------


@dataclass
class RequirementItem:
    """One unit of a posting, classified and typed (before the CV check)."""

    text: str
    priority: str  # "must" | "nice" | "context"
    kind: str  # TYPED_KINDS | "skill" | "duty" | "offer"
    data: dict = field(default_factory=dict)

    @property
    def weight(self) -> float:
        if self.kind == "offer":
            return 0.0
        return PRIORITY_WEIGHTS[self.priority] + (DUTY_BONUS if self.kind == "duty" else 0)


@dataclass
class CVEvidence:
    """CV lines that can be quoted as evidence, built once per profile.

    Only evidence lines of the extracted items (roles, skills, languages,
    degrees, work experience): the header and personal-data rows are never
    used, so names, birth dates and contacts cannot influence the levels.
    """

    lines: list[str] = field(default_factory=list)
    terms: list[tuple[str, str]] = field(default_factory=list)  # (CV term, its evidence line)
    vectors: np.ndarray | None = None
    stems: list[frozenset[str]] = field(default_factory=list)


def _shorten(text: str, n: int = MAX_EVIDENCE_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _cv_line(item) -> str:
    for e in getattr(item, "evidence", None) or []:
        if getattr(e, "source", "cv") == "cv" and (e.text or "").strip():
            return _shorten(e.text)
    return _shorten(getattr(item, "value", "") or getattr(item, "title", "") or "")


def build_cv_evidence(profile, embedder=None) -> CVEvidence:
    lines: list[str] = []
    terms: list[tuple[str, str]] = []

    def add(line: str) -> None:
        if line and line not in lines and len(lines) < MAX_CV_LINES:
            lines.append(line)

    for group in ("roles", "skills", "degrees", "spoken_languages"):
        for item in getattr(profile, group, None) or []:
            line = _cv_line(item)
            add(line)
            for e in getattr(item, "evidence", None) or []:
                if getattr(e, "source", "cv") == "cv":
                    add(_shorten(e.text))
            if group in ("roles", "skills") and item.value:
                # English value and CV wording (translated CVs): both are matched.
                terms.extend((form, line) for form in item_variants(item))
    for exp in getattr(profile, "experiences", None) or []:
        line = _cv_line(exp)
        add(line)
        for e in exp.evidence or []:
            if getattr(e, "source", "cv") == "cv":
                add(_shorten(e.text))
        if exp.title:
            terms.extend((form, line) for form in item_variants(exp))
    vectors = None
    if embedder is not None and lines:
        try:
            vectors = np.asarray(embedder.embed(lines, kind="query"))
        except Exception:
            vectors = None
    return CVEvidence(lines=lines, terms=terms, vectors=vectors, stems=[stems(x) for x in lines])


# --- extraction -------------------------------------------------------------------


def _units(description: str) -> list[str]:
    units = [" ".join(u.split()) for u in _UNIT_SPLIT.split(description or "") if u and u.strip()]
    return [piece for unit in units for piece in _split_runon(unit)]


# A list flattened into one line ("...Nord Europa. Cosa farai Gestirai e svilupperai il
# portafoglio clienti assegnato Identificherai nuove opportunità..." from a JSON-LD
# description stripped of its <li> tags): the items are only told apart by a capital
# letter after a lower-case word. Split there, and cut a heading glued to its first item
# ("Requisiti Ottima conoscenza..." -> "Requisiti:", "Ottima conoscenza...").
RUNON_MIN_WORDS = 30
RUNON_MIN_PARTS = 3
RUNON_MIN_PIECE_WORDS = 3
_RUNON_SPLIT = re.compile(r"(?<=[a-zà-ÿ0-9)])\s+(?=[A-ZÀ-Ü][a-zà-ÿ])")


# A capital after one of these words is a proper noun ("colleghi di Modena", "in Francia"),
# not the start of a new item.
_RUNON_JOINERS = frozenset(
    "di a da in con su per tra fra e ed o il lo la i gli le l un una uno del dello della dei degli delle "
    "al allo alla ai agli alle dal dalla dai nel nello nella nei negli nelle sul sulla presso tipo come "
    "the of in at on and or to for with from by as an a".split())


def _split_runon(unit: str) -> list[str]:
    if len(unit.split()) < RUNON_MIN_WORDS:
        return [unit]
    parts: list[str] = []
    for part in _RUNON_SPLIT.split(unit):
        previous = parts[-1].split()[-1].lower().strip("'’") if parts else ""
        if previous in _RUNON_JOINERS or previous.endswith(("'", "’")):
            parts[-1] += " " + part
        else:
            parts.append(part)
    if len(parts) < RUNON_MIN_PARTS:
        return [unit]  # a long sentence with a proper noun or two
    pieces: list[str] = []
    for part in parts:
        words = part.split()
        for n in (3, 2, 1):
            if len(words) > n and words[n][:1].isupper():
                head = " ".join(words[:n])
                if _heading_section(head + ":") not in (None, "context"):
                    pieces += [head + ":", " ".join(words[n:])]
                    break
        else:
            # proper nouns cut a fragment ("Nord Europa"): it stays with the item before
            if pieces and len(words) < RUNON_MIN_PIECE_WORDS and not pieces[-1].endswith(":"):
                pieces[-1] += " " + part
            else:
                pieces.append(part)
    return pieces


def _heading_section(unit: str) -> str | None:
    """Section started by a heading unit, or None if the unit is not a heading."""
    norm = normalize_text(unit)
    if not norm:
        return None
    words = norm.split()
    colon = unit.rstrip().endswith(":")
    if not colon and (len(words) > MAX_HEADING_WORDS or unit.rstrip().endswith((".", ";"))):
        return None
    if len(words) > MAX_HEADING_WORDS + 3:
        return None
    if _HEAD_MUST_WORD.match(norm):
        return "must"
    for section, pattern in (("nice", _HEAD_NICE), ("offer", _HEAD_OFFER), ("duty", _HEAD_DUTY),
                             ("must", _HEAD_MUST)):
        if pattern.search(norm):
            return section
    return "context" if colon else None


def _clause(text: str, pos: int) -> str:
    start = max(text.rfind(c, 0, pos) for c in ",;()") + 1
    ends = [i for i in (text.find(c, pos) for c in ",;()") if i >= 0]
    return text[start : min(ends) if ends else len(text)]


def _typed(unit: str) -> list[tuple[str, dict, int]]:
    """Typed requirements of a unit: (kind, data, position in the lowercase unit)."""
    from . import fit as rules  # local import: fit imports this module

    low = rules._lower(unit)
    found: list[tuple[str, dict, int]] = []
    years = []
    for pattern in rules.YEARS_PATTERNS:
        for m in pattern.finditer(low):
            n = rules._to_number(m.group(1))
            if n is not None and 0 < n <= 40:
                years.append((n, m.start()))
    if years:
        n, pos = max(years)
        found.append(("years", {"years": n}, pos))
    norm = normalize_text(unit)
    context = any(c in norm for c in rules.LANGUAGE_CONTEXT) or re.search(r"\b[abc][12]\b", norm)
    for code, pos in rules._languages_in(low):
        clause_level = rules._level_from_text(normalize_text(_clause(low, pos)))
        if not (context or clause_level):
            continue
        level = clause_level or rules._level_from_text(norm) or rules.DEFAULT_LANGUAGE_LEVEL
        found.append(("language", {"language": code, "level": level}, pos))
    degree = rules._degree_level_pos(low)
    if degree is not None:
        found.append(("degree", {"level": degree[0]}, degree[1]))
    m = _LICENCE.search(low)
    if m:
        categories = cv_mobility._licence_categories(low) or ["B"]
        found.append(("licence", {"categories": categories}, m.start()))
    if cv_mobility._matches(low, "own_car_yes") and not cv_mobility._matches(low, "own_car_no"):
        found.append(("car", {}, 0))
    m = _TRAVEL.search(low)
    if m and _TRAVEL_CONTEXT.search(low) and "agenzia" not in low and "agency" not in low:
        frequency = "occasional" if _TRAVEL_OCCASIONAL.search(low) else (
            "required" if _TRAVEL_FREQUENT.search(low) or MUST_CUE.search(low) else None)
        found.append(("travel", {"frequency": frequency, "days": cv_mobility.travel_days(low)},
                      m.start()))
    return found


def extract_items(description: str, posting=None) -> list[RequirementItem]:
    """Requirement items of a posting (offer units included with kind "offer").

    ``posting`` (optional) adds a travel item from its ``travel`` attribute
    when the text does not state one.
    """
    items: list[RequirementItem] = []
    seen: set = set()
    section = "context"
    for unit in _units(description):
        heading = _heading_section(unit)
        if heading is not None:
            section = heading
            rest = unit.split(":", 1)[1].strip() if ":" in unit else ""
            if not rest:
                continue
            unit = rest
        low = lower_text(unit)
        typed = _typed(unit)
        n_words = len(normalize_text(unit).split())
        if not typed and (n_words == 0 or (section == "context" and n_words < MIN_UNIT_WORDS)):
            continue  # under a heading a one-word bullet ("Excel") is a requirement
        if section == "offer" or (not typed and _OFFER_CUE.search(low) and not MUST_CUE.search(low)):
            items.append(RequirementItem(_shorten(unit, MAX_ITEM_CHARS), "context", "offer"))
            continue

        def priority(pos: int | None) -> str:
            clause = _clause(low, pos) if pos is not None else low
            if section == "nice" or NICE_CUE.search(clause):
                return "nice"
            if section == "must" or MUST_CUE.search(low) or pos is not None:
                return "must"
            return "context"

        if typed:
            for kind, data, pos in typed:
                key = (kind, data.get("language"))
                if key in seen:
                    continue
                seen.add(key)
                items.append(RequirementItem(_shorten(unit, MAX_ITEM_CHARS), priority(pos), kind, data))
            continue
        if section == "duty":
            items.append(RequirementItem(_shorten(unit, MAX_ITEM_CHARS), "context", "duty"))
        else:
            items.append(RequirementItem(_shorten(unit, MAX_ITEM_CHARS), priority(None), "skill"))
        if len(items) >= MAX_ITEMS:
            break
    travel = getattr(posting, "travel", None) if posting is not None else None
    if travel in ("required", "occasional") and ("travel", None) not in seen:
        note = getattr(posting, "travel_note", None) or ""
        days = cv_mobility.travel_days(lower_text(note)) if note else None
        text = _shorten(note, MAX_ITEM_CHARS) if note else f"travel: {travel}"
        items.append(RequirementItem(text, "must" if travel == "required" else "nice", "travel",
                                     {"frequency": travel, "days": days}))
    elif travel is not None:
        for item in items:
            if item.kind == "travel" and item.data.get("frequency") is None:
                item.data["frequency"] = travel if travel != "none" else None
    return items


# --- checks -----------------------------------------------------------------------


def _status(level: int | None) -> str:
    if level is None:
        return UNCERTAIN
    return {4: MET, 3: MET, 2: PARTIAL, 1: NOT_MET}.get(level, CONTRADICTED)


def _first_evidence(evidence_list) -> str | None:
    for e in evidence_list or []:
        if (getattr(e, "text", "") or "").strip():
            return _shorten(e.text)
    return None


def _language_line(profile, code: str) -> str | None:
    from . import fit as rules

    for item in getattr(profile, "spoken_languages", None) or []:
        if any(c == code for text in item_variants(item) for c, _ in rules._languages_in(rules._lower(text))):
            return _cv_line(item)
    return None


def _years_line(profile) -> str | None:
    for exp in getattr(profile, "experiences", None) or []:
        line = _cv_line(exp)
        if line:
            return line
    return None


def check_years(data: dict, profile) -> tuple[int | None, str | None]:
    cv_years = getattr(profile, "years_experience", None)
    if cv_years is None:
        return None, None
    from .fit import YEARS_TOLERANCE

    if cv_years + YEARS_TOLERANCE >= data["years"]:
        level = 4
    elif cv_years >= YEARS_PARTIAL_SHARE * data["years"]:
        level = 3
    else:
        level = 1
    return level, _years_line(profile)


def check_language(data: dict, profile) -> tuple[int | None, str | None]:
    from . import fit as rules

    status, values = rules.check_requirement("language", data, profile)
    line = _language_line(profile, data["language"])
    if status == rules.SATISFIED:
        return 4, line
    if status == rules.UNVERIFIABLE:
        return None, None
    cv_level = values.get("cv")
    if cv_level is None:  # the CV lists languages, but not this one: not explicit
        return 1, None
    steps = rules._cefr_index(data["level"]) - rules._cefr_index(cv_level)
    return (2 if steps <= LANGUAGE_PARTIAL_STEPS else 0), line


def check_degree(data: dict, profile) -> tuple[int | None, str | None]:
    from . import fit as rules

    status, _ = rules.check_requirement("degree", data, profile)
    if status == rules.UNVERIFIABLE:
        return None, None
    line = next((_cv_line(d) for d in profile.degrees or []), None)
    return (4 if status == rules.SATISFIED else 1), line


def check_licence(data: dict, mobility) -> tuple[int | None, str | None]:
    held = {c.upper() for c in getattr(mobility, "driving_licence", None) or []}
    if not held:
        return None, None
    line = _first_evidence(getattr(mobility, "licence_evidence", None))
    wanted = {c.upper() for c in data.get("categories") or ["B"]}
    ok = all(c in held or (c == "B" and held & LICENCE_IMPLIES_B) for c in wanted)
    return (4 if ok else 0), line


def check_car(mobility) -> tuple[int | None, str | None]:
    own = getattr(mobility, "own_car", None)
    line = _first_evidence(getattr(mobility, "licence_evidence", None))
    if own is True:
        return 4, line
    if own is False:
        return 0, line
    return None, None


def accepted_travel_days(mobility, max_travel_days: int | None = None) -> int | None:
    """Days a year the candidate accepts: the explicit preference wins over the CV."""
    if max_travel_days is not None and max_travel_days >= 0:
        return max_travel_days
    return getattr(mobility, "travel_days_per_year", None)


def check_travel(frequency: str | None, days: int | None, mobility,
                 max_travel_days: int | None = None) -> tuple[int | None, str | None]:
    """Business travel asked by the posting vs the CV willingness (and days a year).

    "required" against travel "excluded" is an explicit contradiction (0);
    "occasional" against "excluded" is weak (1); with both numbers the posting
    days are compared with the accepted days (% already converted on 220 days).
    """
    if frequency == "none":
        return None, None
    willingness = getattr(mobility, "travel", "unknown") or "unknown"
    line = _first_evidence(getattr(mobility, "travel_evidence", None))
    limit = accepted_travel_days(mobility, max_travel_days)
    if days is not None and limit is not None:
        if days <= limit:
            return 4, line
        return (2 if days <= TRAVEL_DAYS_PARTIAL * limit else 0), line
    if willingness == "excluded" or limit == 0:
        return (1 if frequency == "occasional" else 0), line
    if willingness == "declared":
        return 4, line
    if willingness == "likely":
        return 3, line
    return None, None


def _skill_level(item: RequirementItem, cv: CVEvidence, req_vector, interval) -> tuple[int | None, str | None]:
    index = TextIndex(item.text)
    anchor_line = None
    for term, line in cv.terms:
        if len(normalize_text(term)) >= 3 and index.contains(term):
            anchor_line = line
            break
    best, best_line = 0.0, None
    if req_vector is not None and cv.vectors is not None and len(cv.lines):
        sims = cv.vectors @ req_vector
        j = int(np.argmax(sims))
        best = calibrate_similarity(float(sims[j]), interval) / 100.0
        best_line = cv.lines[j]
    req_stems = {t for t in stems(item.text) if len(t) >= MIN_SHARED_STEM_CHARS}
    overlap_line, overlap = None, 0
    for line, line_stems in zip(cv.lines, cv.stems):
        n = len(req_stems & line_stems)
        if n > overlap:
            overlap, overlap_line = n, line
    if anchor_line is not None:
        return (4 if best >= ANCHOR_STRONG_SIMILARITY else 3), anchor_line
    level = None
    for threshold, value in SKILL_LEVEL_THRESHOLDS:
        if best >= threshold:
            level = value
            break
    if overlap >= MIN_STEM_OVERLAP and (level or 0) < 2:
        return 2, overlap_line
    if overlap >= 1 and level is None:
        return 1, overlap_line
    return level, (best_line if level is not None else None)


def check_items(items: list[RequirementItem], profile, embedder=None, cv: CVEvidence | None = None,
                max_travel_days: int | None = None) -> list[RequirementCheck]:
    """Anchored level, status and CV evidence of every scored item (offer items skipped)."""
    cv = cv if cv is not None else build_cv_evidence(profile, embedder)
    mobility = getattr(profile, "mobility", None)
    graded = [i for i in items if i.kind in ("skill", "duty")]
    vectors = None
    if graded and embedder is not None and cv.vectors is not None:
        try:
            vectors = np.asarray(embedder.embed([i.text for i in graded], kind="query"))
        except Exception:
            vectors = None
    interval = tuple(getattr(embedder, "requirement_range", None)
                     or getattr(embedder, "similarity_range", (0.0, 1.0)))
    result: list[RequirementCheck] = []
    g = 0
    for item in items:
        if item.kind == "offer":
            continue
        if item.kind == "years":
            level, line = check_years(item.data, profile)
        elif item.kind == "language":
            level, line = check_language(item.data, profile)
        elif item.kind == "degree":
            level, line = check_degree(item.data, profile)
        elif item.kind == "licence":
            level, line = check_licence(item.data, mobility)
        elif item.kind == "car":
            level, line = check_car(mobility)
        elif item.kind == "travel":
            level, line = check_travel(item.data.get("frequency"), item.data.get("days"), mobility,
                                       max_travel_days)
        else:
            level, line = _skill_level(item, cv, None if vectors is None else vectors[g], interval)
            g += 1
            if item.priority == "context" and item.kind == "skill" and (level or 0) < CONTEXT_MIN_LEVEL:
                continue  # plain context sentences count only as positive evidence
        blocking = item.kind in GATE_KINDS and item.priority == "must" and level == 0
        result.append(RequirementCheck(text=item.text, priority=item.priority, kind=item.kind,
                                       level=level, status=_status(level), evidence=line,
                                       blocking=blocking))
    return result


def check_weight(check: RequirementCheck) -> float:
    return PRIORITY_WEIGHTS.get(check.priority, 1) + (DUTY_BONUS if check.kind == "duty" else 0)


def coverage(checks: list[RequirementCheck]) -> float | None:
    """Weighted share 0..1 of the requirements met, or None if nothing is scored.

    Skills and duties: level/4, no evidence = ``UNCERTAIN_CREDIT``. Typed
    requirements count only when met (level >= 3): the unmet ones are handled
    by the multiplicative factors of ``fit.py`` and the unverifiable ones never
    penalise.
    """
    earned = available = 0.0
    for c in checks:
        w = check_weight(c)
        if c.kind in TYPED_KINDS:
            if c.level is None or c.level < 3:
                continue
            credit = c.level / 4
        else:
            credit = UNCERTAIN_CREDIT if c.level is None else c.level / 4
        earned += w * credit
        available += w
    return earned / available if available else None


def must_summary(checks: list[RequirementCheck]) -> tuple[int, int]:
    """(must-haves met, must-haves in total)."""
    musts = [c for c in checks if c.priority == "must"]
    return sum(1 for c in musts if c.status == MET), len(musts)
