"""The Register manager: the ONE window of the company register (Search menu, and the
"Register manager…" button of tab 2).

Design choice: one register, one window, one discovery checkbox
-----------------------------------------------------------------
Older versions had two registers: the "Local register" dialog over the lists the user
imported (kept in the Store, searched by the discovery source "imports") and, newer, the
regional register of free official lists (`reversehire.registers`, its own SQLite file).
The imported lists were MIGRATED into the regional register instead of showing two
databases side by side, because the register already does everything the old dialog did
and more (per-field provenance, locked hand edits, hide/undo, duplicates, CSV export) and
merges by tax code: a company of an imported list that an official source also lists is
one row, not two, and a search finds it once. Each imported list is one source of the
register (`registers/lists.py`, id "list:<id>"); the move from the Store runs once, is
idempotent and keeps the Store's copy as a backup (see `migrate_imported_lists`). The
search tab shows one "Regional register" checkbox; the old "imports" source name in the
settings is read as the register.

The window
----------
- Companies: every row of the register (search by name or tax code, sort by header,
  correct a cell in place: saved at once and LOCKED, shown with a padlock; "Unlock field"
  goes back to the sources' value), add, hide / show again, delete (asks first), undo,
  possible duplicates, export CSV. Right-click on a row shows the same actions.
- Official sources: state, licence, update rhythm, last refresh, companies, downloaded
  size; check for updates (size estimate), download / update with progress
  "done/estimated" (this window and the shared bottom bar), stop, disk usage, delete
  the downloaded files (the register stays).
- Lists: lists the user imported (CSV/XLSX) or bought (Registro Imprese "Elenco"),
  import, refresh from a newer file, delete.
Every button has an entry in the window's menu bar, sharing the button's QAction.
Everything that touches the database or the network runs on a worker (`runner`, the
main window's `_in_background` when there is one) with a deadline and a cancel event.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu,
    QMenuBar, QMessageBox, QProgressBar, QTableWidget, QTableWidgetItem, QTabWidget, QPushButton, QToolButton, QVBoxLayout,
    QWidget,
)

from ..i18n import t
from . import table_filter, table_sort
from .menus import bind_action
from .widgets import wrapped_label

from ..numbers import format_int, format_number  # noqa: E402

log = logging.getLogger(__name__)

OWNER = "registers"                 # owner name on the shared progress bar
ROW_LIMIT = 2000                    # rows shown at once (search to narrow)
JOB_TIMEOUT_S = 120                 # database work
NETWORK_TIMEOUT_S = 6 * 3600        # a full download of every source (cancel stops it at once)
CHECK_TIMEOUT_S = 600
ROW_COLUMNS = ("name", "cf", "vat", "municipality", "province", "ateco", "website", "legal_form", "employees_class",
               "status", "email", "phone", "sources", "signals")
SOURCE_COLUMNS = ("source", "state", "rows", "last", "size", "update", "licence")
LIST_COLUMNS = ("name", "source", "date", "rows")
KEY_ROLE = int(Qt.ItemDataRole.UserRole) + 1


def _rtext(key: str, **params) -> str:
    from ..registers.texts import text

    return text(key, **params)


def _editable() -> tuple[str, ...]:
    from ..registers.model import EDITABLE

    return tuple(c for c in ROW_COLUMNS if c in EDITABLE)


def lock_icon(colour: str = "#8a8f98", size: int = 14) -> QIcon:
    """A small padlock (drawn, so it needs no font glyph)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(colour))
    pen.setWidthF(max(1.2, size / 10))
    painter.setPen(pen)
    shackle = QPainterPath()
    shackle.moveTo(QPointF(size * 0.32, size * 0.5))
    shackle.lineTo(QPointF(size * 0.32, size * 0.32))
    shackle.arcTo(QRectF(size * 0.32, size * 0.1, size * 0.36, size * 0.42), 180, -180)
    shackle.lineTo(QPointF(size * 0.68, size * 0.5))
    painter.drawPath(shackle)
    painter.setBrush(QColor(colour))
    painter.drawRoundedRect(QRectF(size * 0.2, size * 0.48, size * 0.6, size * 0.44), 1.5, 1.5)
    painter.end()
    return QIcon(pixmap)


class _Bridge(QObject):
    done = Signal(object, object, object)   # callback, result, error


def _date(value) -> str:
    text = str(value or "")
    if isinstance(value, (int, float)) and value:
        import datetime as _dt

        return _dt.date.fromtimestamp(float(value)).isoformat()
    return text[:10]


class RegisterManagerWindow(QDialog):
    """See the module docstring. Callables injected (all optional) keep it testable:
    `runner(function, on_success, on_error, name=, timeout=, cancel=)`, `progress(owner,
    BarState or None)`, `notice(text)`, `ask(text) -> bool`, `ask_text(prompt) -> str`,
    `choose_file(title, filters) -> path`, `choose_save(title, default_name) -> path`,
    `import_dialog(preview) -> (mapping, kind, source, source_date) or None`,
    `on_changed()` (the register changed: e.g. the search tab's summary)."""

    def __init__(self, parent: QWidget | None = None, manager=None, *, store=None, runner: Callable | None = None,
                 progress: Callable | None = None, notice: Callable[[str], None] | None = None,
                 ask: Callable[[str], bool] | None = None, ask_text: Callable[[str], str | None] | None = None,
                 choose_file: Callable | None = None, choose_save: Callable | None = None,
                 import_dialog: Callable | None = None, on_changed: Callable[[], None] | None = None):
        super().__init__(parent)
        self.setObjectName("registerManager")
        if manager is None:
            from ..registers import shared_manager

            manager = shared_manager()
        self.manager = manager
        self.store = store
        self._runner = runner
        self._progress = progress
        self._notice = notice
        self._ask = ask
        self._ask_text = ask_text
        self._choose_file = choose_file
        self._choose_save = choose_save
        self._import_dialog = import_dialog
        self._on_changed = on_changed or (lambda: None)
        self._bridge = _Bridge()
        self._bridge.done.connect(self._deliver)
        self._threads: list[threading.Thread] = []
        self._pending = 0
        self._filling = False
        self._closed = False
        self.records: list = []                # RegisterCompany shown
        self.sources: list[dict] = []
        self.list_infos: list[dict] = []
        self.checks: dict[str, object] = {}    # source -> UpdateCheck
        self.estimates: dict[str, object] = {}
        self.stats: dict = {}
        self.disk: dict = {}
        self.can_undo = False
        self.last_report = None
        self._download_cancel: threading.Event | None = None
        self._last_event = None
        self._lock_icon = lock_icon()
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(250)
        self._progress_timer.timeout.connect(self._poll_progress)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(lambda: self.reload_rows())

        self._build_actions()
        self._build_body()
        self.retranslate()
        self.resize(1100, 680)
        self._update_actions()
        self.reload(migrate=True)

    # --- construction -------------------------------------------------------------------

    def _action(self, handler: Callable[[], object]) -> QAction:
        action = QAction(self)
        action.triggered.connect(lambda _checked=False: handler())
        return action

    def _build_actions(self) -> None:
        self.check_action = self._action(self.check_updates)
        self.download_action = self._action(self.download)
        self.cancel_action = self._action(self.cancel_download)
        self.purge_action = self._action(self.purge_downloads)
        self.import_list_action = self._action(self.import_list_file)
        self.import_bought_action = self._action(self.import_bought_list)
        self.refresh_list_action = self._action(self.refresh_list)
        self.delete_list_action = self._action(self.delete_list)
        self.add_action = self._action(self.add_company)
        self.unlock_action = self._action(self.unlock_field)
        self.hide_action = self._action(self.hide_selected)
        self.unhide_action = self._action(self.unhide_selected)
        self.delete_action = self._action(self.delete_selected)
        self.undo_action = self._action(self.undo)
        self.duplicates_action = self._action(self.show_duplicates)
        self.export_action = self._action(self.export_csv)
        self.close_action = self._action(self.close)
        self.row_actions = (self.add_action, self.unlock_action, self.hide_action, self.unhide_action,
                            self.delete_action, self.undo_action, self.duplicates_action, self.export_action)
        self.source_actions = (self.check_action, self.download_action, self.cancel_action, self.purge_action)
        self.list_actions = (self.import_list_action, self.import_bought_action, self.refresh_list_action,
                             self.delete_list_action)

    def _button(self, action: QAction) -> QPushButton:
        """A push button (styled like every other button of the app) that runs `action` and
        follows its text and enabled state."""
        button = QPushButton(action.text())
        button.clicked.connect(lambda _checked=False: action.trigger())

        def sync() -> None:
            try:
                button.setText(action.text())
                button.setEnabled(action.isEnabled())
                button.setToolTip(action.toolTip() if action.toolTip() != action.text() else "")
            except RuntimeError:  # the button was deleted
                pass

        action.changed.connect(sync)
        sync()
        bind_action(button, action)
        return button

    def _table(self, name: str, columns: tuple[str, ...]) -> QTableWidget:
        table = QTableWidget(0, len(columns))
        table.setObjectName(name)
        table_filter.attach(table)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.verticalHeader().setVisible(False)
        table.setWordWrap(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        return table

    def _build_body(self) -> None:
        self.menu_bar = QMenuBar(self)
        self.rows_menu = self.menu_bar.addMenu("")
        self.rows_menu.addActions(list(self.row_actions))
        self.rows_menu.addSeparator()
        self.rows_menu.addAction(self.close_action)
        self.sources_menu = self.menu_bar.addMenu("")
        self.sources_menu.addActions(list(self.source_actions))
        self.lists_menu = self.menu_bar.addMenu("")
        self.lists_menu.addActions(list(self.list_actions))

        self.help_label = wrapped_label(role="muted")
        self.tabs = QTabWidget()

        # companies
        rows_page = QWidget()
        self.search_field = QLineEdit()
        self.search_field.setClearButtonEnabled(True)
        self.search_field.textChanged.connect(lambda _t: self._search_timer.start())
        self.hidden_check = QCheckBox()
        self.hidden_check.toggled.connect(lambda _on: self.reload_rows())
        self.count_label = QLabel()
        self.count_label.setProperty("role", "muted")
        top = QHBoxLayout()
        top.addWidget(self.search_field, 2)
        top.addWidget(self.hidden_check)
        top.addWidget(self.count_label, 1)
        self.rows_table = self._table("registerRowsTable", ROW_COLUMNS)
        self.rows_table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                        | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.rows_table.itemChanged.connect(self._item_changed)
        self.rows_table.itemSelectionChanged.connect(self._update_actions)
        self.rows_table.currentCellChanged.connect(lambda *_a: self._update_actions())
        self.rows_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.rows_table.customContextMenuRequested.connect(self._row_menu)
        row_buttons = QHBoxLayout()
        for action in self.row_actions:
            row_buttons.addWidget(self._button(action))
        row_buttons.addStretch(1)
        rows_layout = QVBoxLayout(rows_page)
        rows_layout.addLayout(top)
        rows_layout.addWidget(self.rows_table, 1)
        self.coverage_label = wrapped_label(role="muted")   # how much the free registers cover
        self.coverage_label.setObjectName("registerCoverage")
        rows_layout.addWidget(self.coverage_label)
        rows_layout.addLayout(row_buttons)

        # official sources
        sources_page = QWidget()
        self.sources_help = wrapped_label(role="muted")
        self.sources_table = self._table("registerSourcesTable", SOURCE_COLUMNS)
        self.sources_table.itemSelectionChanged.connect(self._update_actions)
        self.disk_label = wrapped_label(role="muted")
        source_buttons = QHBoxLayout()
        for action in self.source_actions:
            source_buttons.addWidget(self._button(action))
        source_buttons.addStretch(1)
        sources_layout = QVBoxLayout(sources_page)
        sources_layout.addWidget(self.sources_help)
        sources_layout.addWidget(self.sources_table, 1)
        sources_layout.addWidget(self.disk_label)
        sources_layout.addLayout(source_buttons)

        # lists
        lists_page = QWidget()
        self.lists_help = wrapped_label(role="muted")
        self.lists_table = self._table("registerListsTable", LIST_COLUMNS)
        self.lists_table.itemSelectionChanged.connect(self._update_actions)
        list_buttons = QHBoxLayout()
        for action in self.list_actions:
            list_buttons.addWidget(self._button(action))
        list_buttons.addStretch(1)
        lists_layout = QVBoxLayout(lists_page)
        lists_layout.addWidget(self.lists_help)
        lists_layout.addWidget(self.lists_table, 1)
        lists_layout.addLayout(list_buttons)

        self.tabs.addTab(rows_page, "")
        self.tabs.addTab(sources_page, "")
        self.tabs.addTab(lists_page, "")

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.hide()
        self.status_label = wrapped_label(role="muted")
        self.close_button = self._button(self.close_action)
        bottom = QHBoxLayout()
        bottom.addWidget(self.status_label, 1)
        bottom.addWidget(self.progress_bar)
        bottom.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.setMenuBar(self.menu_bar)
        layout.addWidget(self.help_label)
        layout.addWidget(self.tabs, 1)
        layout.addLayout(bottom)

    # --- workers --------------------------------------------------------------------------

    def _run(self, function: Callable[[], object], on_success: Callable[[object], None], *, name: str,
             timeout: float = JOB_TIMEOUT_S, cancel: threading.Event | None = None,
             on_error: Callable[[BaseException], None] | None = None) -> None:
        self._pending += 1
        self._update_actions()

        def success(result) -> None:
            self._pending = max(0, self._pending - 1)
            if self._closed:
                return
            try:
                on_success(result)
            finally:
                self._update_actions()

        def failure(error) -> None:
            self._pending = max(0, self._pending - 1)
            if self._closed:
                return
            log.warning("Register manager: %s failed: %s", name, type(error).__name__)
            if on_error is not None:
                on_error(error)
            else:
                self._message(t("registers.manager.failed", detail=str(error) or type(error).__name__))
            self._update_actions()

        if self._runner is not None:
            self._runner(function, success, failure, name=name, timeout=timeout,
                         cancel=cancel or threading.Event())
            return
        bridge = self._bridge

        def job() -> None:
            try:
                result, error = function(), None
            except Exception as exc:  # noqa: BLE001 - shown in the window, logged
                log.warning("Register manager: %s failed", name, exc_info=True)
                result, error = None, exc
            try:
                bridge.done.emit((success, failure), result, error)
            except RuntimeError:   # the window was deleted meanwhile
                pass

        thread = threading.Thread(target=job, daemon=True, name=f"register: {name}")
        self._threads = [th for th in self._threads if th.is_alive()] + [thread]
        thread.start()

    def _deliver(self, callbacks, result, error) -> None:
        success, failure = callbacks
        try:
            if error is not None:
                failure(error)
            else:
                success(result)
        except RuntimeError:   # the window was deleted meanwhile
            pass

    def wait(self, timeout: float = 15.0) -> None:
        """Waits for the window's own workers and delivers their results (tests)."""
        from PySide6.QtWidgets import QApplication

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for thread in list(self._threads):
                thread.join(max(0.0, deadline - time.monotonic()))
            QApplication.processEvents()
            if self._runner is not None or (not any(th.is_alive() for th in self._threads) and self._pending == 0):
                break   # with the window's runner, the window's own wait delivers the results
        for _ in range(3):
            QApplication.processEvents()

    @property
    def busy(self) -> bool:
        return self._pending > 0

    @property
    def downloading(self) -> bool:
        return self._download_cancel is not None

    # --- messages -------------------------------------------------------------------------

    def _message(self, text: str, notice: bool = False) -> None:
        self.status_label.setText(text)
        if notice and self._notice is not None and text:
            self._notice(text)

    def _confirm(self, text: str) -> bool:
        if self._ask is not None:
            return bool(self._ask(text))
        answer = QMessageBox.question(self, t("registers.manager.title"), text)
        return answer == QMessageBox.StandardButton.Yes

    def _changed(self) -> None:
        try:
            self._on_changed()
        except Exception:  # noqa: BLE001
            log.debug("Register manager: change listener failed", exc_info=True)

    # --- loading --------------------------------------------------------------------------

    def reload(self, message: str = "", migrate: bool = False) -> None:
        """Everything again: rows, sources, lists (after moving old imported lists in)."""
        manager, store = self.manager, self.store

        def work():
            report = None
            if migrate and store is not None:
                from ..registers.lists import migrate_imported_lists
                report = migrate_imported_lists(store, manager.db)
            return report

        def done(report) -> None:
            text = message
            if report is not None and report.changed:
                text = t("registers.manager.migrated", lists=report.lists, rows=format_int(report.rows))
                self._changed()
            if not text and self.status_label.text() == t("registers.manager.loading"):
                self.status_label.setText("")   # the "Reading the register…" line is done
            self.reload_rows(text)
            self.reload_sources()

        if message or migrate:
            self._message(message or t("registers.manager.loading"))
        self._run(work, done, name="register open")

    def reload_rows(self, message: str = "") -> None:
        db = self.manager.db
        text = " ".join(self.search_field.text().split()) or None
        hidden = self.hidden_check.isChecked()

        province = self._chosen_province()

        def work():
            records = db.find(text=text, only_active=False, include_hidden=hidden, limit=ROW_LIMIT + 1)
            total = db.count(only_active=False, include_hidden=hidden)
            coverage = db.coverage_note(province, "") if total else None
            return records, total, db.can_undo(), coverage

        def done(result) -> None:
            records, total, can_undo, coverage = result
            self.can_undo = can_undo
            self._fill_rows(records[:ROW_LIMIT], total, len(records) > ROW_LIMIT)
            text = coverage.get("text", "") if isinstance(coverage, dict) else ""
            self.coverage_label.setText(text)
            self.coverage_label.setVisible(bool(text))
            if message:
                self._message(message)

        self._run(work, done, name="register rows")

    def _chosen_province(self) -> str:
        """The province chosen in tab 2 (for the coverage note), "" when none."""
        try:
            from ..discovery import prefs

            value = prefs.read(self.store) if self.store is not None else None
            return (value.province or "") if value is not None and value.area == "province" else ""
        except Exception:  # noqa: BLE001
            return ""

    def reload_sources(self) -> None:
        manager = self.manager

        def work():
            from ..registers import lists

            return manager.sources_info(), manager.stats(), manager.disk_usage(), lists.lists(manager.db)

        def done(result) -> None:
            self.sources, self.stats, self.disk, self.list_infos = result
            self._fill_sources()
            self._fill_lists()

        self._run(work, done, name="register sources")

    # --- filling --------------------------------------------------------------------------

    def _list_titles(self) -> dict[str, str]:
        return {info["id"]: info.get("source") or info.get("name") or info["id"] for info in self.list_infos}

    def _source_names(self, record) -> str:
        from ..registers.lists import is_list_source
        from ..registers.texts import source_title

        titles = self._list_titles()
        names = []
        for source in record.sources:
            if is_list_source(source):
                names.append(titles.get(source) or t("registers.manager.imported_list"))
            else:
                names.append(source_title(source) if source != "derived" else "")
        text = ", ".join(n for n in names if n)
        if record.hidden:
            text = f"{t('registers.manager.hidden_mark')} · {text}" if text else t("registers.manager.hidden_mark")
        return text

    def _fill_rows(self, records: list, total: int, cut: bool) -> None:
        editable = _editable()
        self.records = list(records)
        self._filling = True
        try:
            self.rows_table.setRowCount(len(records))
            for r, record in enumerate(records):
                values = {c: "" if record.values.get(c) in (None, "") else str(record.values.get(c))
                          for c in ROW_COLUMNS}
                values["cf"] = record.cf
                values["sources"] = self._source_names(record)
                values["signals"] = "; ".join(b["text"] for b in record.badges())
                if record.province_approx and not values["province"]:
                    values["province"] = ", ".join(record.province_guess)
                for c, column in enumerate(ROW_COLUMNS):
                    item = QTableWidgetItem(values[column])
                    item.setData(KEY_ROLE, record.key)
                    if column not in editable:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    if column in record.locked:
                        item.setIcon(self._lock_icon)
                        item.setToolTip(_rtext("registers.edit.locked"))
                        font = item.font()
                        font.setItalic(True)
                        item.setFont(font)
                    elif column == "province" and record.province_approx and values["province"]:
                        item.setToolTip(_rtext("registers.field.province_approx", province=values["province"]))
                    if record.hidden:
                        item.setForeground(QColor("#8a8f98"))
                    self.rows_table.setItem(r, c, item)
            table_sort.resort(self.rows_table)
            self._fit_columns(self.rows_table, ROW_COLUMNS)
        finally:
            self._filling = False
        self._total, self._cut = total, cut
        key = "registers.manager.count_cut" if cut else "registers.manager.count"
        self.count_label.setText(t(key, shown=format_int(len(records)), total=format_int(total)))
        self._update_actions()

    def _fit_columns(self, table: QTableWidget, columns) -> None:
        table.resizeColumnsToContents()
        char = max(1, self.fontMetrics().averageCharWidth())
        for c in range(len(columns)):
            if table.columnWidth(c) > char * 34:
                table.setColumnWidth(c, char * 34)

    def _fill_sources(self) -> None:
        from ..registers.model import DOWNLOADABLE

        per_source = (self.stats or {}).get("per_source", {})
        table = self.sources_table
        table.setRowCount(len(self.sources))
        for r, info in enumerate(self.sources):
            source = info.get("id", "")
            check = self.checks.get(source)
            if not info.get("enabled", True):
                state = t("registers.manager.state.off")
            elif check is not None:
                state = t(f"registers.manager.state.{check.state}")
            elif source in DOWNLOADABLE and not info.get("downloaded"):
                state = t("registers.manager.state.not_downloaded")
            else:
                state = ""
            estimate = self.estimates.get(source)
            if estimate is not None and getattr(estimate, "bytes", None):
                state = f"{state} · {t('registers.manager.estimate', mb=format_number(estimate.bytes / 1e6))}".strip(" ·")
            last = (info.get("last_refresh") or {}).get("finished", "") if isinstance(info.get("last_refresh"),
                                                                                       dict) else ""
            raw = info.get("raw_bytes") or 0
            values = {"source": info.get("title", source), "state": state, "rows": format_int(per_source.get(source, 0)),
                      "last": _date(last) or t("registers.manager.never"),
                      "size": format_number(raw / 1e6, 1) + " MB" if raw else "",
                      "update": info.get("update_text", ""), "licence": info.get("licence", "")}
            for c, column in enumerate(SOURCE_COLUMNS):
                item = QTableWidgetItem(values[column])
                item.setData(KEY_ROLE, source)
                tip = info.get("note") or ""
                if info.get("attribution"):
                    tip = f"{tip}\n{info['attribution']}" if tip else info["attribution"]
                item.setToolTip(tip)
                table.setItem(r, c, item)
        table_sort.resort(table)
        self._fit_columns(table, SOURCE_COLUMNS)
        if self.disk:
            self.disk_label.setText(_rtext("registers.disk.usage", raw=format_number(self.disk.get('raw_total', 0) / 1e6),
                                           db=format_number(self.disk.get('db', 0) / 1e6, 1)))
        self._update_actions()

    def _fill_lists(self) -> None:
        table = self.lists_table
        table.setRowCount(len(self.list_infos))
        for r, info in enumerate(self.list_infos):
            values = {"name": info.get("name", ""), "source": info.get("source", ""),
                      "date": _date(info.get("source_date")) or _date(info.get("refreshed") or info.get("created")),
                      "rows": format_int(info.get('count', 0))}
            for c, column in enumerate(LIST_COLUMNS):
                item = QTableWidgetItem(values[column])
                item.setData(KEY_ROLE, info["id"])
                item.setToolTip(info.get("path", ""))
                table.setItem(r, c, item)
        table_sort.resort(table)
        self._fit_columns(table, LIST_COLUMNS)
        self._update_actions()

    # --- selection ------------------------------------------------------------------------

    def _selected(self, table: QTableWidget):
        rows = table.selectionModel().selectedRows() if table.selectionModel() else []
        if not rows:
            return None
        item = table.item(rows[0].row(), 0)
        return item.data(KEY_ROLE) if item is not None else None

    def selected_key(self) -> str | None:
        return self._selected(self.rows_table)

    def selected_record(self):
        key = self.selected_key()
        return next((r for r in self.records if r.key == key), None) if key else None

    def selected_field(self) -> str | None:
        column = self.rows_table.currentColumn()
        return ROW_COLUMNS[column] if 0 <= column < len(ROW_COLUMNS) else None

    def selected_list(self) -> str | None:
        return self._selected(self.lists_table)

    def _update_actions(self) -> None:
        record = self.selected_record()
        field = self.selected_field()
        idle = not self.downloading
        self.unlock_action.setEnabled(record is not None and field in record.locked)
        self.hide_action.setEnabled(record is not None and not record.hidden)
        self.unhide_action.setEnabled(record is not None and record.hidden)
        self.delete_action.setEnabled(record is not None)
        self.duplicates_action.setEnabled(record is not None)
        self.undo_action.setEnabled(self.can_undo)
        self.export_action.setEnabled(bool(self.records))
        self.check_action.setEnabled(idle)
        self.download_action.setEnabled(idle)
        self.cancel_action.setEnabled(not idle)
        self.purge_action.setEnabled(idle and bool((self.disk or {}).get("raw_total")))
        self.refresh_list_action.setEnabled(self.selected_list() is not None)
        self.delete_list_action.setEnabled(self.selected_list() is not None)
        self.import_bought_action.setEnabled(idle)

    def _row_menu(self, position) -> QMenu:
        menu = QMenu(self)
        menu.addActions(list(self.row_actions))
        self._update_actions()
        if not self.isVisible():   # tests: built, not shown
            return menu
        menu.popup(self.rows_table.viewport().mapToGlobal(position))
        return menu

    # --- companies ------------------------------------------------------------------------

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if self._filling:
            return
        key = item.data(KEY_ROLE)
        column = item.column()
        if not key or not (0 <= column < len(ROW_COLUMNS)):
            return
        field = ROW_COLUMNS[column]
        if field not in _editable():
            return
        value = item.text()
        db = self.manager.db
        log.info("Register: a %s corrected by hand", field)
        self._run(lambda: db.edit(key, {field: value}), lambda _ok: self._saved(), name="register edit")

    def _saved(self, message: str = "") -> None:
        self._changed()
        self.reload_rows(message or t("registers.manager.saved"))

    def edit_cell(self, key: str, field: str, value: str) -> bool:
        """Correct one field of one company (the same as typing in the cell)."""
        for r in range(self.rows_table.rowCount()):
            item = self.rows_table.item(r, ROW_COLUMNS.index(field))
            if item is not None and item.data(KEY_ROLE) == key:
                item.setText(value)
                return True
        return False

    def select_key(self, key: str, field: str | None = None) -> bool:
        for r in range(self.rows_table.rowCount()):
            item = self.rows_table.item(r, 0)
            if item is not None and item.data(KEY_ROLE) == key:
                column = ROW_COLUMNS.index(field) if field in ROW_COLUMNS else 0
                self.rows_table.setCurrentCell(r, column)
                self.rows_table.selectRow(r)
                self.rows_table.setCurrentCell(r, column)
                self._update_actions()
                return True
        return False

    def _ask_name(self) -> str:
        prompt = t("registers.manager.add_prompt")
        if self._ask_text is not None:
            return (self._ask_text(prompt) or "").strip()
        name, ok = QInputDialog.getText(self, t("registers.manager.title"), prompt)
        return name.strip() if ok else ""

    def add_company(self, name: str | None = None) -> bool:
        name = (name if name is not None else self._ask_name()).strip()
        if not name:
            return False
        db = self.manager.db
        log.info("Register: company added by hand")
        self._run(lambda: db.add_company({"name": name}), lambda _key: self._saved(), name="register add")
        return True

    def unlock_field(self) -> bool:
        record, field = self.selected_record(), self.selected_field()
        if record is None or field not in record.locked:
            return False
        db, key = self.manager.db, record.key
        log.info("Register: a %s unlocked", field)
        self._run(lambda: db.unlock(key, field), lambda _ok: self._saved(), name="register unlock")
        return True

    def hide_selected(self) -> bool:
        record = self.selected_record()
        if record is None or record.hidden:
            return False
        db, key = self.manager.db, record.key
        log.info("Register: company hidden")
        self._run(lambda: db.hide(key), lambda _ok: self._saved(_rtext("registers.edit.hidden")), name="register hide")
        return True

    def unhide_selected(self) -> bool:
        record = self.selected_record()
        if record is None or not record.hidden:
            return False
        db, key = self.manager.db, record.key
        log.info("Register: company shown again")
        self._run(lambda: db.unhide(key), lambda _ok: self._saved(), name="register unhide")
        return True

    def delete_selected(self) -> bool:
        record = self.selected_record()
        if record is None or not self._confirm(t("registers.manager.confirm_delete", name=record.name or record.key)):
            return False
        db, key = self.manager.db, record.key
        log.info("Register: company deleted")
        self._run(lambda: db.delete(key), lambda _ok: self._saved(), name="register delete")
        return True

    def undo(self) -> bool:
        db = self.manager.db

        def done(action) -> None:
            if action:
                self._saved(_rtext("registers.edit.undone", action=action.split(" ")[0]))
            else:
                self._message(_rtext("registers.edit.nothing_to_undo"))

        log.info("Register: undo")
        self._run(db.undo, done, name="register undo")
        return True

    def show_duplicates(self) -> bool:
        record = self.selected_record()
        if record is None:
            return False
        db, key = self.manager.db, record.key

        def done(found) -> None:
            if not found:
                self._message(t("registers.manager.no_duplicates"))
                return
            names = "; ".join(f"{r.name} ({r.cf or '-'}, {r.get('municipality') or '?'})" for r in found)
            self._message(t("registers.manager.duplicates", name=record.name, list=names))

        self._run(lambda: db.possible_duplicates(key), done, name="register duplicates")
        return True

    def export_csv(self, path: str | None = None) -> bool:
        if path is None:
            if self._choose_save is not None:
                path = self._choose_save(t("registers.manager.export"), "register.csv")
            else:
                from PySide6.QtWidgets import QFileDialog

                path, _ = QFileDialog.getSaveFileName(self, t("registers.manager.export"), "register.csv",
                                                      "CSV (*.csv)")
        if not path:
            return False
        db = self.manager.db
        log.info("Register: exported to CSV")
        self._run(lambda: db.export_csv(path),
                  lambda n: self._message(t("registers.manager.exported", n=format_int(n), file=Path(path).name)),
                  name="register export")
        return True

    # --- official sources -----------------------------------------------------------------

    def check_updates(self) -> bool:
        if self.downloading:
            return False
        manager = self.manager
        cancel = threading.Event()

        def work():
            return manager.check_updates(cancel=cancel), manager.estimate(cancel=cancel)

        def done(result) -> None:
            checks, estimates = result
            self.checks = {c.source: c for c in checks}
            self.estimates = dict(estimates)
            changed = [c.source for c in checks if c.state in ("new", "changed")]
            from ..registers.texts import source_title

            if changed:
                self._message(t("registers.manager.updates", sources=", ".join(source_title(s) for s in changed)))
            else:
                self._message(t("registers.manager.no_updates"))
            self._fill_sources()

        log.info("Register: checking the official sources for updates")
        self._message(t("registers.manager.checking"))
        self._run(work, done, name="register check", timeout=CHECK_TIMEOUT_S, cancel=cancel)
        return True

    def _estimated_mb(self) -> float | None:
        sizes = [getattr(e, "bytes", None) for e in self.estimates.values()]
        if not sizes or any(s is None for s in sizes):
            return None
        return sum(sizes) / 1e6

    def download(self, confirmed: bool = False) -> bool:
        """Download what changed and merge it (enabled sources, chosen regions)."""
        if self.downloading:
            return False
        manager = self.manager
        mb = self._estimated_mb()
        sources = manager.enabled_sources()
        text = (t("registers.manager.confirm_download", n=len(sources), mb=format_number(mb)) if mb is not None
                else t("registers.manager.confirm_download_unknown", n=len(sources)))
        if not confirmed and not self._confirm(text):
            return False
        cancel = threading.Event()
        self._download_cancel = cancel
        self._last_event = None

        def on_progress(event) -> None:
            self._last_event = event      # read by the GUI timer (no Qt call from the worker)

        def done(report) -> None:
            self._download_finished()
            self.last_report = report
            messages = report.messages() if report is not None else []
            text = " ".join(messages) or t(f"registers.manager.refresh.{getattr(report, 'state', 'ok')}")
            self._message(text, notice=True)
            log.info("Register: refresh finished (%s, %.0f s)", getattr(report, "state", "?"),
                     getattr(report, "seconds", 0) or 0)
            self.checks = {}
            self._changed()
            self.reload_rows()
            self.reload_sources()

        def failed(error) -> None:
            self._download_finished()
            self._message(t("registers.manager.failed", detail=str(error) or type(error).__name__), notice=True)

        log.info("Register: download of %d official sources started", len(sources))
        self._message(t("registers.manager.downloading"))
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()
        self._progress_timer.start()
        self._update_actions()
        self._run(lambda: manager.refresh(progress=on_progress, cancel=cancel), done, name="register download",
                  timeout=NETWORK_TIMEOUT_S, cancel=cancel, on_error=failed)
        return True

    def cancel_download(self) -> bool:
        if self._download_cancel is None:
            return False
        log.info("Register: download stopped by the user")
        self._download_cancel.set()
        self._message(t("registers.manager.stopping"))
        return True

    def _download_finished(self) -> None:
        self._download_cancel = None
        self._progress_timer.stop()
        self.progress_bar.hide()
        if self._progress is not None:
            try:
                self._progress(OWNER, None)
            except Exception:  # noqa: BLE001
                log.debug("Register: progress bar not released", exc_info=True)
        self._update_actions()

    def progress_state(self, event):
        """(fraction or None, "done/estimated" counts, line) of a progress event."""
        from ..registers.texts import progress_text

        try:
            from .main_window import counts_text
        except Exception:  # noqa: BLE001
            def counts_text(done, total, estimated):
                return f"{done}/~{total}" if estimated else f"{done}/{total}"
        mb = 1_000_000
        if event.total_bytes:
            counts = counts_text(int(event.done_bytes / mb), max(1, int(event.total_bytes / mb)), False) + " MB"
        elif event.rows_estimated:
            counts = counts_text(int(event.rows), int(event.rows_estimated), True)
        elif event.rows:
            counts = format_int(event.rows)
        else:
            counts = ""
        return event.fraction, counts, progress_text(event)

    def _poll_progress(self) -> None:
        event = self._last_event
        if event is None or self._closed:
            return
        fraction, counts, line = self.progress_state(event)
        if fraction is None:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(round(fraction * 1000))
        self.progress_bar.setFormat(counts)
        self.progress_bar.setTextVisible(bool(counts) and fraction is not None)
        self.status_label.setText(line)
        if self._progress is not None:
            from .progress_bar import BarState

            try:
                self._progress(OWNER, BarState(fraction, counts, line))
            except Exception:  # noqa: BLE001
                log.debug("Register: shared progress bar not updated", exc_info=True)

    def purge_downloads(self) -> bool:
        total = (self.disk or {}).get("raw_total", 0)
        if self.downloading or not self._confirm(t("registers.manager.confirm_purge", mb=format_number(total / 1e6))):
            return False
        manager = self.manager
        log.info("Register: downloaded files deleted")
        self._run(manager.purge_raw,
                  lambda freed: (self._message(_rtext("registers.disk.purged", mb=format_number(freed / 1e6))),
                                 self.reload_sources()),
                  name="register purge")
        return True

    # --- lists ----------------------------------------------------------------------------

    def _pick_file(self, title: str, filters: str = "CSV / Excel (*.csv *.txt *.xlsx);;*.*") -> str | None:
        if self._choose_file is not None:
            return self._choose_file(title, filters)
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(self, title, "", filters)
        return path or None

    def _default_import_dialog(self, preview):
        from .search_panel import ImportDialog

        dialog = ImportDialog(self, preview)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.mapping(), dialog.kind(), dialog.source(), dialog.source_date()

    def import_list_file(self, path: str | None = None, *, interactive: bool = True, source: str = "",
                         source_date: str = "") -> bool:
        """Import a CSV/XLSX company list (column mapping shown first when interactive)."""
        path = path or self._pick_file(t("registers.manager.import_list"))
        if not path:
            return False
        db = self.manager.db

        def previewed(preview) -> None:
            from ..discovery import imports
            from ..registers import lists
            from .search_panel import file_date

            mapping, kind, chosen_source, chosen_date = preview.mapping, preview.kind, source, source_date
            if interactive:
                answer = (self._import_dialog or self._default_import_dialog)(preview)
                if not answer:
                    return
                mapping, kind, chosen_source, chosen_date = answer
            rows = imports.records(preview, mapping)
            if not rows:
                self._message(_text_discovery("discovery.import_failed", detail="no company with a name"))
                return
            name = Path(preview.path).name
            log.info("Register: list imported (%d rows)", len(rows))
            self._run(lambda: lists.import_list(db, name, kind, rows, source=chosen_source,
                                                source_date=chosen_date or file_date(preview.path),
                                                path=str(preview.path)),
                      lambda report: self._list_done(report, t("registers.manager.list_imported",
                                                                n=format_int(report.rows), name=name)),
                      name="register import list")

        self._read_preview(path, previewed)
        return True

    def _read_preview(self, path: str, then: Callable) -> None:
        from ..discovery import imports

        def failed(error) -> None:
            self._message(_text_discovery("discovery.import_failed", detail=str(error) or type(error).__name__))

        self._message(t("registers.manager.loading"))
        self._run(lambda: imports.preview(path), then, name="register read list", on_error=failed)

    def _list_done(self, report, message: str) -> None:
        self.last_report = report
        if report.state != "ok":
            self._message(t("registers.manager.failed", detail=report.detail or report.state))
            return
        self._changed()
        self.reload_rows(message)
        self.reload_sources()

    def import_bought_list(self, path: str | None = None) -> bool:
        """Import a Registro Imprese "Elenco" the user bought (CSV/XLSX)."""
        path = path or self._pick_file(t("registers.manager.import_bought"))
        if not path:
            return False
        manager = self.manager

        def done(result) -> None:
            self.last_report = result
            if result.get("state") != "ok":
                self._message(result.get("message") or t("registers.manager.failed", detail=result.get("error")))
                return
            self._changed()
            self.reload_rows(t("registers.manager.bought_imported", rows=format_int(result.get('rows', 0)),
                               new=format_int(result.get('new', 0))))
            self.reload_sources()

        log.info("Register: bought Registro Imprese list imported")
        self._run(lambda: manager.import_bought_list(path), done, name="register import bought")
        return True

    def refresh_list(self, path: str | None = None) -> bool:
        """Replace the chosen list's rows with a newer file (automatic column mapping)."""
        source_id = self.selected_list()
        if source_id is None:
            self._message(t("registers.manager.choose_list"))
            return False
        path = path or self._pick_file(t("registers.manager.refresh_list"))
        if not path:
            return False
        db = self.manager.db
        info = next((i for i in self.list_infos if i["id"] == source_id), {})

        def previewed(preview) -> None:
            from ..discovery import imports
            from ..registers import lists
            from .search_panel import file_date

            rows = imports.records(preview)
            log.info("Register: list refreshed from a newer file (%d rows)", len(rows))
            self._run(lambda: lists.refresh_list(db, source_id, rows, path=str(path),
                                                 source_date=file_date(path)),
                      lambda report: self._list_done(report, t(
                          "registers.manager.list_refreshed", name=info.get("name", ""),
                          added=format_int(report.added), removed=format_int(report.removed))),
                      name="register refresh list")

        self._read_preview(path, previewed)
        return True

    def delete_list(self) -> bool:
        source_id = self.selected_list()
        info = next((i for i in self.list_infos if i["id"] == source_id), None)
        if info is None or not self._confirm(t("registers.manager.confirm_delete_list", name=info["name"],
                                               n=format_int(info.get('count', 0)))):
            return False
        db = self.manager.db
        from ..registers import lists

        log.info("Register: imported list deleted")
        self._run(lambda: lists.delete_list(db, source_id),
                  lambda _n: (self._changed(), self.reload_rows(t("registers.manager.list_deleted")),
                              self.reload_sources()),
                  name="register delete list")
        return True

    # --- texts ----------------------------------------------------------------------------

    def retranslate(self) -> None:
        from ..registers.texts import field_label

        self.setWindowTitle(t("registers.manager.title"))
        self.help_label.setText(t("registers.manager.help"))
        self.rows_menu.setTitle(t("registers.manager.menu_rows"))
        self.sources_menu.setTitle(t("registers.manager.menu_sources"))
        self.lists_menu.setTitle(t("registers.manager.menu_lists"))
        self.tabs.setTabText(0, t("registers.manager.tab_rows"))
        self.tabs.setTabText(1, t("registers.manager.tab_sources"))
        self.tabs.setTabText(2, t("registers.manager.tab_lists"))
        self.search_field.setPlaceholderText(t("registers.manager.search"))
        self.hidden_check.setText(t("registers.manager.show_hidden"))
        self.sources_help.setText(t("registers.manager.sources_help"))
        self.lists_help.setText(t("registers.manager.lists_help"))
        labels = {"sources": t("registers.manager.col.sources"), "signals": t("registers.manager.col.signals"),
                  "cf": field_label("cf")}
        self.rows_table.setHorizontalHeaderLabels([labels.get(c) or field_label(c) for c in ROW_COLUMNS])
        self.sources_table.setHorizontalHeaderLabels([t(f"registers.manager.col.{c}") for c in SOURCE_COLUMNS])
        self.lists_table.setHorizontalHeaderLabels([t(f"registers.manager.list_col.{c}") for c in LIST_COLUMNS])
        for action, key in ((self.check_action, "check"), (self.download_action, "download"),
                            (self.cancel_action, "cancel"), (self.purge_action, "purge"),
                            (self.import_list_action, "import_list"), (self.import_bought_action, "import_bought"),
                            (self.refresh_list_action, "refresh_list"), (self.delete_list_action, "delete_list"),
                            (self.add_action, "add"), (self.unlock_action, "unlock"), (self.hide_action, "hide"),
                            (self.unhide_action, "unhide"), (self.delete_action, "delete"),
                            (self.undo_action, "undo"), (self.duplicates_action, "duplicates"),
                            (self.export_action, "export")):
            action.setText(t(f"registers.manager.{key}"))
            action.setToolTip(t(f"registers.manager.{key}_tip"))
        self.close_action.setText(t("common.close"))
        if self.records:
            self._fill_rows(self.records, getattr(self, "_total", len(self.records)), getattr(self, "_cut", False))
        if self.sources:
            self._fill_sources()

    # --- closing --------------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt name)
        if self._download_cancel is not None:
            self._download_cancel.set()     # a download never outlives its window silently
            self._download_finished()
        self._search_timer.stop()
        self._closed = True
        super().closeEvent(event)


def _text_discovery(key: str, **params) -> str:
    from ..discovery.texts import text

    return text(key, **params)


__all__ = ["OWNER", "ROW_COLUMNS", "RegisterManagerWindow", "lock_icon"]
