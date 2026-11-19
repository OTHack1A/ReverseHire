"""``ApplicationsPanel``: the "Applications sent" tab (table + detail tabs).

Everything the panel needs from outside is injected, so it can be built offscreen:
the service, a translation callable ``tr(key, default)``, a background runner, the dialogs
(``ask``/``open_file``/``save_file``/``choose_folder``), a list of companies and the
opener functions. Threading: database reads and the small writes of the autosave run on
the GUI thread (the database uses the short GUI busy timeout and raises ``StoreBusyError``
instead of waiting); everything that reads or copies files (archiving a CV, loading the
PDF bytes, exports, CSV import, deleting with garbage collection) runs on the runner.
Errors are logged (``reversehire.applications``) and shown as a non-blocking notice.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QModelIndex, QPoint, QSignalBlocker, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMenu, QPushButton, QScrollArea, QSplitter, QTableView, QTabWidget, QVBoxLayout,
                               QWidget)

from .detail_widgets import (DEFAULT_DEBOUNCE_MS, AppointmentsTab, CvTab, DetailsTab, HistoryTab, LetterTab)
from .model import KINDS, STATUS_SENT, STATUSES, Application
from .service import ApplicationService, DEFAULT_FOLLOW_UP_DAYS
from .table import ID_ROLE, ApplicationsModel, ApplicationsProxy, Row
from .texts import Texts
from .workers import ThreadRunner, guarded

log = logging.getLogger("reversehire.applications")

UNDO_LIMIT = 50
TABS = ("details", "letter", "cv", "appointments", "history")


class PanelDialogs:
    """Default dialogs: the application's themed ``gui.widgets.Dialogs`` plus a folder picker."""

    def __init__(self, parent: QWidget):
        from ..gui.widgets import Dialogs

        self._parent = parent
        self._base = Dialogs(parent)

    def ask(self, text: str, title: str | None = None, warning: bool = False) -> bool:
        return self._base.ask(text, title, warning)

    def info(self, text: str, title: str | None = None) -> None:
        self._base.info(text, title)

    def error(self, text: str, title: str | None = None) -> None:
        self._base.error(text, title)

    def open_file(self, title: str, filters: str) -> str:
        return self._base.open_file(title, filters)

    def save_file(self, title: str, default_name: str, filters: str) -> str:
        return self._base.save_file(title, default_name, filters)

    def choose_folder(self, title: str) -> str:
        from PySide6.QtWidgets import QFileDialog

        return QFileDialog.getExistingDirectory(self._parent, title, "", QFileDialog.Option.DontUseNativeDialog)


def _scroll_area(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidget(widget)
    return area


def open_local_path(path: str | Path) -> bool:
    """Opens a file or folder with the system's default program."""
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


class ApplicationsPanel(QWidget):
    openCompanyRequested = Signal(int)   # company card id
    statusChanged = Signal(int, str)     # application id, new status
    followUpsChanged = Signal()          # the set of applications to chase changed
    applicationsChanged = Signal()       # anything in the list changed

    def __init__(self, service: ApplicationService, *, tr: Callable | None = None,
                 run: Callable | None = None, dialogs=None,
                 companies: Callable[[], list] | list | None = None,
                 reference_cv: Callable[[], str | None] | str | Path | None = None,
                 notify: Callable[[str], None] | None = None,
                 open_path: Callable[[str], object] | None = None,
                 open_url: Callable[[str], object] | None = None,
                 today: Callable[[], date] | None = None, follow_up_days: int = DEFAULT_FOLLOW_UP_DAYS,
                 debounce_ms: int = DEFAULT_DEBOUNCE_MS, parent: QWidget | None = None):
        super().__init__(parent)
        self.service = service
        self.T = tr if isinstance(tr, Texts) else Texts(tr)
        self._run = run if run is not None else ThreadRunner(self)
        self.dialogs = dialogs if dialogs is not None else PanelDialogs(self)
        self._companies = companies
        self._reference_cv = reference_cv
        self._notify_callback = notify
        self._open_path = open_path or open_local_path
        self._open_url = open_url or self._default_open_url
        self._today = today or date.today
        self.follow_up_days = follow_up_days
        self._undo: list[tuple[int, dict]] = []
        self._due_signature: tuple = ()
        self._cv_token = 0
        self._cv_loaded_for = 0
        self._dialog = None
        self._shown_id = 0
        self._building = True

        self._build_widgets(debounce_ms)
        self._build_actions()
        self._building = False
        self.refresh()

    # --- construction ------------------------------------------------------------------

    def _build_widgets(self, debounce_ms: int) -> None:
        T = self.T
        self.search = QLineEdit()
        self.search.setPlaceholderText(T("applications.filter.search"))
        self.search.setToolTip(T("applications.filter.search_tip"))
        self.search.setClearButtonEnabled(True)
        self.status_filter = QComboBox()
        self.status_filter.addItem(T("applications.filter.all_statuses"), "")
        for status in STATUSES:
            self.status_filter.addItem(T(f"applications.status.{status}"), status)
        self.kind_filter = QComboBox()
        self.kind_filter.addItem(T("applications.filter.all_kinds"), "")
        for kind in KINDS:
            self.kind_filter.addItem(T(f"applications.kind.{kind}"), kind)
        self.new_button = QPushButton(T("applications.action.new"))
        self.summary_label = QLabel()
        self.summary_label.setProperty("role", "muted")
        self.notice_label = QLabel()
        self.notice_label.setWordWrap(True)
        self.notice_label.setProperty("role", "muted")

        self.model = ApplicationsModel(self.T, self)
        self.proxy = ApplicationsProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setObjectName("applicationsTable")
        self.table.setModel(self.proxy)
        self._attach_sorting()  # before anything polishes the view (the safety net would attach it first)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.verticalHeader().setVisible(False)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.setAlternatingRowColors(True)

        self.details_tab = DetailsTab(T, debounce_ms)
        self.letter_tab = LetterTab(T, debounce_ms)
        self.cv_tab = CvTab(T)
        self.appointments_tab = AppointmentsTab(T, debounce_ms)
        self.history_tab = HistoryTab(T)
        self.tabs = QTabWidget()
        # the long forms scroll inside their tab: the window's minimum height must stay small
        # (1366x768 screens); the CV preview and the history table scroll by themselves
        scrolled = (self.details_tab, self.letter_tab, self.appointments_tab)
        for name, widget in zip(TABS, (self.details_tab, self.letter_tab, self.cv_tab, self.appointments_tab,
                                       self.history_tab)):
            self.tabs.addTab(_scroll_area(widget) if widget in scrolled else widget, T(f"applications.tab.{name}"))
        self.empty_label = QLabel(T("applications.empty"))
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setProperty("role", "muted")

        bar = QHBoxLayout()
        bar.addWidget(self.search, 2)
        bar.addWidget(self.status_filter)
        bar.addWidget(self.kind_filter)
        bar.addWidget(self.new_button)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addLayout(bar)
        top_layout.addWidget(self.summary_label)
        top_layout.addWidget(self.table, 1)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.addWidget(top)
        self.splitter.addWidget(self.tabs)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        layout = QVBoxLayout(self)
        layout.addWidget(self.splitter, 1)
        layout.addWidget(self.empty_label)
        layout.addWidget(self.notice_label)

        selection = self.table.selectionModel()
        selection.currentRowChanged.connect(self._on_current_changed)
        self.table.doubleClicked.connect(lambda *_: self.open_selected())
        self.table.customContextMenuRequested.connect(self._on_context_menu_requested)
        self.search.textChanged.connect(lambda *_: self._apply_filters())
        self.status_filter.activated.connect(lambda *_: self._apply_filters())
        self.kind_filter.activated.connect(lambda *_: self._apply_filters())
        self.new_button.clicked.connect(lambda *_: self.new_application())
        self.tabs.currentChanged.connect(lambda *_: self._on_tab_changed())
        self.details_tab.edited.connect(lambda i, c: self._on_edited(i, c, "details"))
        self.letter_tab.edited.connect(lambda i, c: self._on_edited(i, c, "letter"))
        self.details_tab.openCompanyRequested.connect(lambda: self.open_company())
        self.details_tab.openPostingRequested.connect(lambda: self.open_posting())
        self.cv_tab.replaceRequested.connect(lambda: self.replace_cv())
        self.cv_tab.openExternalRequested.connect(lambda: self.open_cv_external())
        self.appointments_tab.addRequested.connect(lambda: self.add_appointment())
        self.appointments_tab.changed.connect(lambda i, c: self._on_appointment_edited(i, c))
        self.appointments_tab.deleteRequested.connect(lambda i: self.delete_appointment(i))
        self.appointments_tab.icsRequested.connect(lambda i: self.export_appointment_ics(i))

    def _attach_sorting(self) -> None:
        """Header-click sorting the way every table of the app has it (``gui.table_sort``:
        arrow, first click ascending, second descending, session memory; no filter row).
        The panel sorts its own rows ("delegate"): the proxy orders by the model's sort keys
        (numbers, dates and workflow order, blanks last)."""
        from ..gui import table_filter, table_sort
        from .table import COLUMNS

        dates = ("sent", "appointment", "follow_up")
        kinds = {i: ("date" if name in dates else "text") for i, name in enumerate(COLUMNS)}
        table_filter.attach(self.table, kinds=kinds, key="applicationsTable", sorting="delegate")
        self._sorter = table_sort.sorter_of(self.table)
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        if self._sorter is not None and self._sorter.column is not None:
            self._sort_rows()  # the order chosen earlier in this session

    def _on_header_clicked(self, column: int) -> None:
        sorter = self._sorter
        if sorter is None:
            return
        ascending = not sorter.ascending if column == sorter.column else True
        sorter.record(column, ascending)
        self._sort_rows()
        log.info("Applications sorted by column %d (%s)", column, "ascending" if ascending else "descending")

    def _sort_rows(self) -> None:
        sorter = self._sorter
        order = Qt.SortOrder.AscendingOrder if sorter.ascending else Qt.SortOrder.DescendingOrder
        self.proxy.sort(sorter.column, order)
        sorter.show_indicator()

    def _build_actions(self) -> None:
        T = self.T
        self.actions_by_name: dict[str, QAction] = {}

        def make(name: str, key: str, slot: Callable[[], object], checkable: bool = False) -> QAction:
            action = QAction(T(key), self)
            action.setCheckable(checkable)
            action.triggered.connect(lambda *_: slot())
            self.actions_by_name[name] = action
            return action

        make("new", "applications.action.new", self.new_application)
        make("open", "applications.action.open", self.open_selected)
        make("mark_sent", "applications.action.mark_sent", self.mark_sent_selected)
        self.status_group = QActionGroup(self)
        self.status_group.setExclusive(True)
        for status in STATUSES:
            action = make(f"status_{status}", f"applications.status.{status}",
                          lambda s=status: self.set_status_selected(s), checkable=True)
            self.status_group.addAction(action)
        make("add_appointment", "applications.action.add_appointment", self.add_appointment)
        make("open_company", "applications.action.open_company", self.open_company)
        make("open_cv", "applications.action.open_cv", self.open_cv_external)
        make("export", "applications.action.export", self.export_selected)
        make("export_csv", "applications.action.export_csv", self.export_csv)
        make("import_csv", "applications.action.import_csv", self.import_csv)
        make("calendar", "applications.action.calendar", self.export_current_appointment_ics)
        make("undo", "applications.action.undo", self.undo)
        make("delete", "applications.action.delete", self.delete_selected)
        self.status_menu = QMenu(T("applications.action.change_status"), self)
        for status in STATUSES:
            self.status_menu.addAction(self.actions_by_name[f"status_{status}"])
        self._update_actions()

    def menu_actions(self) -> list[QAction | None]:
        """The actions in the order the integrator should put them in a menu (None = separator)."""
        names = ["new", "open", "mark_sent", "add_appointment", "calendar", "open_company", "open_cv", None,
                 "export", "export_csv", "import_csv", None, "undo", "delete"]
        return [self.actions_by_name[n] if n else None for n in names]

    # --- notices and errors ------------------------------------------------------------

    def notify(self, text: str) -> None:
        self.notice_label.setText(text)
        try:
            if self._notify_callback is not None:
                self._notify_callback(text)
            else:
                from ..gui import safety

                safety.notify(text)
        except Exception:  # noqa: BLE001 - a notice must never fail loudly
            log.debug("Notice not shown", exc_info=True)

    def report_error(self, error: BaseException) -> None:
        log.warning("Applications tracker error: %s", error, exc_info=error)
        try:
            from ..store import StoreBusyError

            if isinstance(error, StoreBusyError):
                self.notify(self.T("applications.error.busy"))
                return
        except Exception:  # noqa: BLE001
            pass
        if isinstance(error, ValueError):
            self.notify(self.T("applications.error.invalid", detail=str(error)))
        else:
            self.notify(self.T("applications.error.generic", detail=str(error)[:200]))

    def _job(self, work: Callable, on_success: Callable | None = None) -> None:
        """Runs `work` on the runner; the outcome is handled on the GUI thread, errors become notices."""

        def ok(result):
            try:
                if on_success is not None:
                    on_success(result)
            except Exception as error:  # noqa: BLE001
                self.report_error(error)

        self._run(work, ok, self.report_error)

    # --- data --------------------------------------------------------------------------

    def refresh(self, select_id: int | None = None) -> None:
        """Reloads the table from the database, keeping (or choosing) the selection."""
        keep = select_id if select_id is not None else self.current_id()
        today = self._today()
        try:
            applications = self.service.list()
            nexts = self.service.next_appointments()
            follow = {f.application.id: f for f in self.service.due_follow_ups(today, self.follow_up_days)}
            stats = self.service.statistics()
        except Exception as error:  # noqa: BLE001
            self.report_error(error)
            return
        rows = [Row(a, nexts.get(a.id), follow.get(a.id)) for a in applications]
        blocker = QSignalBlocker(self.table.selectionModel())
        self.model.set_rows(rows, today)
        del blocker
        self.empty_label.setVisible(not rows)
        self.summary_label.setText(self._summary(stats))
        self._select_row_for(keep, notify=False)
        signature = tuple(sorted((i, f.reason) for i, f in follow.items()))
        if signature != self._due_signature:
            self._due_signature = signature
            self.followUpsChanged.emit()
        self._update_actions()
        self.applicationsChanged.emit()

    def _summary(self, stats) -> str:
        if not stats.total:
            return ""
        median = (self.T("applications.summary.median", days=f"{stats.median_days_to_answer:g}")
                  if stats.median_days_to_answer is not None else "")
        return self.T("applications.summary", total=stats.total, sent=stats.sent, answered=stats.answered,
                      interviews=stats.interviews, median=median).strip()

    def follow_ups(self):
        """The applications to chase today (for a start-up notice)."""
        return self.service.due_follow_ups(self._today(), self.follow_up_days)

    def companies(self) -> list[tuple[int | None, str]]:
        source = self._companies() if callable(self._companies) else (self._companies or [])
        return [(c[0], c[1]) for c in source]

    def reference_cv(self) -> str:
        value = self._reference_cv() if callable(self._reference_cv) else self._reference_cv
        return str(value) if value else ""

    def _apply_filters(self) -> None:
        self.proxy.set_filters(self.search.text(), self.status_filter.currentData() or "",
                               self.kind_filter.currentData() or "")
        current = self.current_id()
        if current and not self._visible(current):
            self.table.clearSelection()
            self._show_application(None)

    def _visible(self, application_id: int) -> bool:
        return self._proxy_row_of(application_id) >= 0

    def _proxy_row_of(self, application_id: int) -> int:
        for row in range(self.proxy.rowCount()):
            if self.proxy.index(row, 0).data(ID_ROLE) == application_id:
                return row
        return -1

    # --- selection ---------------------------------------------------------------------

    def current_id(self) -> int:
        index = self.table.currentIndex()
        if not index.isValid() or not self.table.selectionModel().isRowSelected(index.row(), QModelIndex()):
            return 0
        return int(index.sibling(index.row(), 0).data(ID_ROLE) or 0)

    def current_application(self) -> Application | None:
        ident = self.current_id()
        return self.service.get(ident) if ident else None

    def select_application(self, application_id: int) -> bool:
        return self._select_row_for(application_id, notify=True)

    def reveal(self, application_id: int) -> bool:
        """Selects a record even when the filters hide it (the filters are cleared first)."""
        self.refresh(select_id=application_id)
        if application_id and not self._visible(application_id):
            for box in (self.status_filter, self.kind_filter):
                box.setCurrentIndex(0)
            blocker = QSignalBlocker(self.search)
            self.search.clear()
            del blocker
            self._apply_filters()
        return self.select_application(application_id)

    def flush_pending(self) -> None:
        """Saves at once the edits still waiting for their debounce (before closing or rebuilding)."""
        self._flush_editors()

    def _select_row_for(self, application_id: int, notify: bool) -> bool:
        row = self._proxy_row_of(application_id) if application_id else -1
        if self._shown_id and self._shown_id != application_id:
            self._flush_editors()  # an edit still waiting for its debounce belongs to the record being left
        if row < 0:
            blocker = QSignalBlocker(self.table.selectionModel())
            self.table.clearSelection()
            self.table.setCurrentIndex(QModelIndex())
            del blocker
            if notify or self._shown_id:
                self._show_application(None)
            return False
        blocker = QSignalBlocker(self.table.selectionModel())
        self.table.selectRow(row)
        self.table.setCurrentIndex(self.proxy.index(row, 0))
        del blocker
        if notify or self._shown_id != application_id:
            self._show_application(self.service.get(application_id))
        return True

    def _on_current_changed(self, current, _previous) -> None:
        self._flush_editors()
        ident = int(current.sibling(current.row(), 0).data(ID_ROLE) or 0) if current.isValid() else 0
        self._show_application(self.service.get(ident) if ident else None)
        self._update_actions()

    def _flush_editors(self) -> None:
        for tab in (self.details_tab, self.letter_tab):
            tab.flush()
        self.appointments_tab.flush()

    def _show_application(self, app: Application | None) -> None:
        self._shown_id = app.id if app else 0
        self._reload_details(app, skip=None)
        self.tabs.setEnabled(app is not None)
        self._update_actions()
        self._cv_loaded_for = 0
        self._maybe_load_cv(app)

    def _reload_details(self, app: Application | None, skip: str | None) -> None:
        if skip != "details":
            self.details_tab.load(app)
        if skip != "letter":
            self.letter_tab.load(app)
        self.appointments_tab.set_appointments(self.service.appointments_for(app.id) if app else [])
        self.history_tab.set_entries(self.service.history(app.id) if app else [])

    def _on_tab_changed(self) -> None:
        if self.tabs.currentWidget() is self.cv_tab:
            self._maybe_load_cv(self.current_application())

    def show_tab(self, name: str) -> None:
        self.tabs.setCurrentIndex(TABS.index(name))

    def current_tab(self) -> str:
        """The name (see TABS) of the detail tab shown; some tabs sit in a scroll area."""
        index = self.tabs.currentIndex()
        return TABS[index] if 0 <= index < len(TABS) else ""

    # --- CV preview --------------------------------------------------------------------

    def _maybe_load_cv(self, app: Application | None) -> None:
        self.cv_tab.show_application(app)
        if app is None or not app.cv_hash or self.tabs.currentWidget() is not self.cv_tab:
            return
        self._cv_token += 1
        token = self._cv_token
        self._cv_loaded_for = app.id

        def done(data):
            if token != self._cv_token:
                return  # another record was selected meanwhile
            self.cv_tab.preview.show_bytes(data)

        self._job(lambda: self.service.read_cv(app), done)

    # --- actions -----------------------------------------------------------------------

    def _update_actions(self) -> None:
        if getattr(self, "_building", False):
            return
        app = self.service.get(self.current_id()) if self.current_id() else None
        has = app is not None
        for name in ("open", "add_appointment", "export", "delete"):
            self.actions_by_name[name].setEnabled(has)
        self.actions_by_name["mark_sent"].setEnabled(has and app.status != STATUS_SENT)
        self.actions_by_name["open_company"].setEnabled(has and app.company_id is not None)
        self.actions_by_name["open_cv"].setEnabled(has and bool(app.cv_hash))
        self.actions_by_name["calendar"].setEnabled(has and bool(self.appointments_tab.current_id()))
        self.actions_by_name["undo"].setEnabled(bool(self._undo))
        self.status_menu.setEnabled(has)
        for status in STATUSES:
            action = self.actions_by_name[f"status_{status}"]
            action.setEnabled(has)
            blocker = QSignalBlocker(action)
            action.setChecked(has and app.status == status)
            del blocker

    @guarded
    def open_selected(self) -> None:
        if self.current_id():
            self.show_tab("details")
            self.details_tab.company.setFocus()

    @guarded
    def mark_sent_selected(self) -> None:
        app = self.current_application()
        if app is None:
            return
        self._apply_changes(app.id, {"status": STATUS_SENT, "sent_date": app.sent_date or self._today().isoformat()})

    @guarded
    def set_status_selected(self, status: str) -> None:
        app = self.current_application()
        if app is not None and app.status != status:
            self._apply_changes(app.id, {"status": status})

    @guarded
    def open_company(self) -> None:
        app = self.current_application()
        if app is not None and app.company_id is not None:
            self.openCompanyRequested.emit(app.company_id)

    @guarded
    def open_posting(self) -> None:
        app = self.current_application()
        if app is not None and app.posting_url:
            self._open_url(app.posting_url)

    @staticmethod
    def _default_open_url(url: str) -> bool:
        from ..gui.common import open_url

        return open_url(url)

    # --- editing, undo -----------------------------------------------------------------

    def _on_edited(self, application_id: int, changes: dict, origin: str) -> None:
        try:
            self._apply_changes(application_id, changes, origin=origin)
        except Exception as error:  # noqa: BLE001
            self.report_error(error)

    def _apply_changes(self, application_id: int, changes: dict, origin: str | None = None,
                       undoable: bool = True) -> Application | None:
        before = self.service.get(application_id)
        if before is None:
            return None
        old = {k: getattr(before, k) for k in changes}
        try:
            updated = self.service.update(application_id, who="user", **changes)
        except Exception as error:  # noqa: BLE001 - invalid input or a busy database
            self.report_error(error)
            self._reload_details(before, skip=None)
            return None
        if undoable and any(old[k] != getattr(updated, k) for k in changes):
            self._undo.append((application_id, old))
            del self._undo[:-UNDO_LIMIT]
        if "status" in changes and before.status != updated.status:
            self.statusChanged.emit(application_id, updated.status)
        mismatch = any(getattr(updated, k) != v for k, v in changes.items())
        self.refresh()
        if self.current_id() == application_id:
            self._reload_details(updated, skip=None if mismatch or origin is None else origin)
        self._update_actions()
        return updated

    @guarded
    def undo(self) -> None:
        if not self._undo:
            return
        self._flush_editors()
        application_id, old = self._undo.pop()
        before = self.service.get(application_id)
        if before is None:
            self._update_actions()
            return
        updated = self.service.update(application_id, who="user", **old)
        if before.status != updated.status:
            self.statusChanged.emit(application_id, updated.status)
        self.refresh(select_id=application_id)
        self.select_application(application_id)
        self.notify(self.T("applications.notice.undone"))

    # --- creating ----------------------------------------------------------------------

    @guarded
    def new_application(self):
        """Opens the dialog to record an application by hand (non-blocking)."""
        from .dialog import ApplicationDialog

        dialog = ApplicationDialog(self.service, tr=self.T, companies=self.companies(),
                                   reference_cv=self.reference_cv(), dialogs=self.dialogs, run=self._run,
                                   today=self._today, parent=self)
        dialog.valuesAccepted.connect(lambda values: self.create_from_values(values))
        self._dialog = dialog
        dialog.open()
        return dialog

    @guarded
    def create_from_values(self, values: dict) -> None:
        """Creates a record from the dialog's values (archiving the CV on the runner)."""

        def done(app):
            self.refresh(select_id=app.id)
            self.select_application(app.id)
            self.notify(self.T("applications.notice.created", name=app.display_title))

        self._job(lambda: self.service.create(**values), done)

    # --- deleting ----------------------------------------------------------------------

    @guarded
    def delete_selected(self) -> None:
        app = self.current_application()
        if app is None:
            return
        if not self.dialogs.ask(self.T("applications.delete.confirm", name=app.display_title or "-"),
                                self.T("applications.delete.title"), warning=True):
            return
        self._flush_editors()
        self.details_tab.discard_pending()
        self.letter_tab.discard_pending()

        def done(result):
            self._undo = [entry for entry in self._undo if entry[0] != app.id]
            self.refresh(select_id=0)
            self.notify(self.T("applications.notice.deleted", files=result.files_removed))

        self._job(lambda: self.service.delete(app.id), done)

    # --- CV ----------------------------------------------------------------------------

    @guarded
    def replace_cv(self) -> None:
        app = self.current_application()
        if app is None:
            return
        if app.status != "draft" and app.cv_hash and not self.dialogs.ask(
                self.T("applications.cv.replace_confirm"), self.T("applications.cv.replace"), warning=True):
            return
        path = self.dialogs.open_file(self.T("applications.cv.choose"), self.T("applications.cv.filter"))
        if not path:
            return

        def done(updated):
            self.refresh(select_id=updated.id)
            self._reload_details(updated, skip=None)
            self._maybe_load_cv(updated)
            self.notify(self.T("applications.notice.cv_replaced"))

        self._job(lambda: self.service.set_cv(app.id, path), done)

    @guarded
    def open_cv_external(self) -> None:
        app = self.current_application()
        if app is None or not app.cv_hash:
            return
        folder = self.service.folder / "opened" / app.cv_hash[:12]

        def done(path):
            if path is None:
                self.notify(self.T("applications.pdf.missing"))
            else:
                self._open_path(str(path))

        self._job(lambda: self.service.materialize_cv(app, folder), done)

    # --- appointments ------------------------------------------------------------------

    @guarded
    def add_appointment(self) -> None:
        app = self.current_application()
        if app is None:
            return
        start = (datetime.now().astimezone() + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
        created = self.service.add_appointment(app.id, start=start)
        self.show_tab("appointments")
        self.appointments_tab.set_appointments(self.service.appointments_for(app.id), select_id=created.id)
        self.refresh(select_id=app.id)
        self._update_actions()

    def _on_appointment_edited(self, appointment_id: int, changes: dict) -> None:
        try:
            updated = self.service.update_appointment(appointment_id, **changes)
        except Exception as error:  # noqa: BLE001
            self.report_error(error)
            return
        if {"start", "kind"} & set(changes):
            self.appointments_tab.update_item(updated)
        self.refresh()

    @guarded
    def delete_appointment(self, appointment_id: int) -> None:
        if not self.dialogs.ask(self.T("applications.appointment.delete_confirm"),
                                self.T("applications.appointment.delete"), warning=True):
            return
        item = self.service.get_appointment(appointment_id)
        self.service.delete_appointment(appointment_id)
        if item is not None:
            self.appointments_tab.set_appointments(self.service.appointments_for(item.application_id))
            self.refresh(select_id=item.application_id)
        self._update_actions()

    @guarded
    def export_current_appointment_ics(self) -> None:
        ident = self.appointments_tab.current_id()
        if ident:
            self.export_appointment_ics(ident)

    @guarded
    def export_appointment_ics(self, appointment_id: int) -> None:
        item = self.service.get_appointment(appointment_id)
        if item is None:
            return
        path = self.dialogs.save_file(self.T("applications.appointment.ics_title"), "appointment.ics",
                                      self.T("applications.appointment.ics_filter"))
        if not path:
            return

        def work():
            text = self.service.appointment_ics(appointment_id)
            Path(path).write_text(text, encoding="utf-8", newline="")
            return path

        self._job(work, lambda p: self.notify(self.T("applications.notice.ics_saved", path=p)))

    # --- export and import -------------------------------------------------------------

    @guarded
    def export_selected(self) -> None:
        app = self.current_application()
        if app is None:
            return
        folder = self.dialogs.choose_folder(self.T("applications.export.choose_folder"))
        if not folder:
            return
        self._job(lambda: self.service.export_application(app.id, folder),
                  lambda made: self.notify(self.T("applications.notice.exported", path=str(made))))

    @guarded
    def export_csv(self) -> None:
        path = self.dialogs.save_file(self.T("applications.csv.export_title"), "applications.csv",
                                      self.T("applications.csv.filter"))
        if not path:
            return
        self._job(lambda: (self.service.export_csv(path), path)[1],
                  lambda p: self.notify(self.T("applications.notice.csv_saved", path=p)))

    @guarded
    def import_csv(self) -> None:
        path = self.dialogs.open_file(self.T("applications.csv.import_title"), self.T("applications.csv.filter"))
        if not path:
            return

        def done(report):
            self.refresh()
            self.notify(self.T("applications.notice.csv_imported", created=report.created,
                               skipped=report.skipped))

        self._job(lambda: self.service.import_csv(Path(path)), done)

    # --- context menu ------------------------------------------------------------------

    def _on_context_menu_requested(self, position: QPoint) -> None:
        menu = self.context_menu(self.table.indexAt(position))
        if menu is not None:
            menu.popup(self.table.viewport().mapToGlobal(position))

    def context_menu(self, index: QModelIndex | None = None) -> QMenu | None:
        """The right-click menu of a row (selects it first); None when there is no row."""
        if index is not None and index.isValid():
            self.select_application(int(index.sibling(index.row(), 0).data(ID_ROLE)))
        if not self.current_id():
            return None
        menu = QMenu(self)
        a = self.actions_by_name
        for name in ("open", "mark_sent"):
            menu.addAction(a[name])
        sub = menu.addMenu(self.T("applications.action.change_status"))
        for status in STATUSES:
            sub.addAction(a[f"status_{status}"])
        menu.addAction(a["add_appointment"])
        menu.addAction(a["open_company"])
        menu.addSeparator()
        menu.addAction(a["export"])
        menu.addSeparator()
        menu.addAction(a["delete"])
        return menu
