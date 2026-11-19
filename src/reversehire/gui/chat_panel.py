"""The assistant strip, always visible at the bottom of the main window.

One row: the conversation toggle, the assistant (Claude Code / Codex), the question
field, Send, Stop, New conversation and the assistant settings; under it a status line
(and, when the assistant cannot answer, the specific hint with a button to the
settings page). Expanding the strip shows the conversation: the questions, the answer
streamed as it arrives and a small notice when a ReverseHire tool is used.

The questions are answered by the user's own installed CLI (`chat.ChatSession`), which
reads the app's data through its MCP server; the notice that it counts against the
user's own plan is shown once. ChatSession calls its callbacks on a worker thread:
`_Bridge` turns them into queued Qt signals, so the widgets are only touched on the GUI
thread. Detection (which runs the CLI's `--version` and sign-in status, never a model)
runs on a worker with a deadline, the first time the strip is used. Every safety rule
of the chat package (tools, permissions, working folder) is left to that package.
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLineEdit, QPushButton, QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

from .. import chat, config, i18n
from ..chat.messages import CLI_NAMES
from ..config import ASSISTANTS, DEFAULT_ASSISTANT
from ..i18n import t
from .common import error_message, run_job
from .theme import Palette, active_palette, set_role
from .widgets import ElidedLabel

log = logging.getLogger(__name__)

CHECK_TIMEOUT_S = 90.0
CONVERSATION_LINES = 8    # height of the open conversation, in text lines


def default_cli_checker(kind: str, override: str | None):
    """`chat.check_cli` (runs the CLI's --version and sign-in status: call it on a worker).
    The test suite replaces this function: no test ever starts a real CLI."""
    return chat.check_cli(kind, override or None)


class _Bridge(QObject):
    text = Signal(str)
    tool = Signal(object)
    notice = Signal(object)
    done = Signal(object)
    error = Signal(object)


class ChatPanel(QWidget):
    def __init__(self, settings_provider: Callable[[], object], run_in_background: Callable, *,
                 save_settings: Callable[[], None] | None = None,
                 on_open_settings: Callable[[], None] | None = None,
                 mcp_enabled: Callable[[], bool] | None = None,
                 cli_checker: Callable | None = None, mcp_checker: Callable | None = None,
                 session_factory: Callable | None = None, data_dir: Callable[[], object] | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("chatPanel")
        self._settings_provider = settings_provider
        self._run = run_in_background
        self._save_settings = save_settings or (lambda: None)
        self._on_open_settings = on_open_settings or (lambda: None)
        self._mcp_enabled = mcp_enabled or (lambda: bool(getattr(self.settings(), "mcp_enabled", False)))
        self._cli_checker = cli_checker
        self._mcp_checker = mcp_checker or (lambda enabled: chat.check_mcp(enabled))
        self._session_factory = session_factory or chat.ChatSession
        self._data_dir = data_dir or config.data_folder
        self._palette: Palette = active_palette()
        self.cli_info = None
        self.mcp_status = None
        self.session = None
        self.checking = False
        self.answering = False
        self._streamed = False
        self._fresh_block = False
        self._pending = ""
        self._entries: list[list] = []
        self._status_key: tuple[str, dict] = ("chat.status.unknown", {})
        self._closed = False

        self._bridge = _Bridge(self)
        self._bridge.text.connect(self._on_text)
        self._bridge.tool.connect(self._on_tool)
        self._bridge.notice.connect(self._on_notice)
        self._bridge.done.connect(self._on_done)
        self._bridge.error.connect(self._on_error)

        self.toggle_button = QToolButton()
        self.toggle_button.setObjectName("chatToggle")
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_button.clicked.connect(lambda: self.set_expanded(not self.expanded))
        self.assistant_combo = QComboBox()
        for kind in ASSISTANTS:
            self.assistant_combo.addItem(CLI_NAMES.get(kind, kind), kind)
        self._select_assistant(self.kind())
        self.assistant_combo.currentIndexChanged.connect(lambda _i: self._assistant_changed())
        self.input = QLineEdit()
        self.input.setObjectName("chatInput")
        self.input.setClearButtonEnabled(True)
        self.input.returnPressed.connect(lambda: self.send())
        self.input.textChanged.connect(lambda _text: self._update_buttons())
        self.send_button = QPushButton()
        self.send_button.setProperty("role", "accent")
        self.send_button.clicked.connect(lambda: self.send())
        self.stop_button = QPushButton()
        self.stop_button.clicked.connect(lambda: self.stop())
        self.new_button = QToolButton()
        self.new_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_button.clicked.connect(lambda: self.new_conversation())
        self.settings_button = QToolButton()
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_button.clicked.connect(lambda: self._on_open_settings())
        self.status_label = ElidedLabel()
        set_role(self.status_label, "muted")
        self.hint_label = ElidedLabel()
        set_role(self.hint_label, "warning")
        self.hint_button = QPushButton()
        self.hint_button.setFlat(True)
        self.hint_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hint_button.clicked.connect(lambda: self._on_open_settings())
        self.conversation = QTextBrowser()
        self.conversation.setObjectName("chatConversation")
        self.conversation.setOpenExternalLinks(False)
        self.conversation.setOpenLinks(False)
        self.conversation.setMinimumHeight(self.fontMetrics().lineSpacing() * 6)
        self.conversation.setFixedHeight(self.fontMetrics().lineSpacing() * CONVERSATION_LINES + 12)

        for button, icon in ((self.new_button, "new"), (self.settings_button, "settings")):
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            button.setProperty("chatIcon", icon)
        # One row (the window must still fit a small laptop): the status, or the hint with
        # its settings button, sits between the question field and the buttons.
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.toggle_button)
        row.addWidget(self.assistant_combo)
        row.addWidget(self.input, 3)
        row.addWidget(self.status_label, 2)
        row.addWidget(self.hint_label, 2)
        row.addWidget(self.hint_button)
        row.addWidget(self.send_button)
        row.addWidget(self.stop_button)
        row.addWidget(self.new_button)
        row.addWidget(self.settings_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 3, 8, 3)
        layout.setSpacing(3)
        layout.addWidget(self.conversation)
        layout.addLayout(row)
        self.hint_label.hide()
        self.hint_button.hide()
        self.input.installEventFilter(self)
        self.expanded = False
        self.set_expanded(bool(getattr(self.settings(), "chat_expanded", False)), save=False)
        self.apply_palette(self._palette)
        self.retranslate()

    # --- settings and state
    def settings(self):
        try:
            return self._settings_provider()
        except Exception:  # noqa: BLE001
            return config.load()

    def kind(self) -> str:
        kind = getattr(self.settings(), "assistant", DEFAULT_ASSISTANT)
        return kind if kind in ASSISTANTS else DEFAULT_ASSISTANT

    def _select_assistant(self, kind: str) -> None:
        index = self.assistant_combo.findData(kind)
        self.assistant_combo.blockSignals(True)
        self.assistant_combo.setCurrentIndex(max(0, index))
        self.assistant_combo.blockSignals(False)

    def cli_name(self) -> str:
        return CLI_NAMES.get(self.kind(), self.kind())

    @property
    def ready(self) -> bool:
        return (self.cli_info is not None and self.cli_info.usable and self.mcp_status is not None
                and self.mcp_status.ready)

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        if self._entries:
            self._redraw()
        self.new_button.setIcon(_icon(palette.strong, "new"))
        self.settings_button.setIcon(_icon(palette.strong, "settings"))

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt name)
        from PySide6.QtCore import QEvent

        if obj is self.input and event.type() == QEvent.Type.FocusIn and self.cli_info is None:
            self.check()  # the first use of the strip: find the assistant (worker)
        return False

    def settings_changed(self) -> None:
        """The settings were saved: another assistant or path means a new check."""
        self._select_assistant(self.kind())
        if self.session is not None and getattr(self.session.cli, "kind", None) != self.kind():
            self._drop_session()
        if self.cli_info is not None:
            self.check()

    def mcp_changed(self) -> None:
        if self.cli_info is not None:
            self.mcp_status = self._mcp_checker(bool(self._mcp_enabled()))
            self._update_state()

    def _assistant_changed(self) -> None:
        kind = self.assistant_combo.currentData() or DEFAULT_ASSISTANT
        settings = self.settings()
        if getattr(settings, "assistant", None) == kind:
            return
        settings.assistant = kind
        self._save_settings()
        self._drop_session()
        self.cli_info = None
        self.check()

    def _drop_session(self) -> None:
        if self.session is not None:
            self.session.cancel()
            self.session = None
        self.answering = False

    def set_expanded(self, on: bool, save: bool = True) -> None:
        self.expanded = bool(on)
        self.conversation.setVisible(self.expanded)
        self.toggle_button.setArrowType(Qt.ArrowType.DownArrow if self.expanded else Qt.ArrowType.UpArrow)
        self.toggle_button.setToolTip(t("chat.hide") if self.expanded else t("chat.show"))
        self.toggle_button.setAccessibleName(self.toggle_button.toolTip())
        if save:
            settings = self.settings()
            if getattr(settings, "chat_expanded", None) != self.expanded:
                settings.chat_expanded = self.expanded
                self._save_settings()

    # --- detection
    def checker(self) -> Callable:
        return self._cli_checker or default_cli_checker

    def check(self) -> bool:
        """Finds the selected CLI and checks the MCP server, on a worker."""
        if self.checking or self._closed:
            return False
        self.checking = True
        kind = self.kind()
        override = str(getattr(self.settings(), f"{kind}_path", "") or "") or None
        checker, mcp_checker, enabled = self.checker(), self._mcp_checker, bool(self._mcp_enabled())
        self._status("chat.status.checking")

        def work():
            return checker(kind, override), mcp_checker(enabled)

        run_job(self._run, work, self._checked, self._check_failed, name="assistant detection",
                timeout=CHECK_TIMEOUT_S)
        return True

    def set_detection(self, cli_info, mcp_status) -> None:
        """Results found elsewhere (the Assistant settings page)."""
        if cli_info is not None and cli_info.kind == self.kind():
            self._checked((cli_info, mcp_status))

    def _checked(self, result) -> None:
        self.checking = False
        if self._closed:
            return
        self.cli_info, self.mcp_status = result
        if self.session is not None and self.session.cli.path != self.cli_info.path:
            self._drop_session()
        self._update_state()
        pending, self._pending = self._pending, ""
        if pending and self.ready:
            self.send(pending)
        elif pending and not self.expanded:
            self.set_expanded(True)

    def _check_failed(self, error: BaseException) -> None:
        self.checking = False
        if self._closed:
            return
        self._status_text(error_message(error))
        self._update_buttons()

    def hint(self) -> str:
        """Why the assistant cannot answer ('' when it can)."""
        if self.cli_info is None:
            return ""
        if not self.cli_info.usable:
            return self.cli_info.hint
        if self.mcp_status is not None and not self.mcp_status.ready:
            return self.mcp_status.hint
        return ""

    def _update_state(self) -> None:
        hint = self.hint()
        self.hint_label.setText(hint)
        self.hint_label.setVisible(bool(hint))
        self.hint_button.setVisible(bool(hint))
        self.status_label.setVisible(not hint)
        if not self.answering:
            if hint:
                self._status_text("")
            elif self.cli_info is not None:
                self._status_text(self.cli_info.hint)
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.send_button.setEnabled(not self.answering and not self.checking and bool(self.input.text().strip())
                                    and (self.cli_info is None or self.ready))
        self.stop_button.setEnabled(self.answering)
        self.new_button.setEnabled(not self.answering)

    # --- conversation
    def send(self, text: str | None = None) -> bool:
        """Asks the question in the field (or `text`). False when it cannot be asked now."""
        question = (text if text is not None else self.input.text()).strip()
        if not question or self.answering or self._closed:
            return False
        if self.cli_info is None:
            self._pending = question  # asked as soon as the check says the assistant is ready
            self.check()
            return False
        if not self.ready:
            self._update_state()
            if not self.expanded:
                self.set_expanded(True)
            return False
        self._notice_once()
        if self.session is None:
            self.session = self._session_factory(
                self.cli_info, data_dir=self._data_dir(), language=i18n.current_language(),
                callbacks=chat.ChatCallbacks(on_text=self._bridge.text.emit, on_tool=self._bridge.tool.emit,
                                             on_notice=self._bridge.notice.emit, on_done=self._bridge.done.emit,
                                             on_error=self._bridge.error.emit))
        self.session.language = i18n.current_language()
        self.answering = True
        self._streamed = False
        self._append(t("chat.you"), question)
        self._append(self.cli_name(), "")
        self._fresh_block = False
        if self.input.text().strip() == question:
            self.input.clear()
        self._status("chat.status.answering", cli=self.cli_name())
        self._update_buttons()
        if not self.expanded:
            self.set_expanded(True)
        if not self.session.send(question):
            # refused at once: on_error has already been queued
            return False
        return True

    def stop(self) -> None:
        if self.session is not None and self.answering:
            self.session.cancel()

    def new_conversation(self) -> None:
        if self.answering:
            return
        if self.session is not None:
            self.session.reset()
        self._entries.clear()
        self.conversation.clear()
        self._status("chat.status.new")

    def _notice_once(self) -> None:
        settings = self.settings()
        if getattr(settings, "chat_notice_shown", False):
            return
        self._append("", _usage_notice(self.cli_name()), role="warning")
        settings.chat_notice_shown = True
        self._save_settings()

    # --- callbacks (GUI thread, through the bridge)
    def _on_text(self, chunk: str) -> None:
        if not chunk:
            return
        self._streamed = True
        self._insert(chunk)

    def _on_tool(self, notice) -> None:
        if notice.phase == "started":
            self._append("", t("chat.tool", tool=notice.tool), role="muted", italic=True)
            self._fresh_block = True
        elif notice.is_error:
            self._append("", t("chat.tool_failed", tool=notice.tool), role="warning", italic=True)
            self._fresh_block = True

    def _on_notice(self, notice) -> None:
        self._append("", notice.message, role="warning", italic=True)
        self._fresh_block = True

    def _on_done(self, answer) -> None:
        self.answering = False
        if not self._streamed and answer.text:
            self._insert(answer.text)
        self._status("chat.status.done", seconds=f"{answer.duration_s:.0f}")
        self._update_buttons()

    def _on_error(self, error) -> None:
        self.answering = False
        self._append("", error.message, role="error")
        self._status_text(error.message)
        if error.code == "not_logged_in" and self.cli_info is not None:
            self.cli_info = None  # signed out meanwhile: check again before the next question
        self._update_buttons()

    # --- text helpers
    def _cursor(self) -> QTextCursor:
        cursor = self.conversation.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        return cursor

    # The conversation is kept as entries [name, text, role, italic] and drawn with the
    # palette's colours, so a theme change redraws it readable in the other theme.
    def _colour(self, role: str) -> QColor:
        p = self._palette
        return QColor({"muted": p.muted, "warning": p.warning, "error": p.error}.get(role, p.strong))

    def _draw(self, cursor: QTextCursor, entry: list, new_block: bool) -> None:
        name, text, role, italic = entry
        if new_block:
            cursor.insertBlock()
        if name:
            name_format = QTextCharFormat()
            name_format.setForeground(QColor(self._palette.accent))
            name_format.setFontWeight(QFont.Weight.Bold)
            cursor.insertText(f"{name}: ", name_format)
        if text:
            body = QTextCharFormat()
            body.setForeground(self._colour(role))
            body.setFontItalic(italic)
            cursor.insertText(text, body)

    def _append(self, name: str, text: str, *, role: str = "body", italic: bool = False) -> None:
        entry = [name, text, role, italic]
        self._entries.append(entry)
        cursor = self._cursor()
        self._draw(cursor, entry, not self.conversation.document().isEmpty())
        self.conversation.setTextCursor(cursor)
        self.conversation.ensureCursorVisible()

    def _insert(self, text: str) -> None:
        """Streamed answer text: goes on in the last paragraph (a new one after a notice)."""
        if self._fresh_block or not self._entries:
            self._fresh_block = False
            self._append("", text)
            return
        self._entries[-1][1] += text
        cursor = self._cursor()
        body = QTextCharFormat()
        body.setForeground(self._colour(self._entries[-1][2]))
        cursor.insertText(text, body)
        self.conversation.setTextCursor(cursor)
        self.conversation.ensureCursorVisible()

    def _redraw(self) -> None:
        self.conversation.clear()
        cursor = self._cursor()
        for index, entry in enumerate(self._entries):
            self._draw(cursor, entry, index > 0)
        self.conversation.setTextCursor(cursor)
        self.conversation.ensureCursorVisible()

    def plain_text(self) -> str:
        return self.conversation.toPlainText()

    def _status(self, key: str, **params) -> None:
        self._status_key = (key, params)
        self.status_label.setText(t(key, **params))

    def _status_text(self, text: str) -> None:
        self._status_key = ("", {"text": text})
        self.status_label.setText(text)

    def status_text(self) -> str:
        return self.status_label.text()

    # --- texts
    def retranslate(self) -> None:
        self.toggle_button.setText(t("chat.title"))
        self.set_expanded(self.expanded, save=False)
        self.input.setPlaceholderText(t("chat.placeholder"))
        self.input.setAccessibleName(t("chat.placeholder"))
        self.send_button.setText(t("chat.send"))
        self.stop_button.setText(t("chat.stop"))
        self.new_button.setText(t("chat.new"))
        self.new_button.setToolTip(t("chat.new"))
        self.settings_button.setText(t("chat.settings"))
        self.settings_button.setToolTip(t("chat.settings"))
        self.hint_button.setText(t("chat.settings"))
        key, params = self._status_key
        if key:
            self.status_label.setText(t(key, **params))
        if self.cli_info is not None:
            self._update_state()
        self._update_buttons()

    def shutdown(self) -> None:
        """Stops a running answer without waiting (the CLI's process tree is killed)."""
        self._closed = True
        if self.session is not None:
            try:
                self.session.cancel()
            except Exception:  # noqa: BLE001
                log.debug("Chat cancel failed", exc_info=True)


def _icon(colour: str, kind: str, size: int = 14) -> QIcon:
    """Small drawn icons (the bundled font has no such glyphs): "new" is a plus in a
    speech bubble, "settings" three sliders."""
    icon = QIcon()
    for ratio in (1.0, 2.0):
        pixmap = QPixmap(round(size * ratio), round(size * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(colour))
        pen.setWidthF(1.4)
        painter.setPen(pen)
        if kind == "new":
            painter.drawRoundedRect(QRectF(1.5, 1.5, size - 3, size - 5), 2.5, 2.5)
            painter.drawLine(QPointF(size / 2, 4.0), QPointF(size / 2, size - 6.5))
            painter.drawLine(QPointF(size / 2 - 3, size / 2 - 1.2), QPointF(size / 2 + 3, size / 2 - 1.2))
        else:
            for i, knob in enumerate((0.3, 0.65, 0.45)):
                y = 3.0 + i * (size - 6) / 2
                painter.drawLine(QPointF(1.5, y), QPointF(size - 1.5, y))
                painter.setBrush(QColor(colour))
                painter.drawEllipse(QPointF(1.5 + knob * (size - 3), y), 1.8, 1.8)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def _usage_notice(cli: str) -> str:
    """"Questions are answered by your installed … and count against your own plan."""
    from ..chat.messages import text

    return text("chat.notice.usage", cli=cli)
