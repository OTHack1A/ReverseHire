"""The "Write e-mail" dialog: the application e-mail to one company, opened as a draft.

Recipient (pre-filled: the card's HR contact first, editable), subject and text rendered
from the fixed templates of Settings > Mail (editable for this message only), the PDF to
attach, how to open the draft; "Open draft" runs on a worker (opening Outlook can take
seconds) and then shows what happened and what the user still has to do. ReverseHire
never sends anything: the user sends the draft from the mail program.
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout,
    QWidget,
)

from .. import mail
from ..i18n import t
from . import mail_support
from .common import error_message, run_job
from .theme import set_role
from .widgets import ElidedLabel, wrapped_label

log = logging.getLogger(__name__)


def recipient_for(card) -> mail.Recipient | None:
    """The card's best recipient: HR contacts first (privacy mailboxes never), then the
    other contacts, the HR address of the card, the company e-mail."""
    contacts = [c for c in (getattr(card, "contacts_list", None) or []) if c.get("kind") != "privacy"]
    contacts.sort(key=lambda c: c.get("kind") != "hr")  # stable: the card's order within each group
    company_email = ""
    try:
        company_email = card.registry_value("email")
    except Exception:  # noqa: BLE001 - a card-like object without registry data
        company_email = ""
    recipient = mail.pick_recipient(contacts, company_email=company_email)
    if recipient is None:
        hr = getattr(card, "hr_email", "") or ""
        if mail.is_plausible_email(hr):
            recipient = mail.Recipient(hr, getattr(card, "hr_contact_name", "") or "", "hr_contact")
    elif recipient.source == "contact" and any(c.get("kind") == "hr" and c.get("email") == recipient.email
                                                 for c in contacts):
        recipient = mail.Recipient(recipient.email, recipient.name, "hr_contact")
    return recipient


def company_name(card) -> str:
    return (getattr(card, "name", "") or getattr(card, "legal_name", "") or "").strip()


def draft_for(card, settings: mail.MailSettings, recipient: mail.Recipient | None = None,
              role: str = "") -> mail.Draft:
    """The draft of the fixed templates for this company (the same text the application
    export prints as its letter)."""
    contact = recipient.name if recipient is not None and recipient.source in ("hr_contact", "contact") else ""
    contact = contact or (getattr(card, "hr_contact_name", "") or "")
    return mail.build_draft(settings, company=company_name(card), role=" ".join(str(role or "").split()),
                            contact_name=contact, to=recipient.email if recipient is not None else "")


class MailDialog(QDialog):
    def __init__(self, parent: QWidget | None, card, settings: mail.MailSettings, run_in_background: Callable,
                 *, system: mail.System | None = None, environment=None,
                 on_open_settings: Callable[[], None] | None = None,
                 on_done: Callable[[mail.Outcome], None] | None = None, role: str = ""):
        super().__init__(parent)
        self.setModal(False)
        self.card = card
        self._settings = settings
        self._run = run_in_background
        self._system = system or mail_support.gui_system()
        self.environment = environment
        self._on_open_settings = on_open_settings
        self._on_done = on_done or (lambda _o: None)
        self.busy = False
        self.outcome: mail.Outcome | None = None
        # Called with (outcome, draft) every time "Open draft" ends (other features, e.g. the
        # "Applications sent" tracker, follow the dialog this way); a failing listener is logged.
        self.opened_listeners: list[Callable[[mail.Outcome, mail.Draft], None]] = []
        self._opening_draft: mail.Draft | None = None
        self.recipient = recipient_for(card)
        draft = draft_for(card, settings, self.recipient, role)
        # {role} of the templates: the posting's title or the profile's first role, editable;
        # while subject and body are as rendered, they follow the role typed here
        self.role_field = QLineEdit(" ".join(str(role or "").split()))
        self.role_field.setPlaceholderText(t("mail.dialog.role_placeholder"))
        self._rendered = (draft.subject, draft.body)
        self.role_field.textChanged.connect(lambda _text: self._role_changed())

        self.never_sent_label = wrapped_label(t("mail.dialog.never_sent"), "muted")
        self.to_field = QLineEdit(draft.to)
        self.to_field.setPlaceholderText(t("mail.dialog.to_placeholder"))
        self.to_field.textChanged.connect(lambda _text: self._check_to())
        self.source_label = wrapped_label(role="muted")
        self.subject_field = QLineEdit(draft.subject)
        self.body_field = QPlainTextEdit(draft.body)
        self.body_field.setMinimumHeight(self.fontMetrics().lineSpacing() * 10)
        self.body_note = wrapped_label(t("mail.dialog.body_note"), "muted")
        attachment = settings.attachment
        self.attachment_label = ElidedLabel(str(attachment) if attachment else t("mail.dialog.no_attachment"))
        self.attachment_warning = wrapped_label(role="warning")
        if attachment is not None and not attachment.is_file():
            self.attachment_warning.setText(t("settings.mail.attachment_missing", file=str(attachment)))
        self.attachment_warning.setVisible(bool(self.attachment_warning.text()))
        self.method_combo = QComboBox()
        self.method_explanation = wrapped_label(role="muted")
        self.method_combo.currentIndexChanged.connect(lambda _i: self._explain())
        self._choices: list[mail_support.MethodChoice] = []
        self._fill_methods()
        self.result_label = wrapped_label()
        self.result_label.hide()

        form = QFormLayout()
        form.addRow(t("mail.dialog.to"), self.to_field)
        form.addRow("", self.source_label)
        form.addRow(t("mail.dialog.role"), self.role_field)
        form.addRow(t("mail.dialog.subject"), self.subject_field)
        form.addRow(t("mail.dialog.body"), self.body_field)
        form.addRow("", self.body_note)
        form.addRow(t("mail.dialog.attachment"), self.attachment_label)
        form.addRow("", self.attachment_warning)
        form.addRow(t("mail.dialog.method"), self.method_combo)
        form.addRow("", self.method_explanation)

        self.open_button = QPushButton(t("mail.dialog.open"))
        self.open_button.setProperty("role", "accent")
        self.open_button.setDefault(True)
        self.open_button.clicked.connect(lambda: self.open_draft())
        self.settings_button = QPushButton(t("menu.mail_settings"))
        self.settings_button.clicked.connect(lambda: self._open_settings())
        self.settings_button.setVisible(on_open_settings is not None)
        self.close_button = QPushButton(t("common.close"))
        self.close_button.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(self.settings_button)
        buttons.addStretch(1)
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.never_sent_label)
        layout.addLayout(form, 1)
        layout.addWidget(self.result_label)
        layout.addLayout(buttons)
        self.setWindowTitle(t("mail.dialog.title", company=company_name(card)))
        self._show_source()
        width = max(self.sizeHint().width(), self.fontMetrics().averageCharWidth() * 96)
        self.resize(width, max(self.sizeHint().height(), self.fontMetrics().lineSpacing() * 34))
        if self.environment is None:
            self._detect()

    @property
    def role(self) -> str:
        return " ".join(self.role_field.text().split())

    def _role_changed(self) -> None:
        """The subject and body follow the role unless the user edited them by hand."""
        current = (self.subject_field.text(), self.body_field.toPlainText())
        if current != self._rendered:
            return
        draft = draft_for(self.card, self._settings, self.recipient, self.role)
        self.subject_field.setText(draft.subject)
        self.body_field.setPlainText(draft.body)
        self._rendered = (draft.subject, draft.body)

    # --- recipient
    def _show_source(self) -> None:
        source = self.recipient.source if self.recipient is not None else "none"
        if self.to_field.text().strip() and self.recipient is not None \
                and self.to_field.text().strip() != self.recipient.email:
            source = "typed"
        texts = {"hr_contact": "mail.dialog.source.hr_contact", "contact": "mail.dialog.source.contact",
                 "company": "mail.dialog.source.company", "none": "mail.dialog.source.none"}
        self.source_label.setText(t(texts[source]) if source in texts else "")
        self.source_label.setVisible(bool(self.source_label.text()))

    def _check_to(self) -> bool:
        address = self.to_field.text().strip()
        valid = not address or mail.is_plausible_email(address)
        set_role(self.source_label, "muted" if valid else "warning")
        if valid:
            self._show_source()
        else:
            self.source_label.setText(t("mail.dialog.invalid_to", address=address))
            self.source_label.show()
        return valid

    # --- methods
    def _fill_methods(self) -> None:
        current = self.method_combo.currentData() if self.method_combo.count() else self._settings.method
        self._choices = mail_support.method_choices(self.environment, self._system)
        if self.environment is None and self._settings.method:
            # not detected yet: keep the saved choice visible
            self._choices.append(mail_support.MethodChoice(self._settings.method,
                                                           mail_support.method_label(self._settings.method), ""))
        self.method_combo.blockSignals(True)
        self.method_combo.clear()
        for choice in self._choices:
            self.method_combo.addItem(choice.label, choice.method_id)
        index = self.method_combo.findData(current if current is not None else "")
        self.method_combo.setCurrentIndex(max(0, index))
        self.method_combo.blockSignals(False)
        self._explain()

    def _explain(self) -> None:
        index = self.method_combo.currentIndex()
        if self.environment is None:
            self.method_explanation.setText(t("settings.mail.detecting"))
        elif 0 <= index < len(self._choices):
            self.method_explanation.setText(self._choices[index].explanation)

    def _detect(self) -> None:
        def done(environment) -> None:
            self.environment = environment
            self._fill_methods()

        def failed(error) -> None:
            log.warning("Mail programs not detected: %s", type(error).__name__)
            self.environment = mail.MailEnvironment(platform="other")
            self._fill_methods()

        run_job(self._run, mail_support.detect_job(), done, failed, name="mail program detection",
                timeout=mail_support.DETECT_TIMEOUT_S)

    # --- action
    def draft(self) -> mail.Draft:
        return mail.Draft(to=self.to_field.text().strip(), subject=" ".join(self.subject_field.text().split()),
                          body=self.body_field.toPlainText(), attachment=self._settings.attachment)

    def open_draft(self) -> bool:
        """Opens the draft in a worker; False when busy or the recipient is not valid."""
        if self.busy or not self._check_to():
            return False
        draft = self.draft()
        self._opening_draft = draft
        method = self.method_combo.currentData() or mail.AUTOMATIC
        self.busy = True
        self.open_button.setEnabled(False)
        self._result(t("mail.dialog.opening"), "muted")
        run_job(self._run, mail_support.open_draft_job(draft, method, self.environment, self._system,
                                                       sender=self._settings.my_email),
                self._opened, self._failed, name="open mail draft", timeout=mail_support.MAIL_TIMEOUT_S)
        return True

    def _opened(self, outcome: mail.Outcome) -> None:
        self.busy = False
        self.open_button.setEnabled(True)
        self.outcome = outcome
        self._result(mail_support.outcome_text(outcome), None if outcome.ok else "warning")
        self._on_done(outcome)
        draft = self._opening_draft or self.draft()
        for listener in list(self.opened_listeners):
            try:
                listener(outcome, draft)
            except Exception:  # noqa: BLE001 - another feature must never break the dialog
                log.warning("Mail dialog listener failed", exc_info=True)

    def _failed(self, error: BaseException) -> None:
        self.busy = False
        self.open_button.setEnabled(True)
        self._result(error_message(error), "error")

    def _result(self, text: str, role: str | None) -> None:
        set_role(self.result_label, role)
        self.result_label.setText(text)
        self.result_label.show()

    def _open_settings(self) -> None:
        if self._on_open_settings is not None:
            self._on_open_settings()

    def insert_text(self, text: str) -> None:
        """Inserts a paragraph into the draft body (used by the affinity box: talking points),
        before the closing paragraph when the body has more than one, else at the end."""
        text = (text or "").strip()
        if not text:
            return
        body = self.body_field.toPlainText().rstrip()
        paragraphs = body.split("\n\n") if body else []
        if len(paragraphs) >= 2:
            paragraphs.insert(len(paragraphs) - 1, text)
        else:
            paragraphs.append(text)
        self.body_field.setPlainText("\n\n".join(paragraphs) + "\n")
