"""The "Mail" and "Assistant and MCP" pages of the settings dialog.

MailPage: the fixed presentation subject and text (placeholder help, a warning for
unknown placeholders), the user's own name / e-mail / phone, the PDF to attach (default:
the primary CV of "My CV"), how to open the draft (the methods found on this computer,
with their explanations; "Automatic" by default) and "Test draft", which opens a draft
to the user's own address. Detection and the test run on a worker.

AssistantPage: which installed CLI answers in the chat strip, optional explicit paths,
the detection state of each CLI (found / version / signed in) with "Check again" on a
worker, and the MCP switch with the result of `chat.check_mcp`.
"""

from __future__ import annotations

import logging
import sys
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from .. import chat, i18n, mail
from ..chat.messages import CLI_NAMES
from ..config import ASSISTANTS, DEFAULT_ASSISTANT
from ..i18n import t
from . import mail_support, theme
from .common import error_message, run_job
from .theme import section, set_role
from .widgets import StatusDot, wrapped_label

log = logging.getLogger(__name__)

CHECK_TIMEOUT_S = 90.0


def _title(text: str) -> QLabel:
    label = QLabel(section(text))
    label.setProperty("role", "title")
    return label


# --- Mail ------------------------------------------------------------------------------------

class MailPage(QWidget):
    def __init__(self, settings, run_in_background: Callable, *, primary_cv: Callable[[], str] | None = None,
                 system: mail.System | None = None, environment=None, parent: QWidget | None = None):
        super().__init__(parent)
        self._run = run_in_background
        self._primary_cv = primary_cv or (lambda: "")
        self._system = system
        self.environment = environment
        self.testing = False
        self.outcome: mail.Outcome | None = None
        current = mail_support.mail_settings_of(settings)
        if not current.attachment_path:
            current.attachment_path = self._primary()

        self.intro = wrapped_label(t("settings.mail.intro"), "muted")
        self.subject_field = QLineEdit(current.subject_template)
        self.body_field = QPlainTextEdit(current.body_template)
        self.body_field.setMinimumHeight(self.fontMetrics().lineSpacing() * 9)
        self.body_field.setTabChangesFocus(True)
        names = ", ".join("{" + name + "}" for name in mail.PLACEHOLDERS)
        self.placeholders_label = wrapped_label(t("settings.mail.placeholders", names=names,
                                                  fallback="{contact_name|…}", optional="[[ … ]]"), "muted")
        self.unknown_label = wrapped_label(role="warning")
        self.unknown_label.hide()
        self.defaults_button = QPushButton(t("settings.mail.defaults"))
        self.defaults_button.clicked.connect(lambda: self.reset_templates())
        for widget in (self.subject_field,):
            widget.textChanged.connect(lambda _text: self._check_placeholders())
        self.body_field.textChanged.connect(self._check_placeholders)

        self.name_field = QLineEdit(current.my_name)
        self.email_field = QLineEdit(current.my_email)
        self.phone_field = QLineEdit(current.my_phone)
        self.attachment_field = QLineEdit(current.attachment_path)
        self.attachment_field.textChanged.connect(lambda _text: self._check_attachment())
        self.browse_button = QPushButton(t("common.browse"))
        self.browse_button.clicked.connect(lambda: self.browse())
        self.primary_button = QPushButton(t("settings.mail.use_primary"))
        self.primary_button.clicked.connect(lambda: self.use_primary())
        self.attachment_note = wrapped_label(t("settings.mail.attachment_default"), "muted")

        self.method_combo = QComboBox()
        self.method_combo.currentIndexChanged.connect(lambda _i: self._explain())
        self.method_explanation = wrapped_label(role="muted")
        self._saved_method = current.method
        self._choices: list[mail_support.MethodChoice] = []
        self.test_button = QPushButton(t("settings.mail.test"))
        self.test_button.clicked.connect(lambda: self.test_draft())
        self.test_note = wrapped_label(t("settings.mail.test_note"), "muted")
        self.test_result = wrapped_label()
        self.test_result.hide()

        grid = QGridLayout()
        row = 0
        grid.addWidget(QLabel(t("settings.mail.subject")), row, 0)
        grid.addWidget(self.subject_field, row, 1, 1, 3)
        row += 1
        grid.addWidget(QLabel(t("settings.mail.body")), row, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self.body_field, row, 1, 1, 3)
        row += 1
        grid.addWidget(self.placeholders_label, row, 1, 1, 2)
        grid.addWidget(self.defaults_button, row, 3, Qt.AlignmentFlag.AlignTop)
        row += 1
        grid.addWidget(self.unknown_label, row, 1, 1, 3)
        row += 1
        for key, widget in (("settings.mail.my_name", self.name_field), ("settings.mail.my_email", self.email_field),
                            ("settings.mail.my_phone", self.phone_field)):
            grid.addWidget(QLabel(t(key)), row, 0)
            grid.addWidget(widget, row, 1, 1, 3)
            row += 1
        grid.addWidget(QLabel(t("settings.mail.attachment")), row, 0)
        grid.addWidget(self.attachment_field, row, 1)
        grid.addWidget(self.browse_button, row, 2)
        grid.addWidget(self.primary_button, row, 3)
        row += 1
        grid.addWidget(self.attachment_note, row, 1, 1, 3)
        row += 1
        grid.addWidget(QLabel(t("settings.mail.method")), row, 0)
        grid.addWidget(self.method_combo, row, 1, 1, 3)
        row += 1
        grid.addWidget(self.method_explanation, row, 1, 1, 3)
        row += 1
        test_row = QHBoxLayout()
        test_row.addWidget(self.test_button)
        test_row.addWidget(self.test_note, 1)
        grid.addLayout(test_row, row, 1, 1, 3)
        row += 1
        grid.addWidget(self.test_result, row, 1, 1, 3)
        grid.setColumnStretch(1, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.intro)
        layout.addLayout(grid)
        layout.addStretch(1)
        self._fill_methods()
        self._check_placeholders()
        self._check_attachment()
        if self.environment is None:
            self.detect()

    # --- values
    def mail_settings(self) -> mail.MailSettings:
        return mail.MailSettings(
            subject_template=self.subject_field.text(), body_template=self.body_field.toPlainText(),
            attachment_path=self.attachment_field.text().strip().strip('"'), my_name=self.name_field.text().strip(),
            my_email=self.email_field.text().strip(), my_phone=self.phone_field.text().strip(),
            method=self.method_combo.currentData() or "")

    def values(self) -> dict:
        """What is saved (config.Settings.mail). A template equal to the default text of
        the interface language is saved as "": it then follows a later language change."""
        values = self.mail_settings().to_dict()
        defaults = mail.MailSettings.defaults_for(i18n.current_language())
        if values["subject_template"] == defaults.subject_template:
            values["subject_template"] = ""
        if values["body_template"] == defaults.body_template:
            values["body_template"] = ""
        return values

    # --- templates
    def reset_templates(self) -> None:
        defaults = mail.MailSettings.defaults_for(i18n.current_language())
        self.subject_field.setText(defaults.subject_template)
        self.body_field.setPlainText(defaults.body_template)

    def unknown_names(self) -> list[str]:
        names: list[str] = []
        for template in (self.subject_field.text(), self.body_field.toPlainText()):
            for name in mail.unknown_placeholders(template):
                if name not in names:
                    names.append(name)
        return names

    def _check_placeholders(self) -> None:
        unknown = self.unknown_names()
        self.unknown_label.setText(t("settings.mail.unknown", names=", ".join("{" + n + "}" for n in unknown))
                                   if unknown else "")
        self.unknown_label.setVisible(bool(unknown))

    # --- attachment
    def _primary(self) -> str:
        try:
            return self._primary_cv() or ""
        except Exception:  # noqa: BLE001
            return ""

    def use_primary(self) -> bool:
        path = self._primary()
        if not path:
            self.attachment_note.setText(t("settings.mail.no_primary"))
            set_role(self.attachment_note, "warning")
            return False
        self.attachment_field.setText(path)
        return True

    def browse(self) -> str:
        path, _ = QFileDialog.getOpenFileName(self, t("settings.mail.attachment"), self.attachment_field.text(),
                                              f"{t('settings.mail.pdf_filter')};;{t('file.all')} (*)",
                                              options=QFileDialog.Option.DontUseNativeDialog)
        if path:
            self.attachment_field.setText(path)
        return path or ""

    def _check_attachment(self) -> None:
        settings = self.mail_settings()
        path = settings.attachment
        if path is not None and not path.is_file():
            self.attachment_note.setText(t("settings.mail.attachment_missing", file=str(path)))
            set_role(self.attachment_note, "warning")
        else:
            self.attachment_note.setText(t("settings.mail.attachment_default"))
            set_role(self.attachment_note, "muted")

    # --- methods
    def system(self) -> mail.System:
        if self._system is None:
            self._system = mail_support.gui_system()
        return self._system

    def _fill_methods(self) -> None:
        current = self.method_combo.currentData() if self.method_combo.count() else self._saved_method
        self._choices = mail_support.method_choices(self.environment, self.system())
        if self.environment is None and self._saved_method:
            self._choices.append(mail_support.MethodChoice(self._saved_method,
                                                           mail_support.method_label(self._saved_method), ""))
        self.method_combo.blockSignals(True)
        self.method_combo.clear()
        for choice in self._choices:
            self.method_combo.addItem(choice.label, choice.method_id)
        index = self.method_combo.findData(current or "")
        if index < 0 and current:  # saved method not available here any more: keep it visible
            self.method_combo.addItem(mail_support.method_label(current), current)
            self._choices.append(mail_support.MethodChoice(current, mail_support.method_label(current), ""))
            index = self.method_combo.count() - 1
        self.method_combo.setCurrentIndex(max(0, index))
        self.method_combo.blockSignals(False)
        self._explain()

    def _explain(self) -> None:
        index = self.method_combo.currentIndex()
        if self.environment is None:
            self.method_explanation.setText(t("settings.mail.detecting"))
        elif 0 <= index < len(self._choices):
            self.method_explanation.setText(self._choices[index].explanation)

    def detect(self) -> None:
        def done(environment) -> None:
            self.environment = environment
            self._fill_methods()

        def failed(error) -> None:
            log.warning("Mail programs not detected: %s", type(error).__name__)
            self.environment = mail.MailEnvironment(platform="other")
            self._fill_methods()

        run_job(self._run, mail_support.detect_job(), done, failed, name="mail program detection",
                timeout=mail_support.DETECT_TIMEOUT_S)

    # --- test
    def test_draft(self) -> bool:
        """A draft to the user's own address (or without recipient), on a worker."""
        if self.testing:
            return False
        settings = self.mail_settings()
        own = settings.my_email if mail.is_plausible_email(settings.my_email) else ""
        draft = mail.build_draft(settings, company=t("settings.mail.test_company"), to=own)
        self.testing = True
        self.test_button.setEnabled(False)
        self._test_text(t("mail.dialog.opening"), "muted")

        def done(outcome: mail.Outcome) -> None:
            self.testing = False
            self.test_button.setEnabled(True)
            self.outcome = outcome
            self._test_text(mail_support.outcome_text(outcome), None if outcome.ok else "warning")

        def failed(error: BaseException) -> None:
            self.testing = False
            self.test_button.setEnabled(True)
            self._test_text(error_message(error), "error")

        run_job(self._run, mail_support.open_draft_job(draft, settings.method, self.environment, self.system(),
                                                       sender=own),
                done, failed, name="test mail draft", timeout=mail_support.MAIL_TIMEOUT_S)
        return True

    def _test_text(self, text: str, role: str | None) -> None:
        set_role(self.test_result, role)
        self.test_result.setText(text)
        self.test_result.show()


# --- Assistant and MCP ----------------------------------------------------------------------

def default_cli_checker(kind: str, override: str | None):
    """Detection of one CLI (runs `--version` and the sign-in status: on a worker)."""
    from . import chat_panel

    return chat_panel.default_cli_checker(kind, override)


class AssistantPage(QWidget):
    def __init__(self, settings, run_in_background: Callable, *, mcp_enabled: Callable[[], bool],
                 on_mcp_toggle: Callable[[bool], None] | None = None,
                 on_mcp_details: Callable[[], None] | None = None,
                 cli_checker: Callable | None = None, mcp_checker: Callable | None = None,
                 on_checked: Callable[[dict], None] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self._run = run_in_background
        self._mcp_enabled = mcp_enabled
        self._on_mcp_toggle = on_mcp_toggle or (lambda _on: None)
        self._on_mcp_details = on_mcp_details
        self._cli_checker = cli_checker
        self._mcp_checker = mcp_checker or (lambda enabled: chat.check_mcp(enabled))
        self._on_checked = on_checked or (lambda _infos: None)
        self.checking = False
        self.checked_once = False
        self.infos: dict = {}

        self.intro = wrapped_label(t("settings.assistant.intro"), "muted")
        self.assistant_combo = QComboBox()
        for kind in ASSISTANTS:
            self.assistant_combo.addItem(CLI_NAMES.get(kind, kind), kind)
        index = self.assistant_combo.findData(getattr(settings, "assistant", DEFAULT_ASSISTANT))
        self.assistant_combo.setCurrentIndex(max(0, index))
        self.path_fields: dict[str, QLineEdit] = {}
        self.browse_buttons: dict[str, QPushButton] = {}
        self.status_labels: dict[str, QLabel] = {}
        grid = QGridLayout()
        row = 0
        grid.addWidget(QLabel(t("settings.assistant.which")), row, 0)
        grid.addWidget(self.assistant_combo, row, 1, 1, 2)
        row += 1
        for kind in ASSISTANTS:
            field = QLineEdit(str(getattr(settings, f"{kind}_path", "") or ""))
            field.setPlaceholderText(t("settings.assistant.path_placeholder"))
            browse = QPushButton(t("common.browse"))
            browse.clicked.connect(lambda _checked=False, k=kind: self.browse(k))
            status = wrapped_label(t("settings.assistant.not_checked"), "muted")
            grid.addWidget(QLabel(t("settings.assistant.path", cli=CLI_NAMES.get(kind, kind))), row, 0)
            grid.addWidget(field, row, 1)
            grid.addWidget(browse, row, 2)
            row += 1
            grid.addWidget(status, row, 1, 1, 2)
            row += 1
            self.path_fields[kind] = field
            self.browse_buttons[kind] = browse
            self.status_labels[kind] = status
        self.check_button = QPushButton(t("settings.assistant.check"))
        self.check_button.clicked.connect(lambda: self.check())
        check_row = QHBoxLayout()
        check_row.addWidget(self.check_button)
        check_row.addStretch(1)
        grid.addLayout(check_row, row, 1, 1, 2)
        row += 1
        grid.setColumnStretch(1, 1)

        self.mcp_title = _title(t("settings.assistant.mcp_title"))
        self.mcp_switch = QCheckBox(t("mcp.switch"))
        self.mcp_switch.setChecked(bool(mcp_enabled()))
        self.mcp_switch.toggled.connect(self._mcp_toggled)
        self.mcp_dot = StatusDot()
        self.mcp_dot.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.mcp_dot.setCursor(Qt.CursorShape.ArrowCursor)
        self.mcp_status_label = wrapped_label(role="muted")
        self.mcp_details_button = QPushButton(t("settings.assistant.mcp_details"))
        self.mcp_details_button.clicked.connect(lambda: self._details())
        self.mcp_details_button.setVisible(on_mcp_details is not None)
        mcp_row = QHBoxLayout()
        mcp_row.addWidget(self.mcp_switch, 1)
        mcp_row.addWidget(self.mcp_dot)
        details_row = QHBoxLayout()
        details_row.addWidget(self.mcp_status_label, 1)
        details_row.addWidget(self.mcp_details_button, 0, Qt.AlignmentFlag.AlignTop)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.intro)
        layout.addLayout(grid)
        layout.addSpacing(6)
        layout.addWidget(self.mcp_title)
        layout.addLayout(mcp_row)
        layout.addLayout(details_row)
        layout.addStretch(1)
        self.apply_palette(theme.active_palette())
        self.set_mcp_enabled(bool(mcp_enabled()))

    def values(self) -> dict:
        return {"assistant": self.assistant_combo.currentData() or DEFAULT_ASSISTANT,
                **{f"{kind}_path": field.text().strip().strip('"') for kind, field in self.path_fields.items()}}

    def apply_palette(self, palette) -> None:
        self.mcp_dot.set_colours(*theme.status_colours(palette))

    def showEvent(self, event) -> None:  # noqa: N802 (Qt name)
        super().showEvent(event)
        if not self.checked_once and not self.checking:
            self.check()

    # --- CLIs
    def browse(self, kind: str) -> str:
        programs = "*.exe *.cmd *.bat" if sys.platform == "win32" else "*"
        path, _ = QFileDialog.getOpenFileName(
            self, t("settings.assistant.path", cli=CLI_NAMES.get(kind, kind)), self.path_fields[kind].text(),
            f"{t('settings.assistant.programs_filter')} ({programs});;{t('file.all')} (*)",
            options=QFileDialog.Option.DontUseNativeDialog)
        if path:
            self.path_fields[kind].setText(path)
        return path or ""

    def checker(self) -> Callable:
        return self._cli_checker or default_cli_checker

    def check(self) -> bool:
        """Detects both CLIs (and the MCP server) on a worker: commands run, never a model."""
        if self.checking:
            return False
        self.checking = True
        self.checked_once = True
        self.check_button.setEnabled(False)
        for label in self.status_labels.values():
            label.setText(t("settings.assistant.checking"))
            set_role(label, "muted")
        overrides = {kind: field.text().strip().strip('"') or None for kind, field in self.path_fields.items()}
        checker, mcp_checker, enabled = self.checker(), self._mcp_checker, bool(self._mcp_enabled())

        def work() -> dict:
            infos = {kind: checker(kind, overrides[kind]) for kind in ASSISTANTS}
            infos["mcp"] = mcp_checker(enabled)
            return infos

        run_job(self._run, work, self._checked, self._check_failed, name="assistant detection",
                timeout=CHECK_TIMEOUT_S)
        return True

    def _checked(self, infos: dict) -> None:
        self.checking = False
        self.check_button.setEnabled(True)
        self.infos = infos
        for kind in ASSISTANTS:
            info = infos.get(kind)
            label = self.status_labels[kind]
            if info is None:
                continue
            label.setText(self.cli_text(info))
            set_role(label, "success" if info.status == chat.READY else
                     ("muted" if info.status == chat.FOUND else "warning"))
        self._show_mcp(infos.get("mcp"))
        self._on_checked(infos)

    def _check_failed(self, error: BaseException) -> None:
        self.checking = False
        self.check_button.setEnabled(True)
        for label in self.status_labels.values():
            label.setText(error_message(error))
            set_role(label, "error")

    @staticmethod
    def cli_text(info) -> str:
        text = info.hint or ""
        if info.path:
            text = f"{text} ({info.path})" if text else info.path
        return text

    # --- MCP
    def _mcp_toggled(self, on: bool) -> None:
        self._on_mcp_toggle(on)
        self.set_mcp_enabled(on)
        try:
            self._show_mcp(self._mcp_checker(bool(on)))  # same interpreter: a module lookup only
        except Exception:  # noqa: BLE001
            log.debug("MCP check failed", exc_info=True)

    def set_mcp_enabled(self, on: bool) -> None:
        """Follows a change made elsewhere (menu bar dot, MCP dialog) without calling back."""
        if self.mcp_switch.isChecked() != on:
            self.mcp_switch.blockSignals(True)
            self.mcp_switch.setChecked(on)
            self.mcp_switch.blockSignals(False)
        self.mcp_dot.set_state(on, t("mcp.on") if on else t("mcp.off"),
                               t("mcp.tooltip_on") if on else t("mcp.tooltip_off"))
        status = self.infos.get("mcp")
        if status is not None and status.can_start and status.ready != on:
            self._show_mcp(self._mcp_checker(on))

    def _show_mcp(self, status) -> None:
        if status is None:
            return
        self.infos["mcp"] = status
        self.mcp_status_label.setText(status.hint)
        set_role(self.mcp_status_label, "success" if status.ready else "warning")

    def _details(self) -> None:
        if self._on_mcp_details is not None:
            self._on_mcp_details()
