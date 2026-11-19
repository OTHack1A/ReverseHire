"""What the mail settings page and the mail dialog share: the user's MailSettings, the
GUI-safe side effects, the list of methods shown to the user and the worker job that
opens a draft. Nothing here ever sends an e-mail.

The mail package's methods run on a worker thread (opening Outlook may take seconds);
the clipboard is the only side effect that must happen on the GUI thread, so
`gui_system()` replaces it with a call marshalled to the GUI thread.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QGuiApplication

from .. import i18n, mail
from ..i18n import t
from ..mail import system as mail_system

log = logging.getLogger(__name__)

MAIL_TIMEOUT_S = 90.0     # the window stops waiting for a draft after this (Outlook start-up)
DETECT_TIMEOUT_S = 30.0


def translate(key: str, **params) -> str:
    """The `translate` callable of mail.Text.render: the locale text, else the key."""
    return t(key, **params)


# --- settings ----------------------------------------------------------------------------

def mail_settings_of(settings) -> mail.MailSettings:
    """The user's MailSettings (config.Settings.mail); the default templates of the
    interface language when nothing was saved yet."""
    saved = dict(getattr(settings, "mail", None) or {})
    defaults = mail.MailSettings.defaults_for(i18n.current_language()).to_dict()
    if not saved.get("subject_template", "").strip():
        saved.pop("subject_template", None)
    if not saved.get("body_template", "").strip():
        saved.pop("body_template", None)
    return mail.MailSettings.from_dict({**defaults, **saved})


def primary_cv_pdf(library) -> str:
    """The primary CV of "My CV" when it is a PDF ('' otherwise)."""
    if library is None:
        return ""
    try:
        version = library.primary()
        if version is None:
            return ""
        path = Path(library.path_of(version))
        if path.suffix.lower() == ".pdf" and path.is_file():
            return str(path)
        for other in library.versions():  # the primary is a DOCX: another PDF version
            candidate = Path(library.path_of(other))
            if candidate.suffix.lower() == ".pdf" and candidate.is_file():
                return str(candidate)
    except Exception:  # noqa: BLE001 - the library is optional here
        log.debug("Primary CV not available", exc_info=True)
    return ""


# --- GUI-safe side effects --------------------------------------------------------------

class _ClipboardBridge(QObject):
    request = Signal(str)

    def __init__(self):
        super().__init__()
        self.request.connect(self.put, Qt.ConnectionType.BlockingQueuedConnection)

    def put(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)


_bridge: _ClipboardBridge | None = None


def _clipboard(text: str) -> bool:
    """Clipboard from any thread: on the GUI thread through a blocking queued signal (the
    GUI thread never waits for a mail worker, so this cannot dead-lock)."""
    mail_system._check_dry_run()  # REVERSEHIRE_MAIL_DRY_RUN=1: refused like the real one
    bridge = _bridge
    if bridge is None:
        return False
    if QThread.currentThread() is bridge.thread():
        bridge.put(text)
    else:
        bridge.request.emit(text)
    return True


def gui_system() -> mail.System:
    """mail.System for the GUI (call it on the GUI thread once, e.g. when a dialog opens)."""
    global _bridge
    if _bridge is None:
        _bridge = _ClipboardBridge()
    return mail.System(clipboard=_clipboard)


# --- methods shown to the user ----------------------------------------------------------

@dataclass(frozen=True)
class MethodChoice:
    method_id: str       # "" = automatic
    label: str
    explanation: str
    can_attach: bool = False


def method_label(method_id: str) -> str:
    """Label of a method id without an environment (e.g. the saved choice before the
    detection has finished)."""
    key = f"mail.method.{method_id or 'automatic'}.label"
    if key not in mail.TEXTS_EN:
        return method_id
    client = mail.Text("mail.client.default", mail.TEXTS_EN["mail.client.default"])
    return mail.Text(key, mail.TEXTS_EN[key], {"client": client}).render(translate)


def method_choices(environment, system: mail.System | None = None) -> list[MethodChoice]:
    """"Automatic (recommended)" first (with the method it would use), then every
    available method in recommended order, with its explanation."""
    infos = mail.describe_methods(environment, system) if environment is not None else []
    if infos:
        automatic = t("mail.method.automatic.explanation", method=infos[0].label_text(translate))
    else:
        automatic = t("mail.method.automatic.none")
    choices = [MethodChoice(mail.AUTOMATIC, t("mail.method.automatic.label"), automatic, True)]
    choices += [MethodChoice(i.method_id, i.label_text(translate), i.explanation_text(translate), i.can_attach)
                for i in infos]
    return choices


def detect_job() -> Callable[[], object]:
    """Worker job: the mail programs of this computer (read-only)."""
    return mail.detect_environment


def open_draft_job(draft: mail.Draft, method_id: str, environment, system: mail.System,
                   sender: str = "") -> Callable[[], mail.Outcome]:
    """Worker job: opens the draft with the chosen method (the next ones as fallback);
    returns the outcome the user sees. The environment is detected first if unknown."""

    def work() -> mail.Outcome:
        env = environment if environment is not None else mail.detect_environment()
        methods = mail.recommended_methods(env, system, sender=sender)
        outcomes = mail.open_with_fallback(draft, methods, first=method_id)
        if not outcomes:  # nothing available at all (cannot happen: "assisted" always is)
            raise mail.LaunchError("no way to open a draft")
        return outcomes[-1]

    return work


def outcome_text(outcome: mail.Outcome) -> str:
    return outcome.full_text(translate)
