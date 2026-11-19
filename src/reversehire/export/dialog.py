"""Reusable export preview: shows the document, then print / save as PDF, Excel or Word.

Everything the user reads goes through `tr(key, default)`. The file dialog and the printer
are injectable (`file_dialog`, `print_pdf`) so the dialog is testable offscreen without
opening anything real. Rendering runs on a worker thread; errors are shown inline and
never raised.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)

from .docx_render import render_docx
from .html_render import render_html
from .model import ExportDocument, ExportError, NoPrinterError
from .pdf import merge_pdfs, print_pdf_file, typeset_parts
from .texts import Translate, make_t
from .xlsx import render_xlsx

log = logging.getLogger("reversehire.export")

FORMATS = {
    "pdf": (".pdf", "export.dialog.pdf", "export.filter.pdf", None),  # typeset on the GUI thread
    "excel": (".xlsx", "export.dialog.excel", "export.filter.excel", render_xlsx),
    "word": (".docx", "export.dialog.word", "export.filter.word", render_docx),
}

FileDialog = Callable[[str, str, str], str]  # (window title, suggested file name, name filter) -> path or ""
PrintPdf = Callable[[Path], object]          # sends a PDF file to the printer; raises on failure


def default_file_dialog(parent: QWidget | None = None) -> FileDialog:
    def ask(title: str, suggested: str, name_filter: str) -> str:
        path, _ = QFileDialog.getSaveFileName(parent, title, suggested, name_filter)
        return path or ""

    return ask


def default_print_pdf(path: Path) -> object:
    return print_pdf_file(path)


class _Bridge(QObject):
    finished = Signal(int, object, object)  # job id, result, error


class ExportPreviewDialog(QDialog):
    """Preview of an ExportDocument with Print…, Save PDF, Save Excel, Save Word, Close."""

    def __init__(self, document: ExportDocument, parent: QWidget | None = None, *,
                 tr: Translate | None = None, file_dialog: FileDialog | None = None,
                 print_pdf: PrintPdf | None = None, title: str | None = None):
        super().__init__(parent)
        self.document = document
        self._t = make_t(tr)
        self._file_dialog = file_dialog or default_file_dialog(self)
        self._print_pdf = print_pdf or default_print_pdf
        self._job = 0
        self._busy = False
        self._closed = False
        self._after: dict[int, Callable] = {}
        self._cleanup: dict[int, Callable | None] = {}
        self._bridge = _Bridge(self)
        self._bridge.finished.connect(self._on_finished)
        # Called with (format, path or None) after a successful save ("pdf", "excel", "word")
        # or print ("print", None); a failing listener is logged, never raised.
        self.exported_listeners: list[Callable[[str, Path | None], None]] = []

        t = self._t
        default_title = t("export.preview_application_title" if document.kind == "application"
                          else "export.preview_card_title")
        self.setWindowTitle(title or default_title)
        self.resize(820, 700)
        self.setObjectName("exportPreviewDialog")

        self.browser = QTextBrowser(self)
        self.browser.setOpenLinks(False)
        self.browser.setStyleSheet("QTextBrowser { background-color: #ffffff; color: #000000; }")
        self.browser.anchorClicked.connect(self._anchor)
        self.browser.setHtml(render_html(document))

        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("exportStatus")

        self.print_button = QPushButton(t("export.print"), self)
        self.pdf_button = QPushButton(t("export.save_pdf"), self)
        self.excel_button = QPushButton(t("export.save_excel"), self)
        self.word_button = QPushButton(t("export.save_word"), self)
        self.close_button = QPushButton(t("export.close"), self)
        self.print_button.clicked.connect(lambda: self.print_document())
        self.pdf_button.clicked.connect(lambda: self.save("pdf"))
        self.excel_button.clicked.connect(lambda: self.save("excel"))
        self.word_button.clicked.connect(lambda: self.save("word"))
        self.close_button.clicked.connect(self.close)

        buttons = QHBoxLayout()
        for button in (self.print_button, self.pdf_button, self.excel_button, self.word_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.browser, 1)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

    # --- state -----------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._busy

    def status_text(self) -> str:
        return self.status_label.text()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for button in (self.print_button, self.pdf_button, self.excel_button, self.word_button):
            button.setEnabled(not busy)

    def _status(self, text: str, error: bool = False) -> None:
        self.status_label.setStyleSheet("color: #b00020;" if error else "")
        self.status_label.setText(text)

    def _notify_exported(self, fmt: str, path: Path | None) -> None:
        for listener in list(self.exported_listeners):
            try:
                listener(fmt, path)
            except Exception:  # noqa: BLE001 - another feature must never break the export
                log.warning("export listener failed", exc_info=True)

    def _anchor(self, url: QUrl) -> None:
        if url.scheme() in ("http", "https"):
            QDesktopServices.openUrl(url)

    # --- worker ----------------------------------------------------------------------------

    def _run(self, work: Callable[[], object], after: Callable[[object], None],
             cleanup: Callable[[], None] | None = None) -> bool:
        if self._busy:
            return False
        self._job += 1
        job = self._job
        self._after[job] = after
        self._cleanup[job] = cleanup
        self._set_busy(True)
        self._status(self._t("export.working"))

        def target() -> None:
            result, error = None, None
            try:
                result = work()
            except BaseException as exc:  # noqa: BLE001 - reported inline, never raised
                error = exc
            try:
                self._bridge.finished.emit(job, result, error)
            except RuntimeError:  # the dialog was deleted meanwhile
                pass

        threading.Thread(target=target, name="export-worker", daemon=True).start()
        return True

    def _on_finished(self, job: int, result, error) -> None:
        after = self._after.pop(job, None)
        cleanup = self._cleanup.pop(job, None)
        self._set_busy(False)
        if self._closed or after is None:
            if cleanup:
                cleanup()
            return
        try:
            if error is not None:
                raise error
            after(result)
        except NoPrinterError:
            log.warning("print failed: no default printer")
            self._status(self._t("export.no_printer"), error=True)
        except BaseException as exc:  # noqa: BLE001
            log.warning("export failed: %s", type(exc).__name__)
            self._status(self._t("export.failed", error=str(exc) or type(exc).__name__), error=True)
        finally:
            if cleanup:
                cleanup()

    def wait_idle(self, timeout_ms: int = 30000) -> bool:
        """Process events until the worker is done (for tests and scripted use)."""
        from PySide6.QtWidgets import QApplication

        deadline = time.monotonic() + timeout_ms / 1000
        while self._busy and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)
        QApplication.processEvents()
        return not self._busy

    # --- actions ---------------------------------------------------------------------------

    def save(self, fmt: str) -> bool:
        """Ask for a file name (injected dialog) and export; False when cancelled or busy."""
        if fmt not in FORMATS or self._busy:
            return False
        ext, title_key, filter_key, _ = FORMATS[fmt]
        chosen = self._file_dialog(self._t(title_key), self.document.file_stem + ext, self._t(filter_key))
        if not chosen:
            return False
        return self.export_to(fmt, chosen)

    def export_to(self, fmt: str, path: str | Path) -> bool:
        if fmt not in FORMATS or self._busy:
            return False
        ext, _, _, renderer = FORMATS[fmt]
        target = Path(path)
        if target.suffix.lower() != ext:
            target = target.with_name(target.name + ext)
        document = self.document

        def done(_result) -> None:
            log.info("export done format=%s file=%s kind=%s", fmt, target.name, document.kind)
            self._status(self._t("export.saved", path=str(target)))
            self._notify_exported(fmt, target)

        if renderer is None:  # PDF: Qt typesetting on this thread, merge on the worker
            work = self._pdf_work(document, target)
            if work is None:
                return False
            return self._run(work, done)
        return self._run(lambda: renderer(document, target), done)

    def _pdf_work(self, document: ExportDocument, target: Path) -> Callable[[], object] | None:
        """Typeset now (GUI thread); the returned callable merges the parts on the worker."""
        folder = Path(tempfile.mkdtemp(prefix="reversehire-export-"))
        try:
            parts = typeset_parts(document, folder)
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(folder, ignore_errors=True)
            log.warning("export failed: %s", type(exc).__name__)
            self._status(self._t("export.failed", error=str(exc) or type(exc).__name__), error=True)
            return None

        def work() -> object:
            try:
                return merge_pdfs(parts, target)
            finally:
                shutil.rmtree(folder, ignore_errors=True)

        return work

    def print_document(self) -> bool:
        """Typeset to a temporary PDF, then hand that file to the printer callable."""
        if self._busy:
            return False
        folder = Path(tempfile.mkdtemp(prefix="reversehire-print-"))
        pdf = folder / "print.pdf"
        document = self.document

        def done(_result) -> None:
            try:
                self._print_pdf(pdf)
                log.info("printed kind=%s", document.kind)
                self._status(self._t("export.printed"))
                self._notify_exported("print", None)
            except NoPrinterError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("print failed: %s", type(exc).__name__)
                self._status(self._t("export.print_failed", error=str(exc) or type(exc).__name__), error=True)

        work = self._pdf_work(document, pdf)
        if work is None:
            shutil.rmtree(folder, ignore_errors=True)
            return False
        started = self._run(work, done, cleanup=lambda: shutil.rmtree(folder, ignore_errors=True))
        if not started:
            shutil.rmtree(folder, ignore_errors=True)
        return started

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._closed = False
        super().showEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._closed = True
        super().closeEvent(event)

    def done(self, result: int) -> None:  # noqa: D401 - Qt API (reject/accept/close all end here)
        self._closed = True
        super().done(result)


__all__ = ["ExportPreviewDialog", "FORMATS", "ExportError"]
