"""Settings pages of the job search and of the recruiter analysis.

- "Job search": the sources with each one's terms verdict (the sources whose terms
  forbid automated reading are listed greyed with the reason and can never be switched
  on), the user's own Adzuna / Jooble / Careerjet keys (masked, stored like the paid
  register key, never logged), pages per agency and the time limit per source.
- "My answers to screening questions": the truthful answers the recruiter simulators
  use for the knock-out questions (recruiter_sim.KnockoutAnswers), "not answered" by
  default.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QGridLayout, QLabel, QLineEdit, QScrollArea, QVBoxLayout, QWidget,
)

from .. import config
from ..i18n import t
from ..jobsearch.texts import source_label, text as js_text
from .common import parse_number
from .theme import section
from .widgets import wrapped_label

TRI_STATE = ("", "yes", "no")  # not answered / yes / no


class _Grid(QWidget):
    def __init__(self):
        super().__init__()
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setColumnStretch(1, 1)
        self.row = 0

    def field(self, text: str, widget: QWidget) -> QWidget:
        self.grid.addWidget(QLabel(text), self.row, 0)
        self.grid.addWidget(widget, self.row, 1)
        self.row += 1
        return widget

    def wide(self, widget: QWidget) -> QWidget:
        self.grid.addWidget(widget, self.row, 0, 1, 2)
        self.row += 1
        return widget

    def title(self, text: str) -> QLabel:
        label = QLabel(section(text))
        label.setProperty("role", "title")
        return self.wide(label)


def _scrolled(content: QWidget) -> QWidget:
    page = QWidget()
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    holder = QWidget()
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(0, 0, 8, 0)
    layout.addWidget(content)
    layout.addStretch(1)
    scroll.setWidget(holder)
    outer = QVBoxLayout(page)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.addWidget(scroll)
    return page


def describe_sources() -> list[dict]:
    """Static facts of every job source (the forbidden ones included), in display order."""
    from ..jobsearch import build_sources

    return [s.describe() for s in build_sources({}, cards=[], local_postings=[])]


class JobSearchPage(QWidget):
    def __init__(self, settings):
        super().__init__()
        self._settings = settings
        grid = _Grid()
        grid.title(js_text("jobsearch.settings.title"))
        grid.wide(wrapped_label(t("jobs.settings.intro"), "muted"))
        overrides = dict(getattr(settings, "jobsearch_sources", {}) or {})
        self.source_boxes: dict[str, QCheckBox] = {}
        self.forbidden_boxes: dict[str, QCheckBox] = {}
        for facts in describe_sources():
            source_id = facts["id"]
            box = QCheckBox(source_label(source_id, facts["label"]))
            verdict = js_text(f"jobsearch.verdict.{facts['terms_verdict']}")
            note_key = f"jobsearch.terms.{source_id.replace(':', '.')}"
            note = js_text(note_key)
            note = note if note != note_key else facts.get("terms_note", "")
            checked = facts.get("terms_checked") or ""
            details = verdict + (f" — {note}" if note else "")
            if checked:
                details += " " + js_text("jobsearch.terms.checked", date=checked)
            if facts.get("needs_key"):
                details += " " + js_text("jobsearch.untested_live")
            info = wrapped_label(details, "muted")
            if not facts["allowed_by_terms"]:
                box.setChecked(False)
                box.setEnabled(False)  # can never be switched on
                box.setToolTip(t("jobs.settings.forbidden"))
                info.setProperty("role", "muted")
                self.forbidden_boxes[source_id] = box
            else:
                box.setChecked(bool(overrides.get(source_id, facts["enabled_by_default"])))
                self.source_boxes[source_id] = box
            grid.wide(box)
            grid.wide(info)
        grid.title(t("jobs.settings.keys"))
        self.key_fields: dict[str, QLineEdit] = {}
        for name in config.JOBSEARCH_KEY_FIELDS:
            field = QLineEdit(str(getattr(settings, name, "") or ""))
            field.setEchoMode(QLineEdit.EchoMode.Password)
            self.key_fields[name] = grid.field(js_text(f"jobsearch.settings.{_key_label(name)}"), field)
        self.show_keys = grid.wide(QCheckBox(t("settings.show_key")))
        self.show_keys.toggled.connect(self._show_keys)
        grid.wide(wrapped_label(js_text("jobsearch.settings.keys_note"), "muted"))
        grid.title(t("jobs.settings.limits"))
        self.pages_field = grid.field(js_text("jobsearch.settings.max_pages"), QLineEdit(
            str(getattr(settings, "jobsearch_max_pages", config.JOBSEARCH_PAGES_DEFAULT))))
        self.timeout_field = grid.field(js_text("jobsearch.settings.timeout"), QLineEdit(
            f"{float(getattr(settings, 'jobsearch_timeout_s', config.JOBSEARCH_TIMEOUT_DEFAULT_S)):g}"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_scrolled(grid))

    def _show_keys(self, on: bool) -> None:
        for field in self.key_fields.values():
            field.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)

    def values(self) -> dict:
        return {
            "jobsearch_sources": {sid: box.isChecked() for sid, box in self.source_boxes.items()},
            **{name: field.text() for name, field in self.key_fields.items()},
            "jobsearch_max_pages": self.pages_field.text(),
            "jobsearch_timeout_s": self.timeout_field.text(),
        }


def _key_label(name: str) -> str:
    return {"adzuna_app_id": "adzuna_id", "adzuna_app_key": "adzuna_key"}.get(name, name)


class AnswersPage(QWidget):
    """"My answers to screening questions" (recruiter_sim.KnockoutAnswers)."""

    BOOLEANS = ("right_to_work", "needs_sponsorship", "willing_to_relocate", "own_car", "shifts_ok")
    NUMBERS = ("max_commute_km", "notice_weeks", "min_salary_year", "max_travel_days")

    def __init__(self, settings):
        super().__init__()
        answers = config.clean_knockout_answers(getattr(settings, "knockout_answers", {}) or {})
        grid = _Grid()
        grid.title(t("answers.title"))
        grid.wide(wrapped_label(t("answers.intro"), "muted"))
        self.combos: dict[str, QComboBox] = {}
        self.numbers: dict[str, QLineEdit] = {}
        for name in self.BOOLEANS:
            combo = QComboBox()
            for value in TRI_STATE:
                combo.addItem(t(f"answers.choice.{value or 'unset'}"), value)
            current = answers.get(name)
            combo.setCurrentIndex(0 if current is None else (1 if current else 2))
            self.combos[name] = grid.field(t(f"answers.{name}"), combo)
        for name in self.NUMBERS:
            value = answers.get(name)
            field = QLineEdit("" if value is None else f"{value:g}" if isinstance(value, float) else str(value))
            field.setPlaceholderText(t("answers.unset_placeholder"))
            self.numbers[name] = grid.field(t(f"answers.{name}"), field)
        self.licence_field = grid.field(t("answers.driving_licence"),
                                        QLineEdit(", ".join(answers.get("driving_licence") or [])))
        self.licence_field.setPlaceholderText(t("answers.licence_placeholder"))
        grid.wide(wrapped_label(t("answers.note"), "muted"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_scrolled(grid))

    def values(self) -> dict:
        result: dict = {}
        for name, combo in self.combos.items():
            value = combo.currentData()
            if value:
                result[name] = value == "yes"
        for name, field in self.numbers.items():
            text = field.text().strip()
            if text:
                result[name] = text
        licences = [x.strip().upper() for x in self.licence_field.text().replace(";", ",").split(",") if x.strip()]
        if licences:
            result["driving_licence"] = licences
        return {"knockout_answers": result}


def validate_extra(base, values: dict) -> tuple[dict, list[str]]:
    """Changes and errors of the job-search, answers and map fields (see validate_settings)."""
    changes: dict = {}
    errors: list[str] = []
    if "jobsearch_sources" in values:
        changes["jobsearch_sources"] = {str(k): bool(v) for k, v in values["jobsearch_sources"].items()}
    for name in config.JOBSEARCH_KEY_FIELDS:
        if name in values:
            value = str(values[name] or "").strip()
            if any(c.isspace() for c in value):
                errors.append(t("settings.key_spaces_error"))
            else:
                changes[name] = value
    if "jobsearch_max_pages" in values:
        try:
            pages = int(str(values["jobsearch_max_pages"]).strip())
        except ValueError:
            pages = None
        low, high = config.JOBSEARCH_PAGES_RANGE
        if pages is None or not low <= pages <= high:
            errors.append(t("jobs.settings.pages_error", min=low, max=high))
        else:
            changes["jobsearch_max_pages"] = pages
    if "jobsearch_timeout_s" in values:
        seconds = parse_number(str(values["jobsearch_timeout_s"]))
        low, high = config.JOBSEARCH_TIMEOUT_RANGE_S
        if seconds is None or not low <= seconds <= high:
            errors.append(t("jobs.settings.timeout_error", min=f"{low:g}", max=f"{high:g}"))
        else:
            changes["jobsearch_timeout_s"] = float(seconds)
    if "knockout_answers" in values:
        raw = dict(values["knockout_answers"])
        cleaned = {}
        for name, value in raw.items():
            kind = config.KNOCKOUT_FIELDS.get(name)
            if kind in (int, float) and isinstance(value, str):
                number = parse_number(value)
                if number is None or number < 0:
                    errors.append(t("answers.number_error", field=t(f"answers.{name}")))
                    continue
                cleaned[name] = int(number) if kind is int else float(number)
            else:
                cleaned[name] = value
        changes["knockout_answers"] = config.clean_knockout_answers(cleaned)
    if "map_tile_url" in values:
        from ..mapview import validate_tile_template

        url = str(values["map_tile_url"] or "").strip()
        if url:
            checked, _reason = validate_tile_template(url)
            if checked is None:
                errors.append(t("map.settings.url_error"))
            else:
                changes["map_tile_url"] = checked
        else:
            changes["map_tile_url"] = ""
    if "map_offline" in values:
        changes["map_offline"] = bool(values["map_offline"])
    return changes, errors
