"""Interface texts of the mail package: i18n keys with their English text.

The GUI shows them through ``reversehire.i18n.t``; until a key is added to the locale
files, the English text below is used (``Text.render`` falls back to it). Every key
here must be added, with the same ``{placeholders}``, to locales/en.json, it.json and
uk.json.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

EN: dict[str, str] = {
    # --- outcome messages (what happened) ---------------------------------------------
    "mail.outcome.opened_attached": "The draft is open in {client} with the PDF attached.",
    "mail.outcome.opened": "The draft is open in {client}.",
    "mail.outcome.opened_no_attachment": (
        "The draft is open in {client}, but {client} cannot receive the PDF automatically."),
    "mail.outcome.pending": "{client} is starting: the message window should appear shortly.",
    "mail.outcome.eml_opened": (
        "A draft file with the text and the PDF was created and opened with the program set for .eml files."),
    "mail.outcome.assisted": (
        "No mail program was opened: the text is in the clipboard and the folder with the PDF is open."),
    "mail.outcome.assisted_partial": "No mail program was opened. Some steps could not be done automatically.",
    "mail.outcome.outlook_new_attached": (
        "The draft is open in new Outlook with the PDF attached. New Outlook also keeps it in the Drafts "
        "folder of your account."),
    "mail.outcome.outlook_new_opened": (
        "The draft is open in new Outlook. New Outlook also keeps it in the Drafts folder of your account."),
    "mail.outcome.failed": "{client} could not be opened: {error}",
    "mail.outcome.unavailable": "{client} is not available on this computer.",
    # --- steps (what the user still has to do) ----------------------------------------
    "mail.step.review_send": "Check the message in {client} and press Send yourself.",
    "mail.step.attach_drag": "Attach the PDF: drag {file} from the folder window that opened into the message.",
    "mail.step.attach_manual": "Attach the PDF yourself: {file} is in the folder {folder}.",
    "mail.step.attachment_missing": "The PDF {file} was not found: check the path in the settings.",
    "mail.step.paste_body": (
        "The text was too long for the link and was shortened: replace the message text with the full "
        "text from the clipboard (paste)."),
    "mail.step.paste_all": (
        "Open a new message in your mail program, write {to} as recipient and \"{subject}\" as subject, "
        "then paste the text from the clipboard."),
    "mail.step.paste_all_no_to": (
        "Open a new message in your mail program, write the recipient's address and \"{subject}\" as "
        "subject, then paste the text from the clipboard."),
    "mail.step.copy_manual": "The clipboard could not be used: copy the text from the preview yourself.",
    "mail.step.add_recipient": "Write the recipient's address in the message.",
    "mail.step.check_eml": (
        "Check that the text and the PDF are in the draft. If something is missing, choose another method."),
    "mail.step.outlook_new_drafts": (
        "If you close the message without sending it, you find it again in the Drafts folder of new Outlook."),
    "mail.step.gmail_sign_in": "If Gmail asks you to sign in, sign in and then open the draft again.",
    "mail.step.outlook_starting": (
        "Wait for the Outlook message window. If the Outlook setup wizard appears, finish it, or close it "
        "and choose another method."),
    "mail.step.allow_mail_control": "If macOS asks whether ReverseHire may control Mail, allow it.",
    "mail.step.choose_other": "Choose another method from the list.",
    # --- method names and explanations -------------------------------------------------
    "mail.method.outlook_classic.label": "Classic Outlook",
    "mail.method.outlook_classic.explanation": (
        "Opens a new message in classic Outlook with the PDF already attached. You review it and press Send."),
    "mail.method.outlook_new.label": "New Outlook",
    "mail.method.outlook_new.explanation": (
        "Opens the message in new Outlook with recipient, subject, text and the PDF already attached (through "
        "a draft file that new Outlook opens). New Outlook also keeps the draft in the Drafts folder of your "
        "account. You review it and press Send."),
    "mail.method.mailto.label": "Default mail program ({client})",
    "mail.method.mailto.explanation": (
        "Opens a new message in your default mail program with a mailto link. Attachments are not possible "
        "this way: the folder with the PDF opens so you can drag the file in."),
    "mail.method.gmail_web.label": "Gmail in the browser",
    "mail.method.gmail_web.explanation": (
        "Opens the Gmail compose window in your browser (you must be signed in). A Gmail link cannot carry "
        "attachments: the folder with the PDF opens so you can drag the file in. A very long text is "
        "shortened in the link and copied in full to the clipboard."),
    "mail.method.eml_draft.label": "Draft file (.eml)",
    "mail.method.eml_draft.explanation": (
        "Creates a draft file with the text and the PDF and opens it with the program set for .eml files "
        "(classic Outlook, new Outlook and Thunderbird open it as a draft with the PDF attached)."),
    "mail.method.thunderbird.label": "Thunderbird",
    "mail.method.thunderbird.explanation": "Opens a new message in Thunderbird with the PDF attached.",
    "mail.method.xdg_email.label": "Desktop mail program",
    "mail.method.xdg_email.explanation": (
        "Opens a new message in the desktop's default mail program. The PDF is attached if that program "
        "supports it (Thunderbird, Evolution and KMail do; webmail does not)."),
    "mail.method.apple_mail.label": "Apple Mail",
    "mail.method.apple_mail.explanation": (
        "Opens a new message in Mail with the PDF attached. The first time, macOS asks whether ReverseHire "
        "may control Mail."),
    "mail.method.assisted.label": "Copy the text and attach by hand",
    "mail.method.assisted.explanation": (
        "Copies the text to the clipboard and opens the folder with the PDF selected. You open a new message "
        "yourself, paste the text and drag the PDF in. Always works."),
    "mail.method.automatic.label": "Automatic (recommended)",
    "mail.method.automatic.explanation": (
        "Chooses by your default mail program: {method}. If that does not work, the next way in the list is "
        "tried."),
    "mail.method.automatic.none": "Chooses the best way available on this computer.",
    # --- client names used in {client} -------------------------------------------------
    "mail.client.default": "your mail program",
    "mail.client.browser": "the browser",
}

Translate = Callable[..., str]


def _i18n_translate(key: str, **params) -> str:
    from reversehire import i18n  # lazy: keeps this module importable without locale files

    return i18n.t(key, **params)


def _format(template: str, params: dict) -> str:
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        return template


@dataclass(frozen=True)
class Text:
    """A translatable text: i18n key, English text and the values of its placeholders."""

    key: str
    default: str
    params: dict = field(default_factory=dict)

    def render(self, translate: Translate | None = None) -> str:
        """Text in the current language; the English default while the key is not in
        the locale files (``i18n.t`` returns the key itself in that case)."""
        translate = translate or _i18n_translate
        # A parameter may itself be a Text (e.g. {client} = "your mail program").
        params = {k: v.render(translate) if isinstance(v, Text) else v for k, v in self.params.items()}
        try:
            result = translate(self.key, **params)
        except Exception:  # noqa: BLE001 - a broken translation must not hide the message
            result = self.key
        if not result or result == self.key:
            return _format(self.default, params)
        return result

    def __str__(self) -> str:
        return _format(self.default, {k: str(v) for k, v in self.params.items()})


def text(key: str, **params) -> Text:
    """Text for one of the keys in EN (KeyError on an unknown key: caught by the tests)."""
    return Text(key, EN[key], dict(params))
