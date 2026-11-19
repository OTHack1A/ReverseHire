"""MCP connection dialog: the on/off switch, the last use and the configuration
snippets for Claude, Codex and Gemini.

The snippets are built at run time from the interpreter that runs the app, so
they point at this installation. Copying them is the user's action: the app
never writes other programs' configuration files.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QTabWidget,
    QVBoxLayout, QWidget,
)

from .. import config
from ..i18n import t
from . import theme
from .widgets import StatusDot, wrapped_label

SERVER_KEY = "reversehire"
SERVER_MODULE = "reversehire.mcp_server"
SCRIPT_NAME = "reversehire-mcp"
CLIENTS = ("claude", "codex", "gemini")
CLIENT_NAMES = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini"}  # product names
TOOL_TIMEOUT_MS = 600_000  # site analysis and the first model download can take minutes


def _console_python(executable: Path) -> Path:
    """python.exe instead of pythonw.exe: an MCP server talks through stdin/stdout."""
    if executable.name.lower() == "pythonw.exe":
        console = executable.with_name("python.exe")
        if console.is_file():
            return console
    return executable


def server_command(executable: str | None = None) -> tuple[str, list[str]]:
    """(command, args) that start this installation's MCP server.

    The `reversehire-mcp` script next to the interpreter when it exists,
    otherwise `<python> -m reversehire.mcp_server`.
    """
    python = Path(executable or sys.executable)
    folder = python.parent
    for name in (f"{SCRIPT_NAME}.exe", SCRIPT_NAME):
        script = folder / name
        if script.is_file():
            return str(script), []
    return str(_console_python(python)), ["-m", SERVER_MODULE]


def claude_snippet(command: str, args: list[str]) -> str:
    entry = {"command": command, "args": list(args), "timeout": TOOL_TIMEOUT_MS}
    return json.dumps({"mcpServers": {SERVER_KEY: entry}}, indent=2)


def gemini_snippet(command: str, args: list[str]) -> str:
    entry = {"command": command, "args": list(args), "timeout": TOOL_TIMEOUT_MS}
    return json.dumps({"mcpServers": {SERVER_KEY: entry}}, indent=2)


def _toml_string(text: str) -> str:
    # JSON string escaping is valid for TOML basic strings (\\, \", \uXXXX).
    return json.dumps(text)


def codex_snippet(command: str, args: list[str]) -> str:
    items = ", ".join(_toml_string(a) for a in args)
    return "\n".join([
        f"[mcp_servers.{SERVER_KEY}]",
        f"command = {_toml_string(command)}",
        f"args = [{items}]",
        "startup_timeout_sec = 30",
        f"tool_timeout_sec = {TOOL_TIMEOUT_MS // 1000}",
    ])


def snippets(executable: str | None = None) -> dict[str, str]:
    command, args = server_command(executable)
    return {
        "claude": claude_snippet(command, args),
        "codex": codex_snippet(command, args),
        "gemini": gemini_snippet(command, args),
    }


def mcp_package_installed() -> bool:
    try:
        return importlib.util.find_spec("mcp") is not None
    except (ImportError, ValueError):
        return False


def last_used_text(usage: dict) -> str:
    when = usage.get("last_used")
    if not when:
        return t("mcp.never_used")
    try:
        stamp = datetime.fromtimestamp(float(when)).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return t("mcp.never_used")
    return t("mcp.last_used", when=stamp, calls=int(usage.get("calls") or 0))


class McpDialog(QDialog):
    def __init__(self, parent: QWidget | None, enabled: bool, on_toggle: Callable[[bool], None],
                 usage_reader: Callable[[], dict] = config.read_mcp_usage,
                 executable: str | None = None):
        super().__init__(parent)
        self.setModal(True)
        self._on_toggle = on_toggle
        self._usage_reader = usage_reader
        self.snippets = snippets(executable)

        self.dot = StatusDot()
        self.dot.setEnabled(True)
        self.dot.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.dot.setCursor(Qt.CursorShape.ArrowCursor)
        self.switch = QCheckBox()
        self.switch.setChecked(enabled)
        self.switch.toggled.connect(self._toggled)
        top = QHBoxLayout()
        top.addWidget(self.switch, 1)
        top.addWidget(self.dot)
        self.switch_note = wrapped_label(role="muted")
        self.last_used_label = wrapped_label()
        self.package_label = wrapped_label(role="warning")
        self.package_label.setVisible(not mcp_package_installed())
        self.privacy_label = wrapped_label()
        self.howto_label = wrapped_label(role="muted")

        self.tabs = QTabWidget()
        self.snippet_fields: dict[str, QPlainTextEdit] = {}
        self.file_labels: dict[str, QLabel] = {}
        self.copy_buttons: dict[str, QPushButton] = {}
        self.copied_labels: dict[str, QLabel] = {}
        for client in CLIENTS:
            page = QWidget()
            page_layout = QVBoxLayout(page)
            file_label = wrapped_label(role="muted")
            file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            field = QPlainTextEdit(self.snippets[client])
            field.setReadOnly(True)
            field.setProperty("role", "code")
            field.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            lines = self.snippets[client].count("\n") + 2
            field.setMinimumHeight(field.fontMetrics().lineSpacing() * lines + 16)
            copy = QPushButton()
            copy.clicked.connect(lambda _checked=False, c=client: self.copy(c))
            copied = QLabel()
            copied.setProperty("role", "success")
            row = QHBoxLayout()
            row.addStretch(1)
            row.addWidget(copied)
            row.addWidget(copy)
            page_layout.addWidget(file_label)
            page_layout.addWidget(field, 1)
            page_layout.addLayout(row)
            self.tabs.addTab(page, CLIENT_NAMES[client])
            self.snippet_fields[client] = field
            self.file_labels[client] = file_label
            self.copy_buttons[client] = copy
            self.copied_labels[client] = copied

        self.close_button = QPushButton()
        self.close_button.setProperty("role", "accent")
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.switch_note)
        layout.addWidget(self.last_used_label)
        layout.addWidget(self.package_label)
        layout.addSpacing(4)
        layout.addWidget(self.privacy_label)
        layout.addSpacing(4)
        layout.addWidget(self.howto_label)
        layout.addWidget(self.tabs, 1)
        layout.addLayout(bottom)

        self.apply_palette(theme.active_palette())
        self.retranslate()
        width = max(self.sizeHint().width(), self.fontMetrics().averageCharWidth() * 100)
        height = self.heightForWidth(width) if self.hasHeightForWidth() else self.sizeHint().height()
        self.resize(width, max(height, self.sizeHint().height()))

    # ------------------------------------------------------------------ state

    @property
    def enabled(self) -> bool:
        return self.switch.isChecked()

    def _toggled(self, on: bool) -> None:
        self._on_toggle(on)
        self._update_state()

    def set_enabled(self, on: bool) -> None:
        """Follows a change made elsewhere, without calling back."""
        if self.switch.isChecked() != on:
            self.switch.blockSignals(True)
            self.switch.setChecked(on)
            self.switch.blockSignals(False)
        self._update_state()

    def apply_palette(self, palette) -> None:
        off, on = theme.status_colours(palette)
        self.dot.set_colours(off, on)

    def _update_state(self) -> None:
        on = self.switch.isChecked()
        self.dot.set_state(on, t("mcp.on") if on else t("mcp.off"),
                           t("mcp.tooltip_on") if on else t("mcp.tooltip_off"))
        try:
            usage = self._usage_reader()
        except Exception:
            usage = {}
        self.last_used_label.setText(last_used_text(usage))

    def refresh(self) -> None:
        self._update_state()

    # ------------------------------------------------------------------ copy

    def copy(self, client: str) -> None:
        QApplication.clipboard().setText(self.snippets[client])
        for name, label in self.copied_labels.items():
            label.setText(t("common.copied") if name == client else "")

    # ------------------------------------------------------------------ texts

    def retranslate(self) -> None:
        self.setWindowTitle(t("mcp.title"))
        self.switch.setText(t("mcp.switch"))
        self.switch_note.setText(t("mcp.switch_note"))
        self.package_label.setText(t("mcp.package_missing"))
        self.privacy_label.setText(t("mcp.privacy"))
        self.howto_label.setText(t("mcp.howto"))
        for client in CLIENTS:
            self.file_labels[client].setText(t(f"mcp.file_{client}"))
            self.copy_buttons[client].setText(t("common.copy"))
            if self.copied_labels[client].text():
                self.copied_labels[client].setText(t("common.copied"))
        self.close_button.setText(t("common.close"))
        self._update_state()
