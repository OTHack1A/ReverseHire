"""``ApplicationDialog``: records a CV already sent (or about to be) by hand.

The dialog only collects and checks the values; the caller (``ApplicationsPanel``) creates
the record on a worker, because archiving the PDF copies and hashes the file.
``valuesAccepted(dict)`` carries the keyword arguments of ``ApplicationService.create``.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPlainTextEdit, QPushButton, QRadioButton, QVBoxLayout,
                               QWidget)

from .detail_widgets import OptionalDate
from .model import CHANNELS, KIND_POSTING, KIND_SPONTANEOUS, STATUS_DRAFT, STATUS_SENT, STATUSES
from .service import ApplicationService, fold_text
from .texts import Texts
from .workers import ThreadRunner

log = logging.getLogger("reversehire.applications")


class ApplicationDialog(QDialog):
    valuesAccepted = Signal(dict)

    def __init__(self, service: ApplicationService | None = None, *, tr: Callable | None = None,
                 companies: list[tuple[int | None, str]] | None = None, reference_cv: str | Path = "",
                 dialogs=None, run: Callable | None = None, today: Callable[[], date] | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.service = service
        self.T = tr if isinstance(tr, Texts) else Texts(tr)
        T = self.T
        self._run = run if run is not None else ThreadRunner(self)
        if dialogs is None:
            from .panel import PanelDialogs

            dialogs = PanelDialogs(self)
        self.dialogs = dialogs
        self._today = today or date.today
        self.setWindowTitle(T("applications.dialog.title"))
        self.setModal(True)

        self.company = QComboBox()
        self.company.setEditable(True)
        self.company.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for ident, name in companies or []:
            self.company.addItem(name, ident)
        self.company.setCurrentIndex(-1)
        self.company.setPlaceholderText(T("applications.dialog.company_hint"))
        self.spontaneous = QRadioButton(T("applications.kind.spontaneous"))
        self.posting = QRadioButton(T("applications.kind.posting"))
        self.kind_group = QButtonGroup(self)
        self.kind_group.addButton(self.spontaneous)
        self.kind_group.addButton(self.posting)
        self.spontaneous.setChecked(True)
        self.role = QLineEdit()
        self.posting_url = QLineEdit()
        self.channel = QComboBox()
        for channel in CHANNELS:
            self.channel.addItem(T(f"applications.channel.{channel}"), channel)
        self.recipient = QLineEdit()
        self.contact = QLineEdit()
        self.sent_date = OptionalDate(T)
        self.sent_date.set_value(self._today())
        self.status = QComboBox()
        for status in STATUSES:
            self.status.addItem(T(f"applications.status.{status}"), status)
        self.status.setCurrentIndex(self.status.findData(STATUS_SENT))
        self.cv_path = QLineEdit(str(reference_cv or ""))
        self.cv_browse = QPushButton(T("applications.dialog.browse"))
        self.cv_language = QLineEdit()
        self.subject = QLineEdit()
        self.letter = QPlainTextEdit()
        self.letter.setPlaceholderText(T("applications.dialog.letter_hint"))
        self.load_letter_button = QPushButton(T("applications.dialog.load_letter"))
        self.notes = QPlainTextEdit()
        self.notes.setMaximumHeight(60)
        self.error_label = QLabel()
        self.error_label.setProperty("role", "error")
        self.error_label.setWordWrap(True)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(T("applications.dialog.save"))
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(T("applications.dialog.cancel"))

        kind_row = QHBoxLayout()
        kind_row.addWidget(self.spontaneous)
        kind_row.addWidget(self.posting)
        kind_row.addStretch(1)
        cv_row = QHBoxLayout()
        cv_row.addWidget(self.cv_path, 1)
        cv_row.addWidget(self.cv_browse)
        form = QFormLayout()
        form.addRow(T("applications.field.company"), self.company)
        form.addRow(T("applications.field.kind"), kind_row)
        form.addRow(T("applications.field.role"), self.role)
        form.addRow(T("applications.field.posting_url"), self.posting_url)
        form.addRow(T("applications.field.channel"), self.channel)
        form.addRow(T("applications.field.recipient"), self.recipient)
        form.addRow(T("applications.field.contact"), self.contact)
        form.addRow(T("applications.field.sent_date"), self.sent_date)
        form.addRow(T("applications.field.status"), self.status)
        form.addRow(T("applications.dialog.cv_pdf"), cv_row)
        form.addRow(T("applications.field.cv_language"), self.cv_language)
        form.addRow(T("applications.field.subject"), self.subject)
        form.addRow(T("applications.dialog.letter"), self.letter)
        form.addRow("", self.load_letter_button)
        form.addRow(T("applications.field.notes"), self.notes)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.error_label)
        layout.addWidget(self.buttons)
        self.resize(560, 640)

        self.posting.toggled.connect(lambda *_: self._sync_kind())
        self.cv_browse.clicked.connect(lambda *_: self.browse_cv())
        self.load_letter_button.clicked.connect(lambda *_: self.load_letter_file())
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self._sync_kind()

    # --- state -------------------------------------------------------------------------

    def _sync_kind(self) -> None:
        self.posting_url.setEnabled(self.posting.isChecked())

    def kind(self) -> str:
        return KIND_POSTING if self.posting.isChecked() else KIND_SPONTANEOUS

    def company_name(self) -> str:
        return self.company.currentText().strip()

    def company_id(self) -> int | None:
        """The card id when the text is one of the offered companies, None for a new name."""
        wanted = fold_text(self.company_name())
        for index in range(self.company.count()):
            if fold_text(self.company.itemText(index)) == wanted:
                ident = self.company.itemData(index)
                return int(ident) if ident not in (None, "") else None
        return None

    def values(self) -> dict:
        sent = self.sent_date.value()
        status = self.status.currentData()
        if not sent and status == STATUS_SENT:
            status = STATUS_DRAFT  # "sent" needs a date
        path = self.cv_path.text().strip()
        return {
            "company_name": self.company_name(), "company_id": self.company_id(),
            "role": self.role.text().strip(), "kind": self.kind(),
            "posting_url": self.posting_url.text().strip() if self.posting.isChecked() else "",
            "channel": self.channel.currentData(), "recipient_email": self.recipient.text().strip(),
            "contact_name": self.contact.text().strip(), "sent_date": sent, "status": status,
            "cover_subject": self.subject.text().strip(), "cover_letter": self.letter.toPlainText(),
            "cv_path": path or None, "cv_language": self.cv_language.text().strip(),
            "notes": self.notes.toPlainText(), "source": "manual",
        }

    # --- actions -----------------------------------------------------------------------

    def browse_cv(self) -> None:
        try:
            path = self.dialogs.open_file(self.T("applications.dialog.choose_cv"), self.T("applications.cv.filter"))
        except Exception:  # noqa: BLE001
            log.warning("File dialog failed", exc_info=True)
            return
        if path:
            self.cv_path.setText(path)

    def load_letter_file(self) -> None:
        try:
            path = self.dialogs.open_file(self.T("applications.dialog.choose_letter"),
                                          self.T("applications.dialog.letter_filter"))
        except Exception:  # noqa: BLE001
            log.warning("File dialog failed", exc_info=True)
            return
        if not path:
            return

        def read() -> str:
            raw = Path(path).read_bytes()
            for encoding in ("utf-8-sig", "cp1252"):
                try:
                    return raw.decode(encoding)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", errors="replace")

        def done(text: str) -> None:
            self.letter.setPlainText(text)

        def failed(error: BaseException) -> None:
            log.warning("Letter file not read: %s", error)
            self.error_label.setText(self.T("applications.dialog.letter_unreadable"))

        self._run(read, done, failed)

    def accept(self) -> None:  # noqa: D401 - Qt override
        values = self.values()
        if not values["company_name"]:
            self.error_label.setText(self.T("applications.dialog.company_required"))
            self.company.setFocus()
            return
        path = values["cv_path"]
        if path and not Path(path).is_file():
            self.error_label.setText(self.T("applications.dialog.cv_missing"))
            return
        if self.service is not None:
            try:
                duplicates = self.service.find_duplicates(
                    company_name=values["company_name"], company_id=values["company_id"], role=values["role"],
                    when=values["sent_date"])
            except Exception:  # noqa: BLE001
                log.warning("Duplicate check failed", exc_info=True)
                duplicates = []
            if duplicates and not self.dialogs.ask(
                    self.T("applications.dialog.duplicate", count=len(duplicates), name=values["company_name"]),
                    self.T("applications.dialog.title"), warning=True):
                return
        self.error_label.setText("")
        self.valuesAccepted.emit(values)
        super().accept()
