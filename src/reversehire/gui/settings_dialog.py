"""The one settings dialog, in pages: General, Search and discovery, Registers and paid
key, Mail, Assistant and MCP, Appearance (a list on the left, the page on the right),
with validation of the values. Every option of the earlier single-page dialog is kept,
with the same attribute names."""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton,
    QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

from . import settings_registry

from .. import i18n, mail
from ..config import (
    ASSISTANTS, CRAWL_ENGINES, DEFAULT_STARTUP_LANGUAGE, OCR_ADVANCED_DEFAULT, OCR_ADVANCED_MODES,
    STARTUP_LANGUAGES, STARTUP_LAST, THEMES, Settings,
)
from ..i18n import t
from .common import parse_number
from .theme import section
from .widgets import wrapped_label
from .workers import runner_or_local

LANGUAGE_NAMES = i18n.LANGUAGE_NAMES
DELAY_MIN_S = 0.5     # politeness towards websites: never lower than this
DELAY_MAX_S = 60.0
PAGES_MIN = 1
PAGES_MAX = 50
PARALLEL_MIN = 1
PARALLEL_MAX = 16
SALARY_MAX = 10_000_000
# Candidate preferences: added to Settings in this version; guarded so that an
# older Settings class without them still works.
PREFERENCE_FLAGS = ("prefer_permanent", "prefer_remote", "job_boards_enabled")
# Pause between two requests to the same discovery service (owner: "go slowly").
PAUSE_MIN_S = 1.0
PAUSE_MAX_S = 60.0
DEFAULT_PAUSE_S = 2.0
PAGES = ("general", "search", "jobsearch", "answers", "registers", "mail", "assistant", "appearance")


def _text(key: str, **params) -> str:
    """Translated text with the discovery layer's English fallback for new keys."""
    from ..discovery.texts import text

    return text(key, **params)


def _integer(text: str) -> int | None:
    try:
        return int((text or "").strip())
    except ValueError:
        return None


def validate_settings(base: Settings, values: dict) -> tuple[Settings | None, list[str]]:
    """New settings from the field values (strings and booleans), or a list of errors."""
    errors: list[str] = []
    language = values.get("language", base.language)
    if language not in i18n.available_languages():
        errors.append(t("settings.language_error"))
    engine = values.get("crawl_engine", base.crawl_engine)
    if engine not in CRAWL_ENGINES:
        errors.append(t("settings.engine_error"))
    key = (values.get("firecrawl_key", base.firecrawl_key) or "").strip()
    if engine == "firecrawl" and not key:
        errors.append(t("settings.key_error"))
    if any(c.isspace() for c in key):
        errors.append(t("settings.key_spaces_error"))

    delay = parse_number(str(values.get("request_delay_s", base.request_delay_s)))
    if delay is None or not DELAY_MIN_S <= delay <= DELAY_MAX_S:
        errors.append(t("settings.delay_error", min=DELAY_MIN_S, max=DELAY_MAX_S))
    pages = _integer(str(values.get("max_pages_per_site", base.max_pages_per_site)))
    if pages is None or not PAGES_MIN <= pages <= PAGES_MAX:
        errors.append(t("settings.pages_error", min=PAGES_MIN, max=PAGES_MAX))
    maximum = _integer(str(values.get("max_companies", base.max_companies)))
    if maximum is None or maximum < 0:
        errors.append(t("settings.max_companies_error"))
    include = values.get("include_without_site", base.include_without_site)
    if not isinstance(include, bool):
        errors.append(t("settings.generic_error"))
    theme = values.get("theme", base.theme)
    if theme not in THEMES:
        errors.append(t("settings.theme_error"))
    changes = {}
    if "startup_language" in values and hasattr(base, "startup_language"):
        if values["startup_language"] not in STARTUP_LANGUAGES:
            errors.append(t("settings.language_error"))
        else:
            changes["startup_language"] = values["startup_language"]
    if "parallel_companies" in values:
        parallel = _integer(str(values["parallel_companies"]))
        if parallel is None or not PARALLEL_MIN <= parallel <= PARALLEL_MAX:
            errors.append(t("search.parallel_error", min=PARALLEL_MIN, max=PARALLEL_MAX))
        else:
            changes["parallel_companies"] = parallel
    for name in PREFERENCE_FLAGS:
        if name in values and hasattr(base, name):
            if not isinstance(values[name], bool):
                errors.append(t("settings.generic_error"))
            else:
                changes[name] = values[name]
    if "request_pause_s" in values:
        pause = parse_number(str(values["request_pause_s"]))
        if pause is None or not PAUSE_MIN_S <= pause <= PAUSE_MAX_S:
            errors.append(_text("settings.request_pause_error", min=PAUSE_MIN_S, max=PAUSE_MAX_S))
        else:
            changes["request_pause_s"] = float(pause)
    for name in ("paid_registry_provider", "paid_registry_key"):
        if name in values:
            value = str(values[name] or "").strip()
            if name == "paid_registry_key" and any(c.isspace() for c in value):
                errors.append(t("settings.key_spaces_error"))
            else:
                changes[name] = value
    if "min_salary_year" in values and hasattr(base, "min_salary_year"):
        text = str(values["min_salary_year"]).strip().replace(".", "").replace(" ", "").replace("'", "")
        salary = _integer(text or "0")
        if salary is None or not 0 <= salary <= SALARY_MAX:
            errors.append(t("settings.salary_error"))
        else:
            changes["min_salary_year"] = salary
    if "mail" in values and hasattr(base, "mail"):
        settings = mail.MailSettings.from_dict(values["mail"])
        if settings.my_email and not mail.is_plausible_email(settings.my_email):
            errors.append(t("settings.mail.email_error"))
        else:
            changes["mail"] = settings.to_dict()
    if "assistant" in values and hasattr(base, "assistant"):
        if values["assistant"] not in ASSISTANTS:
            errors.append(t("settings.generic_error"))
        else:
            changes["assistant"] = values["assistant"]
    for name in ("claude_path", "codex_path"):
        if name in values and hasattr(base, name):
            changes[name] = str(values[name] or "").strip().strip('"')
    if "ocr_advanced" in values and hasattr(base, "ocr_advanced"):
        if values["ocr_advanced"] not in OCR_ADVANCED_MODES:
            errors.append(t("settings.generic_error"))
        else:
            changes["ocr_advanced"] = values["ocr_advanced"]
    from .settings_extra import validate_extra

    extra_changes, extra_errors = validate_extra(base, {k: v for k, v in values.items() if hasattr(base, k)})
    changes.update(extra_changes)
    errors += extra_errors
    from . import settings_registry

    section_changes, section_errors = settings_registry.validate(
        base, {k: v for k, v in values.items() if hasattr(base, k)})
    changes.update({k: v for k, v in section_changes.items() if hasattr(base, k)})
    errors += section_errors
    if errors:
        return None, errors
    new = replace(
        base, language=language, crawl_engine=engine, firecrawl_key=key,
        request_delay_s=float(delay), max_pages_per_site=pages, max_companies=maximum,
        include_without_site=include, theme=theme,
    )
    for name, value in changes.items():
        setattr(new, name, value)
    return new, []


class _Page(QWidget):
    """A page: a grid of labels and fields, top-aligned."""

    def __init__(self):
        super().__init__()
        self.grid = QGridLayout()
        self.grid.setColumnStretch(1, 1)
        self.row = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.grid)
        layout.addStretch(1)

    def label(self, text: str) -> QLabel:
        widget = QLabel(text)
        self.grid.addWidget(widget, self.row, 0)
        return widget

    def field(self, text: str, widget: QWidget) -> QWidget:
        self.label(text)
        self.grid.addWidget(widget, self.row, 1)
        self.row += 1
        return widget

    def wide(self, widget: QWidget) -> QWidget:
        self.grid.addWidget(widget, self.row, 0, 1, 2)
        self.row += 1
        return widget

    def under(self, widget: QWidget) -> QWidget:
        self.grid.addWidget(widget, self.row, 1)
        self.row += 1
        return widget

    def title(self, text: str) -> QLabel:
        label = QLabel(section(text))
        label.setProperty("role", "title")
        return self.wide(label)


class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget | None, settings: Settings, on_save: Callable[[Settings], None],
                 on_clear_cache: Callable[[], None], dialogs, *, run_in_background: Callable | None = None,
                 primary_cv: Callable[[], str] | None = None, mcp_enabled: Callable[[], bool] | None = None,
                 on_mcp_toggle: Callable[[bool], None] | None = None,
                 on_mcp_details: Callable[[], None] | None = None, cli_checker: Callable | None = None,
                 mcp_checker: Callable | None = None, mail_system=None, mail_environment=None,
                 on_assistant_checked: Callable[[dict], None] | None = None, page: str | None = None,
                 ocr_reader=None):
        super().__init__(parent)
        from .settings_pages import AssistantPage, MailPage

        self.setWindowTitle(t("settings.title"))
        self._base = settings
        self._on_save = on_save
        self._on_clear_cache = on_clear_cache
        self._dialogs = dialogs
        run = runner_or_local(run_in_background, self)

        # --- General: language and the candidate's preferences
        general = _Page()
        self.language_combo = QComboBox()
        for code in i18n.available_languages():
            self.language_combo.addItem(i18n.language_name(code), code)
        self._select(self.language_combo, settings.language)
        general.field(t("settings.language"), self.language_combo)
        # Which language a new start begins with (English by default): the language
        # chips only change the running session.
        self.startup_language_combo = QComboBox()
        for code in STARTUP_LANGUAGES:
            name = t("settings.startup_language.last") if code == STARTUP_LAST else i18n.language_name(code)
            self.startup_language_combo.addItem(name, code)
        self._select(self.startup_language_combo,
                     getattr(settings, "startup_language", DEFAULT_STARTUP_LANGUAGE))
        general.field(t("settings.startup_language"), self.startup_language_combo)
        general.under(wrapped_label(t("settings.startup_language_note"), "muted"))
        self.preferences_label = general.title(t("settings.preferences"))
        self.permanent_box = general.wide(QCheckBox(t("settings.prefer_permanent")))
        self.permanent_box.setChecked(bool(getattr(settings, "prefer_permanent", True)))
        self.remote_box = general.wide(QCheckBox(t("settings.prefer_remote")))
        self.remote_box.setChecked(bool(getattr(settings, "prefer_remote", True)))
        self.salary_field = general.field(t("settings.min_salary"),
                                          QLineEdit(str(int(getattr(settings, "min_salary_year", 0) or 0))))
        self.salary_field.setPlaceholderText("0")
        self.job_boards_box = general.wide(QCheckBox(t("settings.job_boards")))
        self.job_boards_box.setChecked(bool(getattr(settings, "job_boards_enabled", False)))
        general.wide(wrapped_label(t("settings.job_boards_warning"), "warning"))
        for name, widget in (("prefer_permanent", self.permanent_box), ("prefer_remote", self.remote_box),
                             ("job_boards_enabled", self.job_boards_box),
                             ("min_salary_year", self.salary_field)):
            widget.setEnabled(hasattr(settings, name))
        self._build_ocr_section(general, settings, ocr_reader)

        # --- Search and discovery: reading engine, politeness, limits
        search = _Page()
        self.engine_combo = QComboBox()
        for code in CRAWL_ENGINES:
            self.engine_combo.addItem(code, code)
        self._select(self.engine_combo, settings.crawl_engine)
        search.field(t("settings.engine"), self.engine_combo)
        search.wide(wrapped_label(t("settings.engine_note"), "muted"))
        self.key_field = QLineEdit(settings.firecrawl_key)
        self.key_field.setEchoMode(QLineEdit.EchoMode.Password)
        search.field(t("settings.firecrawl_key"), self.key_field)
        self.show_key = search.under(QCheckBox(t("settings.show_key")))
        self.show_key.toggled.connect(lambda on: self.key_field.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        search.wide(wrapped_label(t("settings.key_note"), "muted"))
        self.delay_field = search.field(t("settings.delay"), QLineEdit(f"{settings.request_delay_s:g}"))
        self.pages_field = search.field(t("settings.pages"), QLineEdit(str(settings.max_pages_per_site)))
        self.max_field = search.field(t("settings.max_companies"), QLineEdit(str(settings.max_companies)))
        self.include_box = search.wide(QCheckBox(t("settings.include_without_site")))
        self.include_box.setChecked(settings.include_without_site)
        self.discovery_label = search.title(_text("settings.discovery"))
        self.pause_field = search.field(_text("settings.request_pause"), QLineEdit(
            f"{float(getattr(settings, 'request_pause_s', DEFAULT_PAUSE_S)):g}"))

        # --- Registers and the paid key (prepared)
        registers = _Page()
        self.paid_label = registers.title(_text("settings.paid_registry"))
        self.paid_provider_field = registers.field(_text("settings.paid_provider"), QLineEdit(
            str(getattr(settings, "paid_registry_provider", "") or "")))
        self.paid_provider_field.setPlaceholderText("openapi.it")
        self.paid_key_field = registers.field(_text("settings.paid_key"), QLineEdit(
            str(getattr(settings, "paid_registry_key", "") or "")))
        self.paid_key_field.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_paid_key = registers.under(QCheckBox(t("settings.show_key")))
        self.show_paid_key.toggled.connect(lambda on: self.paid_key_field.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        registers.wide(wrapped_label(_text("settings.paid_note"), "muted"))
        for name, widget in (("request_pause_s", self.pause_field),
                             ("paid_registry_provider", self.paid_provider_field),
                             ("paid_registry_key", self.paid_key_field)):
            widget.setEnabled(hasattr(settings, name))

        # --- Mail and Assistant
        self.mail_page = MailPage(settings, run, primary_cv=primary_cv, system=mail_system,
                                  environment=mail_environment)
        self.assistant_page = AssistantPage(settings, run, mcp_enabled=mcp_enabled or (
            lambda: bool(getattr(settings, "mcp_enabled", False))), on_mcp_toggle=on_mcp_toggle,
            on_mcp_details=on_mcp_details, cli_checker=cli_checker, mcp_checker=mcp_checker,
            on_checked=on_assistant_checked)

        # --- Appearance
        appearance = _Page()
        self.theme_combo = QComboBox()
        for code in THEMES:
            self.theme_combo.addItem(t(f"theme.{code}"), code)
        self._select(self.theme_combo, settings.theme)
        appearance.field(t("settings.theme"), self.theme_combo)
        # Map (mapview): online OpenStreetMap tiles by default, none at all, or the user's server.
        self.map_label = appearance.title(t("map.settings.title"))
        # Positive wording, ticked by default: "Show the map background" (= not map_offline).
        self.map_background_box = appearance.wide(QCheckBox(t("map.settings.background")))
        self.map_background_box.setChecked(not bool(getattr(settings, "map_offline", False)))
        self.map_url_field = appearance.field(t("map.settings.url"), QLineEdit(
            str(getattr(settings, "map_tile_url", "") or "")))
        self.map_url_field.setPlaceholderText("https://tile.openstreetmap.org/{z}/{x}/{y}.png")
        appearance.wide(wrapped_label(t("map.settings.note"), "muted"))

        # --- Job search and the screening answers (settings_extra)
        from .settings_extra import AnswersPage, JobSearchPage

        self.jobsearch_page = JobSearchPage(settings)
        self.answers_page = AnswersPage(settings)

        self.page_list = QListWidget()
        self.page_list.setObjectName("settingsPages")
        self.pages = QStackedWidget()
        self.page_widgets = {"general": general, "search": search, "jobsearch": self.jobsearch_page,
                             "answers": self.answers_page, "registers": registers,
                             "mail": self.mail_page, "assistant": self.assistant_page, "appearance": appearance}
        self._add_sections(settings, {"window": parent, "run": run, "dialogs": dialogs})
        self.page_names = tuple(name for name in settings_registry.page_order(PAGES) if name in self.page_widgets)
        for name in self.page_names:
            self.page_list.addItem(t(f"settings.page.{name}"))
            self.pages.addWidget(self.page_widgets[name])
        self.page_list.currentRowChanged.connect(self.pages.setCurrentIndex)
        metrics = self.page_list.fontMetrics()
        widest = max(metrics.horizontalAdvance(t(f"settings.page.{name}")) for name in self.page_names)
        self.page_list.setFixedWidth(widest + 36)

        self.clear_button = QPushButton(t("settings.clear_cache"))
        self.clear_button.clicked.connect(self._clear_cache)
        self.save_button = QPushButton(t("common.save"))
        self.save_button.setProperty("role", "accent")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save)
        self.cancel_button = QPushButton(t("common.cancel"))
        self.cancel_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.clear_button)
        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.cancel_button)

        body = QHBoxLayout()
        body.addWidget(self.page_list)
        body.addWidget(self.pages, 1)
        layout = QVBoxLayout(self)
        layout.addLayout(body, 1)
        layout.addSpacing(8)
        layout.addLayout(buttons)
        self.show_page(page or "general")
        # A comfortable initial size for the wrapped notes. No setMinimumWidth: an
        # explicit minimum would replace the layout's, letting labels be cut.
        width = max(self.sizeHint().width(), self.fontMetrics().averageCharWidth() * 110)
        height = max(self.sizeHint().height(), self.fontMetrics().lineSpacing() * 36)
        self.resize(width, height)

    def _build_ocr_section(self, page: "_Page", settings: Settings, reader) -> None:
        """Text recognition: the advanced reader's state, when to use it, Download/Remove."""
        self._ocr_reader = reader
        self.ocr_label = page.title(t("settings.ocr.title"))
        self.ocr_state_label = page.wide(wrapped_label("", "muted"))
        self.ocr_mode_combo = QComboBox()
        for code in OCR_ADVANCED_MODES:
            self.ocr_mode_combo.addItem(t(f"settings.ocr.mode.{code}"), code)
        self._select(self.ocr_mode_combo, getattr(settings, "ocr_advanced", OCR_ADVANCED_DEFAULT))
        self.ocr_mode_combo.setEnabled(hasattr(settings, "ocr_advanced"))
        page.field(t("settings.ocr.mode"), self.ocr_mode_combo)
        self.ocr_download_button = QPushButton(t("settings.ocr.download"))
        self.ocr_cancel_button = QPushButton(t("settings.ocr.cancel"))
        self.ocr_remove_button = QPushButton(t("settings.ocr.remove"))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        for button in (self.ocr_download_button, self.ocr_cancel_button, self.ocr_remove_button):
            row.addWidget(button)
        row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        page.under(holder)
        page.wide(wrapped_label(t("settings.ocr.note"), "muted"))
        if reader is not None:
            from .menus import bind_action

            # the same commands as Tools -> Advanced reader (one QAction each)
            for button, action in ((self.ocr_download_button, reader.download_action),
                                   (self.ocr_cancel_button, reader.cancel_action),
                                   (self.ocr_remove_button, reader.remove_action)):
                bind_action(button, action)
                button.clicked.connect(lambda _checked=False, a=action: a.trigger())
        self.refresh_ocr()

    def refresh_ocr(self) -> None:
        """The advanced reader's state and which buttons apply now."""
        reader = self._ocr_reader
        if reader is None:
            self.ocr_state_label.setText("")
            for button in (self.ocr_download_button, self.ocr_cancel_button, self.ocr_remove_button):
                button.setEnabled(False)
            return
        self.ocr_state_label.setText(reader.state_text())
        installed, downloading = reader.installed(), reader.downloading
        self.ocr_download_button.setEnabled(not installed and not downloading)
        self.ocr_cancel_button.setEnabled(downloading)
        self.ocr_remove_button.setEnabled(installed and not downloading)

    @staticmethod
    def _select(combo: QComboBox, code: str) -> None:
        index = combo.findData(code)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _add_sections(self, settings: Settings, context: dict) -> None:
        """The sections of the feature modules (gui/settings_registry.py): on their page,
        or on a new scrollable page."""
        self.section_widgets: list = []
        for section in settings_registry.sections():
            try:
                widget = section.factory(settings, context)
            except Exception:  # noqa: BLE001 - one broken section must not stop the dialog
                import logging

                logging.getLogger(__name__).warning("Settings section not built", exc_info=True)
                continue
            if widget is None:
                continue
            page = self.page_widgets.get(section.page)
            if page is None:
                inner = _Page()
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                scroll.setFrameShape(QScrollArea.Shape.NoFrame)
                scroll.setWidget(inner)
                scroll.inner_page = inner
                self.page_widgets[section.page] = scroll
                page = scroll
            target = getattr(page, "inner_page", page)
            if isinstance(target, _Page):
                target.wide(widget)
            else:
                target.layout().addWidget(widget)
            self.section_widgets.append(widget)

    def section_values(self) -> dict:
        values: dict = {}
        for widget in getattr(self, "section_widgets", []):
            try:
                values.update(widget.values())
            except Exception:  # noqa: BLE001
                import logging

                logging.getLogger(__name__).warning("Settings section values not read", exc_info=True)
        return values

    def show_page(self, name: str) -> None:
        names = getattr(self, "page_names", PAGES)
        index = names.index(name) if name in names else 0
        self.page_list.setCurrentRow(index)
        self.pages.setCurrentIndex(index)

    def current_page(self) -> str:
        return getattr(self, "page_names", PAGES)[self.pages.currentIndex()]

    def values(self) -> dict:
        return {
            "language": self.language_combo.currentData(),
            "startup_language": self.startup_language_combo.currentData(),
            "crawl_engine": self.engine_combo.currentData(),
            "firecrawl_key": self.key_field.text(),
            "request_delay_s": self.delay_field.text(),
            "max_pages_per_site": self.pages_field.text(),
            "max_companies": self.max_field.text(),
            "include_without_site": self.include_box.isChecked(),
            "theme": self.theme_combo.currentData(),
            "prefer_permanent": self.permanent_box.isChecked(),
            "prefer_remote": self.remote_box.isChecked(),
            "min_salary_year": self.salary_field.text(),
            "job_boards_enabled": self.job_boards_box.isChecked(),
            **{name: widget.text() for name, widget in (
                ("request_pause_s", self.pause_field),
                ("paid_registry_provider", self.paid_provider_field),
                ("paid_registry_key", self.paid_key_field)) if hasattr(self._base, name)},
            **({"mail": self.mail_page.values()} if hasattr(self._base, "mail") else {}),
            **{k: v for k, v in self.assistant_page.values().items() if hasattr(self._base, k)},
            **{k: v for k, v in self.jobsearch_page.values().items() if hasattr(self._base, k)},
            **{k: v for k, v in self.answers_page.values().items() if hasattr(self._base, k)},
            **{k: v for k, v in (("map_offline", not self.map_background_box.isChecked()),
                                 ("map_tile_url", self.map_url_field.text())) if hasattr(self._base, k)},
            **({"ocr_advanced": self.ocr_mode_combo.currentData()} if hasattr(self._base, "ocr_advanced") else {}),
            **{k: v for k, v in self.section_values().items() if hasattr(self._base, k)},
        }

    def save(self) -> bool:
        new, errors = validate_settings(self._base, self.values())
        if errors:
            self._dialogs.error("\n".join(errors), t("settings.title"))
            return False
        self._on_save(new)
        self.accept()
        return True

    def _clear_cache(self) -> None:
        if self._dialogs.ask(t("settings.confirm_clear"), t("settings.clear_cache")):
            self._on_clear_cache()
