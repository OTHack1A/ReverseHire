"""Parts of the "CV and profile" page added around the editable lists: dropping a CV
file anywhere on the page, the extracted-text viewer, work experience, mobility
(business travel, relocation, driving licence), home town and seniority."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from .. import display_text
from ..i18n import t
from ..models import EmploymentGap, Evidence, Mobility, WorkExperience
from .common import enum_label, format_number, is_cv_file
from .theme import active_palette, section
from . import table_filter, table_sort
from .widgets import ElidedLabel, wrapped_label

WORKING_DAYS = 220  # reference year of working days for business travel (as cv/mobility.py)
NOT_STATED = -1     # spin-box value meaning "days not stated"
TEXT_COLUMN_CHARS = 22  # role/employer columns of the work-experience table
TRAVEL_STATES = ("declared", "likely", "excluded", "unknown")
RELOCATION_STATES = ("declared", "excluded", "unknown")
SENIORITY_LEVELS = ("junior", "mid", "senior")
CAR_STATES = ("yes", "no", "unknown")


# --- drag and drop -----------------------------------------------------------------------


def cv_paths_from_mime(mime) -> list[str]:
    """Local CV files (supported formats only) carried by a drag-and-drop payload."""
    if mime is None or not mime.hasUrls():
        return []
    paths = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
    return [p for p in paths if p and is_cv_file(p)]


class FileDropFilter(QObject):
    """Accepts a CV file dropped on the watched widgets (the whole page and its children).

    Installed on every child too, so that line edits and lists do not swallow the
    drop as text; anything that is not a supported file is left to the widget.
    """

    def __init__(self, on_file: Callable[[str], None], parent: QObject | None = None):
        super().__init__(parent)
        self._on_file = on_file

    def watch(self, root: QWidget) -> None:
        for widget in [root, *root.findChildren(QWidget)]:
            widget.installEventFilter(self)
            if widget is root:
                widget.setAcceptDrops(True)

    def handle(self, event) -> bool:
        """True when the event was a CV-file drag/drop and has been handled."""
        kind = event.type()
        if kind not in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop):
            return False
        paths = cv_paths_from_mime(event.mimeData())
        if not paths:
            return False
        event.acceptProposedAction()
        if kind == QEvent.Type.Drop:
            self._on_file(paths[0])
        return True

    def eventFilter(self, obj, event):  # noqa: N802 (Qt name)
        return self.handle(event)


# --- extracted text ------------------------------------------------------------------------


class TextViewer(QDialog):
    """Read-only window with the text extracted from the CV (useful after OCR)."""

    def __init__(self, parent: QWidget, text: str, title: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(False)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlainText(text)
        self.note = wrapped_label(t("profile.text_note"), "muted")
        self.close_button = QPushButton(t("common.close"))
        self.close_button.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.note)
        layout.addWidget(self.text, 1)
        layout.addLayout(buttons)
        metrics = self.fontMetrics()
        self.resize(metrics.averageCharWidth() * 100, metrics.height() * 36)


# --- helpers -----------------------------------------------------------------------------------


def evidence_text(evidence: list[Evidence] | None) -> str:
    """The CV lines, in English when the interface is English (display layer)."""
    return " · ".join(display_text.display(e.text) for e in evidence or [] if e.text)


def evidence_tooltip(evidence: list[Evidence] | None) -> str:
    """Tooltip of quoted CV lines: the shown text plus the CV wording when translated."""
    shown = [display_text.show(e.text) for e in evidence or [] if e.text]
    text = " · ".join(s.text for s in shown)
    originals = [s.original for s in shown if s.original]
    if originals:
        text += "\n" + t("display.original", text=" · ".join(originals))
    return text


# English texts used until the translation files carry these keys.
FALLBACK_TEXTS = {
    "profile.from_cv": "From the CV: {text}",
    "profile.translated_local": "CV in {language}: fields translated into English (local translation)",
    "profile.translated_tables": "CV in {language}: fields translated into English (built-in tables)",
    "profile.untranslated": "{count} entries stay in the original language (local translation model not available)",
    "cv_language.uk": "Ukrainian",
}


def text_or_default(key: str, **params) -> str:
    """`t(key)`, or the English fallback of FALLBACK_TEXTS while the key is missing."""
    text = t(key, **params)
    if text == key and key in FALLBACK_TEXTS:
        try:
            return FALLBACK_TEXTS[key].format(**params)
        except (KeyError, IndexError):
            return FALLBACK_TEXTS[key]
    return text


def original_text(original: str | None) -> str:
    """'From the CV: ...' line for a translated value ("" when not translated)."""
    return text_or_default("profile.from_cv", text=original) if original else ""


def period_point(value: str) -> str:
    """"2021-03" -> "03/2021", "2019" -> "2019", "present" -> localised word."""
    value = (value or "").strip()
    if not value:
        return "?"
    if value.lower() in ("present", "now", "current"):
        return t("experience.present")
    if len(value) == 7 and value[4] == "-":
        return f"{value[5:]}/{value[:4]}"
    return value


def period_text(start: str, end: str) -> str:
    return f"{period_point(start)} – {period_point(end)}"


def duration_text(months: int | None) -> str:
    if months is None or months < 0:
        return ""
    years, rest = divmod(int(months), 12)
    if not years:
        return t("experience.months", months=rest)
    if not rest:
        return t("experience.years", years=years)
    return t("experience.years_months", years=years, months=rest)


def _sort_point(value: str) -> str:
    value = (value or "").strip().lower()
    return "9999-99" if value in ("present", "now", "current") else value


def history_rows(experiences: list[WorkExperience], gaps: list[EmploymentGap]) -> list[tuple[str, object]]:
    """Experiences and gaps in one list, most recent first: [("job"|"gap", item)]."""
    rows = [("job", e) for e in experiences] + [("gap", g) for g in gaps]
    rows.sort(key=lambda r: (_sort_point(r[1].end), _sort_point(r[1].start)), reverse=True)
    return rows


class _Choice(QComboBox):
    """Combo box of enum values with translated labels; `value()` gives the code."""

    def __init__(self, values: tuple[str, ...], kind: str, on_change: Callable[[], None]):
        super().__init__()
        self._values = values
        self._kind = kind
        for value in values:
            self.addItem("", value)
        self.setCurrentIndex(self.count() - 1)  # the "not stated" choice until a CV is read
        self.currentIndexChanged.connect(lambda _i: on_change())

    def retranslate(self) -> None:
        for i, value in enumerate(self._values):
            self.setItemText(i, enum_label(self._kind, value))
        # never narrower than the longest choice (the styled box reports a tiny minimum)
        widest = max(self.fontMetrics().horizontalAdvance(self.itemText(i)) for i in range(self.count()))
        self.setMinimumWidth(widest + 52)  # padding, frame and drop-down arrow of the stylesheet

    def value(self) -> str:
        return self.currentData()

    def set_value(self, value: str | None) -> None:
        index = self.findData(value)
        self.blockSignals(True)
        self.setCurrentIndex(index if index >= 0 else self.count() - 1)
        self.blockSignals(False)


# --- boxes ----------------------------------------------------------------------------------


class ExperienceBox(QGroupBox):
    """Chronological work history (most recent first) with the gaps between jobs."""

    COLUMNS = ("period", "duration", "role", "employer")

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._experiences: list[WorkExperience] = []
        self._gaps: list[EmploymentGap] = []
        self._years: float | None = None
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setObjectName("experienceTable")
        table_filter.attach(self.table)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for i in range(len(self.COLUMNS)):
            self.table.setHorizontalHeaderItem(i, QTableWidgetItem())
        # Single-line labels only inside the page's scroll area: a word-wrapped label would
        # make the scroll area use the preferred (taller) height instead of the minimum.
        self.summary_label = ElidedLabel()
        self.summary_label.setProperty("role", "muted")
        layout = QVBoxLayout(self)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.summary_label)

    def set_history(self, experiences: list[WorkExperience], gaps: list[EmploymentGap],
                    years: float | None) -> None:
        self._experiences, self._gaps, self._years = list(experiences), list(gaps), years
        self._redraw()

    def _redraw(self) -> None:
        p = active_palette()
        rows = history_rows(self._experiences, self._gaps)
        self.table.setRowCount(len(rows))
        for r, (kind, item) in enumerate(rows):
            if kind == "job":
                values = (period_text(item.start, item.end), duration_text(item.months), item.title,
                          item.employer)
                tooltip = "\n".join(x for x in (original_text(getattr(item, "original_title", None)),
                                                evidence_tooltip(item.evidence)) if x)
            else:
                values = (period_text(item.start, item.end), duration_text(item.months),
                          t("experience.gap"), "")
                tooltip = t("experience.gap_tooltip", months=item.months)
            for c, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setToolTip(tooltip or value)
                if kind == "gap":
                    cell.setForeground(QBrush(QColor(p.warning)))
                    font = QFont(self.table.font())
                    font.setItalic(True)
                    cell.setFont(font)
                self.table.setItem(r, c, cell)
        table_sort.resort(self.table)
        self._fit_text_columns()
        self._update_summary()

    def _fit_text_columns(self) -> None:
        """Role: its content up to TEXT_COLUMN_CHARS (longer texts elide, with tooltip);
        the employer takes the rest."""
        header = self.table.horizontalHeader()
        metrics = self.table.fontMetrics()
        for column in (2,):
            widest = max([metrics.horizontalAdvance(self.table.item(r, column).text())
                          for r in range(self.table.rowCount())] or [0])
            limit = metrics.averageCharWidth() * TEXT_COLUMN_CHARS
            header.resizeSection(column, max(header.sectionSizeHint(column), min(widest + 20, limit)))

    def _update_summary(self) -> None:
        parts = []
        if self._years is not None:
            parts.append(t("experience.total", years=format_number(self._years, 1)))
        parts.append(t("experience.jobs", count=len(self._experiences)))
        if self._gaps:
            parts.append(t("experience.gaps", count=len(self._gaps)))
        if not self._experiences:
            parts = [t("experience.none")]
        self.summary_label.setText(" · ".join(parts))

    def row_texts(self) -> list[tuple[str, ...]]:
        return [tuple(self.table.item(r, c).text() for c in range(self.table.columnCount()))
                for r in range(self.table.rowCount())]

    def retranslate(self) -> None:
        self.setTitle(section(t("profile.experience")))
        for i, column in enumerate(self.COLUMNS):
            self.table.horizontalHeaderItem(i).setText(t(f"experience.{column}"))
        self._redraw()


class MobilityBox(QGroupBox):
    """Business travel (state + days a year out of 220), relocation, driving licence, car."""

    def __init__(self, on_change: Callable[[], None], parent: QWidget | None = None):
        super().__init__(parent)
        self._on_change = on_change
        self._mobility = Mobility()
        self.travel_label = QLabel()
        self.travel_combo = _Choice(TRAVEL_STATES, "mobility.travel", self._changed)
        self.travel_evidence = ElidedLabel()
        self.travel_evidence.setProperty("role", "muted")
        self.days_label = QLabel()
        self.days_spin = QSpinBox()
        self.days_spin.setRange(NOT_STATED, WORKING_DAYS)
        self.days_spin.setSpecialValueText("—")
        self.days_spin.setValue(NOT_STATED)
        self.days_spin.valueChanged.connect(self._days_changed)
        self.percent_label = ElidedLabel()
        self.percent_label.setProperty("role", "muted")
        self.relocation_label = QLabel()
        self.relocation_combo = _Choice(RELOCATION_STATES, "mobility.relocation", self._changed)
        self.relocation_evidence = ElidedLabel()
        self.relocation_evidence.setProperty("role", "muted")
        self.licence_label = QLabel()
        self.licence_field = QLineEdit()
        self.licence_field.editingFinished.connect(self._licence_edited)
        self.car_label = QLabel()
        self.car_combo = _Choice(CAR_STATES, "mobility.car", self._changed)

        grid = QGridLayout(self)
        grid.setVerticalSpacing(4)
        grid.addWidget(self.travel_label, 0, 0)
        grid.addWidget(self.travel_combo, 0, 1, 1, 3)
        grid.addWidget(self.travel_evidence, 1, 0, 1, 4)
        grid.addWidget(self.days_label, 2, 0, 1, 4)
        days_row = QHBoxLayout()
        days_row.setContentsMargins(0, 0, 0, 0)
        days_row.addWidget(self.days_spin)
        days_row.addWidget(self.percent_label, 1)
        grid.addLayout(days_row, 3, 0, 1, 4)
        grid.addWidget(self.relocation_label, 4, 0)
        grid.addWidget(self.relocation_combo, 4, 1, 1, 3)
        grid.addWidget(self.relocation_evidence, 5, 0, 1, 4)
        grid.addWidget(self.licence_label, 6, 0)
        grid.addWidget(self.licence_field, 6, 1, 1, 3)
        grid.addWidget(self.car_label, 7, 0)
        grid.addWidget(self.car_combo, 7, 1, 1, 3)
        grid.setColumnStretch(3, 1)
        grid.setRowStretch(8, 1)

    def set_mobility(self, mobility: Mobility | None) -> None:
        self._mobility = Mobility.from_dict(mobility.to_dict()) if mobility is not None else Mobility()
        m = self._mobility
        self.travel_combo.set_value(m.travel)
        self.relocation_combo.set_value(m.relocation)
        self.days_spin.blockSignals(True)
        self.days_spin.setValue(NOT_STATED if m.travel_days_per_year is None
                                else max(0, min(WORKING_DAYS, int(m.travel_days_per_year))))
        self.days_spin.blockSignals(False)
        self.licence_field.setText(", ".join(m.driving_licence))
        self.car_combo.set_value({True: "yes", False: "no"}.get(m.own_car, "unknown"))
        self._update_texts()

    def mobility(self) -> Mobility:
        """The mobility with the user's corrections (a copy)."""
        m = Mobility.from_dict(self._mobility.to_dict())
        m.travel = self.travel_combo.value()
        m.relocation = self.relocation_combo.value()
        days = self.days_spin.value()
        m.travel_days_per_year = None if days == NOT_STATED else days
        m.driving_licence = [x.strip().upper() for x in self.licence_field.text().replace(";", ",").split(",")
                             if x.strip()]
        m.own_car = {"yes": True, "no": False}.get(self.car_combo.value())
        return m

    def _changed(self) -> None:
        self._update_texts()
        self._on_change()

    def _days_changed(self, _value: int) -> None:
        self._update_texts()
        self._on_change()

    def _licence_edited(self) -> None:
        if self.licence_field.isModified():
            self.licence_field.setModified(False)
            self._on_change()

    def _update_texts(self) -> None:
        days = self.days_spin.value()
        self.percent_label.setText("" if days == NOT_STATED else t(
            "mobility.percent", percent=format_number(days * 100 / WORKING_DAYS),
            weeks=format_number(days / 5, 1)))
        travel = evidence_text(self._mobility.travel_evidence)
        self.travel_evidence.setText(t("mobility.evidence", text=travel) if travel else t("mobility.no_evidence"))
        self.travel_evidence.setToolTip(evidence_tooltip(self._mobility.travel_evidence))
        relocation = evidence_text(self._mobility.relocation_evidence)
        self.relocation_evidence.setText(t("mobility.evidence", text=relocation) if relocation
                                         else t("mobility.no_evidence"))
        self.relocation_evidence.setToolTip(evidence_tooltip(self._mobility.relocation_evidence))
        self.licence_field.setToolTip(evidence_tooltip(self._mobility.licence_evidence))
        self.licence_field.setPlaceholderText(t("mobility.licence_placeholder"))

    def retranslate(self) -> None:
        self.setTitle(section(t("profile.mobility")))
        self.travel_label.setText(t("mobility.travel_label"))
        self.days_label.setText(t("mobility.days_label", days=WORKING_DAYS))
        self.days_spin.setSuffix(f" / {WORKING_DAYS}")
        self.relocation_label.setText(t("mobility.relocation_label"))
        self.licence_label.setText(t("mobility.licence_label"))
        self.car_label.setText(t("mobility.car_label"))
        for combo in (self.travel_combo, self.relocation_combo, self.car_combo):
            combo.retranslate()
        self._update_texts()


class HomeBox(QGroupBox):
    """Home town read from the CV, editable, usable as the search's starting point."""

    def __init__(self, on_change: Callable[[], None], on_use: Callable[[str], None],
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._on_change = on_change
        self.field = QLineEdit()
        self.field.textEdited.connect(lambda _t: self._edited())
        self.use_button = QPushButton()
        self.use_button.clicked.connect(lambda: on_use(self.home() or ""))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.field, 1)
        layout = QVBoxLayout(self)
        layout.addLayout(row)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self.use_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self._update_button()

    def _edited(self) -> None:
        self._update_button()
        self._on_change()

    def _update_button(self) -> None:
        self.use_button.setEnabled(bool(self.home()))

    def set_home(self, home: str | None) -> None:
        self.field.setText(home or "")
        self._update_button()

    def home(self) -> str | None:
        text = " ".join(self.field.text().split())
        return text or None

    def retranslate(self) -> None:
        self.setTitle(section(t("profile.home")))
        self.field.setPlaceholderText(t("profile.home_placeholder"))
        self.use_button.setText(t("profile.use_home"))
        self.use_button.setToolTip(t("profile.use_home_tooltip"))


class SeniorityBox(QGroupBox):
    """Seniority estimated from the years and the job titles, with its evidence."""

    def __init__(self, on_change: Callable[[], None], parent: QWidget | None = None):
        super().__init__(parent)
        self._evidence: list[Evidence] = []
        self._estimated: str | None = None
        self.combo = _Choice(SENIORITY_LEVELS + ("unknown",), "seniority", on_change)
        self.evidence_label = ElidedLabel()
        self.evidence_label.setProperty("role", "muted")
        layout = QVBoxLayout(self)
        layout.addWidget(self.combo)
        layout.addWidget(self.evidence_label)

    def set_seniority(self, level: str | None, evidence: list[Evidence]) -> None:
        self._estimated = level
        self._evidence = [Evidence.from_dict(e.to_dict()) for e in evidence or []]
        self.combo.set_value(level if level in SENIORITY_LEVELS else "unknown")
        self._update_evidence()

    def seniority(self) -> str | None:
        value = self.combo.value()
        return value if value in SENIORITY_LEVELS else None

    def evidence(self) -> list[Evidence]:
        if self.seniority() == self._estimated:
            return [Evidence.from_dict(e.to_dict()) for e in self._evidence]
        return [Evidence(enum_label("seniority", self.seniority()), "user")] if self.seniority() else []

    def _update_evidence(self) -> None:
        text = evidence_text(self._evidence)
        self.evidence_label.setText(t("mobility.evidence", text=text) if text else t("mobility.no_evidence"))
        self.evidence_label.setToolTip(evidence_tooltip(self._evidence))

    def retranslate(self) -> None:
        self.setTitle(section(t("profile.seniority")))
        self.combo.retranslate()
        self._update_evidence()
