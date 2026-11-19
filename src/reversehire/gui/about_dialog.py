"""About dialog: information about this PC on top, logo and credits below.

The PC information is collected on a worker thread when the dialog opens (disk
queries can wake a sleeping drive), only shown: it is never logged, saved or sent
anywhere. Screens and the Qt version are read on the GUI thread first and handed
to the worker. The data folder and the embedding model are in Diagnostics.
"""

from __future__ import annotations

from typing import Callable

import PySide6
from PySide6.QtCore import Qt, qVersion
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import sysinfo, version_text
from ..i18n import t
from . import theme
from . import table_filter
from .widgets import separator, wrapped_label
from .workers import runner_or_local

# Fixed lines: never translated.
DEVELOPER_LINE = "Developed by 0THack1A"  # the first character is the digit zero
UKRAINE_LINE = "Слава Україні!"  # always in Ukrainian, whatever the interface language
LOGO_WIDTH = 360
COLLECT_TIMEOUT_S = 30.0


def screen_text() -> str | None:
    """Every screen: "1920 × 1080 px, scale 100% (primary)", one line each. GUI thread only."""
    screens = QGuiApplication.screens()
    primary = QGuiApplication.primaryScreen()
    lines = []
    for screen in screens:
        ratio = screen.devicePixelRatio() or 1.0
        size = screen.size()
        line = t("about.screen_value", width=round(size.width() * ratio), height=round(size.height() * ratio),
                 scale=round(ratio * 100))
        if screen is primary and len(screens) > 1:
            line += " " + t("about.screen_primary")
        lines.append(line)
    return "\n".join(lines) or None


def qt_text() -> str:
    return f"Qt {qVersion()} / PySide6 {PySide6.__version__}"


def labels() -> dict[str, str]:
    """The translated words and templates sysinfo fills in (sysinfo.DEFAULT_LABELS)."""
    return {"available": t("about.available"), "not_available": t("about.not_available"),
            "online": t("about.online"), "offline": t("about.offline"),
            "uptime": t("about.uptime_value"), "disk": t("about.disk_value"),
            "disk_program": t("about.disk_program"), "disk_data": t("about.disk_data"),
            "app_version": t("about.app_version_value"),
            "app_version_source": t("about.app_version_source")}


def gui_snapshot(mcp_enabled: bool) -> dict:
    """What only the GUI thread can read (screens, Qt) and the translated words."""
    try:
        screen = screen_text()
    except Exception:
        screen = None
    return {"screen": screen, "qt": qt_text(), "mcp": mcp_text(mcp_enabled), "labels": labels()}


def mcp_text(enabled: bool) -> str:
    return t("mcp.on") if enabled else t("mcp.off")


def collect_info(mcp_enabled: bool, snapshot: dict | None = None) -> dict[str, str]:
    """sysinfo values with the GUI-only ones; blocking (run it on a worker)."""
    snapshot = snapshot if snapshot is not None else gui_snapshot(mcp_enabled)
    return sysinfo.collect(**snapshot)


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None, mcp_enabled: bool, effective_theme: str = "dark",
                 collector: Callable[[bool], dict[str, str]] | None = None,
                 run_in_background: Callable | None = None):
        super().__init__(parent)
        self.setModal(True)
        self._mcp_enabled = mcp_enabled
        self._collector = collector
        self._run = runner_or_local(run_in_background, self)
        self._theme = effective_theme
        self._generation = 0
        self.values: dict[str, str] = {}
        self.ready = False  # the values of the current collection have arrived

        self.pc_title = QLabel()
        self.pc_title.setProperty("role", "title")
        self.table = QTableWidget(len(sysinfo.KEYS), 2)
        self.table.setObjectName("aboutTable")
        table_filter.attach(self.table)
        self.table.horizontalHeader().setVisible(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(True)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.pc_note = wrapped_label(role="muted")
        self.copy_button = QPushButton()
        self.copy_button.clicked.connect(self.copy)
        self.copied_label = QLabel()
        self.copied_label.setProperty("role", "success")
        copy_row = QHBoxLayout()
        copy_row.addWidget(self.pc_note, 1)
        copy_row.addWidget(self.copied_label)
        copy_row.addWidget(self.copy_button)

        self.logo = QLabel()
        self.logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.developer_label = QLabel(DEVELOPER_LINE)
        self.developer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.developer_label.setProperty("role", "strong")
        self.ukraine_label = QLabel(UKRAINE_LINE)
        self.ukraine_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.ukraine_label.setProperty("role", "title")
        self.version_label = QLabel()
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.version_label.setProperty("role", "muted")
        small = self.version_label.font()
        small.setPointSizeF(max(6.0, small.pointSizeF() - 1))
        self.version_label.setFont(small)
        self.close_button = QPushButton()
        self.close_button.setProperty("role", "accent")
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.accept)
        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_row.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.pc_title)
        layout.addWidget(self.table, 1)
        layout.addLayout(copy_row)
        layout.addSpacing(6)
        self.divider = separator()
        layout.addWidget(self.divider)
        layout.addSpacing(10)
        layout.addWidget(self.logo)
        layout.addSpacing(4)
        layout.addWidget(self.developer_label)
        layout.addWidget(self.ukraine_label)
        layout.addWidget(self.version_label)
        layout.addSpacing(6)
        layout.addLayout(close_row)

        self.set_theme(effective_theme)
        self.retranslate()
        width = max(self.sizeHint().width(), self.fontMetrics().averageCharWidth() * 96)
        self.resize(width, max(self.sizeHint().height(), self._table_height() + 300))

    def _table_height(self) -> int:
        return sum(self.table.rowHeight(r) for r in range(self.table.rowCount())) + 4

    def set_theme(self, effective_theme: str) -> None:
        """Logo for the dark or light theme (text title when the image is missing)."""
        self._theme = effective_theme
        pixmap = theme.load_pixmap(f"header_{effective_theme}.png")
        if pixmap is None:
            self.logo.setText(theme.APP_NAME)
            self.logo.setProperty("role", "title")
            return
        if pixmap.width() > LOGO_WIDTH:
            pixmap = pixmap.scaledToWidth(LOGO_WIDTH, Qt.TransformationMode.SmoothTransformation)
        self.logo.setPixmap(pixmap)
        self.logo.setToolTip(theme.APP_NAME)

    def has_logo_image(self) -> bool:
        pixmap = self.logo.pixmap()
        return pixmap is not None and not pixmap.isNull()

    def set_mcp_enabled(self, enabled: bool) -> None:
        self._mcp_enabled = enabled
        if self.values:
            self.values["mcp"] = mcp_text(enabled)
        self._fill()

    # --- collection on a worker

    def refresh(self) -> None:
        """Collects the values again on a worker; the table shows "collecting" meanwhile."""
        self._generation += 1
        generation = self._generation
        self.ready = False
        enabled = self._mcp_enabled
        if self._collector is None:
            snapshot = gui_snapshot(enabled)  # GUI thread: screens, Qt, translated words

            def work():
                return collect_info(enabled, snapshot)
        else:
            collector = self._collector

            def work():
                return collector(enabled)

        self._run(work, lambda values: self._collected(generation, values),
                  lambda error: self._collected(generation, {}), name="About: PC information",
                  timeout=COLLECT_TIMEOUT_S)

    def _collected(self, generation: int, values: dict) -> None:
        if generation != self._generation:
            return  # an older collection (the language changed meanwhile)
        self.values = {key: str(values.get(key, sysinfo.UNKNOWN)) for key in sysinfo.KEYS}
        self.ready = True
        self._fill()

    def _fill(self) -> None:
        waiting = t("about.collecting")
        for row, key in enumerate(sysinfo.KEYS):
            name = QTableWidgetItem(t(f"about.key.{key}"))
            text = self.values.get(key, sysinfo.UNKNOWN) if self.values else waiting
            value = QTableWidgetItem(text)
            value.setToolTip(text)
            for item in (name, value):
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, value)
        self.table.resizeRowsToContents()
        self.copy_button.setEnabled(self.ready)

    def names(self) -> dict[str, str]:
        return {key: t(f"about.key.{key}") for key in sysinfo.KEYS}

    def text(self) -> str:
        """The PC information as plain text (what Copy puts in the clipboard)."""
        return sysinfo.as_text(self.values, self.names())

    def copy(self) -> None:
        QApplication.clipboard().setText(self.text())
        self.copied_label.setText(t("common.copied"))

    def retranslate(self) -> None:
        self.setWindowTitle(t("about.title"))
        self.pc_title.setText(theme.section(t("about.pc_section")))
        self.pc_note.setText(t("about.pc_note"))
        self.copy_button.setText(t("common.copy"))
        if self.copied_label.text():
            self.copied_label.setText(t("common.copied"))
        from .. import BUILD_DATE, __version__

        self.version_label.setText(t("about.version", version=version_text(__version__, BUILD_DATE)))
        self.close_button.setText(t("common.close"))
        self._fill()
        self.refresh()  # the values carry translated words
