"""Tab "My CV": the user's CV in several language versions (English primary,
Italian secondary, more can be added), a built-in viewer of the selected
version (PDF with QtPdf, images scaled, DOCX as extracted text), the photo of
each version, and a consistency check between two versions.

The library itself (`cv_library.CVLibrary`) has no Qt; slow work (reading a CV
to detect its language, extracting the photo, the consistency check) runs in
worker threads through the window's `run_in_background`.
"""

from __future__ import annotations

import html
import logging
import webbrowser
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QPointF, QSize, Qt, QUrl
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QSplitter, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from ..cv_library import CVLibrary, CVVersion, LibraryError, analyse_for_comparison, compare_profiles
from ..i18n import t
from .common import cv_file_filter
from .theme import Palette, active_palette, section
from .widgets import wrapped_label

log = logging.getLogger(__name__)

VERSION_LANGUAGES = ("en", "it", "uk", "de", "fr", "es")
PHOTO_SIZE = QSize(72, 90)
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp")
ZOOM_STEP = 1.25


def language_label(code: str) -> str:
    """"English", "Italian"... in the interface language (the code when unknown)."""
    text = t(f"cv_language.{code}")
    return text if text != f"cv_language.{code}" else code.upper()


def version_title(version: CVVersion) -> str:
    """List text of a version: "[EN] label (file) · primary"."""
    name = version.label or version.file_name
    text = f"[{version.language.upper()}] {name}"
    if version.primary:
        text += " · " + t("mycv.primary")
    return text


def photo_pixmap(png: bytes | None, size: QSize = PHOTO_SIZE) -> QPixmap | None:
    if not png:
        return None
    pixmap = QPixmap()
    if not pixmap.loadFromData(png, "PNG"):
        return None
    return pixmap.scaled(size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)


class PhotoLabel(QLabel):
    """Small portrait (hidden when there is no photo)."""

    def __init__(self, parent: QWidget | None = None, size: QSize = PHOTO_SIZE):
        super().__init__(parent)
        self._size = size
        self._png: bytes | None = None
        self.setFixedSize(size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hide()

    def set_photo(self, png: bytes | None, tooltip: str = "") -> None:
        pixmap = photo_pixmap(png, self._size)
        self._png = png if pixmap is not None else None
        if pixmap is None:
            self.clear()
            self.hide()
            return
        self.setPixmap(pixmap)
        self.setToolTip(tooltip)
        self.show()

    @property
    def png(self) -> bytes | None:
        return self._png


class CVViewer(QWidget):
    """Viewer of one CV file: PDF (multi-page, zoom, fit width), image, DOCX text."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.kind = ""
        self.document = None
        self.pdf_view = None
        try:
            from PySide6.QtPdf import QPdfDocument
            from PySide6.QtPdfWidgets import QPdfView

            self.document = QPdfDocument(self)
            self.pdf_view = QPdfView(self)
            self.pdf_view.setDocument(self.document)
            self.pdf_view.setPageMode(QPdfView.PageMode.MultiPage)
            self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        except Exception:  # QtPdfWidgets missing: pages rendered as images below
            log.info("QPdfView not available: PDF pages shown as images", exc_info=True)
            try:
                from PySide6.QtPdf import QPdfDocument

                self.document = QPdfDocument(self)
            except Exception:
                self.document = None
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.image_scroll = QScrollArea()
        self.image_scroll.setWidgetResizable(True)
        self.image_scroll.setWidget(self.image_label)
        self._pixmaps: list[QPixmap] = []
        self._zoom = 1.0
        self.text_view = QPlainTextEdit()
        self.text_view.setReadOnly(True)
        self.text_note = wrapped_label(role="muted")
        text_page = QWidget()
        text_layout = QVBoxLayout(text_page)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.addWidget(self.text_note)
        text_layout.addWidget(self.text_view, 1)
        self.empty_label = wrapped_label(role="muted")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.empty_label)
        self.stack.addWidget(self.image_scroll)
        self.stack.addWidget(text_page)
        if self.pdf_view is not None:
            self.stack.addWidget(self.pdf_view)

        self.zoom_out_button = QPushButton("−")
        self.zoom_in_button = QPushButton("+")
        self.fit_button = QPushButton()
        for button in (self.zoom_out_button, self.zoom_in_button, self.fit_button):
            button.setProperty("role", "small")
        self.zoom_out_button.clicked.connect(lambda: self.zoom(1 / ZOOM_STEP))
        self.zoom_in_button.clicked.connect(lambda: self.zoom(ZOOM_STEP))
        self.fit_button.clicked.connect(self.fit_width)
        self.pages_label = QLabel()
        self.pages_label.setProperty("role", "muted")
        tools = QHBoxLayout()
        tools.setContentsMargins(0, 0, 0, 0)
        tools.addWidget(self.zoom_out_button)
        tools.addWidget(self.zoom_in_button)
        tools.addWidget(self.fit_button)
        tools.addStretch(1)
        tools.addWidget(self.pages_label)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(tools)
        layout.addWidget(self.stack, 1)
        self.show_file(None)

    # --- loading

    def show_file(self, path: str | Path | None) -> str:
        """Shows the file; returns the kind: "pdf", "image", "docx", "empty" or "error"."""
        self._pixmaps = []
        self._zoom = 1.0
        if self.document is not None:
            self.document.close()
        self.current_path = Path(path) if path is not None else None
        if path is None or not Path(path).is_file():
            self.kind = "empty" if path is None else "error"
            self.empty_label.setText(t("mycv.viewer_empty") if path is None else t("mycv.viewer_missing"))
            self.stack.setCurrentWidget(self.empty_label)
        else:
            suffix = Path(path).suffix.lower()
            if suffix == ".pdf":
                self.kind = self._show_pdf(Path(path))
            elif suffix in IMAGE_SUFFIXES:
                self.kind = self._show_image(Path(path))
            elif suffix == ".docx":
                self.kind = self._show_docx(Path(path))
            else:
                self.kind = "error"
                self.empty_label.setText(t("mycv.viewer_unsupported"))
                self.stack.setCurrentWidget(self.empty_label)
        self._update_tools()
        return self.kind

    def release(self, path: str | Path | None = None) -> bool:
        """Closes the shown file (the PDF stays open while shown: Windows then refuses
        to delete or move it) when it is `path` (any file when None); True if closed."""
        current = getattr(self, "current_path", None)
        if current is None:
            return False
        if path is not None:
            try:
                if Path(path).resolve() != current.resolve():
                    return False
            except OSError:
                return False
        self.show_file(None)
        return True

    def page_count(self) -> int:
        if self.kind == "pdf" and self.document is not None:
            return self.document.pageCount()
        return len(self._pixmaps) if self.kind == "image" else 0

    def _show_pdf(self, path: Path) -> str:
        if self.document is None:
            return self._fail()
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from PySide6.QtPdf import QPdfDocument

        # Loaded from memory: QPdfDocument.load(path) keeps the file open even after
        # close(), and Windows then refuses to remove or replace it in the library.
        try:
            data = path.read_bytes()
        except OSError:
            return self._fail()
        buffer = QBuffer(self.document)
        buffer.setData(QByteArray(data))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        previous, self._pdf_buffer = getattr(self, "_pdf_buffer", None), buffer
        self.document.load(buffer)
        if previous is not None:
            previous.deleteLater()
        if self.document.status() != QPdfDocument.Status.Ready or self.document.pageCount() < 1:
            return self._fail()
        if self.pdf_view is not None:
            from PySide6.QtPdfWidgets import QPdfView

            self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
            self.stack.setCurrentWidget(self.pdf_view)
            return "pdf"
        # Fallback: every page rendered as an image, one under the other.
        for index in range(min(self.document.pageCount(), 20)):
            size = self.document.pagePointSize(index)
            image = self.document.render(index, QSize(int(size.width() * 2), int(size.height() * 2)))
            if not image.isNull():
                self._pixmaps.append(QPixmap.fromImage(image))
        self._draw_images()
        self.stack.setCurrentWidget(self.image_scroll)
        return "pdf"

    def _show_image(self, path: Path) -> str:
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return self._fail()
        self._pixmaps = [pixmap]
        self._draw_images()
        self.stack.setCurrentWidget(self.image_scroll)
        return "image"

    def _show_docx(self, path: Path) -> str:
        try:
            from ..cv.reading import read_document

            text = read_document(path).text
        except Exception:
            log.info("DOCX version not readable", exc_info=True)
            return self._fail()
        self.text_note.setText(t("mycv.docx_note"))
        self.text_view.setPlainText(text)
        self.stack.setCurrentIndex(2)
        return "docx"

    def _fail(self) -> str:
        self.empty_label.setText(t("mycv.viewer_error"))
        self.stack.setCurrentWidget(self.empty_label)
        return "error"

    # --- zoom

    def _draw_images(self) -> None:
        if not self._pixmaps:
            self.image_label.clear()
            return
        width = max(200, self.image_scroll.viewport().width() - 8)
        target = int(width * self._zoom)
        scaled = [p.scaledToWidth(min(target, p.width() * 3), Qt.TransformationMode.SmoothTransformation)
                  for p in self._pixmaps]
        total = QPixmap(max(p.width() for p in scaled), sum(p.height() for p in scaled) + 8 * (len(scaled) - 1))
        total.fill(Qt.GlobalColor.transparent)
        from PySide6.QtGui import QPainter

        painter = QPainter(total)
        y = 0
        for p in scaled:
            painter.drawPixmap(QPointF(0, y), p)
            y += p.height() + 8
        painter.end()
        self.image_label.setPixmap(total)

    def zoom(self, factor: float) -> None:
        if self.kind == "pdf" and self.pdf_view is not None:
            from PySide6.QtPdfWidgets import QPdfView

            current = self.pdf_view.zoomFactor()
            self.pdf_view.setZoomMode(QPdfView.ZoomMode.Custom)
            self.pdf_view.setZoomFactor(max(0.2, min(5.0, current * factor)))
        elif self._pixmaps:
            self._zoom = max(0.2, min(3.0, self._zoom * factor))
            self._draw_images()

    def fit_width(self) -> None:
        if self.kind == "pdf" and self.pdf_view is not None:
            from PySide6.QtPdfWidgets import QPdfView

            self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        elif self._pixmaps:
            self._zoom = 1.0
            self._draw_images()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(event)
        if self._pixmaps and self._zoom == 1.0:
            self._draw_images()

    def _update_tools(self) -> None:
        zoomable = self.kind in ("pdf", "image")
        for button in (self.zoom_out_button, self.zoom_in_button, self.fit_button):
            button.setEnabled(zoomable)
        count = self.page_count()
        self.pages_label.setText(t("mycv.pages", pages=count) if self.kind == "pdf" else "")

    def retranslate(self) -> None:
        self.fit_button.setText(t("mycv.fit_width"))
        self.zoom_in_button.setToolTip(t("mycv.zoom_in"))
        self.zoom_out_button.setToolTip(t("mycv.zoom_out"))
        self._update_tools()
        if self.kind == "docx":
            self.text_note.setText(t("mycv.docx_note"))
        elif self.kind == "empty":
            self.empty_label.setText(t("mycv.viewer_empty"))


class CVPanel(QWidget):
    """The "My CV" tab."""

    def __init__(self, library: CVLibrary | Callable[[], CVLibrary], run_in_background: Callable, dialogs,
                 *, on_use: Callable[[str], None] | None = None, opener: Callable = webbrowser.open,
                 store=None, settings=None, on_changed: Callable[[], None] | None = None,
                 photo_extractor: Callable | None = None, folder_opener: Callable | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._library_source = library
        self._library: CVLibrary | None = library if isinstance(library, CVLibrary) else None
        self._run = run_in_background
        self._dialogs = dialogs
        self._on_use = on_use or (lambda _p: None)
        self._opener = opener
        self._store = store
        self._settings = settings
        self._on_changed = on_changed or (lambda: None)
        self._photo_extractor = photo_extractor
        self._folder_opener = folder_opener
        self._photo_tried: set[str] = set()
        self._palette: Palette = active_palette()
        self._differences = None  # (label_a, label_b, list[Difference]) or an error key
        self._comparing = False

        self.title_label = QLabel()
        self.title_label.setProperty("role", "title")
        self.intro_label = wrapped_label(role="muted")

        self.list = QListWidget()
        self.list.currentRowChanged.connect(lambda _r: self._selection_changed())
        self.photo = PhotoLabel()
        self.language_label = QLabel()
        self.language_combo = QComboBox()
        for code in VERSION_LANGUAGES:
            self.language_combo.addItem("", code)
        self.language_combo.activated.connect(lambda _i: self._language_chosen())
        self.label_label = QLabel()
        self.label_field = QLineEdit()
        self.label_field.editingFinished.connect(self._label_edited)
        self.file_label = wrapped_label(role="muted")

        self.add_button = QPushButton()
        self.replace_button = QPushButton()
        self.remove_button = QPushButton()
        self.primary_button = QPushButton()
        self.use_button = QPushButton()
        self.use_button.setProperty("role", "accent")
        self.open_button = QPushButton()
        self.folder_button = QPushButton()
        self.save_photo_button = QPushButton()
        self.add_button.clicked.connect(lambda: self.add_version())
        self.replace_button.clicked.connect(lambda: self.replace_file())
        self.remove_button.clicked.connect(lambda: self.remove_version())
        self.primary_button.clicked.connect(lambda: self.set_primary())
        self.use_button.clicked.connect(lambda: self.use_for_analysis())
        self.open_button.clicked.connect(lambda: self.open_externally())
        self.folder_button.clicked.connect(lambda: self.show_in_folder())
        self.save_photo_button.clicked.connect(lambda: self.save_photo())
        buttons = QGridLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        order = (self.add_button, self.replace_button, self.remove_button, self.primary_button,
                 self.open_button, self.folder_button, self.save_photo_button, self.use_button)
        for i, button in enumerate(order):
            if button is not self.use_button:
                button.setProperty("role", "small")
            buttons.addWidget(button, i // 2, i % 2)

        details = QGridLayout()
        details.setContentsMargins(0, 0, 0, 0)
        details.addWidget(self.language_label, 0, 0)
        details.addWidget(self.language_combo, 0, 1)
        details.addWidget(self.label_label, 1, 0)
        details.addWidget(self.label_field, 1, 1)
        details.addWidget(self.file_label, 2, 0, 1, 2)
        details.setColumnStretch(1, 1)
        selected = QHBoxLayout()
        selected.setContentsMargins(0, 0, 0, 0)
        selected.addWidget(self.photo, 0, Qt.AlignmentFlag.AlignTop)
        selected.addLayout(details, 1)

        # Consistency check between two versions.
        self.compare_label = QLabel()
        self.compare_label.setProperty("role", "title")
        self.compare_combo = QComboBox()
        self.compare_button = QPushButton()
        self.compare_button.setProperty("role", "small")
        self.compare_button.clicked.connect(lambda: self.compare())
        compare_row = QHBoxLayout()
        compare_row.setContentsMargins(0, 0, 0, 0)
        compare_row.addWidget(self.compare_combo, 1)
        compare_row.addWidget(self.compare_button)
        self.compare_view = QTextBrowser()
        self.compare_view.setOpenLinks(False)
        self.compare_view.setMinimumHeight(self.fontMetrics().height() * 5)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 4, 0)
        self.list.setMinimumHeight(self.list.fontMetrics().height() * 4 + 12)
        left_layout.addWidget(self.list, 1)
        left_layout.addLayout(selected)
        left_layout.addLayout(buttons)
        left_layout.addWidget(self.compare_label)
        left_layout.addLayout(compare_row)
        left_layout.addWidget(self.compare_view, 3)
        left.setMinimumWidth(self.fontMetrics().averageCharWidth() * 44)

        self.viewer = CVViewer()
        # In a scroll area: at 1280x800 everything fits, smaller windows scroll.
        self.left_scroll = QScrollArea()
        self.left_scroll.setWidgetResizable(True)
        self.left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.left_scroll.setWidget(left)
        self.left_scroll.setMinimumWidth(left.minimumSizeHint().width() + 16)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.left_scroll)
        self.splitter.addWidget(self.viewer)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 3)
        self.splitter.setSizes([440, 660])

        layout = QVBoxLayout(self)
        layout.addWidget(self.title_label)
        layout.addWidget(self.intro_label)
        layout.addWidget(self.splitter, 1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.retranslate()

    # --- library

    @property
    def library(self) -> CVLibrary:
        if self._library is None:
            self._library = self._library_source()
        releasers = getattr(self._library, "releasers", None)
        viewer = getattr(self, "viewer", None)
        if releasers is not None and viewer is not None and viewer.release not in releasers:
            releasers.append(viewer.release)   # the preview lets go of a file being removed
        return self._library

    def versions(self) -> list[CVVersion]:
        try:
            return self.library.versions()
        except Exception:
            log.warning("CV library not readable", exc_info=True)
            return []

    def reload(self, select: str | None = None) -> None:
        current = select or (self.selected().id if self.selected() else None)
        versions = self.versions()
        self.list.blockSignals(True)
        self.list.clear()
        row = 0
        for i, version in enumerate(versions):
            item = QListWidgetItem(version_title(version))
            item.setData(Qt.ItemDataRole.UserRole, version.id)
            item.setToolTip(f"{language_label(version.language)} · {version.file_name}")
            self.list.addItem(item)
            if version.id == current:
                row = i
        self.list.blockSignals(False)
        if versions:
            self.list.setCurrentRow(row)
        self._selection_changed()

    def selected(self) -> CVVersion | None:
        item = self.list.currentItem()
        if item is None:
            return None
        return self.library.get(item.data(Qt.ItemDataRole.UserRole))

    def select(self, version_id: str) -> None:
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.ItemDataRole.UserRole) == version_id:
                self.list.setCurrentRow(i)
                return

    def _selection_changed(self) -> None:
        version = self.selected()
        has = version is not None
        for button in (self.replace_button, self.remove_button, self.primary_button, self.use_button,
                       self.open_button, self.folder_button):
            button.setEnabled(has)
        self.primary_button.setEnabled(has and not version.primary)
        self.language_combo.setEnabled(has)
        self.label_field.setEnabled(has)
        if has:
            index = self.language_combo.findData(version.language)
            if index < 0:
                self.language_combo.addItem(language_label(version.language), version.language)
                index = self.language_combo.count() - 1
            self.language_combo.setCurrentIndex(index)
            self.label_field.setText(version.label)
            self.label_field.setPlaceholderText(version.file_name)
            self.file_label.setText(t("mycv.file_info", file=version.file_name,
                                      language=language_label(version.language)))
            self.viewer.show_file(self.library.path_of(version))
        else:
            self.label_field.clear()
            self.file_label.setText(t("mycv.empty") if not self.versions() else "")
            self.viewer.show_file(None)
        self._update_photo(version)
        self._fill_compare_combo()

    # --- photo

    def _update_photo(self, version: CVVersion | None) -> None:
        png = None
        if version is not None:
            path = self.library.photo_path(version)
            if path is not None:
                try:
                    png = path.read_bytes()
                except OSError:
                    png = None
            elif version.id not in self._photo_tried:
                self._extract_photo(version)
        self.photo.set_photo(png, t("mycv.photo_tooltip"))
        self.save_photo_button.setEnabled(png is not None)

    def _extract_photo(self, version: CVVersion) -> None:
        self._photo_tried.add(version.id)
        path = self.library.path_of(version)
        extractor = self._photo_extractor

        def work():
            if extractor is not None:
                return extractor(path)
            from ..cv.photo import extract_photo

            return extract_photo(path)

        def done(photo) -> None:
            if photo is not None and getattr(photo, "png", None):
                self.library.save_photo(version.id, photo.png)
                current = self.selected()
                if current is not None and current.id == version.id:
                    self._update_photo(current)

        self._run(work, done, lambda _e: None)

    def save_photo(self, path: str | None = None) -> bool:
        version = self.selected()
        png = self.photo.png
        if version is None or png is None:
            return False
        if path is None:
            path = self._dialogs.save_file(t("mycv.save_photo"), t("mycv.photo_file_name"),
                                           f"{t('mycv.photo_filter')} (*.png)")
        if not path:
            return False
        try:
            Path(path).write_bytes(png)
        except OSError:
            self._dialogs.error(t("mycv.error_photo"))
            return False
        return True

    # --- actions

    def add_version(self, path: str | None = None, language: str | None = None,
                    label: str | None = None) -> CVVersion | None:
        """Adds a version; without `language` it is detected from the text (in a
        worker), and the version is added when the detection is done."""
        if path is None:
            path = self._dialogs.open_file(t("mycv.add_title"), cv_file_filter())
        if not path:
            return None
        if language is None:
            def detect():
                from ..cv.reading import detect_language, read_document

                return detect_language(read_document(path).text)

            self._run(detect, lambda code: self.add_version(path, code or "en", label),
                      lambda _e: self.add_version(path, "en", label))
            return None
        try:
            version = self.library.add(path, language, label)
        except (LibraryError, OSError):
            log.warning("CV version not added", exc_info=True)
            self._dialogs.error(t("mycv.error_add"))
            return None
        self.reload(select=version.id)
        self._on_changed()
        return version

    def replace_file(self, path: str | None = None) -> CVVersion | None:
        version = self.selected()
        if version is None:
            return None
        if path is None:
            path = self._dialogs.open_file(t("mycv.replace_title"), cv_file_filter())
        if not path:
            return None
        try:
            new = self.library.replace(version.id, path)
        except (LibraryError, OSError, KeyError):
            log.warning("CV version not replaced", exc_info=True)
            self._dialogs.error(t("mycv.error_add"))
            return None
        self._photo_tried.discard(version.id)
        self.reload(select=new.id)
        self._on_changed()
        return new

    def remove_version(self, confirm: bool = True) -> bool:
        version = self.selected()
        if version is None:
            return False
        if confirm and not self._dialogs.ask(t("mycv.confirm_remove", name=version_title(version)),
                                             t("mycv.remove"), warning=True):
            return False
        try:
            self.library.remove(version.id)
        except (LibraryError, OSError):
            log.warning("CV version not removed: its file is in use")
            self._dialogs.error(t("mycv.error_in_use"))
            self.reload(select=version.id)
            return False
        self.reload()
        self._on_changed()
        return True

    def set_primary(self) -> None:
        version = self.selected()
        if version is None:
            return
        self.library.set_primary(version.id)
        self.reload(select=version.id)
        self._on_changed()

    def use_for_analysis(self) -> str | None:
        version = self.selected()
        if version is None:
            return None
        path = str(self.library.path_of(version))
        self._on_use(path)
        return path

    def open_externally(self) -> bool:
        version = self.selected()
        return version is not None and self._open(self.library.path_of(version))

    def show_in_folder(self) -> bool:
        version = self.selected()
        if version is None:
            return False
        folder = self.library.path_of(version).parent
        if self._folder_opener is not None:
            return bool(self._folder_opener(folder))
        return self._open(folder)

    def _open(self, path: Path) -> bool:
        try:
            self._opener(QUrl.fromLocalFile(str(path.resolve())).toString())
        except Exception:
            log.warning("CV file cannot be opened", exc_info=True)
            return False
        return True

    def _language_chosen(self) -> None:
        version = self.selected()
        code = self.language_combo.currentData()
        if version is None or not code or code == version.language:
            return
        try:
            self.library.update(version.id, language=code)
        except (LibraryError, OSError, KeyError):
            self._dialogs.error(t("mycv.error_add"))
        self.reload(select=version.id)
        self._on_changed()

    def _label_edited(self) -> None:
        version = self.selected()
        text = self.label_field.text().strip()
        if version is None or text == version.label:
            return
        self.library.update(version.id, label=text)
        item = self.list.currentItem()
        updated = self.library.get(version.id)
        if item is not None and updated is not None:
            item.setText(version_title(updated))

    # --- consistency check

    def _fill_compare_combo(self) -> None:
        current = self.selected()
        self.compare_combo.clear()
        for version in self.versions():
            if current is not None and version.id != current.id:
                self.compare_combo.addItem(version_title(version), version.id)
        self.compare_button.setEnabled(current is not None and self.compare_combo.count() > 0
                                       and not self._comparing)

    def compare(self, other_id: str | None = None) -> bool:
        """Starts the consistency check between the selected version and another one."""
        a = self.selected()
        other_id = other_id or self.compare_combo.currentData()
        b = self.library.get(other_id) if other_id else None
        if a is None or b is None or self._comparing:
            return False
        path_a, path_b = self.library.path_of(a), self.library.path_of(b)
        label_a = language_label(a.language) if a.language != b.language else (a.label or a.file_name)
        label_b = language_label(b.language) if a.language != b.language else (b.label or b.file_name)
        store, settings = self._store, self._settings
        self._comparing = True
        self.compare_button.setEnabled(False)
        self.compare_view.setPlainText(t("mycv.comparing"))

        def work():
            profile_a = analyse_for_comparison(path_a, store, settings)
            profile_b = analyse_for_comparison(path_b, store, settings)
            return compare_profiles(profile_a, profile_b, label_a, label_b)

        def done(differences) -> None:
            self._comparing = False
            self._differences = (label_a, label_b, list(differences))
            self._draw_differences()
            self._fill_compare_combo()

        def failed(_error) -> None:
            self._comparing = False
            self._differences = "mycv.compare_error"
            self._draw_differences()
            self._fill_compare_combo()

        self._run(work, done, failed)
        return True

    def differences_text(self) -> str:
        return self.compare_view.toPlainText()

    def _draw_differences(self) -> None:
        p = self._palette
        if self._differences is None:
            self.compare_view.setHtml(f'<p style="color:{p.muted}">{html.escape(t("mycv.compare_hint"))}</p>')
            return
        if isinstance(self._differences, str):
            self.compare_view.setHtml(f'<p style="color:{p.error}">{html.escape(t(self._differences))}</p>')
            return
        label_a, label_b, differences = self._differences
        if not differences:
            text = t("mycv.compare_same", a=label_a, b=label_b)
            self.compare_view.setHtml(f'<p style="color:{p.accent}">✓ {html.escape(text)}</p>')
            return
        rows = [f'<p style="color:{p.strong}"><b>{html.escape(t("mycv.compare_found", a=label_a, b=label_b, count=len(differences)))}</b></p>']
        for d in differences:
            rows.append(f'<p style="margin:2px 0 2px 8px; color:{p.warning}">▲ '
                        f'{html.escape(t(d.key, **d.params))}</p>')
        self.compare_view.setHtml("".join(rows))

    # --- texts and theme

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self._draw_differences()

    def retranslate(self) -> None:
        self.title_label.setText(t("mycv.title"))
        self.intro_label.setText(t("mycv.intro"))
        self.language_label.setText(t("mycv.language"))
        for i in range(self.language_combo.count()):
            self.language_combo.setItemText(i, language_label(self.language_combo.itemData(i)))
        self.label_label.setText(t("mycv.label"))
        self.add_button.setText(t("mycv.add"))
        self.replace_button.setText(t("mycv.replace"))
        self.remove_button.setText(t("mycv.remove"))
        self.primary_button.setText(t("mycv.set_primary"))
        self.use_button.setText(t("mycv.use"))
        self.open_button.setText(t("mycv.open"))
        self.folder_button.setText(t("mycv.show_folder"))
        self.save_photo_button.setText(t("mycv.save_photo"))
        self.compare_label.setText(t("mycv.compare_title"))
        self.compare_button.setText(t("mycv.compare"))
        self.viewer.retranslate()
        self.reload()
        self._draw_differences()


__all__ = ["CVPanel", "CVViewer", "PhotoLabel", "language_label", "version_title", "photo_pixmap"]
