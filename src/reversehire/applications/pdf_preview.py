"""Embedded PDF preview with page navigation (the same QtPdf approach as the CV viewer:
the bytes are loaded from memory, so Windows never keeps the archived file open).

The bytes come from a worker (``ApplicationService.read_cv``); only parsing the document
and painting happen here. Without QtPdfWidgets the current page is shown as an image."""

from __future__ import annotations

import logging

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QScrollArea, QStackedWidget, QVBoxLayout,
                               QWidget)

from .texts import Texts

log = logging.getLogger("reversehire.applications")


class PdfPreview(QWidget):
    """Preview of one PDF: ``show_bytes`` / ``show_message`` / ``clear``; ``go_to`` navigates."""

    openExternalRequested = Signal()
    pageChanged = Signal(int, int)  # current page (0-based), page count

    def __init__(self, tr=None, parent: QWidget | None = None):
        super().__init__(parent)
        self.T = tr if isinstance(tr, Texts) else Texts(tr)
        self.document = None
        self.view = None
        self._buffer = None
        self._page = 0
        try:
            from PySide6.QtPdf import QPdfDocument

            self.document = QPdfDocument(self)
        except Exception:  # noqa: BLE001 - QtPdf missing: the preview shows a message only
            log.info("QtPdf not available", exc_info=True)
        try:
            from PySide6.QtPdfWidgets import QPdfView

            self.view = QPdfView(self)
            self.view.setDocument(self.document)
            self.view.setPageMode(QPdfView.PageMode.MultiPage)
            self.view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        except Exception:  # noqa: BLE001
            log.info("QPdfView not available: the page is shown as an image", exc_info=True)
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.image_scroll = QScrollArea()
        self.image_scroll.setWidgetResizable(True)
        self.image_scroll.setWidget(self.image_label)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setProperty("role", "muted")

        self.stack = QStackedWidget()
        self.stack.addWidget(self.message)
        self.stack.addWidget(self.view if self.view is not None else self.image_scroll)

        self.previous_button = QPushButton(self.T("applications.pdf.previous"))
        self.next_button = QPushButton(self.T("applications.pdf.next"))
        self.page_label = QLabel("")
        self.external_button = QPushButton(self.T("applications.pdf.open_external"))
        self.previous_button.clicked.connect(lambda *_: self.previous_page())
        self.next_button.clicked.connect(lambda *_: self.next_page())
        self.external_button.clicked.connect(lambda *_: self.openExternalRequested.emit())
        bar = QHBoxLayout()
        for widget in (self.previous_button, self.page_label, self.next_button):
            bar.addWidget(widget)
        bar.addStretch(1)
        bar.addWidget(self.external_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.stack, 1)
        if self.view is not None:
            self.view.pageNavigator().currentPageChanged.connect(self._navigator_page)
        self.clear()

    # --- content -----------------------------------------------------------------------

    def clear(self, text: str | None = None) -> None:
        self._page = 0
        if self.document is not None:
            try:
                self.document.close()
            except Exception:  # noqa: BLE001
                pass
        self.image_label.clear()
        self.show_message(text if text is not None else self.T("applications.pdf.none"))

    def show_message(self, text: str) -> None:
        self.message.setText(text)
        self.stack.setCurrentWidget(self.message)
        self.external_button.setEnabled(False)
        self._update_controls()

    def show_bytes(self, data: bytes | None) -> bool:
        """Loads a PDF from memory; False (with a message shown) when it cannot be read."""
        if not data or self.document is None:
            self.clear(self.T("applications.pdf.missing"))
            return False
        from PySide6.QtPdf import QPdfDocument

        buffer = QBuffer(self.document)
        buffer.setData(QByteArray(data))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        previous, self._buffer = self._buffer, buffer
        self.document.load(buffer)
        if previous is not None:
            previous.deleteLater()
        if self.document.status() != QPdfDocument.Status.Ready or self.document.pageCount() < 1:
            self.clear(self.T("applications.pdf.unreadable"))
            return False
        self._page = 0
        self.stack.setCurrentIndex(1)
        self.external_button.setEnabled(True)
        if self.view is None:
            self._draw_page()
        else:
            self.view.pageNavigator().jump(0, QPointF(0, 0))
        self._update_controls()
        return True

    # --- navigation --------------------------------------------------------------------

    def page_count(self) -> int:
        return self.document.pageCount() if self.document is not None and self.is_loaded() else 0

    def is_loaded(self) -> bool:
        from PySide6.QtPdf import QPdfDocument

        return self.document is not None and self.document.status() == QPdfDocument.Status.Ready

    def current_page(self) -> int:
        return self._page

    def go_to(self, page: int) -> None:
        count = self.page_count()
        if count < 1:
            return
        self._page = max(0, min(count - 1, int(page)))
        if self.view is None:
            self._draw_page()
        else:
            self.view.pageNavigator().jump(self._page, QPointF(0, 0))
        self._update_controls()

    def next_page(self) -> None:
        self.go_to(self._page + 1)

    def previous_page(self) -> None:
        self.go_to(self._page - 1)

    def _navigator_page(self, page: int) -> None:
        if page != self._page and self.is_loaded():
            self._page = page
            self._update_controls()

    def _draw_page(self) -> None:
        size = self.document.pagePointSize(self._page)
        width = max(300, self.image_scroll.viewport().width() - 8)
        scale = width / max(1.0, size.width())
        image = self.document.render(self._page, QSize(int(size.width() * scale), int(size.height() * scale)))
        if not image.isNull():
            self.image_label.setPixmap(QPixmap.fromImage(image))

    def _update_controls(self) -> None:
        count = self.page_count()
        self.page_label.setText(self.T("applications.pdf.page", page=self._page + 1, count=count) if count else "")
        self.previous_button.setEnabled(count > 1 and self._page > 0)
        self.next_button.setEnabled(count > 1 and self._page < count - 1)
        self.pageChanged.emit(self._page, count)
