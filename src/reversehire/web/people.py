"""People and contacts of a company from its own public pages (company card, section
"People and contacts").

Sources (fonti/COMPANY_DATA_STUDY.md section C), all read earlier by the identity scan or
the site analysis, so no extra request except at most MAX_POSTINGS job postings that name a
contact:
- role mailboxes on the careers, contacts, team/management, about and legal pages,
  classified by their local part: HR ("hr@", "recruiting@", "selezione@", "lavoraconnoi@",
  "cv@"...), privacy/DPO ("dpo@", "hr.privacy@": labelled "privacy", never proposed as the
  recipient of an application) or general ("info@", "sales@"...); an address whose local
  part is not a role (e.g. "anna.rossi@") is HR only when the text around it speaks of HR
  or applications;
- named people with their role, only when the page states them together ("Responsabile
  Risorse Umane: Anna Rossi", "Anna Rossi - HR Manager", or a short team card holding
  just a name and a role);
- contacts named in job postings ("inviare il CV a selezione@…", "referente: …");
- the CEO from Wikidata (registry/wikidata.py), passed in by the caller.

Never guessed: no e-mail pattern is ever built from a name. Every contact keeps the page
URL and the date. Contacts are stored only in the local database; the user can delete
any of them (companies.delete_contact) and a deleted contact is not added again.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date

from .text import analyze_html, compact

KINDS = ("hr", "management", "general", "privacy")
MAX_POSTINGS = 3
MAX_CONTACTS = 30

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,10}\b", re.I)
_PEC_HOST = re.compile(r"@(?:[\w-]+\.)*(?:pec|legalmail|postacert|cert|arubapec|registerpec|sicurezzapostale|"
                       r"gigapec|mypec|casellapec|messaggipec)\.", re.I)
_PRIVACY_LOCAL = re.compile(r"privacy|dpo|gdpr|data\.?protection|protezione\.?dati|rpd", re.I)
_HR_LOCAL = re.compile(
    r"^(?:hr|hrm|h\.r|humanresources|human\.resources|risorseumane|risorse\.umane|risorse_umane|personale|"
    r"selezione|selezione\.personale|recruiting|recruitment|recruiter|recruit|jobs?|careers?|carriere|carriera|"
    r"talent|talents|talentacquisition|talent\.acquisition|lavoraconnoi|lavora\.con\.noi|lavora|lavoro|"
    r"cv|curriculum|curricula|candidature|candidati|candidatura|hiring|people|work|workwithus|joinus|"
    r"stage|internship|ufficio\.personale|ufficiopersonale|hr\.italy|hr\.italia|hr\.it)"
    r"(?:[._-][a-z0-9]+)?$", re.I)
_GENERAL_LOCAL = re.compile(
    r"^(?:info|informazioni|contact|contacts|contatti|contattaci|hello|ciao|mail|email|office|ufficio|segreteria|"
    r"sales|vendite|commerciale|marketing|amministrazione|admin|acquisti|purchasing|support|supporto|assistenza|"
    r"service|tecnico|press|stampa|comunicazione|export|customer|ordini|orders|reception|direzione)"
    r"(?:[._-][a-z0-9]+)?$", re.I)
_HR_CONTEXT = re.compile(
    r"risorse umane|human resources|\bHR\b|recruit|selezione del personale|candidatur|curricul|\bCV\b|"
    r"invia(?:re)?\s+(?:il\s+)?(?:tuo\s+)?(?:cv|curriculum)|send\s+(?:us\s+)?your\s+(?:cv|resume)|"
    r"posizioni aperte|open positions", re.I)
MAX_GENERAL = 8
_PHONE_NEAR = re.compile(r"(?:\bTel(?:efono)?\.?|\bPhone|\bT\.)\s*[:.]?\s*(\+?\s?\d[\d\s./()-]{6,18}\d)", re.I)

_HR_ROLES = (
    r"HR\s+(?:Manager|Director|Business\s+Partner|Generalist|Specialist|Lead|Coordinator)|"
    r"Human\s+Resources?\s+(?:Manager|Director)|Head\s+of\s+(?:HR|Human\s+Resources|People|Talent)|"
    r"Chief\s+(?:People|Human\s+Resources?)\s+Officer|CHRO|"
    r"Talent\s+Acquisition(?:\s+(?:Manager|Specialist|Lead|Partner))?|Recruiter|Recruiting\s+Manager|"
    r"Responsabile\s+(?:(?:delle|della|del)\s+)?(?:Risorse\s+Umane|Personale|Selezione|HR|Recruiting)|"
    r"Direttore\s+(?:(?:delle|del)\s+)?(?:Risorse\s+Umane|Personale|HR)|"
    r"Ufficio\s+(?:del\s+)?Personale|Ufficio\s+Risorse\s+Umane"
)
_MANAGEMENT_ROLES = (
    r"Amministratore\s+Delegato|Chief\s+Executive\s+Officer|CEO|Presidente(?:\s+del\s+CdA)?|"
    r"Chairman|Chairwoman|President|Direttore\s+Generale|General\s+Manager|Managing\s+Director|"
    r"Chief\s+(?:Operating|Financial|Technology|Technical)\s+Officer|COO|CFO|CTO|Vice\s+Presidente|"
    r"Consigliere\s+Delegato|Founder|Fondatore|Co-?founder|Socio\s+fondatore"
)
_ROLE = re.compile(rf"(?P<hr>{_HR_ROLES})|(?P<mgmt>{_MANAGEMENT_ROLES})")
_NAME = r"(?P<name>[A-ZÀ-Ý][a-zà-ÿ'’]+(?:\s+(?:de|di|da|del|della|dal|van|von|la|lo|De|Di|Da|Del|Della)?\s*[A-ZÀ-Ý][a-zà-ÿ'’]+){1,2})"
_ROLE_THEN_NAME = re.compile(rf"(?P<role>{_HR_ROLES}|{_MANAGEMENT_ROLES})\s*[:\-–—|,]\s*(?:(?:Dott|Dr|Ing|Avv|Sig)\.?(?:ssa)?\s+)?{_NAME}\b")
_NAME_THEN_ROLE = re.compile(rf"{_NAME}\s*[,\-–—|:]\s*(?P<role>{_HR_ROLES}|{_MANAGEMENT_ROLES})\b")
_CARD = re.compile(rf"^(?:(?:Dott|Dr|Ing|Avv)\.?(?:ssa)?\s+)?{_NAME}\s*[,\-–—|:]?\s*(?P<role>{_HR_ROLES}|{_MANAGEMENT_ROLES})$"
                   rf"|^(?P<role2>{_HR_ROLES}|{_MANAGEMENT_ROLES})\s*[,\-–—|:]?\s*(?P<name2>[A-ZÀ-Ý][a-zà-ÿ'’]+"
                   r"(?:\s+[A-ZÀ-Ý][a-zà-ÿ'’]+){1,2})$")
# Capitalised words that are never part of a person's name.
_NOT_NAME = {
    "risorse", "umane", "human", "resources", "ufficio", "personale", "direzione", "team", "staff", "contatti",
    "contacts", "azienda", "company", "group", "gruppo", "società", "societa", "spa", "srl", "italia", "italy",
    "milano", "bologna", "roma", "torino", "modena", "via", "piazza", "sede", "the", "our", "nostro", "nostra",
    "chi", "siamo", "about", "lavora", "con", "noi", "board", "consiglio", "amministrazione", "management",
    "leadership", "news", "home", "careers", "privacy", "policy", "cookie", "chief", "officer", "head",
    "manager", "director", "responsabile", "direttore", "presidente", "president", "ceo", "hr", "dott",
    "per", "dei", "delle", "del", "della", "and", "e", "of", "for", "in", "il", "lo", "la", "le", "gli",
    # organisation and role words ("Vice President - Club Ambassador" is not a person)
    "club", "ambassador", "vice", "area", "department", "dipartimento", "divisione", "division", "office",
    "sales", "marketing", "press", "media", "relations", "communication", "comunicazione", "investor",
    "investors", "customer", "service", "services", "global", "europe", "committee", "comitato", "partner",
    "partners", "founder", "fondatore", "executive", "operations", "technical", "commerciale", "amministrativo",
    "finance", "legal", "affairs", "public", "corporate", "brand", "digital", "academy", "foundation",
    "fondazione", "segreteria", "general", "generale", "delegato", "director", "officer", "head",
}
_POSTING_CONTACT = re.compile(
    r"(?:invia(?:re)?|inoltra(?:re)?|manda(?:re)?|spedi(?:re|sci)|send|e-?mail|scrivi(?:ci)?|candidature?|"
    r"curriculum|CV)[^@]{0,80}?(" + _EMAIL.pattern[2:-2] + r")", re.I)
_REFERENT = re.compile(rf"(?:referente|contatto|contact\s+person|riferimento)\s*[:\-–]\s*{_NAME}", re.I)

_KIND_RANK = {kind: i for i, kind in enumerate(KINDS)}
_BLOCK_TAGS = ("p", "li", "td", "th", "dd", "dt", "h2", "h3", "h4", "h5", "h6", "figcaption", "div", "article",
               "section", "blockquote")
_BR_SPLIT = re.compile(r"\n")
_PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "email.com", "domain.com", "yourdomain.com",
                        "dominio.it", "sito.it", "tuodominio.it", "azienda.it"}


def _inner_html_text(element) -> str:
    """Text of an element with its <br> as line breaks."""
    parts: list[str] = []

    def walk(node) -> None:
        if node.text:
            parts.append(node.text)
        for child in node:
            if isinstance(child.tag, str) and child.tag == "br":
                parts.append("\n")
            elif isinstance(child.tag, str) and child.tag not in ("script", "style"):
                walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(element)
    return "".join(parts)


def contact_id(contact: dict) -> str:
    """Stable id of a contact (its e-mail, else its name, else its phone)."""
    key = (contact.get("email") or contact.get("name") or contact.get("phone") or "").strip().lower()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def email_kind(address: str, context: str = "") -> str:
    """'hr', 'privacy', 'general' (a role mailbox) or '' (a personal-looking address)."""
    local = (address or "").split("@")[0].lower()
    if _PRIVACY_LOCAL.search(local):
        return "privacy"
    if _HR_LOCAL.match(local) or re.match(r"^(?:hr|recruiting|selezione|jobs|careers)[._-]", local):
        return "hr"
    if _GENERAL_LOCAL.match(local):
        return "general"
    if context and _HR_CONTEXT.search(context):
        return "hr"
    return ""


def _is_name(name: str) -> bool:
    words = [w for w in re.split(r"\s+", name.strip()) if w]
    if not 2 <= len(words) <= 4:
        return False
    return not any(w.lower().strip(".,'’") in _NOT_NAME for w in words)


def _role_kind(role: str) -> str:
    return "hr" if re.fullmatch(_HR_ROLES, role.strip(), re.I) else "management"


def _contact(**values) -> dict:
    contact = {"name": "", "role": "", "email": "", "phone": "", "source_url": "", "kind": "general",
               "where": "", "origin": "website", "date": date.today().isoformat()}
    contact.update({k: (" ".join(str(v).split()) if isinstance(v, str) else v) for k, v in values.items()})
    contact["id"] = contact_id(contact)
    return contact


def _phone_near(text: str, start: int, end: int) -> str:
    window = text[max(0, start - 120): end + 120]
    match = _PHONE_NEAR.search(window)
    return " ".join(match.group(1).split()) if match else ""


def registrable_domain(host: str) -> str:
    """'mail.officine.example.co.uk' -> 'example.co.uk'; 'hr.ima.it' -> 'ima.it'."""
    labels = [x for x in (host or "").lower().strip(".").split(".") if x]
    if len(labels) >= 3 and labels[-2] in ("co", "com", "gov", "ac", "org", "net") and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def people_from_page(url: str, kind: str, text: str, html: str = "", *,
                     allowed_domains: set[str] | frozenset = frozenset()) -> list[dict]:
    """Contacts stated on one page (see the module docstring). `allowed_domains`: the
    company's own mail domains (other addresses are left out); empty = no filter."""
    out: list[dict] = []
    text = compact(text)
    page_is_hr = kind == "careers"
    mails: list[tuple[str, int, int]] = [(m.group(0), m.start(), m.end()) for m in _EMAIL.finditer(text)]
    tree = analyze_html(html) if html else None
    if tree is not None:
        for a in tree.iter("a"):
            href = (a.get("href") or "").strip()
            if href.lower().startswith("mailto:"):
                address = href[7:].split("?")[0].strip()
                position = text.find(address)
                # not in the visible text: no context (never the top of the page, i.e. the menu)
                mails.append((address, position, position + len(address)) if position >= 0 else (address, -1, -1))
    seen: set[str] = set()
    for address, start, end in mails:
        address = address.strip().strip(".").lower()
        if not _EMAIL.fullmatch(address) or address in seen or _PEC_HOST.search(address):
            continue
        if re.search(r"\.(?:png|jpe?g|gif|svg|webp)$", address) or "sentry" in address or \
                address.split("@")[1] in _PLACEHOLDER_DOMAINS:
            continue
        if allowed_domains and registrable_domain(address.split("@")[1]) not in allowed_domains:
            continue  # a dealer's, a customer's or an authority's address: not this company's
        seen.add(address)
        context = text[max(0, start - 100): end + 60] if start >= 0 else ""
        found = email_kind(address, context)
        local = address.split("@")[0]
        if not found and page_is_hr and _HR_CONTEXT.search(context):
            found = "hr"
        if found == "hr" and not _HR_LOCAL.match(local) and local == address.split("@")[1].split(".")[0]:
            found = "general"  # "acme@acme.it" is the company's general mailbox
        if not found:
            continue  # a personal-looking address without any HR context: not collected
        out.append(_contact(email=address, kind=found, phone=_phone_near(text, start, end) if found == "hr" else "",
                            source_url=url, where=f"{kind} page"))
    # named people: only inside ONE block of the page (a paragraph, a list item, a cell, a
    # team card), never across blocks: the visible text glues neighbouring names together;
    # never from a home page (news: "the group's chairman said..." is not a contact)
    blocks: list[str] = []
    if kind in ("home", "home_it", "privacy", "other"):
        return out
    if tree is not None:
        for element in tree.iter(*_BLOCK_TAGS):
            if any(isinstance(child.tag, str) and child.tag in _BLOCK_TAGS for child in element.iterdescendants()):
                continue  # not a leaf block: its children are visited on their own
            for part in _BR_SPLIT.split(_inner_html_text(element)):
                block = compact(part)
                if 4 <= len(block) <= 240:
                    blocks.append(block)
            whole = compact(" ".join(element.itertext()))
            if 4 <= len(whole) <= 120:
                blocks.append(whole)
    else:
        blocks = [b for b in re.split(r"(?<=[.;])\s+", text) if b]
    for block in dict.fromkeys(blocks):
        match = _CARD.match(block)
        if match:
            name = match.group("name") or match.group("name2")
            role = match.group("role") or match.group("role2")
            if name and role and _is_name(name):
                out.append(_contact(name=name, role=role, kind=_role_kind(role), source_url=url,
                                    where=f"{kind} page, team card"))
            continue
        for pattern in (_ROLE_THEN_NAME, _NAME_THEN_ROLE):
            for found_match in pattern.finditer(block):
                name, role = found_match.group("name"), found_match.group("role")
                end = found_match.end()
                if pattern is _ROLE_THEN_NAME and end < len(block) and re.match(r"\s+[A-ZÀ-Ý][a-zà-ÿ]", block[end:]):
                    continue  # the name goes on: not sure where it ends
                if _is_name(name):
                    out.append(_contact(name=name, role=role, kind=_role_kind(role), source_url=url,
                                        where=f"{kind} page"))
    return out


def people_from_posting(url: str, text: str) -> list[dict]:
    """Contacts named in a job posting ("inviare il CV a ...", "referente: ...")."""
    out = []
    text = compact(text)
    for match in _POSTING_CONTACT.finditer(text):
        address = match.group(1).lower().strip(".")
        if _PEC_HOST.search(address) or _PRIVACY_LOCAL.search(address.split("@")[0]):
            continue
        out.append(_contact(email=address, kind="hr", source_url=url, where="job posting", origin="posting"))
    for match in _REFERENT.finditer(text):
        if _is_name(match.group("name")):
            out.append(_contact(name=match.group("name"), role="referente", kind="hr", source_url=url,
                                where="job posting", origin="posting"))
    return out


def merge_people(*groups: list[dict], deleted: set[str] | list[str] = ()) -> list[dict]:
    """One list without duplicates (same e-mail or same name), HR first, deleted ones left out."""
    deleted = set(deleted or ())
    result: list[dict] = []
    for contact in [c for group in groups for c in group or []]:
        if contact.get("id") in deleted:
            continue
        same = None
        for known in result:
            if (contact.get("email") and contact.get("email") == known.get("email")) or \
                    (contact.get("name") and known.get("name") and
                     contact["name"].lower() == known["name"].lower()):
                same = known
                break
        if same is None:
            result.append(dict(contact))
            continue
        for key in ("name", "role", "email", "phone"):
            if not same.get(key) and contact.get(key):
                same[key] = contact[key]
        if _KIND_RANK.get(contact.get("kind"), 9) < _KIND_RANK.get(same.get("kind"), 9):
            same["kind"] = contact["kind"]
    result.sort(key=lambda c: (_KIND_RANK.get(c.get("kind"), 9), not c.get("name"), c.get("email") or ""))
    general = [c for c in result if c.get("kind") == "general"][MAX_GENERAL:]
    result = [c for c in result if c not in general]
    return result[:MAX_CONTACTS]


def own_domains(pages: list[dict], site: str = "") -> set[str]:
    """The company's mail domains: its website's, and those of the addresses printed on its
    home, legal and privacy pages (a group may use another domain: imagroup.com / ima.it).
    Contact pages are not used: they often list dealers and agents."""
    from urllib.parse import urlsplit

    domains = set()
    host = urlsplit(site if "://" in (site or "") else f"https://{site}").hostname if site else ""
    if host:
        domains.add(registrable_domain(host))
    for page in pages or []:
        if page.get("kind") not in ("home", "home_it", "legal", "privacy"):
            continue
        for match in _EMAIL.finditer(compact(page.get("text", "")) + " " + (page.get("html", "") or "")):
            domain = match.group(0).split("@")[1].lower()
            if not _PEC_HOST.search("@" + domain) and domain not in _PLACEHOLDER_DOMAINS and \
                    not re.search(r"\.(?:png|jpe?g|gif|svg|webp)$", domain):
                domains.add(registrable_domain(domain))
    return domains


def people_of(pages: list[dict], postings: list[tuple[str, str]] = (), extra: list[dict] = (),
              deleted=(), site: str = "") -> list[dict]:
    """All contacts of a company: scanned pages, posting texts [(url, text)], extra ones
    (e.g. the Wikidata CEO); HR first. With `site`, only addresses of the company's own
    mail domains are kept (see own_domains)."""
    found: list[dict] = []
    allowed = own_domains(pages, site) if site else set()
    for page in pages or []:
        found += people_from_page(page.get("url", ""), page.get("kind", "other"), page.get("text", ""),
                                  page.get("html", ""), allowed_domains=allowed)
    for url, text in postings or []:
        found += people_from_posting(url, text)
    return merge_people(found, list(extra or []), deleted=deleted)
