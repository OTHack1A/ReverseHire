"""The table of applications: model, sorting (numeric/date aware, blanks always last) and filters."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from PySide6.QtCore import QAbstractTableModel, QLocale, QModelIndex, QSortFilterProxyModel, Qt, QDate
from PySide6.QtGui import QBrush, QColor

from .model import STATUSES, Application, Appointment, FollowUp, parse_day
from .service import fold_text
from .texts import Texts

log = logging.getLogger("reversehire.applications")

SORT_ROLE = Qt.ItemDataRole.UserRole + 1
ID_ROLE = Qt.ItemDataRole.UserRole + 2
COLUMNS = ("company", "role", "kind", "sent", "status", "appointment", "follow_up")


@dataclass
class Row:
    application: Application
    appointment: Appointment | None = None
    follow_up: FollowUp | None = None


def status_palette(status: str, palette=None) -> tuple[str, str]:
    """(background, foreground) colours of a status badge, from the theme's palette."""
    if palette is None:
        try:
            from ..gui import theme

            palette = theme.active_palette()
        except Exception:  # noqa: BLE001
            from ..gui.theme import PALETTES

            palette = PALETTES["dark"]
    mapping = {
        "draft": (palette.surface2, palette.muted),
        "sent": (palette.remote_bg, palette.remote_fg),
        "acknowledged": (palette.maybe_bg, palette.maybe_fg),
        "interview": (palette.hiring_bg, palette.hiring_fg),
        "offer": (palette.accent, palette.on_accent),
        "rejected": (palette.surface2, palette.error),
        "no_answer": (palette.surface2, palette.warning),
        "withdrawn": (palette.surface2, palette.muted),
    }
    return mapping.get(status, (palette.surface2, palette.text))


def _day_ordinal(value) -> int | None:
    day = parse_day(value)
    return day.toordinal() if day else None


def _format_day(value) -> str:
    day = parse_day(value)
    return QLocale().toString(QDate(day.year, day.month, day.day), QLocale.FormatType.ShortFormat) if day else ""


def _format_moment(moment: datetime | None) -> str:
    if moment is None:
        return ""
    local = moment.astimezone()
    return QLocale().toString(local.replace(tzinfo=None), QLocale.FormatType.ShortFormat)


class ApplicationsModel(QAbstractTableModel):
    def __init__(self, tr=None, parent=None):
        super().__init__(parent)
        self.T = tr if isinstance(tr, Texts) else Texts(tr)
        self.rows: list[Row] = []
        self.today: date = date.today()

    def set_rows(self, rows: list[Row], today: date | None = None) -> None:
        self.beginResetModel()
        self.rows = rows
        self.today = today or date.today()
        self.endResetModel()

    def row_for(self, application_id: int) -> int:
        for index, row in enumerate(self.rows):
            if row.application.id == application_id:
                return index
        return -1

    def headers(self) -> list[str]:
        return [self.T(f"applications.column.{name}") for name in COLUMNS]

    # --- Qt model API ------------------------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers()[section]
        return None

    def _follow_text(self, row: Row) -> str:
        if row.follow_up is None:
            return ""
        if row.follow_up.reason == "scheduled":
            return _format_day(row.application.follow_up_date)
        return self.T("applications.follow_up.due", days=row.follow_up.days_waiting)

    def sort_key(self, row: Row, column: str):
        app = row.application
        if column == "company":
            return fold_text(app.company_name) or None
        if column == "role":
            return fold_text(app.role) or None
        if column == "kind":
            return app.kind
        if column == "sent":
            return _day_ordinal(app.sent_date)
        if column == "status":
            return STATUSES.index(app.status) if app.status in STATUSES else None
        if column == "appointment":
            return row.appointment.start_dt.timestamp() if row.appointment and row.appointment.start_dt else None
        if column == "follow_up":
            if row.follow_up is None:
                return None
            if row.follow_up.reason == "scheduled":
                return _day_ordinal(app.follow_up_date)
            return self.today.toordinal()
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self.rows)):
            return None
        row = self.rows[index.row()]
        app, column = row.application, COLUMNS[index.column()]
        if role == ID_ROLE:
            return app.id
        if role == SORT_ROLE:
            return self.sort_key(row, column)
        if role == Qt.ItemDataRole.DisplayRole:
            if column == "company":
                return app.company_name
            if column == "role":
                return app.role
            if column == "kind":
                return self.T(f"applications.kind.{app.kind}")
            if column == "sent":
                return _format_day(app.sent_date)
            if column == "status":
                return self.T(f"applications.status.{app.status}")
            if column == "appointment":
                return _format_moment(row.appointment.start_dt) if row.appointment else ""
            return self._follow_text(row)
        if column == "status":
            background, foreground = status_palette(app.status)
            if role == Qt.ItemDataRole.BackgroundRole:
                return QBrush(QColor(background))
            if role == Qt.ItemDataRole.ForegroundRole:
                return QBrush(QColor(foreground))
        if column == "follow_up" and row.follow_up is not None and role == Qt.ItemDataRole.ForegroundRole:
            return QBrush(QColor(status_palette("no_answer")[1]))
        if role == Qt.ItemDataRole.ToolTipRole and column in ("company", "role"):
            return app.display_title
        return None


class ApplicationsProxy(QSortFilterProxyModel):
    """Sorts by the model's SORT_ROLE with blank cells last in both directions, and filters."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self.setDynamicSortFilter(True)
        self.text = ""
        self.status = ""
        self.kind = ""

    def set_filters(self, text: str = "", status: str = "", kind: str = "") -> None:
        self.beginFilterChange()
        self.text, self.status, self.kind = fold_text(text), status, kind
        self.endFilterChange()

    def lessThan(self, left, right) -> bool:  # noqa: N802
        a, b = left.data(SORT_ROLE), right.data(SORT_ROLE)
        if a is None or b is None:
            # Qt sorts descending with the arguments swapped: the answer is inverted for a
            # blank so that blank cells end up last in both directions
            if a is None and b is None:
                return False
            if self.sortOrder() == Qt.SortOrder.DescendingOrder:
                return a is None
            return b is None
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)

    def filterAcceptsRow(self, source_row, source_parent) -> bool:  # noqa: N802
        model = self.sourceModel()
        if not (0 <= source_row < len(model.rows)):
            return False
        app = model.rows[source_row].application
        if self.status and app.status != self.status:
            return False
        if self.kind and app.kind != self.kind:
            return False
        if self.text:
            hay = fold_text(" ".join((app.company_name, app.role, app.recipient_email, app.contact_name,
                                      app.notes)))
            return all(word in hay for word in self.text.split())
        return True
