"""Draft message files (.eml) with ``X-Unsent: 1`` and the PDF attached.

Classic Outlook, new Outlook and Thunderbird open such a file as an editable draft
with the PDF attached. Verified on new Outlook (November 2026): the body must be a
single text/plain part (with an HTML alternative the attachment is dropped) and the
file must be opened by new Outlook itself (``olk.exe "<file>"``, see
methods.NewOutlookMethod), not through the .eml file association.
"""

import html
import re
import unicodedata
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid
from pathlib import Path

from reversehire.mail.compose import Draft, recipient_domain

MSGID_DOMAIN = "reversehire.invalid"


def _html_body(body: str) -> str:
    paragraphs = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        paragraphs.append("<p>" + html.escape(block).replace("\n", "<br>\n") + "</p>")
    return ("<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\"></head><body>\n"
            + "\n".join(paragraphs) + "\n</body></html>\n")


def build_eml(draft: Draft, sender: str = "") -> EmailMessage:
    """The draft as a MIME message: one text/plain part and the PDF (base64).

    Headers: X-Unsent: 1 (open as a draft), Message-ID (new Outlook needs one to save a
    draft), Date, MIME-Version, To, Subject; From only when ``sender`` is given.
    """
    message = EmailMessage(policy=SMTP)
    message["X-Unsent"] = "1"
    message["Message-ID"] = make_msgid(domain=recipient_domain(sender) or MSGID_DOMAIN)
    message["Date"] = formatdate(localtime=True)
    if sender:
        message["From"] = sender
    if draft.to:
        message["To"] = draft.to
    message["Subject"] = draft.subject or ""
    body = (draft.body or "").replace("\r\n", "\n")
    # Plain text only. Verified on new Outlook (November 2026): with a text + HTML
    # alternative part the attachment is dropped when the draft opens; with a single
    # text/plain part followed by the PDF, text and attachment both arrive.
    message.set_content(body, subtype="plain", charset="utf-8")
    if draft.attachment_exists:
        data = draft.attachment.read_bytes()
        message.add_attachment(data, maintype="application", subtype="pdf",
                               filename=draft.attachment.name)
    if "MIME-Version" not in message:
        message["MIME-Version"] = "1.0"
    return message


def safe_file_stem(textual: str, fallback: str = "draft", limit: int = 60) -> str:
    """ASCII file name part: accents removed, other characters replaced by "_"."""
    ascii_text = unicodedata.normalize("NFKD", textual or "").encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_text).strip("._-")
    return stem[:limit] or fallback


def write_eml(draft: Draft, folder: Path, sender: str = "") -> Path:
    """Writes the .eml file into ``folder`` (created if needed) and returns its path."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (safe_file_stem(draft.subject, "application") + ".eml")
    number = 2
    while path.exists():
        path = folder / f"{safe_file_stem(draft.subject, 'application')}_{number}.eml"
        number += 1
    path.write_bytes(build_eml(draft, sender).as_bytes())
    return path
