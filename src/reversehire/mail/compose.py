"""The user's fixed presentation text and the draft built from it.

Template syntax (subject and body):
- ``{name}``: replaced by a value (see PLACEHOLDERS); an unknown or empty value is
  removed and the line is tidied (no double spaces, no space before punctuation, no
  dangling dash, a line left empty is dropped), so the raw placeholder never appears;
- ``{name|fallback}``: the fallback text when the value is missing
  ("Dear {contact_name|Hiring Manager},");
- ``[[ ... ]]``: an optional part, dropped as a whole when one of its placeholders
  (without fallback) is missing ("I apply[[ for the {role} position]].");
- ``{{`` and ``}}``: literal braces.

Nothing is stored here: ``MailSettings`` is a plain dataclass with ``to_dict`` /
``from_dict``; the integrator keeps it in the application's settings.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, fields
from pathlib import Path

PLACEHOLDERS = ("company", "role", "contact_name", "my_name", "my_email", "my_phone")

DEFAULT_TEMPLATES: dict[str, tuple[str, str]] = {
    "en": (
        "Application[[ for the {role} position]][[ – {my_name}]]",
        "Dear {contact_name|Hiring Manager},\n"
        "\n"
        "I am writing to offer my candidacy[[ for the {role} position]] at {company|your company}. "
        "Please find my CV attached.\n"
        "\n"
        "I would be glad to tell you more about my experience in an interview.\n"
        "\n"
        "Kind regards,\n"
        "{my_name}[[\n{my_phone}]][[\n{my_email}]]",
    ),
    "it": (
        "Candidatura[[ per la posizione di {role}]][[ – {my_name}]]",
        "Gentile {contact_name|Responsabile della selezione},\n"
        "\n"
        "vi scrivo per propormi[[ per la posizione di {role}]] presso {company|la vostra azienda}. "
        "In allegato trovate il mio curriculum vitae.\n"
        "\n"
        "Resto a disposizione per un colloquio conoscitivo.\n"
        "\n"
        "Cordiali saluti,\n"
        "{my_name}[[\n{my_phone}]][[\n{my_email}]]",
    ),
    "uk": (
        "Кандидатура[[ на посаду {role}]][[ – {my_name}]]",
        "Добрий день[[, {contact_name}]]!\n"
        "\n"
        "Пропоную свою кандидатуру[[ на посаду {role}]][[ у компанії {company}]]. "
        "Моє резюме додаю до листа.\n"
        "\n"
        "Охоче розповім більше про свій досвід на співбесіді.\n"
        "\n"
        "З повагою,\n"
        "{my_name}[[\n{my_phone}]][[\n{my_email}]]",
    ),
}
DEFAULT_LANGUAGE = "en"


@dataclass
class MailSettings:
    """What the user sets once: templates, reference PDF, own details, preferred method."""

    subject_template: str = DEFAULT_TEMPLATES[DEFAULT_LANGUAGE][0]
    body_template: str = DEFAULT_TEMPLATES[DEFAULT_LANGUAGE][1]
    attachment_path: str = ""   # reference PDF (CV); "" = no attachment
    my_name: str = ""
    my_email: str = ""
    my_phone: str = ""
    method: str = ""            # preferred method id (see methods.METHOD_IDS); "" = recommended

    @classmethod
    def defaults_for(cls, language: str) -> "MailSettings":
        """Settings with the default templates of a language (English if unknown)."""
        subject, body = DEFAULT_TEMPLATES.get(language, DEFAULT_TEMPLATES[DEFAULT_LANGUAGE])
        return cls(subject_template=subject, body_template=body)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping | None) -> "MailSettings":
        """From a saved dictionary; unknown keys are ignored, missing ones get the default."""
        known = {f.name for f in fields(cls)}
        values = {k: str(v) for k, v in dict(data or {}).items() if k in known and v is not None}
        return cls(**values)

    @property
    def attachment(self) -> Path | None:
        text = (self.attachment_path or "").strip().strip('"')
        return Path(text) if text else None


@dataclass(frozen=True)
class Draft:
    """A message ready to be opened in a mail program (never sent by ReverseHire)."""

    to: str
    subject: str
    body: str
    attachment: Path | None = None

    @property
    def attachment_exists(self) -> bool:
        return self.attachment is not None and self.attachment.is_file()


# --- template rendering ---------------------------------------------------------------

_REMOVED = "\x00"  # marks where a missing value was removed (only those lines are tidied)
_END_DOT = "\x01"  # after a value ending with "." (avoids "S.r.l.." before a full stop)
_SEGMENT = re.compile(r"\[\[(.*?)\]\]", re.DOTALL)
_TOKEN = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)(?:\|([^{}]*))?\}")


def _clean_value(value) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _substitute(part: str, values: dict[str, str]) -> tuple[str, bool]:
    """(text, complete): complete is False when a placeholder without fallback was missing."""
    complete = True

    def repl(match: re.Match) -> str:
        nonlocal complete
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        name, fallback = match.group(1), match.group(2)
        value = values.get(name, "") or (fallback or "").strip()
        if value:
            # "Acme S.r.l." before a full stop: the marker lets render_template drop one dot.
            return value + _END_DOT if value.endswith(".") else value
        complete = False
        return _REMOVED

    return _TOKEN.sub(repl, part), complete


_SPACES = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([,.;:!?)\]»”])")
_EMPTY_PAIRS = re.compile(r"\(\s*\)|\[\s*\]|\"\s*\"|“\s*”|«\s*»")
_DOUBLE_PUNCT = re.compile(r"([,;:])[ \t]*([,;:.!?])")
_DOUBLE_DASH = re.compile(r"[ \t]*([–—-])(?:[ \t]*[–—-])+[ \t]*")
_LEADING = re.compile(r"^[ \t]*[,;:.!?–—-]+[ \t]*")
_TRAILING_DASH = re.compile(r"[ \t]*[–—-][ \t]*$")


def _tidy_line(line: str) -> str:
    indent = line[: len(line) - len(line.lstrip(" \t"))]
    rest = line[len(indent):].replace(_REMOVED, "")
    rest = _EMPTY_PAIRS.sub("", rest)
    rest = _SPACES.sub(" ", rest)
    rest = _SPACE_BEFORE_PUNCT.sub(r"\1", rest)
    rest = _DOUBLE_PUNCT.sub(r"\2", rest)
    rest = _DOUBLE_DASH.sub(r" \1 ", rest)
    rest = _LEADING.sub("", rest)
    rest = _TRAILING_DASH.sub("", rest)
    rest = rest.strip()
    return indent + rest if rest else ""


def _tidy(textual: str) -> str:
    lines_out: list[str] = []
    dropped = False
    for line in textual.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if _REMOVED in line:
            line = _tidy_line(line)
            if not line:
                dropped = True
                continue
        lines_out.append(line.rstrip())
    result = "\n".join(lines_out)
    if dropped:
        result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip("\n").rstrip()


def render_template(template: str, values: Mapping | None) -> str:
    """Fills a subject or body template (syntax in the module docstring)."""
    clean = {str(k): _clean_value(v) for k, v in dict(values or {}).items()}
    out: list[str] = []
    pos = 0
    for match in _SEGMENT.finditer(template or ""):
        out.append(_substitute(template[pos:match.start()], clean)[0])
        inner, complete = _substitute(match.group(1), clean)
        out.append(inner if complete else _REMOVED)
        pos = match.end()
    out.append(_substitute((template or "")[pos:], clean)[0])
    joined = re.sub("\\." + _END_DOT + r"(\x00*)\.", r".\1", "".join(out)).replace(_END_DOT, "")
    return _tidy(joined)


def template_placeholders(template: str) -> list[str]:
    """Names used in a template, in order, without duplicates (for the settings dialog)."""
    names: list[str] = []
    for match in _TOKEN.finditer(template or ""):
        name = match.group(1)
        if name and name not in names:
            names.append(name)
    return names


def unknown_placeholders(template: str) -> list[str]:
    """Names that are not in PLACEHOLDERS (probably typing mistakes; they are removed)."""
    return [n for n in template_placeholders(template) if n not in PLACEHOLDERS]


def build_draft(settings: MailSettings, *, company: str = "", role: str = "", contact_name: str = "",
                to: str = "", extra: Mapping | None = None) -> Draft:
    """The draft for one company: templates filled, recipient and reference PDF set."""
    values = {
        "company": company, "role": role, "contact_name": contact_name,
        "my_name": settings.my_name, "my_email": settings.my_email, "my_phone": settings.my_phone,
    }
    values.update(dict(extra or {}))
    subject = " ".join(render_template(settings.subject_template, values).split())
    body = render_template(settings.body_template, values)
    return Draft(to=(to or "").strip(), subject=subject, body=body, attachment=settings.attachment)


# --- recipient ------------------------------------------------------------------------

_EMAIL = re.compile(r"^[^@\s<>(),;:\"\[\]]+@[^@\s<>(),;:\"\[\]]+\.[^@\s<>(),;:\"\[\]]{2,}$")

# Words that mark an HR / recruiting contact (role text or e-mail local part).
HR_WORDS = ("hr", "human resources", "risorse umane", "recruiting", "recruiter", "recruitment",
            "talent", "selezione", "personale", "hiring", "careers", "career", "jobs", "job",
            "lavora", "lavoro", "candidature", "candidatura", "cv", "personnel", "people",
            "персонал", "кадр", "кадри", "рекрутер")


def is_plausible_email(address: str) -> bool:
    return bool(_EMAIL.match((address or "").strip()))


def recipient_domain(address: str) -> str:
    """The part after "@", lower case ("" when there is none). The only part logged."""
    address = (address or "").strip()
    return address.rsplit("@", 1)[1].lower() if "@" in address else ""


@dataclass(frozen=True)
class Recipient:
    email: str
    name: str = ""
    source: str = ""  # "typed", "hr_contact", "contact", "company"


def _get(item, *names: str) -> str:
    for name in names:
        value = item.get(name) if isinstance(item, Mapping) else getattr(item, name, None)
        if value:
            return str(value).strip()
    return ""


def _is_hr(contact) -> bool:
    role = _get(contact, "role", "title", "kind", "department", "type").lower()
    email = _get(contact, "email", "address", "value").lower()
    local = email.split("@", 1)[0]
    tokens = set(re.split(r"[^\w]+", role)) | set(re.split(r"[._+\-]+", local))
    for word in HR_WORDS:
        if (" " in word and word in role) or word in tokens:
            return True
    return False


def pick_recipient(contacts: Iterable = (), company_email: str = "", typed: str = "") -> Recipient | None:
    """Recipient for the draft: what the user typed, else the first HR contact of the
    company card, else the first contact with an address, else the company e-mail.

    ``contacts`` are mappings or objects with ``email`` (or ``address``/``value``),
    ``name`` and ``role`` (or ``title``/``kind``/``department``). Contacts whose address
    does not look like an e-mail are skipped. None when nothing is found.
    """
    typed = (typed or "").strip()
    if typed:
        return Recipient(typed, source="typed")
    usable = []
    for contact in contacts or ():
        email = _get(contact, "email", "address", "value")
        if is_plausible_email(email):
            usable.append((contact, email))
    for contact, email in usable:
        if _is_hr(contact):
            return Recipient(email, _get(contact, "name", "full_name"), "hr_contact")
    if usable:
        contact, email = usable[0]
        return Recipient(email, _get(contact, "name", "full_name"), "contact")
    company_email = (company_email or "").strip()
    if is_plausible_email(company_email):
        return Recipient(company_email, source="company")
    return None
