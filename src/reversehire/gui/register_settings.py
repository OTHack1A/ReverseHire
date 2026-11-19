"""Settings of the regional register, on the settings page "registers"
(gui/settings_registry.py): the regions kept, the months of RNA state-aid records, the
start-up update check and the official sources to download.

The fields already exist in `config.Settings` (`register_regions`, `register_rna_months`,
`register_auto_check`, `register_sources`); `config.load` keeps a value only when its type
matches the default, and the register itself tolerates unknown regions and clamps the
months (`RegisterManager.regions` / `options`), so no extra load-time validation is needed.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QGridLayout, QGroupBox, QLabel, QListWidget, QListWidgetItem, QSpinBox,
                               QVBoxLayout, QWidget)

from ..i18n import t
from .settings_registry import Section
from .widgets import wrapped_label

MAX_MONTHS = 120


def _region_keys() -> list[tuple[str, str]]:
    from ..geo.regions import REGIONS

    return [(key, region.name) for key, region in REGIONS.items()]


def _source_ids() -> tuple[str, ...]:
    from ..registers.model import DOWNLOADABLE

    return DOWNLOADABLE


class RegisterSettings(QGroupBox):
    """The section widget: `values()` -> {settings field: value}."""

    def __init__(self, settings, context: dict | None = None):
        super().__init__()
        self.setObjectName("registerSettings")
        from ..registers.texts import source_title

        regions = list(getattr(settings, "register_regions", None) or ["emilia-romagna"])
        sources = list(getattr(settings, "register_sources", None) or _source_ids())
        self.help_label = wrapped_label(role="muted")
        self.regions_label = QLabel()
        self.regions_list = QListWidget()
        self.regions_list.setObjectName("registerRegions")
        self.regions_list.setMaximumHeight(self.fontMetrics().height() * 9)
        for key, name in _region_keys():
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if key in regions else Qt.CheckState.Unchecked)
            self.regions_list.addItem(item)
        self.months_label = QLabel()
        self.months_spin = QSpinBox()
        self.months_spin.setRange(0, MAX_MONTHS)
        try:
            self.months_spin.setValue(max(0, min(MAX_MONTHS, int(getattr(settings, "register_rna_months", 24)))))
        except (TypeError, ValueError):
            self.months_spin.setValue(24)
        self.auto_check = QCheckBox()
        self.auto_check.setChecked(bool(getattr(settings, "register_auto_check", True)))
        self.sources_label = QLabel()
        self.source_checks: dict[str, QCheckBox] = {}
        grid = QGridLayout()
        grid.addWidget(self.regions_label, 0, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self.regions_list, 0, 1)
        grid.addWidget(self.months_label, 1, 0)
        grid.addWidget(self.months_spin, 1, 1, Qt.AlignmentFlag.AlignLeft)
        grid.addWidget(self.sources_label, 2, 0, Qt.AlignmentFlag.AlignTop)
        boxes = QVBoxLayout()
        for source in _source_ids():
            box = QCheckBox(source_title(source))
            box.setChecked(source in sources)
            self.source_checks[source] = box
            boxes.addWidget(box)
        holder = QWidget()
        holder.setLayout(boxes)
        boxes.setContentsMargins(0, 0, 0, 0)
        grid.addWidget(holder, 2, 1)
        grid.setColumnStretch(1, 1)
        layout = QVBoxLayout(self)
        layout.addWidget(self.help_label)
        layout.addLayout(grid)
        layout.addWidget(self.auto_check)
        self.retranslate()

    def retranslate(self) -> None:
        from ..registers.texts import source_title

        self.setTitle(t("registers.settings.title"))
        self.help_label.setText(t("registers.settings.help"))
        self.regions_label.setText(t("registers.settings.regions"))
        self.months_label.setText(t("registers.settings.rna_months"))
        self.auto_check.setText(t("registers.settings.auto_check"))
        self.sources_label.setText(t("registers.settings.sources"))
        for source, box in self.source_checks.items():
            box.setText(source_title(source))

    def values(self) -> dict:
        regions = [self.regions_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.regions_list.count())
                   if self.regions_list.item(i).checkState() == Qt.CheckState.Checked]
        return {"register_regions": regions, "register_rna_months": self.months_spin.value(),
                "register_auto_check": self.auto_check.isChecked(),
                "register_sources": [s for s, box in self.source_checks.items() if box.isChecked()]}


def validate(base, values: dict) -> tuple[dict, list[str]]:
    """Checked changes of the register fields present in `values` (others untouched)."""
    from ..registers.italy_codes import region_key

    changes: dict = {}
    errors: list[str] = []
    if "register_regions" in values:
        raw = values["register_regions"]
        items = raw.split(",") if isinstance(raw, str) else list(raw or [])
        known = {key for key, _ in _region_keys()}
        regions = []
        for item in items:
            key = region_key(str(item)) or str(item).strip().lower()
            if key in known and key not in regions:
                regions.append(key)
        if regions:
            changes["register_regions"] = regions
        else:
            errors.append(t("registers.settings.error_regions"))
    if "register_rna_months" in values:
        raw = values["register_rna_months"]
        try:
            months = int(str(raw).strip()) if not isinstance(raw, bool) else -1
        except (TypeError, ValueError):
            months = -1
        if 0 <= months <= MAX_MONTHS:
            changes["register_rna_months"] = months
        else:
            errors.append(t("registers.settings.error_months", max=MAX_MONTHS))
    if "register_auto_check" in values:
        changes["register_auto_check"] = bool(values["register_auto_check"])
    if "register_sources" in values:
        raw = values["register_sources"]
        items = raw.split(",") if isinstance(raw, str) else list(raw or [])
        chosen = [s for s in _source_ids() if s in {str(i).strip().lower() for i in items}]
        if chosen:
            changes["register_sources"] = chosen
        else:
            errors.append(t("registers.settings.error_sources"))
    return changes, errors


def _factory(settings, context: dict) -> QWidget:
    return RegisterSettings(settings, context)


SECTIONS = [Section("registers", _factory, validate, order=50)]

__all__ = ["MAX_MONTHS", "RegisterSettings", "SECTIONS", "validate"]
