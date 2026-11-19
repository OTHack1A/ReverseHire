"""Settings of the idle analysis and of the assistant suggestions, on the settings page
"background" (gui/settings_registry.py).

Idle analysis (``background.IdleScheduler``, owned by the main window, gui/window_interest.py):
``idle_enabled``, ``idle_minutes`` (how long without input before it starts),
``idle_daily_budget`` (requests per day), ``idle_only_on_mains`` (never on battery). The pause
switch (``idle_paused``) is in the status bar and the Tools menu, not here.

Assistant suggestions (gui/suggestions_view.py): ``suggest_allow_web_search`` (off by
default: see suggest/__init__.py for why) and a way to show the "what is sent" notice again
(``suggest_notice_shown``).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QGridLayout, QGroupBox, QLabel, QSpinBox, QVBoxLayout, QWidget

from ..i18n import t
from .settings_registry import Section
from .widgets import wrapped_label

MIN_MINUTES, MAX_MINUTES = 1, 240
MIN_BUDGET, MAX_BUDGET = 25, 20000


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class IdleSettings(QGroupBox):
    """The idle-analysis section: `values()` -> {settings field: value}."""

    def __init__(self, settings, context: dict | None = None):
        super().__init__()
        self.setObjectName("idleSettings")
        self.help_label = wrapped_label(role="muted")
        self.enabled_check = QCheckBox()
        self.enabled_check.setChecked(bool(getattr(settings, "idle_enabled", True)))
        self.minutes_label = QLabel()
        self.minutes_spin = QSpinBox()
        self.minutes_spin.setRange(MIN_MINUTES, MAX_MINUTES)
        self.minutes_spin.setValue(max(MIN_MINUTES, min(MAX_MINUTES, _int(getattr(settings, "idle_minutes", 5), 5))))
        self.budget_label = QLabel()
        self.budget_spin = QSpinBox()
        self.budget_spin.setRange(MIN_BUDGET, MAX_BUDGET)
        self.budget_spin.setSingleStep(25)
        self.budget_spin.setValue(max(MIN_BUDGET, min(MAX_BUDGET, _int(getattr(settings, "idle_daily_budget", 600),
                                                                       600))))
        self.mains_check = QCheckBox()
        self.mains_check.setChecked(bool(getattr(settings, "idle_only_on_mains", True)))
        grid = QGridLayout()
        grid.addWidget(self.minutes_label, 0, 0)
        grid.addWidget(self.minutes_spin, 0, 1, Qt.AlignmentFlag.AlignLeft)
        grid.addWidget(self.budget_label, 1, 0)
        grid.addWidget(self.budget_spin, 1, 1, Qt.AlignmentFlag.AlignLeft)
        grid.setColumnStretch(1, 1)
        layout = QVBoxLayout(self)
        layout.addWidget(self.help_label)
        layout.addWidget(self.enabled_check)
        layout.addLayout(grid)
        layout.addWidget(self.mains_check)
        self.enabled_check.toggled.connect(self._enable)
        self._enable(self.enabled_check.isChecked())
        self.retranslate()

    def _enable(self, on: bool) -> None:
        for widget in (self.minutes_spin, self.budget_spin, self.mains_check):
            widget.setEnabled(on)

    def retranslate(self) -> None:
        self.setTitle(t("background.title"))
        self.help_label.setText(t("interest.settings.idle_help"))
        self.enabled_check.setText(t("background.switch"))
        self.minutes_label.setText(t("interest.settings.idle_minutes"))
        self.minutes_spin.setSuffix(" " + t("interest.settings.minutes_unit"))
        self.budget_label.setText(t("interest.settings.idle_budget"))
        self.mains_check.setText(t("interest.settings.idle_mains"))

    def values(self) -> dict:
        return {"idle_enabled": self.enabled_check.isChecked(), "idle_minutes": self.minutes_spin.value(),
                "idle_daily_budget": self.budget_spin.value(), "idle_only_on_mains": self.mains_check.isChecked()}


class SuggestSettings(QGroupBox):
    """The assistant-suggestions section."""

    def __init__(self, settings, context: dict | None = None):
        super().__init__()
        self.setObjectName("suggestSettings")
        self.help_label = wrapped_label(role="muted")
        self.web_check = QCheckBox()
        self.web_check.setChecked(bool(getattr(settings, "suggest_allow_web_search", False)))
        self.notice_check = QCheckBox()
        self.notice_check.setChecked(not bool(getattr(settings, "suggest_notice_shown", False)))
        layout = QVBoxLayout(self)
        layout.addWidget(self.help_label)
        layout.addWidget(self.web_check)
        layout.addWidget(self.notice_check)
        self.retranslate()

    def retranslate(self) -> None:
        self.setTitle(t("interest.settings.suggest_title"))
        self.help_label.setText(t("interest.settings.suggest_help"))
        self.web_check.setText(t("interest.settings.web_search"))
        self.notice_check.setText(t("interest.settings.show_notice"))

    def values(self) -> dict:
        return {"suggest_allow_web_search": self.web_check.isChecked(),
                "suggest_notice_shown": not self.notice_check.isChecked()}


def validate(base, values: dict) -> tuple[dict, list[str]]:
    """Checked changes of the fields of this page present in `values` (others untouched)."""
    changes: dict = {}
    errors: list[str] = []
    for name in ("idle_enabled", "idle_only_on_mains", "suggest_allow_web_search", "suggest_notice_shown"):
        if name in values:
            changes[name] = bool(values[name])
    if "idle_minutes" in values:
        raw = values["idle_minutes"]
        minutes = _int(str(raw).strip(), -1) if not isinstance(raw, bool) else -1
        if MIN_MINUTES <= minutes <= MAX_MINUTES:
            changes["idle_minutes"] = minutes
        else:
            errors.append(t("interest.settings.error_minutes", min=MIN_MINUTES, max=MAX_MINUTES))
    if "idle_daily_budget" in values:
        raw = values["idle_daily_budget"]
        budget = _int(str(raw).strip(), -1) if not isinstance(raw, bool) else -1
        if MIN_BUDGET <= budget <= MAX_BUDGET:
            changes["idle_daily_budget"] = budget
        else:
            errors.append(t("interest.settings.error_budget", min=MIN_BUDGET, max=MAX_BUDGET))
    return changes, errors


def _idle_factory(settings, context: dict) -> QWidget:
    return IdleSettings(settings, context)


def _suggest_factory(settings, context: dict) -> QWidget:
    return SuggestSettings(settings, context)


SECTIONS = [Section("background", _idle_factory, validate, order=10),
            Section("background", _suggest_factory, None, order=20)]

__all__ = ["IdleSettings", "MAX_BUDGET", "MAX_MINUTES", "MIN_BUDGET", "MIN_MINUTES", "SECTIONS", "SuggestSettings",
           "validate"]
