"""Ways to open the application e-mail as a draft. None of them ever sends anything.

Each method is a class with ``available()``, ``can_attach``, ``reliable``, ``info()``
and ``open_draft(draft) -> Outcome``. ``recommended_methods(environment)`` returns the
available ones in recommended order; ``open_with_fallback`` tries them in turn.

Side effects go through ``system.System`` (process, default handler, browser, file
manager, clipboard) and, for classic Outlook, an injectable COM factory; errors are
turned into an Outcome, never raised. The log gets the method, the recipient's domain
and the attachment's file name only (never the text).
"""

import importlib.util
import logging
import shutil
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from reversehire.mail import detect as d
from reversehire.mail.compose import Draft, recipient_domain
from reversehire.mail.eml import safe_file_stem, write_eml
from reversehire.mail.outcome import (STATUS_ASSISTED, STATUS_FAILED, STATUS_OPENED, STATUS_PENDING,
                                      STATUS_UNAVAILABLE, Outcome)
from reversehire.mail.system import System, run_with_timeout
from reversehire.mail.texts import Text, text
from reversehire.mail.urls import GMAIL_MAX_LENGTH, MAILTO_MAX_LENGTH, gmail_compose_url, mailto_url

log = logging.getLogger("reversehire.mail")

OUTLOOK_CLASSIC, OUTLOOK_NEW, MAILTO, GMAIL_WEB = "outlook_classic", "outlook_new", "mailto", "gmail_web"
EML_DRAFT, THUNDERBIRD, XDG_EMAIL, APPLE_MAIL, ASSISTED = (
    "eml_draft", "thunderbird", "xdg_email", "apple_mail", "assisted")
METHOD_IDS = (OUTLOOK_CLASSIC, THUNDERBIRD, APPLE_MAIL, XDG_EMAIL, OUTLOOK_NEW, MAILTO, GMAIL_WEB,
              EML_DRAFT, ASSISTED)

COM_TIMEOUT = 15.0   # seconds to wait for Outlook to show the message window
OL_MAIL_ITEM = 0     # Outlook OlItemType.olMailItem
AUTOMATIC = ""       # MailSettings.method value: choose by the default mail program
# Programs that open a ReverseHire .eml file as an editable draft with the PDF attached.
_RELIABLE_EML_KINDS = (d.KIND_OUTLOOK_CLASSIC, d.KIND_OUTLOOK_NEW, d.KIND_THUNDERBIRD)

WorkDir = Callable[[], Path]


def _default_work_dir() -> Path:
    """A new private temporary folder for draft files (kept: the mail program reads it later)."""
    return Path(tempfile.mkdtemp(prefix="reversehire-mail-"))


@dataclass(frozen=True)
class MethodInfo:
    """What the dialog shows for a method."""

    method_id: str
    label: Text
    explanation: Text
    can_attach: bool
    reliable: bool
    available: bool

    def label_text(self, translate=None) -> str:
        return self.label.render(translate)

    def explanation_text(self, translate=None) -> str:
        return self.explanation.render(translate)


# --- shared helpers ---------------------------------------------------------------------

def _attachment_name(draft: Draft) -> str:
    """What the log says about the attachment: its type only. A CV file is usually named
    after the person (the log showed the owner's full name in the file name)."""
    if draft.attachment is None:
        return "-"
    return (draft.attachment.suffix.lower() or "file").lstrip(".") + " file"


def _attachment_steps(draft: Draft, revealed: bool) -> list[Text]:
    if draft.attachment is None:
        return []
    if not draft.attachment_exists:
        return [text("mail.step.attachment_missing", file=str(draft.attachment))]
    if revealed:
        return [text("mail.step.attach_drag", file=draft.attachment.name)]
    return [text("mail.step.attach_manual", file=draft.attachment.name, folder=str(draft.attachment.parent))]


def _try_reveal(system: System, draft: Draft) -> bool:
    if not draft.attachment_exists:
        return False
    try:
        system.reveal(draft.attachment)
        return True
    except Exception as error:  # noqa: BLE001
        log.warning("mail: could not show the PDF in the file manager: %s", type(error).__name__)
        return False


def _try_clipboard(system: System, body: str) -> bool:
    try:
        return bool(system.clipboard(body))
    except Exception as error:  # noqa: BLE001
        log.warning("mail: clipboard not available: %s", type(error).__name__)
        return False


def _short_error(error: BaseException) -> str:
    message = " ".join(str(error).split())
    return (message[:240] + "…") if len(message) > 240 else (message or type(error).__name__)


class MailMethod:
    """Base class: ``open_draft`` checks availability, catches errors and logs."""

    method_id = ""
    can_attach = False

    def __init__(self, environment: d.MailEnvironment, system: System | None = None):
        self.environment = environment
        self.system = system or System.default()

    # --- description ---
    @property
    def reliable(self) -> bool:
        return True

    def available(self) -> bool:
        raise NotImplementedError

    def client(self) -> str | Text:
        """Name of the program used, for {client} (a Text when it must be translated)."""
        return text(f"mail.method.{self.method_id}.label")

    def label(self) -> Text:
        return text(f"mail.method.{self.method_id}.label")

    def info(self) -> MethodInfo:
        return MethodInfo(self.method_id, self.label(), text(f"mail.method.{self.method_id}.explanation"),
                          self.can_attach, self.reliable, self.available())

    # --- action ---
    def open_draft(self, draft: Draft) -> Outcome:
        if not self.available():
            outcome = Outcome(self.method_id, STATUS_UNAVAILABLE,
                              text("mail.outcome.unavailable", client=self.client()),
                              (text("mail.step.choose_other"),), client=str(self.client()))
        else:
            try:
                outcome = self._open(draft)
            except Exception as error:  # noqa: BLE001 - every failure becomes an Outcome
                outcome = self._failed(error)
        log_line = "mail draft: method=%s to_domain=%s attachment=%s status=%s attached=%s"
        args = (self.method_id, recipient_domain(draft.to) or "-", _attachment_name(draft), outcome.status,
                outcome.attached)
        if outcome.status == STATUS_FAILED:
            log.warning(log_line + " error=%s", *args, outcome.error)
        else:
            log.info(log_line, *args)
        return outcome

    def _failed(self, error: BaseException) -> Outcome:
        detail = _short_error(error)
        return Outcome(self.method_id, STATUS_FAILED,
                       text("mail.outcome.failed", client=self.client(), error=detail),
                       (text("mail.step.choose_other"),), client=str(self.client()), error=detail)

    def _open(self, draft: Draft) -> Outcome:
        raise NotImplementedError

    def _opened_without_attachment(self, draft: Draft, *, body_complete: bool, clipboard: bool,
                                   extra_steps: Iterable[Text] = ()) -> Outcome:
        """Outcome of a link-based method: window opened, PDF to be dragged in."""
        revealed = _try_reveal(self.system, draft)
        steps: list[Text] = []
        if not draft.to:
            steps.append(text("mail.step.add_recipient"))
        if not body_complete:
            steps.append(text("mail.step.paste_body") if clipboard else text("mail.step.copy_manual"))
        steps += _attachment_steps(draft, revealed)
        steps += list(extra_steps)
        steps.append(text("mail.step.review_send", client=self.client()))
        key = "mail.outcome.opened_no_attachment" if draft.attachment_exists else "mail.outcome.opened"
        return Outcome(self.method_id, STATUS_OPENED, text(key, client=self.client()), tuple(steps),
                       client=str(self.client()), attached=False, body_complete=body_complete,
                       clipboard_used=clipboard, folder_revealed=revealed)

    def _opened_with_attachment(self, draft: Draft, extra_steps: Iterable[Text] = ()) -> Outcome:
        attached = draft.attachment_exists
        steps: list[Text] = []
        if not draft.to:
            steps.append(text("mail.step.add_recipient"))
        if draft.attachment is not None and not attached:
            steps.append(text("mail.step.attachment_missing", file=str(draft.attachment)))
        steps += list(extra_steps)
        steps.append(text("mail.step.review_send", client=self.client()))
        key = "mail.outcome.opened_attached" if attached else "mail.outcome.opened"
        return Outcome(self.method_id, STATUS_OPENED, text(key, client=self.client()), tuple(steps),
                       client=str(self.client()), attached=attached)


# --- 1. classic Outlook (COM) -------------------------------------------------------------

def _default_outlook_factory():
    """Outlook.Application through pywin32 (imported lazily, in the calling thread). The
    REVERSEHIRE_MAIL_DRY_RUN=1 safety switch refuses it, like every other real launcher."""
    from reversehire.mail.system import _check_dry_run

    _check_dry_run()
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    return win32com.client.Dispatch("Outlook.Application")


def pywin32_available() -> bool:
    try:
        return importlib.util.find_spec("win32com") is not None
    except (ImportError, ValueError):
        return False


class ClassicOutlookMethod(MailMethod):
    """``Outlook.Application`` → ``CreateItem(0)`` → To/Subject/Body → ``Attachments.Add``
    → ``Display(False)``. Never ``Send()``. Only offered when a mail profile exists (else
    Outlook would start its setup wizard). Runs in a worker thread, waited for at most
    ``timeout`` seconds: after that the outcome is "pending" and Outlook keeps going."""

    method_id = OUTLOOK_CLASSIC
    can_attach = True

    def __init__(self, environment, system=None, com_factory: Callable[[], object] | None = None,
                 timeout: float = COM_TIMEOUT):
        super().__init__(environment, system)
        self.com_factory = com_factory
        self.timeout = timeout

    def available(self) -> bool:
        env = self.environment
        if env.platform != "windows" or not env.outlook_classic_usable:
            return False
        return self.com_factory is not None or pywin32_available()

    def _open(self, draft: Draft) -> Outcome:
        factory = self.com_factory or _default_outlook_factory
        attach = draft.attachment_exists

        def work() -> bool:
            application = factory()
            item = application.CreateItem(OL_MAIL_ITEM)
            item.To = draft.to or ""
            item.Subject = draft.subject or ""
            item.Body = draft.body or ""
            if attach:
                item.Attachments.Add(str(draft.attachment.resolve()))
            item.Display(False)  # non-modal: the user edits and presses Send
            return attach

        finished, _result, error = run_with_timeout(work, self.timeout)
        if not finished:
            steps = [text("mail.step.outlook_starting"), text("mail.step.review_send", client=self.client())]
            return Outcome(self.method_id, STATUS_PENDING, text("mail.outcome.pending", client=self.client()),
                           tuple(steps), client=str(self.client()), attached=attach)
        if error is not None:
            return self._failed(error)
        return self._opened_with_attachment(draft)


# --- 2. mailto (new Outlook or any default program) ------------------------------------

class MailtoMethod(MailMethod):
    """``mailto:`` link to the default mail program: no attachment (RFC 6068). A body
    too long for the link is shortened and copied in full to the clipboard; the folder
    with the PDF is opened for drag and drop."""

    method_id = MAILTO

    def __init__(self, environment, system=None, max_length: int = MAILTO_MAX_LENGTH):
        super().__init__(environment, system)
        self.max_length = max_length

    def client(self):
        return self.environment.mailto_name or text("mail.client.default")

    def label(self) -> Text:
        return text("mail.method.mailto.label", client=self.client())

    def available(self) -> bool:
        env = self.environment
        if env.platform == "macos":
            return True  # the default program cannot be read, but "open mailto:" works
        return env.platform in ("windows", "linux") and env.mailto_kind not in (
            d.KIND_NONE, d.KIND_UNKNOWN, d.KIND_OUTLOOK_NEW)

    def _open(self, draft: Draft) -> Outcome:
        link = mailto_url(draft, self.max_length)
        self.system.open_target(link.url)
        clipboard = _try_clipboard(self.system, draft.body) if link.truncated else False
        return self._opened_without_attachment(draft, body_complete=not link.truncated, clipboard=clipboard)


class NewOutlookMethod(MailtoMethod):
    """New Outlook has no COM/MAPI, but it opens a draft file (.eml) as an editable
    message WITH the PDF attached when the body is a single text/plain part (see
    ``eml.build_eml``) and the file is opened by new Outlook itself: ``olk.exe "<file>"``
    through its app execution alias, never through the .eml file association (which is
    often classic Outlook). Verified on a real PC in November 2026. New Outlook keeps
    the opened draft in the account's Drafts folder.

    Fallback when olk.exe is not found or cannot be started: the mailto link (when new
    Outlook handles mailto) plus the assisted attachment."""

    method_id = OUTLOOK_NEW
    can_attach = True

    def __init__(self, environment, system=None, work_dir: WorkDir | None = None, sender: str = "",
                 max_length: int = MAILTO_MAX_LENGTH):
        super().__init__(environment, system, max_length)
        self.work_dir = work_dir or _default_work_dir
        self.sender = sender

    def client(self):
        return text("mail.method.outlook_new.label")

    def label(self) -> Text:
        return text("mail.method.outlook_new.label")

    def available(self) -> bool:
        env = self.environment
        return env.platform == "windows" and (bool(env.outlook_new_path) or env.mailto_kind == d.KIND_OUTLOOK_NEW)

    def _open(self, draft: Draft) -> Outcome:
        if self.environment.outlook_new_path:
            try:
                path = write_eml(draft, self.work_dir(), self.sender)
                self.system.spawn([self.environment.outlook_new_path, str(path)])
            except Exception as error:  # noqa: BLE001 - fall back to the mailto link below
                log.warning("mail: new Outlook did not take the draft file: %s", type(error).__name__)
                if self.environment.mailto_kind != d.KIND_OUTLOOK_NEW:
                    raise
            else:
                return self._eml_outcome(draft, path)
        link = mailto_url(draft, self.max_length)
        self.system.open_target(link.url)
        clipboard = _try_clipboard(self.system, draft.body) if link.truncated else False
        return self._opened_without_attachment(draft, body_complete=not link.truncated, clipboard=clipboard)

    def _eml_outcome(self, draft: Draft, path: Path) -> Outcome:
        attached = draft.attachment_exists
        steps: list[Text] = []
        if not draft.to:
            steps.append(text("mail.step.add_recipient"))
        if draft.attachment is not None and not attached:
            steps.append(text("mail.step.attachment_missing", file=str(draft.attachment)))
        steps.append(text("mail.step.outlook_new_drafts"))
        steps.append(text("mail.step.review_send", client=self.client()))
        key = "mail.outcome.outlook_new_attached" if attached else "mail.outcome.outlook_new_opened"
        return Outcome(self.method_id, STATUS_OPENED, text(key), tuple(steps), client=str(self.client()),
                       attached=attached, extra={"eml_path": str(path)})


# --- 3. Gmail in the browser ------------------------------------------------------------

class GmailWebMethod(MailMethod):
    method_id = GMAIL_WEB

    def __init__(self, environment, system=None, max_length: int = GMAIL_MAX_LENGTH,
                 authuser: int | None = None):
        super().__init__(environment, system)
        self.max_length = max_length
        self.authuser = authuser

    def client(self):
        return text("mail.method.gmail_web.label")

    def available(self) -> bool:
        return self.environment.platform in ("windows", "macos", "linux")

    def _open(self, draft: Draft) -> Outcome:
        link = gmail_compose_url(draft, self.max_length, self.authuser)
        self.system.open_browser(link.url)
        clipboard = _try_clipboard(self.system, draft.body) if link.truncated else False
        return self._opened_without_attachment(draft, body_complete=not link.truncated, clipboard=clipboard,
                                               extra_steps=[text("mail.step.gmail_sign_in")])


# --- 4. .eml draft file -----------------------------------------------------------------

class EmlDraftMethod(MailMethod):
    """Writes a draft .eml (X-Unsent: 1, one text/plain part, PDF attached) and opens it
    with the program set for .eml files. Reliable with classic Outlook, new Outlook and
    Thunderbird; with another program the user is asked to check the draft."""

    method_id = EML_DRAFT
    can_attach = True

    def __init__(self, environment, system=None, work_dir: WorkDir | None = None, sender: str = ""):
        super().__init__(environment, system)
        self.work_dir = work_dir or _default_work_dir
        self.sender = sender

    @property
    def reliable(self) -> bool:
        return self.environment.eml_kind in _RELIABLE_EML_KINDS

    def client(self):
        return text("mail.client.default")

    def available(self) -> bool:
        env = self.environment
        if env.platform == "windows":
            return env.eml_kind != d.KIND_NONE or env.outlook_new_installed or env.outlook_classic_installed
        if env.platform == "linux":
            return bool(env.xdg_open_path)
        return False  # macOS Mail opens .eml files as received messages, not drafts

    def _open(self, draft: Draft) -> Outcome:
        path = write_eml(draft, self.work_dir(), self.sender)
        self.system.open_target(str(path))
        attached = draft.attachment_exists
        steps: list[Text] = []
        if not draft.to:
            steps.append(text("mail.step.add_recipient"))
        if draft.attachment is not None and not attached:
            steps.append(text("mail.step.attachment_missing", file=str(draft.attachment)))
        if not self.reliable:
            steps.append(text("mail.step.check_eml"))
        steps.append(text("mail.step.review_send", client=self.client()))
        return Outcome(self.method_id, STATUS_OPENED, text("mail.outcome.eml_opened"), tuple(steps),
                       client=str(self.client()), attached=attached, extra={"eml_path": str(path)})


# --- 5. Thunderbird, xdg-email, Apple Mail ------------------------------------------------

_UNSAFE_TB = set(",'=\"")


def _tb_value(value: str) -> str:
    """Inside -compose values are quoted with ' : a ' in the text would end the value."""
    return (value or "").replace("'", "’").replace("\r", " ").replace("\n", " ")


class ThunderbirdMethod(MailMethod):
    """``thunderbird -compose "to='…',subject='…',format=html,message='body.html',attachment='file:///…'"``.

    The body goes through a UTF-8 HTML file (``message=``) so quotes, commas and line
    breaks survive. A PDF whose name has , ' = or non-ASCII characters (which break
    -compose, bug 1755454) is copied under a safe name into the work folder."""

    method_id = THUNDERBIRD
    can_attach = True

    def __init__(self, environment, system=None, work_dir: WorkDir | None = None):
        super().__init__(environment, system)
        self.work_dir = work_dir or _default_work_dir

    def client(self):
        return "Thunderbird"

    def available(self) -> bool:
        return bool(self.environment.thunderbird_path)

    def compose_argument(self, draft: Draft, folder: Path) -> str:
        from reversehire.mail.eml import _html_body

        body_file = folder / "message.html"
        body_file.write_text(_html_body(draft.body or ""), encoding="utf-8")
        parts = []
        if draft.to:
            parts.append(f"to='{_tb_value(draft.to)}'")
        parts.append(f"subject='{_tb_value(draft.subject)}'")
        parts.append("format=html")
        parts.append(f"message='{body_file}'")
        if draft.attachment_exists:
            attachment = draft.attachment.resolve()
            if any(c in _UNSAFE_TB for c in attachment.name) or not attachment.name.isascii():
                copy = folder / (safe_file_stem(attachment.stem, "CV") + attachment.suffix.lower())
                shutil.copy2(attachment, copy)
                attachment = copy
            parts.append(f"attachment='{attachment.as_uri()}'")
        return ",".join(parts)

    def _open(self, draft: Draft) -> Outcome:
        argument = self.compose_argument(draft, self.work_dir())
        self.system.spawn([self.environment.thunderbird_path, "-compose", argument])
        return self._opened_with_attachment(draft)


class XdgEmailMethod(MailMethod):
    """Linux ``xdg-email --utf8 --subject … --body … --attach file to``. Whether the PDF
    arrives depends on the mail program (not with webmail handlers): in that case the
    folder is shown as well."""

    method_id = XDG_EMAIL
    can_attach = True

    @property
    def reliable(self) -> bool:
        return self.environment.mailto_kind in (d.KIND_THUNDERBIRD, d.KIND_OTHER)

    def client(self):
        return self.environment.mailto_name or text("mail.client.default")

    def available(self) -> bool:
        return self.environment.platform == "linux" and bool(self.environment.xdg_email_path)

    def command(self, draft: Draft) -> list[str]:
        command = [self.environment.xdg_email_path or "xdg-email", "--utf8",
                   "--subject", draft.subject or "", "--body", draft.body or ""]
        if draft.attachment_exists:
            command += ["--attach", str(draft.attachment.resolve())]
        if draft.to:
            command.append(draft.to)
        return command

    def _open(self, draft: Draft) -> Outcome:
        self.system.spawn(self.command(draft), check_exit=True)
        if self.reliable:
            return self._opened_with_attachment(draft)
        return self._opened_without_attachment(draft, body_complete=True, clipboard=False)


APPLE_MAIL_SCRIPT = """on run argv
    set theTo to item 1 of argv
    set theSubject to item 2 of argv
    set theBody to item 3 of argv
    set thePath to item 4 of argv
    tell application "Mail"
        set newMessage to make new outgoing message with properties {subject:theSubject, content:theBody, visible:true}
        tell newMessage
            if theTo is not "" then make new to recipient at end of to recipients with properties {address:theTo}
            if thePath is not "" then make new attachment with properties {file name:(POSIX file thePath)} at after the last paragraph
        end tell
        activate
    end tell
end run"""


class AppleMailMethod(MailMethod):
    """macOS Mail through AppleScript (values passed as arguments, never pasted into the
    script). The first run triggers macOS's "control Mail" consent."""

    method_id = APPLE_MAIL
    can_attach = True

    def client(self):
        return "Mail"

    def available(self) -> bool:
        return self.environment.platform == "macos" and self.environment.apple_mail

    def command(self, draft: Draft) -> list[str]:
        path = str(draft.attachment.resolve()) if draft.attachment_exists else ""
        return ["osascript", "-e", APPLE_MAIL_SCRIPT, draft.to or "", draft.subject or "", draft.body or "", path]

    def _open(self, draft: Draft) -> Outcome:
        self.system.spawn(self.command(draft), check_exit=True)
        return self._opened_with_attachment(draft, extra_steps=[text("mail.step.allow_mail_control")])


# --- 6. assisted fallback ----------------------------------------------------------------

class AssistedMethod(MailMethod):
    """Always available: the text goes to the clipboard and the file manager shows the
    PDF; the user opens a message, pastes and drags the file in."""

    method_id = ASSISTED

    def client(self):
        return text("mail.client.default")

    def available(self) -> bool:
        return True

    def _open(self, draft: Draft) -> Outcome:
        clipboard = _try_clipboard(self.system, draft.body)
        revealed = _try_reveal(self.system, draft)
        steps: list[Text] = []
        if clipboard:
            if draft.to:
                steps.append(text("mail.step.paste_all", to=draft.to, subject=draft.subject))
            else:
                steps.append(text("mail.step.paste_all_no_to", subject=draft.subject))
        else:
            steps.append(text("mail.step.copy_manual"))
        steps += _attachment_steps(draft, revealed)
        steps.append(text("mail.step.review_send", client=self.client()))
        complete = clipboard and (revealed or not draft.attachment_exists)
        message = text("mail.outcome.assisted" if complete else "mail.outcome.assisted_partial")
        return Outcome(self.method_id, STATUS_ASSISTED, message, tuple(steps), client=str(self.client()),
                       body_complete=clipboard, clipboard_used=clipboard, folder_revealed=revealed)


# --- order and fallback chain ------------------------------------------------------------

def all_methods(environment: d.MailEnvironment, system: System | None = None, *,
                com_factory: Callable[[], object] | None = None, work_dir: WorkDir | None = None,
                gmail_authuser: int | None = None, sender: str = "") -> dict[str, MailMethod]:
    """Every method (available or not), by id."""
    system = system or System.default()
    methods = [
        ClassicOutlookMethod(environment, system, com_factory=com_factory),
        ThunderbirdMethod(environment, system, work_dir=work_dir),
        AppleMailMethod(environment, system),
        XdgEmailMethod(environment, system),
        NewOutlookMethod(environment, system, work_dir=work_dir, sender=sender),
        MailtoMethod(environment, system),
        GmailWebMethod(environment, system, authuser=gmail_authuser),
        EmlDraftMethod(environment, system, work_dir=work_dir, sender=sender),
        AssistedMethod(environment, system),
    ]
    return {method.method_id: method for method in methods}


def _recommended_order(environment: d.MailEnvironment) -> list[str]:
    """Ids in recommended order ("automatic"): first the way that fits the default mail
    program (new Outlook: .eml opened with olk.exe; classic Outlook: COM; Thunderbird:
    -compose; a browser, e.g. for Gmail: the Gmail compose page), then the other ones
    that attach the PDF by themselves, then the link-based ones, the .eml file, and last
    the assisted fallback."""
    kind = environment.mailto_kind
    primary = {d.KIND_OUTLOOK_CLASSIC: OUTLOOK_CLASSIC, d.KIND_THUNDERBIRD: THUNDERBIRD,
               d.KIND_OUTLOOK_NEW: OUTLOOK_NEW, d.KIND_BROWSER: GMAIL_WEB}.get(kind)
    order: list[str] = []
    if primary:
        order.append(primary)
    if environment.platform == "macos":
        order.append(APPLE_MAIL)
    if environment.platform == "linux":
        order.append(XDG_EMAIL)
    order += [OUTLOOK_CLASSIC, THUNDERBIRD, OUTLOOK_NEW, MAILTO, GMAIL_WEB]
    if environment.eml_kind in _RELIABLE_EML_KINDS:
        order.insert(order.index(MAILTO), EML_DRAFT)  # reliable here: before the link methods
    else:
        order.append(EML_DRAFT)
    order.append(ASSISTED)
    seen: list[str] = []
    for method_id in order:
        if method_id not in seen:
            seen.append(method_id)
    return seen


def recommended_methods(environment: d.MailEnvironment, system: System | None = None, **options) -> list[MailMethod]:
    """Available methods in recommended order (the assisted fallback is always last).
    Options: com_factory, work_dir, gmail_authuser, sender (see ``all_methods``)."""
    methods = all_methods(environment, system, **options)
    return [methods[i] for i in _recommended_order(environment) if methods[i].available()]


def describe_methods(environment: d.MailEnvironment, system: System | None = None, **options) -> list[MethodInfo]:
    """Labels and explanations of the available methods, in recommended order."""
    return [method.info() for method in recommended_methods(environment, system, **options)]


def automatic_method(environment: d.MailEnvironment, system: System | None = None,
                     **options) -> MailMethod | None:
    """The method "automatic" tries first (the first recommended one)."""
    methods = recommended_methods(environment, system, **options)
    return methods[0] if methods else None


def method_by_id(methods: Iterable[MailMethod], method_id: str) -> MailMethod | None:
    for method in methods:
        if method.method_id == method_id:
            return method
    return None


def open_with_fallback(draft: Draft, methods: Iterable[MailMethod], first: str = "") -> list[Outcome]:
    """Tries the methods in order (``first`` moved to the front) until one works.
    Returns every outcome; the last one is what the user sees. A method is never retried,
    and nothing is tried after a success (so only one window opens)."""
    ordered = list(methods)
    chosen = method_by_id(ordered, first) if first else None
    if chosen is not None:
        ordered.remove(chosen)
        ordered.insert(0, chosen)
    outcomes: list[Outcome] = []
    for method in ordered:
        outcome = method.open_draft(draft)
        outcomes.append(outcome)
        if outcome.ok:
            break
    return outcomes
