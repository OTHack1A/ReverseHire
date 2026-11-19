"""The tabs of the detail area and the small "next appointments" list.

Every editing tab saves by itself: a change is held for ``debounce_ms`` and then emitted as
``edited(application_id, {field: value})``; the panel writes it and keeps the undo entry.
``flush()`` emits what is pending at once (called before another record is shown).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QDate, QDateTime, QLocale, QSignalBlocker, Qt, QTime, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDateTimeEdit, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPlainTextEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
                               QToolButton, QVBoxLayout, QWidget)

from .model import (APPOINTMENT_KINDS, CHANNELS, KINDS, OUTCOMES, STATUSES, Application, Appointment,
                    HistoryEntry, day_text, parse_day)
from .pdf_preview import PdfPreview
from .texts import Texts

DEFAULT_DEBOUNCE_MS = 500
_MIN_DATE = QDate(1900, 1, 1)  # the "no date" value of OptionalDate


class OptionalDate(QWidget):
    """A date that may be empty: a calendar field and a clear button."""

    changed = Signal()

    def __init__(self, T: Texts, parent: QWidget | None = None):
        super().__init__(parent)
        self.edit = QDateEdit(self)
        self.edit.setCalendarPopup(True)
        self.edit.setMinimumDate(_MIN_DATE)
        self.edit.setSpecialValueText(" ")
        self.edit.setDate(_MIN_DATE)
        self.clear_button = QToolButton(self)
        self.clear_button.setText("x")
        self.clear_button.setToolTip(T("applications.field.clear_date"))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.clear_button)
        self.edit.dateChanged.connect(lambda *_: self.changed.emit())
        self.clear_button.clicked.connect(lambda *_: self.set_value(None, notify=True))

    def value(self) -> str | None:
        date = self.edit.date()
        return None if date == _MIN_DATE else date.toString("yyyy-MM-dd")

    def set_value(self, value, notify: bool = False) -> None:
        day = parse_day(value)
        blocker = None if notify else QSignalBlocker(self.edit)
        self.edit.setDate(QDate(day.year, day.month, day.day) if day else _MIN_DATE)
        del blocker


class _Autosaving(QWidget):
    """Base of the editing tabs: collects field changes and emits them after a pause."""

    edited = Signal(int, object)  # application id, {field: value}

    def __init__(self, T: Texts, debounce_ms: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.T = T
        self._loading = False
        self._app_id = 0
        self._pending: dict = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(max(0, debounce_ms))
        self._timer.timeout.connect(self.flush)

    def _changed(self, name: str, value) -> None:
        if self._loading or not self._app_id:
            return
        self._pending[name] = value
        self._timer.start()

    def flush(self) -> None:
        self._timer.stop()
        if self._pending and self._app_id:
            pending, self._pending = self._pending, {}
            self.edited.emit(self._app_id, pending)
        self._pending = {}

    def discard_pending(self) -> None:
        self._timer.stop()
        self._pending = {}


# --- details -----------------------------------------------------------------------------


class DetailsTab(_Autosaving):
    openCompanyRequested = Signal()
    openPostingRequested = Signal()

    def __init__(self, T: Texts, debounce_ms: int = DEFAULT_DEBOUNCE_MS, parent: QWidget | None = None):
        super().__init__(T, debounce_ms, parent)
        self.company = QLineEdit()
        self.role = QLineEdit()
        self.posting_url = QLineEdit()
        self.kind = QComboBox()
        for kind in KINDS:
            self.kind.addItem(T(f"applications.kind.{kind}"), kind)
        self.channel = QComboBox()
        for channel in CHANNELS:
            self.channel.addItem(T(f"applications.channel.{channel}"), channel)
        self.recipient = QLineEdit()
        self.contact = QLineEdit()
        self.sent_date = OptionalDate(T)
        self.status = QComboBox()
        for status in STATUSES:
            self.status.addItem(T(f"applications.status.{status}"), status)
        self.cv_language = QLineEdit()
        self.follow_up = OptionalDate(T)
        self.notes = QPlainTextEdit()
        self.notes.setMinimumHeight(60)
        self.source_label = QLabel()
        self.source_label.setProperty("role", "muted")
        self.open_company_button = QPushButton(T("applications.action.open_company"))
        self.open_posting_button = QPushButton(T("applications.action.open_posting"))

        form = QFormLayout()
        company_row = QHBoxLayout()
        company_row.addWidget(self.company, 1)
        company_row.addWidget(self.open_company_button)
        posting_row = QHBoxLayout()
        posting_row.addWidget(self.posting_url, 1)
        posting_row.addWidget(self.open_posting_button)
        form.addRow(T("applications.field.company"), company_row)
        form.addRow(T("applications.field.role"), self.role)
        form.addRow(T("applications.field.kind"), self.kind)
        form.addRow(T("applications.field.posting_url"), posting_row)
        form.addRow(T("applications.field.channel"), self.channel)
        form.addRow(T("applications.field.recipient"), self.recipient)
        form.addRow(T("applications.field.contact"), self.contact)
        form.addRow(T("applications.field.sent_date"), self.sent_date)
        form.addRow(T("applications.field.status"), self.status)
        form.addRow(T("applications.field.follow_up"), self.follow_up)
        form.addRow(T("applications.field.cv_language"), self.cv_language)
        form.addRow(T("applications.field.notes"), self.notes)
        form.addRow(T("applications.field.source"), self.source_label)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addStretch(1)

        for name, widget in (("company_name", self.company), ("role", self.role),
                             ("posting_url", self.posting_url), ("recipient_email", self.recipient),
                             ("contact_name", self.contact), ("cv_language", self.cv_language)):
            widget.textEdited.connect(lambda text, n=name: self._changed(n, text))
        self.notes.textChanged.connect(lambda: self._changed("notes", self.notes.toPlainText()))
        for name, combo in (("kind", self.kind), ("channel", self.channel), ("status", self.status)):
            combo.activated.connect(lambda _i, n=name, c=combo: self._changed(n, c.currentData()))
        self.sent_date.changed.connect(lambda: self._changed("sent_date", self.sent_date.value()))
        self.follow_up.changed.connect(lambda: self._changed("follow_up_date", self.follow_up.value()))
        self.open_company_button.clicked.connect(lambda *_: self.openCompanyRequested.emit())
        self.open_posting_button.clicked.connect(lambda *_: self.openPostingRequested.emit())

    def load(self, app: Application | None) -> None:
        self.discard_pending()
        self._loading = True
        try:
            self._app_id = app.id if app else 0
            self.setEnabled(app is not None)
            app = app or Application()
            self.company.setText(app.company_name)
            self.role.setText(app.role)
            self.posting_url.setText(app.posting_url)
            self.kind.setCurrentIndex(max(0, self.kind.findData(app.kind)))
            self.channel.setCurrentIndex(max(0, self.channel.findData(app.channel)))
            self.recipient.setText(app.recipient_email)
            self.contact.setText(app.contact_name)
            self.sent_date.set_value(app.sent_date)
            self.status.setCurrentIndex(max(0, self.status.findData(app.status)))
            self.follow_up.set_value(app.follow_up_date)
            self.cv_language.setText(app.cv_language)
            if self.notes.toPlainText() != app.notes:
                self.notes.setPlainText(app.notes)
            self.source_label.setText(self.T(f"applications.source.{app.source}"))
            self.open_company_button.setEnabled(app.company_id is not None)
            self.open_posting_button.setEnabled(bool(app.posting_url))
        finally:
            self._loading = False


# --- cover letter ------------------------------------------------------------------------


class LetterTab(_Autosaving):
    def __init__(self, T: Texts, debounce_ms: int = DEFAULT_DEBOUNCE_MS, parent: QWidget | None = None):
        super().__init__(T, debounce_ms, parent)
        self.subject = QLineEdit()
        self.letter = QPlainTextEdit()
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setProperty("role", "muted")
        self.edit_button = QPushButton(T("applications.letter.edit"))
        self.edit_button.setCheckable(True)
        self.copy_button = QPushButton(T("applications.letter.copy"))
        buttons = QHBoxLayout()
        buttons.addWidget(self.note, 1)
        buttons.addWidget(self.edit_button)
        buttons.addWidget(self.copy_button)
        form = QFormLayout()
        form.addRow(T("applications.field.subject"), self.subject)
        layout = QVBoxLayout(self)
        layout.addLayout(buttons)
        layout.addLayout(form)
        layout.addWidget(self.letter, 1)
        self._status = "draft"
        self.subject.textEdited.connect(lambda text: self._changed("cover_subject", text))
        self.letter.textChanged.connect(lambda: self._changed("cover_letter", self.letter.toPlainText()))
        self.edit_button.toggled.connect(lambda *_: self._apply_read_only())
        self.copy_button.clicked.connect(self._copy)

    def _copy(self, *_args) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.letter.toPlainText())

    def is_read_only(self) -> bool:
        return self.letter.isReadOnly()

    def _apply_read_only(self) -> None:
        locked = self._app_id != 0 and self._status != "draft" and not self.edit_button.isChecked()
        self.letter.setReadOnly(locked or self._app_id == 0)
        self.subject.setReadOnly(locked or self._app_id == 0)
        self.edit_button.setVisible(self._status != "draft" and self._app_id != 0)
        self.note.setText(self.T("applications.letter.as_sent") if self._status != "draft" and not
                          self.edit_button.isChecked() else "")

    def load(self, app: Application | None) -> None:
        self.discard_pending()
        self._loading = True
        try:
            changed_record = (app.id if app else 0) != self._app_id
            self._app_id = app.id if app else 0
            self._status = app.status if app else "draft"
            if changed_record:
                self.edit_button.setChecked(False)
            app = app or Application()
            if self.subject.text() != app.cover_subject:
                self.subject.setText(app.cover_subject)
            if self.letter.toPlainText() != app.cover_letter:
                self.letter.setPlainText(app.cover_letter)
            self._apply_read_only()
        finally:
            self._loading = False


# --- CV sent -----------------------------------------------------------------------------


class CvTab(QWidget):
    replaceRequested = Signal()
    openExternalRequested = Signal()

    def __init__(self, T: Texts, parent: QWidget | None = None):
        super().__init__(parent)
        self.T = T
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.replace_button = QPushButton(T("applications.cv.replace"))
        self.preview = PdfPreview(T)
        top = QHBoxLayout()
        top.addWidget(self.info, 1)
        top.addWidget(self.replace_button)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.preview, 1)
        self.replace_button.clicked.connect(lambda *_: self.replaceRequested.emit())
        self.preview.openExternalRequested.connect(self.openExternalRequested)

    def show_application(self, app: Application | None) -> None:
        if app is None or not app.cv_hash:
            self.info.setText(self.T("applications.cv.none"))
            self.preview.clear()
            self.replace_button.setEnabled(app is not None)
            return
        self.info.setText(self.T("applications.cv.info", name=app.cv_name or "-", hash=app.cv_hash[:12]))
        self.replace_button.setEnabled(True)
        self.preview.clear(self.T("applications.pdf.loading"))


# --- appointments ------------------------------------------------------------------------


def offset_choices() -> list[int]:
    """UTC offsets (minutes) offered in the editor: half-hours from -12:00 to +14:00 and the 45-minute zones."""
    return sorted(set(range(-720, 841, 30)) | {345, 525, 765, -210})


def offset_label(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours:02d}:{rest:02d}"


def local_offset_minutes(moment: datetime | None = None) -> int:
    offset = (moment or datetime.now().astimezone()).utcoffset() or timedelta(0)
    return int(offset.total_seconds() // 60)


class AppointmentsTab(QWidget):
    addRequested = Signal()
    changed = Signal(int, object)         # appointment id, {field: value}
    deleteRequested = Signal(int)
    icsRequested = Signal(int)
    icsAllRequested = Signal()

    def __init__(self, T: Texts, debounce_ms: int = DEFAULT_DEBOUNCE_MS, parent: QWidget | None = None):
        super().__init__(parent)
        self.T = T
        self._loading = False
        self._current: int = 0
        self._pending: dict = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(max(0, debounce_ms))
        self._timer.timeout.connect(self.flush)

        self.list = QListWidget()
        self.add_button = QPushButton(T("applications.appointment.add"))
        self.delete_button = QPushButton(T("applications.appointment.delete"))
        self.ics_button = QPushButton(T("applications.appointment.ics"))
        self.kind = QComboBox()
        for kind in APPOINTMENT_KINDS:
            self.kind.addItem(T(f"applications.appointment.kind.{kind}"), kind)
        self.start = QDateTimeEdit()
        self.start.setCalendarPopup(True)
        self.start.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.zone = QComboBox()
        for minutes in offset_choices():
            self.zone.addItem(offset_label(minutes), minutes)
        self.duration = QSpinBox()
        self.duration.setRange(0, 24 * 60)
        self.duration.setSingleStep(15)
        self.duration.setSuffix(" " + T("applications.appointment.minutes"))
        self.place = QLineEdit()
        self.people = QLineEdit()
        self.notes = QPlainTextEdit()
        self.notes.setMinimumHeight(50)
        self.outcome = QComboBox()
        for outcome in OUTCOMES:
            self.outcome.addItem(T(f"applications.outcome.{outcome}"), outcome)
        self.reminder = QSpinBox()
        self.reminder.setRange(-1, 7 * 24 * 60)
        self.reminder.setSpecialValueText(T("applications.appointment.no_reminder"))
        self.reminder.setSuffix(" " + T("applications.appointment.minutes_before"))
        self.reminder.setValue(-1)
        self.empty_label = QLabel(T("applications.appointment.empty"))
        self.empty_label.setProperty("role", "muted")

        self.editor = QWidget()
        form = QFormLayout(self.editor)
        start_row = QHBoxLayout()
        start_row.addWidget(self.start, 1)
        start_row.addWidget(self.zone)
        form.addRow(T("applications.appointment.kind"), self.kind)
        form.addRow(T("applications.appointment.start"), start_row)
        form.addRow(T("applications.appointment.duration"), self.duration)
        form.addRow(T("applications.appointment.place"), self.place)
        form.addRow(T("applications.appointment.people"), self.people)
        form.addRow(T("applications.appointment.notes"), self.notes)
        form.addRow(T("applications.appointment.outcome"), self.outcome)
        form.addRow(T("applications.appointment.reminder"), self.reminder)

        left = QVBoxLayout()
        left.addWidget(self.list, 1)
        buttons = QHBoxLayout()
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.delete_button)
        left.addLayout(buttons)
        right = QVBoxLayout()
        right.addWidget(self.empty_label)
        right.addWidget(self.editor)
        right.addWidget(self.ics_button)
        right.addStretch(1)
        layout = QHBoxLayout(self)
        layout.addLayout(left, 1)
        layout.addLayout(right, 2)

        self.add_button.clicked.connect(lambda *_: self.addRequested.emit())
        self.delete_button.clicked.connect(lambda *_: self._current and self.deleteRequested.emit(self._current))
        self.ics_button.clicked.connect(lambda *_: self._current and self.icsRequested.emit(self._current))
        self.list.currentItemChanged.connect(self._select)
        self.place.textEdited.connect(lambda text: self._edit("place", text))
        self.people.textEdited.connect(lambda text: self._edit("people", text))
        self.notes.textChanged.connect(lambda: self._edit("notes", self.notes.toPlainText()))
        self.duration.valueChanged.connect(lambda value: self._edit("duration_min", value))
        self.reminder.valueChanged.connect(lambda value: self._edit("reminder_min", None if value < 0 else value))
        self.kind.activated.connect(lambda _i: self._edit("kind", self.kind.currentData()))
        self.outcome.activated.connect(lambda _i: self._edit("outcome", self.outcome.currentData()))
        self.start.dateTimeChanged.connect(lambda *_: self._edit_start())
        self.zone.activated.connect(lambda _i: self._edit_start())
        self._show_editor(False)

    # --- list --------------------------------------------------------------------------

    def set_appointments(self, items: list[Appointment], select_id: int | None = None) -> None:
        """Shows the appointments of the current application; `select_id` keeps the selection."""
        self.flush()
        keep = select_id if select_id is not None else self._current
        self._loading = True
        try:
            self.list.clear()
            row_to_select = -1
            for index, item in enumerate(items):
                label = f"{self._format_start(item)}  {self.T(f'applications.appointment.kind.{item.kind}')}"
                entry = QListWidgetItem(label)
                entry.setData(Qt.ItemDataRole.UserRole, item)
                self.list.addItem(entry)
                if item.id == keep:
                    row_to_select = index
            if row_to_select < 0 and items:
                row_to_select = 0
            if row_to_select >= 0:
                self.list.setCurrentRow(row_to_select)
        finally:
            self._loading = False
        current = self.list.currentItem()
        self._load_editor(current.data(Qt.ItemDataRole.UserRole) if current else None)
        self.delete_button.setEnabled(current is not None)

    def update_item(self, item: Appointment) -> None:
        """Refreshes the label of one list entry without touching the editor (the user may be typing)."""
        for row in range(self.list.count()):
            entry = self.list.item(row)
            if entry.data(Qt.ItemDataRole.UserRole).id == item.id:
                entry.setData(Qt.ItemDataRole.UserRole, item)
                entry.setText(f"{self._format_start(item)}  {self.T(f'applications.appointment.kind.{item.kind}')}")

    @staticmethod
    def _format_start(item: Appointment) -> str:
        start = item.start_dt
        return QLocale().toString(start.replace(tzinfo=None), QLocale.FormatType.ShortFormat) if start else "-"

    def _select(self, current, _previous) -> None:
        if self._loading:
            return
        self.flush()
        self._load_editor(current.data(Qt.ItemDataRole.UserRole) if current else None)
        self.delete_button.setEnabled(current is not None)

    def current_id(self) -> int:
        return self._current

    def _show_editor(self, visible: bool) -> None:
        self.editor.setVisible(visible)
        self.ics_button.setVisible(visible)
        self.empty_label.setVisible(not visible)

    def _load_editor(self, item: Appointment | None) -> None:
        self._timer.stop()
        self._pending = {}
        self._loading = True
        try:
            self._current = item.id if item else 0
            self._show_editor(item is not None)
            if item is None:
                return
            start = item.start_dt or datetime.now().astimezone()
            minutes = local_offset_minutes(start)
            if self.zone.findData(minutes) < 0:
                self.zone.addItem(offset_label(minutes), minutes)
            self.zone.setCurrentIndex(self.zone.findData(minutes))
            self.start.setDateTime(QDateTime(QDate(start.year, start.month, start.day),
                                             QTime(start.hour, start.minute)))
            self.kind.setCurrentIndex(max(0, self.kind.findData(item.kind)))
            self.duration.setValue(int(item.duration_min or 0))
            self.place.setText(item.place)
            self.people.setText(item.people)
            if self.notes.toPlainText() != item.notes:
                self.notes.setPlainText(item.notes)
            self.outcome.setCurrentIndex(max(0, self.outcome.findData(item.outcome)))
            self.reminder.setValue(-1 if item.reminder_min is None else int(item.reminder_min))
        finally:
            self._loading = False

    # --- editing -----------------------------------------------------------------------

    def start_value(self) -> datetime:
        """The edited start as an aware datetime (the wall-clock time of the chosen UTC offset)."""
        qt = self.start.dateTime()
        offset = timezone(timedelta(minutes=int(self.zone.currentData())))
        return datetime(qt.date().year(), qt.date().month(), qt.date().day(), qt.time().hour(),
                        qt.time().minute(), tzinfo=offset)

    def _edit_start(self) -> None:
        self._edit("start", self.start_value().isoformat(timespec="seconds"))

    def _edit(self, name: str, value) -> None:
        if self._loading or not self._current:
            return
        self._pending[name] = value
        self._timer.start()

    def flush(self) -> None:
        self._timer.stop()
        if self._pending and self._current:
            pending, self._pending = self._pending, {}
            self.changed.emit(self._current, pending)
        self._pending = {}


# --- history -----------------------------------------------------------------------------


class HistoryTab(QWidget):
    def __init__(self, T: Texts, parent: QWidget | None = None):
        super().__init__(parent)
        self.T = T
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([T("applications.history.when"), T("applications.history.who"),
                                              T("applications.history.what"), T("applications.history.change")])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setObjectName("applicationsHistoryTable")
        # header-click sorting like every table of the app (the "When" cells carry their moment)
        from ..gui import table_filter

        table_filter.attach(self.table, kinds={0: "date", 1: "text", 2: "text", 3: "text"},
                            key="applicationsHistoryTable")
        layout = QVBoxLayout(self)
        layout.addWidget(self.table)

    def _value(self, entry: HistoryEntry, text: str) -> str:
        if entry.field == "status" and text:
            return self.T(f"applications.status.{text}")
        return text or "-"

    def set_entries(self, entries: list[HistoryEntry]) -> None:
        from ..gui import table_sort

        self.table.setRowCount(len(entries))
        for row, entry in enumerate(reversed(entries)):
            when = QLocale().toString(entry.moment.replace(tzinfo=None), QLocale.FormatType.ShortFormat)
            cells = (when, self.T(f"applications.who.{entry.who}"), self.T(f"applications.field.{entry.field}"),
                     f"{self._value(entry, entry.old)} -> {self._value(entry, entry.new)}")
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setData(table_sort.SORT_ROLE, entry.moment.timestamp())
                self.table.setItem(row, column, item)
        table_sort.resort(self.table)


# --- next appointments list ---------------------------------------------------------------


class UpcomingAppointments(QWidget):
    """A small list of the next appointments, for a home screen or a side panel.

    ``refresh(service.upcoming_appointments(...))``; double-click emits ``activated(application_id)``."""

    activated = Signal(int)

    def __init__(self, tr=None, parent: QWidget | None = None):
        super().__init__(parent)
        self.T = tr if isinstance(tr, Texts) else Texts(tr)
        self.list = QListWidget()
        self.empty = QLabel(self.T("applications.upcoming.none"))
        self.empty.setProperty("role", "muted")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.empty)
        layout.addWidget(self.list)
        self.list.itemActivated.connect(lambda item: self.activated.emit(int(item.data(Qt.ItemDataRole.UserRole))))
        self.refresh([])

    def refresh(self, pairs: list[tuple[Appointment, Application]]) -> None:
        self.list.clear()
        for item, app in pairs:
            start = item.start_dt
            when = QLocale().toString(start.astimezone().replace(tzinfo=None),
                                      QLocale.FormatType.ShortFormat) if start else "-"
            entry = QListWidgetItem(f"{when}  {app.company_name}  ({self.T(f'applications.appointment.kind.{item.kind}')})")
            entry.setData(Qt.ItemDataRole.UserRole, app.id)
            self.list.addItem(entry)
        self.empty.setVisible(self.list.count() == 0)
        self.list.setVisible(self.list.count() > 0)
