"""Previous searches: reopen without crawling, resume, rename / add a note, delete (one or
several, from the buttons, the right-click menu or the Delete key, always confirmed)."""

from __future__ import annotations

import time
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..i18n import t
from . import context_menu, table_filter, table_sort
from .common import format_number
from .menus import menu_action_of

NOTE_KIND = "companies"   # store.search_notes kind of the company searches

COLUMNS = ("date", "place", "radius", "status", "results")
RESUMABLE = ("cancelled", "running")
_NUMERIC = ("radius", "results")


def place_text(search: dict) -> str:
    """The place of a saved search in the format of every list ("Sogliano al Rubicone
    (FC)" for Nominatim's long name); the long name stays in the tooltip."""
    raw = search.get("place") or ""
    try:
        from ..geo.places import location_of_search

        found = location_of_search(search)
        if found.known and raw:
            return found.display()
    except Exception:  # noqa: BLE001 - the text as saved is still right
        pass
    return raw


class HistoryPanel(QWidget):
    def __init__(self, on_open: Callable[[int], None], on_resume: Callable[[int], None],
                 on_delete: Callable[[int], None], parent: QWidget | None = None):
        super().__init__(parent)
        self.searches: list[dict] = []
        self._counts: dict[int, int] = {}
        self._companies: dict[int, list[str]] = {}
        self._search_running = False
        self._refresh_command: Callable[[], None] | None = None
        self._archive = None
        self._notes: dict[int, dict] = {}
        self.note_dialog: SearchNoteDialog | None = None
        # set by the main window: deletes several searches after ONE confirmation
        self.on_delete_many: Callable[[list[int]], None] | None = None

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setObjectName("historyTable")
        table_filter.attach(self.table, kinds=table_filter.named_kinds(COLUMNS, date="date"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for i in range(len(COLUMNS)):
            self.table.setHorizontalHeaderItem(i, QTableWidgetItem())
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.cellDoubleClicked.connect(lambda *_: self._call(on_open))
        # the shared search box (place, CV, parameters and the companies found);
        # runner, embedder and store are set by the main window
        from .search_box import SearchBox

        self.search_box = SearchBox(self._search_documents, name="search of the previous searches")
        self.search_box.bind_table(self.table, self._search_key_of_row)

        self.open_button = QPushButton()
        self.resume_button = QPushButton()
        self.delete_button = QPushButton()
        self.rename_button = QPushButton()
        self.delete_selected_button = QPushButton()
        self.refresh_button = QPushButton()
        self.open_button.clicked.connect(lambda: self._call(on_open))
        self.resume_button.clicked.connect(lambda: self._call(on_resume))
        self.delete_button.clicked.connect(lambda: self._call(on_delete))
        self.rename_button.clicked.connect(lambda: self.open_note_dialog())
        self.delete_selected_button.clicked.connect(lambda: self.delete_selected())
        self.refresh_button.clicked.connect(self._refresh)
        self._on_delete = on_delete
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        for button in (self.open_button, self.resume_button, self.rename_button, self.delete_button,
                       self.delete_selected_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.refresh_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.search_box)
        from .geo_filter import GeoFilter
        from ..geo.places import location_of_search

        self.geo_filter = GeoFilter(
            self.table, lambda row: location_of_search(self._search_at(row)), self, name="historyGeoFilter")
        layout.addWidget(self.geo_filter)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        # right click on a row: the same actions as the buttons (shared with the Results menu)
        context_menu.install(self.table, self._menu_entries, delete_action=None)
        from PySide6.QtGui import QKeySequence, QShortcut

        self._delete_key = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.table)
        self._delete_key.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._delete_key.activated.connect(self.delete_pressed)
        self._update_buttons()

    def _menu_entries(self) -> list:
        def action(button):
            found = menu_action_of(button)
            if found is not None:
                found.setEnabled(button.isEnabled())
            return found

        return [action(self.open_button), action(self.resume_button), None, action(self.rename_button), None,
                action(self.delete_button), action(self.delete_selected_button)]

    def delete_pressed(self) -> None:
        """The Delete key: the selected searches (one confirmation)."""
        ids = self.selected_ids()
        if len(ids) > 1:
            self.delete_selected()
        elif ids and self.delete_button.isEnabled():
            self._on_delete(ids[0])

    def set_refresh(self, command: Callable[[], None]) -> None:
        self._refresh_command = command

    def _refresh(self) -> None:
        if self._refresh_command is not None:
            self._refresh_command()

    def load(self, archive) -> None:
        self._archive = archive
        try:
            self._notes = archive.search_notes(NOTE_KIND) if archive is not None else {}
        except Exception:  # noqa: BLE001 - an older store: no titles
            self._notes = {}
        self.searches = archive.list_searches() if archive is not None else []
        results = {r["id"]: archive.results_of(r["id"]) for r in self.searches}
        self._counts = {sid: len(rows) for sid, rows in results.items()}
        self._companies = {sid: [company.name for company, _e in rows][:300] for sid, rows in results.items()}
        self._redraw()
        self.search_box.rerun()

    def _search_documents(self) -> list:
        from ..scoring.semantic_index import search_document

        documents = []
        for s in self.searches:
            note = self._notes.get(s["id"], {})
            extra = " ".join(x for x in (note.get("title", ""), note.get("note", "")) if x)
            documents.append(search_document(s, [*self._companies.get(s["id"], ()), *([extra] if extra else [])]))
        return documents

    def _search_key_of_row(self, row: int) -> str | None:
        item = self.table.item(row, 0)
        return f"search:{item.data(Qt.ItemDataRole.UserRole)}" if item is not None else None

    def values(self, search_id: int) -> tuple[str, ...] | None:
        """Texts shown in the row of a search (used by the tests)."""
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) == search_id:
                return tuple(self.table.item(r, c).text() for c in range(len(COLUMNS)))
        return None

    def _redraw(self) -> None:
        chosen = self.selected_id()
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(len(self.searches))
            for r, search in enumerate(self.searches):
                date = time.strftime("%Y-%m-%d %H:%M", time.localtime(search["created"]))
                radius = (format_number(search["radius_km"], 1) + " " + t("unit.km")
                          if search["radius_km"] is not None else "")
                note = self._notes.get(search["id"], {})
                place = place_text(search)
                if note.get("title"):
                    place = f"{note['title']} ({place})" if place else note["title"]
                texts = (date, place, radius, t(f"search.status.{search['status']}"),
                         str(self._counts.get(search["id"], 0)))
                for c, text in enumerate(texts):
                    item = QTableWidgetItem(text)
                    if COLUMNS[c] in _NUMERIC:
                        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    if c == 1:
                        item.setToolTip("\n".join(x for x in (search["place"] or text, note.get("note", "")) if x))
                    item.setData(Qt.ItemDataRole.UserRole, search["id"])
                    self.table.setItem(r, c, item)
            table_sort.resort(self.table)   # the order chosen with a header click stays
            self.table.clearSelection()
            if chosen is not None:
                self._select_row_of(chosen)
        finally:
            self.table.blockSignals(False)
        self._update_buttons()

    def _select_row_of(self, search_id: int) -> bool:
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) == search_id:
                self.table.selectRow(r)
                return True
        return False

    def selected_id(self) -> int | None:
        """The search of the current row (the first selected one when several are)."""
        ids = self.selected_ids()
        if not ids:
            return None
        current = self.table.currentRow()
        item = self.table.item(current, 0) if current >= 0 else None
        if item is not None and item.data(Qt.ItemDataRole.UserRole) in ids:
            return item.data(Qt.ItemDataRole.UserRole)
        return ids[0]

    def selected_ids(self) -> list[int]:
        rows = sorted(index.row() for index in self.table.selectionModel().selectedRows())
        ids = []
        for row in rows:
            item = self.table.item(row, 0)
            if item is not None:
                ids.append(item.data(Qt.ItemDataRole.UserRole))
        return ids

    def delete_selected(self) -> None:
        ids = self.selected_ids()
        if ids and self.on_delete_many is not None and not self._search_running:
            self.on_delete_many(ids)

    # --- title and note

    def note_of(self, search_id: int) -> dict:
        return dict(self._notes.get(search_id, {"title": "", "note": ""}))

    def save_note(self, search_id: int, title: str, note: str) -> None:
        """Saved at once (every keystroke of the note window), shown in the list."""
        if self._archive is None:
            return
        try:
            self._archive.search_note_set(NOTE_KIND, search_id, title, note)
        except Exception:  # noqa: BLE001 - a busy store: the next keystroke saves it
            import logging

            logging.getLogger(__name__).warning("Search note not saved", exc_info=True)
            return
        if title.strip() or note.strip():
            self._notes[search_id] = {"title": title.strip(), "note": note.strip()}
        else:
            self._notes.pop(search_id, None)
        self._redraw()

    def open_note_dialog(self) -> "SearchNoteDialog | None":
        search_id = self.selected_id()
        if search_id is None:
            return None
        if self.note_dialog is not None:
            self.note_dialog.close()
        dialog = SearchNoteDialog(self, search_id, self.note_of(search_id),
                                  lambda title, note: self.save_note(search_id, title, note))
        dialog.finished.connect(lambda _r: setattr(self, "note_dialog", None))
        self.note_dialog = dialog
        dialog.show()
        import logging

        logging.getLogger(__name__).info("Previous search: rename / note window opened")
        return dialog

    def select(self, search_id: int) -> None:
        if self._select_row_of(search_id):
            self._update_buttons()

    def selected_search(self) -> dict | None:
        sid = self.selected_id()
        return next((r for r in self.searches if r["id"] == sid), None)

    def _call(self, function: Callable[[int], None]) -> None:
        sid = self.selected_id()
        if sid is not None:
            function(sid)

    def _update_buttons(self) -> None:
        search = self.selected_search()
        self.open_button.setEnabled(search is not None)
        self.resume_button.setEnabled(search is not None and search["status"] in RESUMABLE
                                      and not self._search_running)
        self.delete_button.setEnabled(search is not None and not self._search_running)
        self.rename_button.setEnabled(search is not None)
        self.delete_selected_button.setEnabled(len(self.selected_ids()) > 1 and not self._search_running)

    def set_search_running(self, running: bool) -> None:
        self._search_running = running
        self._update_buttons()

    def _search_at(self, row: int) -> dict | None:
        item = self.table.item(row, 0)
        search_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return next((s for s in self.searches if s["id"] == search_id), None)

    def retranslate(self) -> None:
        self.geo_filter.retranslate()
        self.search_box.retranslate()
        for i, column in enumerate(COLUMNS):
            self.table.horizontalHeaderItem(i).setText(t(f"history.{column}"))
        self.open_button.setText(t("history.open"))
        self.resume_button.setText(t("history.resume"))
        self.delete_button.setText(t("history.delete"))
        self.rename_button.setText(t("history.rename"))
        self.delete_selected_button.setText(t("history.delete_selected"))
        if self.note_dialog is not None:
            self.note_dialog.retranslate()
        self.refresh_button.setText(t("history.refresh"))
        self._redraw()


class SearchNoteDialog(QDialog):
    """Title and note of a previous search: saved while typing (no Save button)."""

    def __init__(self, parent: QWidget, search_id: int, current: dict, on_change: Callable[[str, str], None]):
        super().__init__(parent)
        self.search_id = search_id
        self._on_change = on_change
        self.title_field = QLineEdit(current.get("title", ""))
        self.note_field = QPlainTextEdit(current.get("note", ""))
        self.note_field.setMinimumHeight(self.fontMetrics().lineSpacing() * 5)
        self.close_button = QPushButton()
        self.close_button.clicked.connect(self.accept)
        self.title_label = QLabel()
        self.note_label = QLabel()
        self.form = QFormLayout()
        self.form.addRow(self.title_label, self.title_field)
        self.form.addRow(self.note_label, self.note_field)
        layout = QVBoxLayout(self)
        layout.addLayout(self.form)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.close_button)
        layout.addLayout(row)
        self.title_field.textChanged.connect(lambda _t: self._changed())
        self.note_field.textChanged.connect(self._changed)
        self.retranslate()

    def _changed(self) -> None:
        self._on_change(self.title_field.text(), self.note_field.toPlainText())

    def retranslate(self) -> None:
        self.setWindowTitle(t("history.note.title"))
        self.title_label.setText(t("history.note.name"))
        self.note_label.setText(t("history.note.note"))
        self.title_field.setPlaceholderText(t("history.note.name_placeholder"))
        self.close_button.setText(t("common.close"))
