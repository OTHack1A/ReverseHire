"""Settings page "Affinity" (gui/settings_registry.py section on the page "affinity").

- career directions on/off, and the user's own directions (name + keywords; removable);
- weight of each company type (0 % excludes it: verdict "Not a fit", named);
- optional distance limit (off = work anywhere: the distance is only informational);
- "only companies within my directions";
- "Calibrate from my judgements" (enabled after ``calibration.MIN_JUDGEMENTS`` judgements
  with Agree / Disagree): shows the weights before and after and how many past verdicts
  would change (``affinity.calibrate`` / ``rejudge``); "Apply these weights" saves them at
  once to ``settings.affinity_weights``; "Default weights" goes back.

Values: ``affinity_preferences`` (the user part of ``AffinityPreferences.to_dict()``:
directions, company_types, max_distance_km, directions_only) and ``affinity_weights``
(``Weights.to_dict()``, {} = defaults). The window's AffinityService recomputes every
result when they change (``settings_applied`` hook).
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace

from PySide6.QtWidgets import (
    QCheckBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from ..affinity import AffinityPreferences, Direction, Weights
from ..affinity import calibration as cal
from ..affinity import text as atext
from ..affinity import weights as W
from ..affinity.knowledge import COMPANY_TYPES
from ..i18n import t
from .common import run_job
from .settings_registry import Section
from .theme import section as section_title
from .widgets import wrapped_label

log = logging.getLogger(__name__)

PREFERENCE_KEYS = ("directions", "company_types", "max_distance_km", "directions_only")
MAX_DISTANCE_KM = 2000
CALIBRATION_TIMEOUT_S = 120.0


def direction_label(direction) -> str:
    """The direction's name in the interface language (shipped directions are translated)."""
    key = f"affinity.ui.direction.{direction.id}"
    text = t(key)
    return direction.name if text == key or direction.custom else text


def user_preferences(settings) -> AffinityPreferences:
    return AffinityPreferences.from_dict(getattr(settings, "affinity_preferences", None) or {})


def preference_values(prefs: AffinityPreferences) -> dict:
    """The user part of the preferences; {} when it is the defaults (so that the shipped
    directions of a newer version still reach a user who never changed them)."""
    data = prefs.to_dict()
    values = {k: data[k] for k in PREFERENCE_KEYS}
    defaults = AffinityPreferences().to_dict()
    return {} if values == {k: defaults[k] for k in PREFERENCE_KEYS} else values


def custom_direction(name: str, keywords: str, existing: list[Direction]) -> Direction | None:
    """A direction added by the user (None when the name is empty)."""
    name = " ".join((name or "").split())
    if not name:
        return None
    words = [w.strip() for w in re.split(r"[,;\n]", keywords or "") if w.strip()] or [name]
    slug = re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_") or "direction"
    ids = {d.id for d in existing}
    ident, n = f"custom_{slug}", 2
    while ident in ids:
        ident, n = f"custom_{slug}_{n}", n + 1
    return Direction(id=ident, name=name, roles=words, skills=words, enabled=True, custom=True)


def weights_text(weights: Weights) -> str:
    comps = weights.normalized()
    return ", ".join(f"{atext(f'affinity.component.{k}')} {round(comps[k] * 100)} %" for k in W.COMPONENTS)


class AffinitySection(QWidget):
    def __init__(self, settings, context: dict | None = None):
        super().__init__()
        self._context = context or {}
        self._settings = settings
        self.prefs = user_preferences(settings)
        self.weights = Weights.from_dict(getattr(settings, "affinity_weights", None) or {})
        self.report = None
        self.direction_boxes: dict[str, QCheckBox] = {}
        self.remove_buttons: dict[str, QPushButton] = {}

        self.title_label = QLabel()
        self.title_label.setProperty("role", "title")
        self.intro = wrapped_label(role="muted")
        self.directions_title = QLabel()
        self.directions_holder = QWidget()
        self.directions_layout = QVBoxLayout(self.directions_holder)
        self.directions_layout.setContentsMargins(0, 0, 0, 0)
        self.name_field = QLineEdit()
        self.keywords_field = QLineEdit()
        self.add_button = QPushButton()
        self.add_button.clicked.connect(lambda: self.add_direction())
        add_row = QHBoxLayout()
        add_row.setContentsMargins(0, 0, 0, 0)
        add_row.addWidget(self.name_field, 1)
        add_row.addWidget(self.keywords_field, 2)
        add_row.addWidget(self.add_button)
        self.only_directions = QCheckBox()
        self.only_directions.setChecked(self.prefs.directions_only)

        self.types_title = QLabel()
        self.types_note = wrapped_label(role="muted")
        self.type_spins: dict[str, QSpinBox] = {}
        self.type_labels: dict[str, QLabel] = {}
        types_grid = QGridLayout()
        types_grid.setContentsMargins(0, 0, 0, 0)
        for row, key in enumerate(COMPANY_TYPES):
            spin = QSpinBox()
            spin.setRange(0, 100)
            spin.setSuffix(" %")
            spin.setValue(round(self.prefs.type_weight(key) * 100))
            label = QLabel()
            self.type_spins[key], self.type_labels[key] = spin, label
            types_grid.addWidget(label, row, 0)
            types_grid.addWidget(spin, row, 1)
        types_grid.setColumnStretch(2, 1)

        self.distance_box = QCheckBox()
        self.distance_spin = QSpinBox()
        self.distance_spin.setRange(1, MAX_DISTANCE_KM)
        self.distance_spin.setSuffix(" km")
        limit = self.prefs.max_distance_km
        self.distance_box.setChecked(limit is not None)
        self.distance_spin.setValue(int(limit) if limit else 50)
        self.distance_spin.setEnabled(limit is not None)
        self.distance_box.toggled.connect(self.distance_spin.setEnabled)
        distance_row = QHBoxLayout()
        distance_row.setContentsMargins(0, 0, 0, 0)
        distance_row.addWidget(self.distance_box)
        distance_row.addWidget(self.distance_spin)
        distance_row.addStretch(1)

        self.calibration_title = QLabel()
        self.calibration_info = wrapped_label(role="muted")
        self.calibrate_button = QPushButton()
        self.calibrate_button.clicked.connect(lambda: self.calibrate())
        self.apply_button = QPushButton()
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(lambda: self.apply_calibration())
        self.reset_button = QPushButton()
        self.reset_button.clicked.connect(lambda: self.reset_weights())
        self.report_label = wrapped_label()
        calibration_row = QHBoxLayout()
        calibration_row.setContentsMargins(0, 0, 0, 0)
        for button in (self.calibrate_button, self.apply_button, self.reset_button):
            calibration_row.addWidget(button)
        calibration_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.title_label)
        layout.addWidget(self.intro)
        layout.addWidget(self.directions_title)
        layout.addWidget(self.directions_holder)
        layout.addLayout(add_row)
        layout.addWidget(self.only_directions)
        layout.addWidget(self.types_title)
        layout.addWidget(self.types_note)
        layout.addLayout(types_grid)
        layout.addLayout(distance_row)
        layout.addWidget(self.calibration_title)
        layout.addWidget(self.calibration_info)
        layout.addLayout(calibration_row)
        layout.addWidget(self.report_label)
        self._fill_directions()
        self.retranslate()

    # --- helpers
    def _service(self):
        window = self._context.get("window")
        return getattr(window, "affinity_service", None)

    def judgements(self) -> int:
        service = self._service()
        try:
            return service.feedback_count() if service is not None else 0
        except Exception:  # noqa: BLE001
            return 0

    def _fill_directions(self) -> None:
        while self.directions_layout.count():
            item = self.directions_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.direction_boxes.clear()
        self.remove_buttons.clear()
        for direction in self.prefs.directions:
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            box = QCheckBox(direction_label(direction))
            box.setChecked(direction.enabled)
            keywords = ", ".join(direction.roles[:6])
            box.setToolTip(t("affinity.ui.settings.keywords_tip", keywords=keywords) if keywords else "")
            line.addWidget(box)
            self.direction_boxes[direction.id] = box
            if direction.custom:
                remove = QPushButton(t("affinity.ui.settings.remove"))
                remove.clicked.connect(lambda _c=False, ident=direction.id: self.remove_direction(ident))
                line.addWidget(remove)
                self.remove_buttons[direction.id] = remove
            line.addStretch(1)
            self.directions_layout.addWidget(row)

    def _sync_directions(self) -> None:
        for direction in self.prefs.directions:
            box = self.direction_boxes.get(direction.id)
            if box is not None:
                direction.enabled = box.isChecked()

    # --- actions
    def add_direction(self) -> bool:
        self._sync_directions()
        direction = custom_direction(self.name_field.text(), self.keywords_field.text(), self.prefs.directions)
        if direction is None:
            self.report_label.setText(t("affinity.ui.settings.name_needed"))
            return False
        self.prefs.directions.append(direction)
        self.name_field.clear()
        self.keywords_field.clear()
        self._fill_directions()
        log.info("Affinity direction added (custom)")
        return True

    def remove_direction(self, ident: str) -> bool:
        self._sync_directions()
        before = len(self.prefs.directions)
        self.prefs.directions = [d for d in self.prefs.directions if not (d.id == ident and d.custom)]
        self._fill_directions()
        return len(self.prefs.directions) < before

    def calibrate(self) -> bool:
        service = self._service()
        if service is None or self.judgements() < cal.MIN_JUDGEMENTS:
            return False
        run = self._context.get("run")
        weights = Weights.from_dict(self.weights.to_dict())

        def work():
            feedback, results = service.calibration_inputs()
            return cal.calibrate(feedback, results, weights)

        def done(report) -> None:
            self.show_report(report)

        def failed(error: BaseException) -> None:
            log.warning("Affinity calibration failed: %s", type(error).__name__)
            self.report_label.setText(t("affinity.ui.settings.calibration_failed"))

        self.calibrate_button.setEnabled(False)
        self.report_label.setText(t("affinity.ui.settings.calibrating"))
        if run is None:
            try:
                done(work())
            except Exception as error:  # noqa: BLE001
                failed(error)
        else:
            run_job(run, work, done, failed, name="affinity calibration", timeout=CALIBRATION_TIMEOUT_S)
        return True

    def show_report(self, report) -> None:
        self.report = report
        self._update_buttons()
        lines = [report.note.render() if report.note is not None else ""]
        lines.append(t("affinity.ui.settings.before", weights=weights_text(report.before)))
        if report.applied:
            lines.append(t("affinity.ui.settings.after", weights=weights_text(report.after)))
            lines.append(t("affinity.ui.settings.changed", count=report.changed_count))
        self.report_label.setText("\n".join(x for x in lines if x))

    def apply_calibration(self) -> bool:
        if self.report is None or not self.report.applied:
            return False
        self._save_weights(self.report.after)
        self.report = None
        self._update_buttons()
        self.report_label.setText(t("affinity.ui.settings.applied"))
        return True

    def reset_weights(self) -> None:
        self._save_weights(Weights())
        self.report = None
        self._update_buttons()
        self.report_label.setText(t("affinity.ui.settings.reset_done"))

    def _save_weights(self, weights: Weights) -> None:
        """The weights are saved at once (and kept when the dialog is saved)."""
        self.weights = weights
        window = self._context.get("window")
        data = {} if weights.to_dict() == Weights().to_dict() else weights.to_dict()
        if window is not None and hasattr(window, "apply_settings") and hasattr(window, "settings"):
            try:
                window.apply_settings(replace(window.settings, affinity_weights=data))
            except Exception:  # noqa: BLE001
                log.warning("Affinity weights not saved", exc_info=True)
        log.info("Affinity weights %s", "reset to the defaults" if not data else "calibrated and saved")

    def _update_buttons(self) -> None:
        count = self.judgements()
        self.calibrate_button.setEnabled(count >= cal.MIN_JUDGEMENTS and self._service() is not None)
        self.apply_button.setEnabled(self.report is not None and self.report.applied)
        self.reset_button.setEnabled(self.weights.to_dict() != Weights().to_dict())
        self.calibration_info.setText(t("affinity.ui.settings.judgements", count=count, needed=cal.MIN_JUDGEMENTS))

    # --- dialog protocol
    def values(self) -> dict:
        self._sync_directions()
        prefs = AffinityPreferences.from_dict(self.prefs.to_dict())
        prefs.directions_only = self.only_directions.isChecked()
        prefs.company_types = {k: spin.value() / 100.0 for k, spin in self.type_spins.items()}
        prefs.max_distance_km = float(self.distance_spin.value()) if self.distance_box.isChecked() else None
        weights = self.weights.to_dict()
        return {"affinity_preferences": preference_values(prefs),
                "affinity_weights": {} if weights == Weights().to_dict() else weights}

    def retranslate(self) -> None:
        self.title_label.setText(section_title(t("affinity.ui.settings.title")))
        self.intro.setText(t("affinity.ui.settings.intro"))
        self.directions_title.setText(t("affinity.ui.settings.directions"))
        self.name_field.setPlaceholderText(t("affinity.ui.settings.name_placeholder"))
        self.keywords_field.setPlaceholderText(t("affinity.ui.settings.keywords_placeholder"))
        self.add_button.setText(t("affinity.ui.settings.add"))
        self.only_directions.setText(t("affinity.ui.settings.only_directions"))
        self.types_title.setText(t("affinity.ui.settings.types"))
        self.types_note.setText(t("affinity.ui.settings.types_note"))
        for key, label in self.type_labels.items():
            label.setText(atext(f"affinity.type.{key}"))
            self.type_spins[key].setToolTip(t("affinity.ui.settings.types_note"))
        self.distance_box.setText(t("affinity.ui.settings.distance"))
        self.distance_box.setToolTip(t("affinity.ui.settings.distance_tip"))
        self.calibration_title.setText(t("affinity.ui.settings.calibration"))
        self.calibrate_button.setText(t("affinity.ui.settings.calibrate"))
        self.apply_button.setText(t("affinity.ui.settings.apply"))
        self.reset_button.setText(t("affinity.ui.settings.reset"))
        self._update_buttons()


def validate(base, values: dict) -> tuple[dict, list[str]]:
    changes: dict = {}
    errors: list[str] = []
    if "affinity_preferences" in values:
        raw = values["affinity_preferences"]
        if not isinstance(raw, dict):
            errors.append(t("affinity.ui.settings.invalid"))
        elif not raw:
            changes["affinity_preferences"] = {}
        else:
            directions = raw.get("directions") or []
            if any(not str((d or {}).get("name", "")).strip() for d in directions):
                errors.append(t("affinity.ui.settings.name_needed"))
            distance = raw.get("max_distance_km")
            if distance is not None and (not isinstance(distance, (int, float)) or not 0 < distance <= MAX_DISTANCE_KM):
                errors.append(t("affinity.ui.settings.distance_invalid", max=MAX_DISTANCE_KM))
            types = raw.get("company_types") or {}
            if any(not isinstance(v, (int, float)) or not 0 <= v <= 1 for v in types.values()):
                errors.append(t("affinity.ui.settings.invalid"))
            if not errors:
                changes["affinity_preferences"] = preference_values(AffinityPreferences.from_dict(raw))
    if "affinity_weights" in values:
        raw = values["affinity_weights"]
        if not isinstance(raw, dict):
            errors.append(t("affinity.ui.settings.invalid"))
        else:
            changes["affinity_weights"] = Weights.from_dict(raw).to_dict() if raw else {}
    return changes, errors


def _factory(settings, context: dict):
    return AffinitySection(settings, context)


SECTIONS = [Section("affinity", _factory, validate, order=10)]
