"""Two small windows for the owner of a PC where something does not work:

- `ModelProblemDialog`: why the scoring model could not be loaded (short cause,
  advice, expandable "Details" with the exception and the model folders, Copy);
- `DiagnosticsDialog`: runs `reversehire.diagnostics.run_checks` in a worker
  thread and shows the PASS / FAIL list, with Copy and "Open log folder".

Both are non-blocking (window-modal `open()`), so tests drive them directly.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from .. import diagnostics
from ..i18n import t
from .widgets import wrapped_label


def _monospace_text() -> QPlainTextEdit:
    text = QPlainTextEdit()
    text.setReadOnly(True)
    text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    if not font.family():
        font = QFont("Consolas")
    text.setFont(font)
    return text


def copy_to_clipboard(text: str) -> None:
    clipboard = QApplication.clipboard()
    if clipboard is not None:
        clipboard.setText(text)


def cause_message(cause: str) -> str:
    """Short cause + advice, translated (unknown causes use the "unknown" texts)."""
    key = cause if t(f"model_cause.{cause}.short") != f"model_cause.{cause}.short" else "unknown"
    return f"{t(f'model_cause.{key}.short').capitalize()}.\n\n{t(f'model_cause.{key}.advice')}"


class ModelProblemDialog(QDialog):
    def __init__(self, parent: QWidget | None, cause: str, details: str,
                 on_diagnostics: Callable[[], None] | None = None):
        super().__init__(parent)
        self.cause = cause
        self.details = details or ""
        self._on_diagnostics = on_diagnostics
        self.setWindowTitle(t("model_problem.title"))
        self.intro_label = wrapped_label(t("model_problem.intro"))
        self.cause_label = wrapped_label(cause_message(cause))
        self.cause_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.retry_label = wrapped_label(t("model_problem.retry_note"), role="muted")
        self.details_button = QPushButton(t("model_problem.details"))
        self.details_button.setCheckable(True)
        self.details_button.toggled.connect(self._toggle_details)
        self.details_text = _monospace_text()
        self.details_text.setPlainText(self.details)
        self.details_text.setVisible(False)
        self.copy_button = QPushButton(t("common.copy"))
        self.copy_button.clicked.connect(self.copy)
        self.copied_label = QLabel()
        self.copied_label.setProperty("role", "muted")
        self.diagnostics_button = QPushButton(t("model_problem.diagnostics"))
        self.diagnostics_button.clicked.connect(self._diagnostics)
        self.diagnostics_button.setEnabled(on_diagnostics is not None)
        self.close_button = QPushButton(t("common.close"))
        self.close_button.clicked.connect(self.accept)

        buttons = QHBoxLayout()
        for button in (self.details_button, self.copy_button):
            buttons.addWidget(button)
        buttons.addWidget(self.copied_label, 1)
        buttons.addWidget(self.diagnostics_button)
        buttons.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.intro_label)
        layout.addWidget(self.cause_label)
        layout.addWidget(self.retry_label)
        layout.addWidget(self.details_text, 1)
        layout.addLayout(buttons)
        self.resize(640, 360)

    def _toggle_details(self, on: bool) -> None:
        self.details_text.setVisible(on)

    def text_for_copy(self) -> str:
        return f"{t('model_problem.title')}\n{self.cause_label.text()}\n\n{self.details}"

    def copy(self) -> None:
        copy_to_clipboard(self.text_for_copy())
        self.copied_label.setText(t("common.copied"))

    def _diagnostics(self) -> None:
        if self._on_diagnostics is not None:
            self._on_diagnostics()


class DiagnosticsDialog(QDialog):
    """Runs the checks with `run_in_background(function, on_success, on_error)`."""

    def __init__(self, parent: QWidget | None, run_in_background: Callable,
                 runner: Callable[[], list] | None = None,
                 on_open_log: Callable[[], None] | None = None):
        super().__init__(parent)
        self._run = run_in_background
        self._runner = runner or diagnostics.run_checks
        self._on_open_log = on_open_log
        self.checks: list = []
        self.running = False
        self.setWindowTitle(t("diagnostics.title"))
        self.intro_label = wrapped_label(t("diagnostics.intro"), role="muted")
        self.status_label = wrapped_label()
        self.text = _monospace_text()
        self.again_button = QPushButton(t("diagnostics.run_again"))
        self.again_button.clicked.connect(self.start)
        self.copy_button = QPushButton(t("common.copy"))
        self.copy_button.clicked.connect(self.copy)
        self.log_button = QPushButton(t("diagnostics.open_log"))
        self.log_button.clicked.connect(lambda: self._on_open_log and self._on_open_log())
        self.log_button.setEnabled(on_open_log is not None)
        self.close_button = QPushButton(t("common.close"))
        self.close_button.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        for button in (self.again_button, self.copy_button, self.log_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.intro_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.text, 1)
        layout.addLayout(buttons)
        self.resize(820, 520)

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.again_button.setEnabled(False)
        self.status_label.setText(t("diagnostics.running"))
        self._run(self._runner, self.show_results, self._failed)

    def show_results(self, checks: list) -> None:
        self.running = False
        self.again_button.setEnabled(True)
        self.checks = list(checks or [])
        failed, warnings = diagnostics.counts(self.checks)
        self.status_label.setText(t("diagnostics.done", failed=failed, warnings=warnings))
        self.text.setPlainText(diagnostics.as_text(self.checks))

    def _failed(self, error: BaseException) -> None:
        self.show_results([diagnostics.Check("diagnostics", diagnostics.FAIL,
                                             f"{type(error).__name__}: {error}")])

    def report(self) -> str:
        return self.text.toPlainText()

    def copy(self) -> None:
        copy_to_clipboard(f"{t('diagnostics.title')}\n{self.report()}")
        self.status_label.setText(t("common.copied"))
