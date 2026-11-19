"""The search box shared by every list of companies or postings (My companies,
Favourites, Results, Job search results, Previous searches): one field, one status
line, the meaning-based engine of scoring/semantic_index.py behind it.

Typing runs at once an exact-word pass on the GUI thread (cheap: no model), so the
list reacts immediately; a moment later (DEBOUNCE_MS) the meaning-based pass runs on
the window's worker (`runner`, the window's `_in_background`), building the index
incrementally (vectors cached in the Store, progress in the status line). The hits
(`hit(key)`, relevance 0..100 %, the sentence that matched and its source) are used by
the panels: `bind_table(view, key_of_row)` hides the rows that do not match through
the table's filter controller (gui/table_filter.py, predicate "search"), combined with
the per-column filters.

Without the language model the status line says that the search runs in keyword mode.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QVBoxLayout, QWidget

from ..i18n import t
from ..scoring import semantic_index as si
from .common import run_job
from .widgets import ElidedLabel

log = logging.getLogger(__name__)

DEBOUNCE_MS = 400
SEARCH_TIMEOUT_S = 600.0
PREDICATE = "search"


class _Bridge(QObject):
    progress = Signal(int, int)


def source_label(passage: si.Passage | None) -> str:
    """"Presentation", "Posting: Analyst"… in the interface language."""
    if passage is None:
        return ""
    key = f"semantic.source.{passage.source}"
    name = t(key)
    if name == key:
        name = passage.source
    return f"{name}: {passage.label}" if passage.label else name


def hit_text(hit: si.Hit | None) -> str:
    """"87 % · Presentation: «… the sentence …»" (tooltips and snippet columns)."""
    if hit is None or hit.passage is None:
        return ""
    return t("semantic.hit", relevance=f"{hit.relevance:.0f}", source=source_label(hit.passage),
             snippet=hit.snippet())


def hit_html(hit: si.Hit | None) -> str:
    if hit is None or hit.passage is None:
        return ""
    import html

    return (f"<b>{hit.relevance:.0f} %</b> · <i>{html.escape(source_label(hit.passage))}</i>: "
            f"«{hit.snippet_html()}»")


class SearchBox(QWidget):
    """Field + status line; `hits` by document key after each search."""

    resultsChanged = Signal()
    started = Signal()           # a worker search began
    finished = Signal()          # a worker search ended (results or failure)

    def __init__(self, documents: Callable[[], list], *, runner: Callable | None = None,
                 embedder_provider: Callable[[], object] | None = None, store=None,
                 name: str = "list search", parent: QWidget | None = None):
        super().__init__(parent)
        self.documents_provider = documents
        self.runner = runner
        self.embedder_provider = embedder_provider
        self.store = store
        self.name = name
        self.index: si.SemanticIndex | None = None
        self.hits: dict[str, si.Hit] = {}
        self.mode = si.MODE_KEYWORD
        self.query = ""
        self._busy = False
        self._pending = False
        self._generation = 0
        self._total = 0
        self._bridge = _Bridge()
        self._bridge.progress.connect(self._progress)
        self._cancel: threading.Event | None = None

        self.field = QLineEdit(self)
        self.field.setObjectName("semanticSearchField")
        self.field.setClearButtonEnabled(True)
        self.field.textChanged.connect(self._text_changed)
        self.field.returnPressed.connect(lambda: self.search())
        self.status = ElidedLabel("", self)
        self.status.setProperty("role", "muted")
        self.status.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(lambda: self.search())
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.field, 1)
        self.row = row
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(row)
        layout.addWidget(self.status)
        self._tables: list[tuple[object, Callable[[int], str | None]]] = []
        self.retranslate()

    # --- state
    @property
    def active(self) -> bool:
        return bool(self.query)

    @property
    def busy(self) -> bool:
        return self._busy

    def hit(self, key: str | None) -> si.Hit | None:
        return self.hits.get(key) if key is not None else None

    def matches(self, key: str | None) -> bool:
        if not self.active:
            return True
        found = self.hit(key)
        return found is not None and found.matches

    def _embedder(self):
        if self.embedder_provider is None:
            return None
        try:
            return self.embedder_provider()
        except Exception:  # noqa: BLE001 - no model: keyword mode
            log.warning("Search without the language model", exc_info=True)
            return None

    def _documents(self) -> list[si.Document]:
        try:
            return list(self.documents_provider() or [])
        except Exception:  # noqa: BLE001
            log.warning("Search: the list could not be read", exc_info=True)
            return []

    # --- searching
    def _text_changed(self, _text: str) -> None:
        query = " ".join(self.field.text().split())
        if not query:
            self.clear_results()
            return
        self._keyword_pass(query)
        if self.embedder_provider is not None and self.runner is not None:
            self._timer.start()

    def _keyword_pass(self, query: str) -> None:
        """Exact words at once, on the GUI thread (no model)."""
        index = si.SemanticIndex(None)
        documents = self._documents()
        index.set_documents(documents)
        self.query = query
        self.mode = si.MODE_KEYWORD
        self._total = len(documents)
        self.hits = index.hits_by_key(query)
        self._publish(final=False)

    def rerun(self) -> None:
        """The list changed (rows added during a search, a reload): search it again."""
        if not self.active:
            return
        self._keyword_pass(self.query)
        if self.embedder_provider is not None and self.runner is not None:
            self._timer.start()

    def clear_results(self) -> None:
        self._timer.stop()
        self._generation += 1
        self.query = ""
        self.hits = {}
        self.status.hide()
        self.status.setText("")
        self._apply_tables()
        self.resultsChanged.emit()

    def search(self, query: str | None = None) -> None:
        """Full search (meaning + exact words) on the worker; empty query = no filter."""
        if query is not None and query != self.field.text():
            self.field.blockSignals(True)
            self.field.setText(query)
            self.field.blockSignals(False)
        self._timer.stop()
        query = " ".join(self.field.text().split())
        if not query:
            self.clear_results()
            return
        if self._busy:
            self._pending = True
            return
        documents = self._documents()
        self._total = len(documents)
        self._generation += 1
        generation = self._generation
        self.query = query
        self._busy = True
        cancel = threading.Event()
        self._cancel = cancel
        bridge = self._bridge
        if self.index is None or self.index.embedder is not self._embedder():
            self.index = si.SemanticIndex(self._embedder(), self.store)
        index = self.index
        self._set_status(t("semantic.status.searching"))

        def work():
            index.build(documents, progress=lambda done, total: bridge.progress.emit(done, total), cancel=cancel)
            return index.hits_by_key(query), index.mode

        def done(result) -> None:
            self._busy = False
            if generation == self._generation:
                self.hits, self.mode = result
                self._publish(final=True)
            self.finished.emit()
            self._again()

        def failed(error) -> None:
            self._busy = False
            log.warning("Search failed (%s): %s", self.name, type(error).__name__)
            if generation == self._generation:
                self._set_status(t("semantic.status.failed"))
            self.finished.emit()
            self._again()

        self.started.emit()
        if self.runner is None:
            try:
                done(work())
            except Exception as error:  # noqa: BLE001
                failed(error)
        else:
            run_job(self.runner, work, done, failed, name=self.name, timeout=SEARCH_TIMEOUT_S, cancel=cancel)

    def _again(self) -> None:
        if self._pending:
            self._pending = False
            self.search()

    def _progress(self, done: int, total: int) -> None:
        if self._busy and total:
            self._set_status(t("semantic.status.indexing", done=done, total=total))

    def _publish(self, *, final: bool) -> None:
        matching = sum(1 for h in self.hits.values() if h.matches)
        if self.mode == si.MODE_SEMANTIC:
            text = t("semantic.status.semantic", n=matching, total=self._total)
        elif final or self.embedder_provider is None or self.runner is None:
            text = t("semantic.status.keyword", n=matching, total=self._total)
        else:
            text = t("semantic.status.partial", n=matching, total=self._total)
        self._set_status(text)
        self._apply_tables()
        self.resultsChanged.emit()

    def _set_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.setToolTip(text)
        self.status.setVisible(bool(text))

    # --- tables
    def bind_table(self, view, key_of_row: Callable[[int], str | None]) -> None:
        """Rows whose key does not match are hidden (with the column filters)."""
        from . import table_filter

        controller = table_filter.controller_of(view) or table_filter.attach(view)
        self._tables.append((controller, key_of_row))

    def _apply_tables(self) -> None:
        for controller, key_of_row in self._tables:
            try:
                if self.active:
                    controller.set_predicate(PREDICATE, lambda row, k=key_of_row: self.matches(k(row)))
                else:
                    controller.set_predicate(PREDICATE, None)
            except RuntimeError:  # the table was deleted
                continue

    def refresh_tables(self) -> None:
        """After the list was refilled (the keys of the rows changed)."""
        self._apply_tables()

    def retranslate(self) -> None:
        self.field.setPlaceholderText(t("semantic.placeholder"))
        self.field.setToolTip(t("semantic.tooltip"))
        self.field.setAccessibleName(t("semantic.placeholder"))
        if self.active:
            self._publish(final=not self._busy)
