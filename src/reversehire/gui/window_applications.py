"""The main window's part for the "Applications sent" tracker (tab, menu, mail/export hooks, card box) (a mixin of MainWindow, kept apart to keep
main_window.py readable).

Hooks called by MainWindow._ext (each optional; a failure is logged, never raised):
_applications_init() before the body is built, _applications_build() after the tabs exist,
_applications_menus(mirror), _applications_retranslate(), _applications_theme(palette), _applications_cv_read(analysis),
_applications_settings_applied(), _applications_started() at the end of the constructor,
_applications_shutdown(), _applications_dialogs() -> list of open dialogs to close at exit.
Everything slow runs through the window's `_in_background` (deadline, cancel, logging);
progress goes to the shared bottom bar (`status_progress`), errors to the log and to
`show_notice`.

How it fits together:
- the tab is a page that shows "Opening..." while a worker opens ``ApplicationService``
  (``<data folder>/applications``); then the ``ApplicationsPanel`` replaces it. The panel is
  rebuilt (same service, same selection) when the interface language changes;
- the submenu Companies > "Applications" (not a menu of its own: the menu bar stays readable
  at 1366 px) holds the panel's actions and, in one submenu per detail tab, a mirrored entry
  for every button of that tab, plus the buttons of the card box;
- "Write e-mail" (``MailDialog.opened_listeners``) and File > Export > application
  (``ExportPreviewDialog.exported_listeners``) record a DRAFT application on a worker; the
  service returns the existing record instead of a second one within the duplicate window;
- the company card ("My companies") shows a box with that company's applications;
- at start-up the archive is cleaned on a worker and a notice (click: opens the tab) tells
  how many follow-ups are due and how many appointments are today or tomorrow.
"""

from __future__ import annotations

import html
import logging
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (QAbstractButton, QCheckBox, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton,
                               QRadioButton, QStackedWidget, QVBoxLayout, QWidget)

from .. import i18n
from ..i18n import t
from .menus import bind_action, menu_action_of
from .theme import set_role

log = logging.getLogger(__name__)

UPCOMING_NOTICE_DAYS = 2      # appointments counted by the start-up notice: today and tomorrow
OPEN_TIMEOUT_S = 60.0         # opening the tracker's file (a sleeping disk, a network folder)
MAX_CARD_ROWS = 6             # applications listed in the card box (newest first)


def _tab_index() -> int:
    from .main_window import TAB_APPLICATIONS  # main_window imports this module

    return TAB_APPLICATIONS


class _WindowDialogs:
    """The panel's dialogs, through the window's (replaceable) ``dialogs`` object."""

    def __init__(self, window):
        self._window = window

    def _base(self):
        return self._window.dialogs

    def ask(self, text: str, title: str | None = None, warning: bool = False) -> bool:
        return self._base().ask(text, title, warning)

    def info(self, text: str, title: str | None = None) -> None:
        self._base().info(text, title)

    def error(self, text: str, title: str | None = None) -> None:
        self._base().error(text, title)

    def open_file(self, title: str, filters: str) -> str:
        return self._base().open_file(title, filters)

    def save_file(self, title: str, default_name: str, filters: str) -> str:
        return self._base().save_file(title, default_name, filters)

    def choose_folder(self, title: str) -> str:
        chooser = getattr(self._base(), "choose_folder", None)
        if chooser is not None:
            return chooser(title)
        from PySide6.QtWidgets import QFileDialog

        return QFileDialog.getExistingDirectory(self._window, title, "", QFileDialog.Option.DontUseNativeDialog)


class _NoticeClick(QObject):
    """A click on the status bar while the tracker's notice is shown opens the tab."""

    def __init__(self, label: QWidget, is_active: Callable[[], bool], on_click: Callable[[], None]):
        super().__init__(label)
        self._is_active = is_active
        self._on_click = on_click
        label.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt name)
        if event.type() == QEvent.Type.MouseButtonRelease:
            try:
                if self._is_active():
                    self._on_click()
            except Exception:  # noqa: BLE001
                log.warning("Applications notice click failed", exc_info=True)
        return False


class ApplicationsCardBox(QFrame):
    """The box on the company card: the company's applications, "Open in Applications sent"
    and "New application for this company"."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("applicationsCardBox")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.on_open: Callable[[int], None] = lambda _id: None
        self.on_new: Callable[[], None] = lambda: None
        self.application_ids: list[int] = []
        self.title_label = QLabel()
        self.title_label.setProperty("role", "title")
        self.list_label = QLabel()
        self.list_label.setWordWrap(True)
        self.list_label.setTextFormat(Qt.TextFormat.RichText)
        self.list_label.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse
                                                | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
        self.list_label.linkActivated.connect(self._link)
        self.open_button = QPushButton()
        self.open_button.clicked.connect(lambda: self.open_newest())
        self.new_button = QPushButton()
        self.new_button.clicked.connect(lambda: self.on_new())
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.new_button)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.addWidget(self.title_label)
        layout.addWidget(self.list_label)
        layout.addLayout(buttons)
        self._rows: list = []
        self._nexts: dict = {}
        self.retranslate()
        self.hide()

    def retranslate(self) -> None:
        self.title_label.setText(t("applications.card.title"))
        self.open_button.setText(t("applications.card.open"))
        self.new_button.setText(t("applications.card.new"))
        self._render()

    def show_applications(self, applications: list, nexts: dict) -> None:
        """`applications` newest first; `nexts` maps application id -> next Appointment."""
        self._rows = list(applications)
        self._nexts = dict(nexts)
        self.application_ids = [a.id for a in self._rows]
        self._render()
        self.show()

    def _render(self) -> None:
        from ..applications.table import _format_day, _format_moment

        self.open_button.setEnabled(bool(self._rows))
        if not self._rows:
            self.list_label.setText(html.escape(t("applications.card.none")))
            return
        lines = []
        for app in self._rows[:MAX_CARD_ROWS]:
            title = app.role or t(f"applications.kind.{app.kind}")
            parts = [t(f"applications.status.{app.status}")]
            parts.append(t("applications.card.sent_on", date=_format_day(app.sent_date)) if app.sent_date
                         else t("applications.card.not_sent"))
            upcoming = self._nexts.get(app.id)
            if upcoming is not None and upcoming.start_dt is not None:
                parts.append(t("applications.card.next", when=_format_moment(upcoming.start_dt)))
            lines.append(f'<a href="app:{app.id}">{html.escape(title)}</a>: '
                         + html.escape(", ".join(parts)))
        if len(self._rows) > MAX_CARD_ROWS:
            lines.append(html.escape(t("applications.card.more", count=len(self._rows) - MAX_CARD_ROWS)))
        self.list_label.setText("<br>".join(lines))

    def _link(self, link: str) -> None:
        if link.startswith("app:"):
            try:
                self.on_open(int(link[4:]))
            except ValueError:
                pass

    def open_newest(self) -> None:
        if self.application_ids:
            self.on_open(self.application_ids[0])


class ApplicationsMixin:
    """Mixed into MainWindow (see the module docstring)."""

    # --- hooks: construction ------------------------------------------------------------

    def _applications_init(self) -> None:
        self.applications_service = None      # ApplicationService once the file is open
        self.applications_panel = None        # ApplicationsPanel once the service is ready
        self.applications_page: QStackedWidget | None = None
        self.applications_menu: QMenu | None = None
        self.applications_card_box: ApplicationsCardBox | None = None
        self._applications_mirror: Callable | None = None
        self._applications_mirrored: list = []
        self._applications_pending: list[Callable[[], None]] = []
        self._applications_card = None
        self._applications_language = ""
        self._applications_error = ""
        self._applications_notice_text = ""
        self._applications_notice_filter = None

    def _build_applications_panel(self):
        """The "Applications sent" tab: a page that holds the panel once the tracker is open."""
        self.applications_page = QStackedWidget()
        self._applications_status = QLabel()
        self._applications_status.setWordWrap(True)
        self._applications_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._applications_status.setProperty("role", "muted")
        self._applications_status.setText(t("applications.loading"))
        self.applications_page.addWidget(self._applications_status)
        self._applications_open()
        return self.applications_page

    def _applications_open(self) -> None:
        from ..applications import ApplicationService, default_folder

        folder = default_folder()
        log.info("Opening the applications tracker")

        def work():
            # one worker: open the file, clean the archive, count the reminders of the start-up notice
            service = ApplicationService(folder)
            try:
                log.info("Applications archive: %d unused files removed", service.gc_archive())
            except Exception as error:  # noqa: BLE001 - the clean-up is retried at the next start
                log.warning("Applications archive not cleaned: %s", error)
            try:
                counts = applications_reminders(service)
            except Exception as error:  # noqa: BLE001
                log.warning("Application reminders not computed: %s", error)
                counts = {}
            return service, counts

        self._in_background(work, self._applications_ready, self._applications_open_failed,
                            name="open applications tracker", timeout=OPEN_TIMEOUT_S)

    def _applications_ready(self, result) -> None:
        service, counts = result
        if getattr(self, "_closed", False):
            service.close()
            return
        self.applications_service = service
        self._applications_error = ""
        self._applications_install_panel()
        self._applications_refresh_card_box()
        pending, self._applications_pending = self._applications_pending, []
        for action in pending:
            try:
                action()
            except Exception:  # noqa: BLE001
                log.warning("Deferred applications action failed", exc_info=True)
        self._applications_show_reminders(counts)

    def _applications_open_failed(self, error: BaseException) -> None:
        log.warning("Applications tracker not opened: %s", error)
        self._applications_error = str(error)[:200] or type(error).__name__
        self._applications_status.setText(t("applications.open_failed", detail=self._applications_error))
        set_role(self._applications_status, "warning")
        self.show_notice(t("applications.open_failed", detail=self._applications_error))

    def _applications_when_ready(self, action: Callable[[], None]) -> None:
        if self.applications_service is not None:
            action()
        elif not self._applications_error:
            self._applications_pending.append(action)

    def _applications_install_panel(self) -> None:
        """Builds (or rebuilds, e.g. for a new language) the panel on the open service."""
        from ..applications import ApplicationsPanel

        old = self.applications_panel
        keep, detail_tab = 0, 0
        if old is not None:
            keep, detail_tab = old.current_id(), old.tabs.currentIndex()
            old.flush_pending()
        panel = ApplicationsPanel(self.applications_service, run=self._applications_run,
                                  dialogs=_WindowDialogs(self), companies=self._applications_companies,
                                  reference_cv=self._primary_cv_pdf, notify=self.show_notice,
                                  open_path=self._applications_open_path, open_url=self._applications_open_url)
        panel.openCompanyRequested.connect(lambda card_id: self.open_card(card_id))
        panel.applicationsChanged.connect(lambda: self._applications_refresh_card_box())
        self.applications_page.addWidget(panel)
        self.applications_page.setCurrentWidget(panel)
        self.applications_panel = panel
        self._applications_language = i18n.current_language()
        if old is not None:
            self.applications_page.removeWidget(old)
            old.hide()
            old.deleteLater()
            if keep:
                panel.reveal(keep)
            panel.tabs.setCurrentIndex(detail_tab)
        self._applications_fill_menu()

    def _applications_run(self, work: Callable, on_success: Callable, on_error: Callable | None = None,
                          **_options) -> int:
        """The panel's runner: the window's workers (deadline, logging, results on the GUI thread)."""
        return self._in_background(work, on_success, on_error or self._applications_job_failed,
                                   name="applications tracker job")

    def _applications_job_failed(self, error: BaseException) -> None:
        log.warning("Applications tracker job failed: %s", error)
        self.show_notice(t("applications.error.generic", detail=str(error)[:200]))

    def _applications_companies(self) -> list[tuple[int, str]]:
        """The saved company cards, for the company field of "New application"."""
        from .. import companies

        try:
            return [(card.id, card.name) for card in companies.list_cards(self._archive) if card.name]
        except Exception:  # noqa: BLE001
            log.warning("Company list for the applications not read", exc_info=True)
            return []

    def _applications_open_path(self, path: str) -> bool:
        from ..applications.panel import open_local_path

        if self._opener is not webbrowser.open:  # tests inject an opener: it gets the file URI
            try:
                self._opener(Path(path).as_uri())
                return True
            except Exception:  # noqa: BLE001
                log.warning("File cannot be opened", exc_info=True)
                return False
        return open_local_path(path)

    def _applications_open_url(self, url: str) -> bool:
        from .common import open_url

        return open_url(url, self._opener)

    def _applications_build(self) -> None:
        box = ApplicationsCardBox()
        box.on_open = self.show_application
        box.on_new = lambda: self.new_application_for_card()
        self.applications_card_box = box
        view = self.companies_panel.card_view
        view.add_extra_section(box)
        view.card_listeners.append(self._applications_card_changed)

    def _applications_started(self) -> None:
        # the status bar exists now: a click on the tracker's notice opens the tab
        self._applications_notice_filter = _NoticeClick(
            self.status_label, lambda: bool(self._applications_notice_text)
            and self.status_label.text() == self._applications_notice_text,
            lambda: self.tabs.setCurrentIndex(_tab_index()))

    # --- menu ---------------------------------------------------------------------------

    def _applications_menus(self, mirror) -> None:
        self._applications_mirror = mirror
        menu = QMenu(self)  # a submenu of Companies: the menu bar stays readable at 1366 px
        self.companies_menu.addSeparator()
        self.companies_menu.addMenu(menu)
        menu.aboutToShow.connect(self._mirrors.refresh)
        self.applications_menu = menu
        self._applications_fill_menu()

    def _applications_drop_menu_entries(self) -> None:
        mirrored = set(self._applications_mirrored)
        if mirrored:
            self._mirrors.mirrors = [m for m in self._mirrors.mirrors if m.action not in mirrored]
        for action in self._applications_mirrored:
            action.deleteLater()
        self._applications_mirrored = []
        if self.applications_menu is not None:
            self.applications_menu.clear()

    def _applications_mirror_button(self, button: QAbstractButton) -> object:
        def text(b=button) -> str:
            label = b.text().replace("&", "")
            return label if len(label.strip()) > 2 else (b.toolTip() or label)

        action = self._applications_mirror(button, text)
        self._applications_mirrored.append(action)
        return action

    def _applications_fill_menu(self) -> None:
        menu, mirror = self.applications_menu, self._applications_mirror
        if menu is None or mirror is None:
            return
        self._applications_drop_menu_entries()
        panel = self.applications_panel
        if panel is None:
            waiting = menu.addAction(t("applications.loading"))
            waiting.setEnabled(False)
        else:
            actions = panel.actions_by_name
            bind_action(panel.new_button, actions["new"])
            for action in panel.actions_by_name.values():
                action.triggered.connect(lambda _checked=False: self.tabs.setCurrentIndex(_tab_index()))
            for action in panel.menu_actions():
                if action is None:
                    menu.addSeparator()
                    continue
                menu.addAction(action)
                if action is actions["mark_sent"]:
                    menu.addMenu(panel.status_menu)
            menu.addSeparator()
            for index in range(panel.tabs.count()):
                buttons = [b for b in panel.tabs.widget(index).findChildren(QAbstractButton)
                           if not isinstance(b, (QCheckBox, QRadioButton)) and menu_action_of(b) is None
                           and type(b).__name__ not in ("QLineEditIconButton", "QTableCornerButton")]
                if buttons:
                    submenu = menu.addMenu(panel.tabs.tabText(index))
                    for button in buttons:
                        submenu.addAction(self._applications_mirror_button(button))
        box = self.applications_card_box
        if box is not None:
            menu.addSeparator()
            menu.addAction(self._applications_mirror_button(box.open_button))
            menu.addAction(self._applications_mirror_button(box.new_button))

    # --- hooks: language, theme, end ----------------------------------------------------

    def _applications_retranslate(self) -> None:
        if self.applications_menu is not None:
            self.applications_menu.setTitle(t("menu.applications"))
        if self.applications_service is None:
            self._applications_status.setText(
                t("applications.open_failed", detail=self._applications_error) if self._applications_error
                else t("applications.loading"))
        if self.applications_card_box is not None:
            self.applications_card_box.retranslate()
        if self.applications_panel is not None and self._applications_language != i18n.current_language():
            self._applications_install_panel()
        elif self.applications_panel is None:
            self._applications_fill_menu()

    def _applications_theme(self, _palette) -> None:
        if self.applications_panel is not None:
            self.applications_panel.refresh()  # the status colours come from the active palette

    def _applications_shutdown(self) -> None:
        panel, service = self.applications_panel, self.applications_service
        if panel is not None:
            try:
                panel.flush_pending()
            except Exception:  # noqa: BLE001
                log.warning("Pending application edits not saved", exc_info=True)
        if service is not None:
            try:
                service.close()
            except Exception:  # noqa: BLE001
                log.warning("Applications tracker not closed", exc_info=True)

    def _applications_dialogs(self) -> list:
        panel = self.applications_panel
        return [panel._dialog] if panel is not None and panel._dialog is not None else []

    # --- the company card box -----------------------------------------------------------

    def _applications_card_changed(self, card) -> None:
        self._applications_card = card
        self._applications_refresh_card_box()

    def _applications_refresh_card_box(self) -> None:
        box, card, service = self.applications_card_box, self._applications_card, self.applications_service
        if box is None:
            return
        if service is None or card is None or not getattr(card, "id", 0):
            box.hide()
            return
        try:
            rows = service.applications_for_company(card.id, getattr(card, "name", "") or "")
            nexts = service.next_appointments() if rows else {}
        except Exception:  # noqa: BLE001 - e.g. the file is busy: the box simply waits for the next change
            log.warning("Applications of the card not read", exc_info=True)
            box.hide()
            return
        box.show_applications(rows, {a.id: nexts[a.id] for a in rows if a.id in nexts})

    def show_application(self, application_id: int) -> bool:
        """Switches to "Applications sent" and selects the record (filters cleared if needed)."""
        panel = self.applications_panel
        self.tabs.setCurrentIndex(_tab_index())
        if panel is None:
            return False
        log.info("Application opened from the company card")
        return panel.reveal(application_id)

    def new_application_for_card(self, card=None):
        """"New application for this company": the panel's dialog with the company filled in."""
        card = card if card is not None else self._applications_card
        panel = self.applications_panel
        if panel is None or card is None:
            return None
        dialog = panel.new_application()
        if dialog is None:
            return None
        index = dialog.company.findData(getattr(card, "id", None)) if getattr(card, "id", 0) else -1
        if index >= 0:
            dialog.company.setCurrentIndex(index)
        else:
            dialog.company.setEditText(getattr(card, "name", "") or "")
        log.info("New application dialog opened from the company card")
        return dialog

    # --- recording from "Write e-mail" and "Export application" ---------------------------

    def _applications_mail_dialog_opened(self, card, dialog) -> None:
        listeners = getattr(dialog, "opened_listeners", None)
        if listeners is None or card is None:
            return
        listeners.append(lambda outcome, draft, card=card, dialog=dialog:
                         self._applications_record_mail(card, outcome, draft, dialog))

    def _applications_record_mail(self, card, outcome, draft, dialog=None) -> None:
        if not getattr(outcome, "ok", False):
            return  # nothing was opened: nothing to record
        self._applications_when_ready(lambda: self._applications_record(
            lambda service: service.record_from_mail_draft(draft, outcome, card, role=getattr(dialog, "role", "") or ""),
            "applications.notice.recorded_mail",
            "mail draft", dialog))

    def _applications_application_export_opened(self, card, document, dialog, info) -> None:
        listeners = getattr(dialog, "exported_listeners", None)
        if listeners is None or card is None:
            return
        info = dict(info or {})
        listeners.append(lambda _fmt, _path, card=card: self._applications_record_export(card, info))

    def _applications_record_export(self, card, info: dict) -> None:
        cv = str(info.get("cv_path") or "")

        def work(service):
            path = cv if cv and Path(cv).is_file() else None
            return service.record_from_export(company=card, cover_letter=info.get("body", "") or "",
                                              cover_subject=info.get("subject", "") or "", cv_path=path)

        self._applications_when_ready(lambda: self._applications_record(work, "applications.notice.recorded_export",
                                                                        "application export"))

    def _applications_record(self, work: Callable, notice_key: str, origin: str, dialog=None) -> None:
        """Records on a worker; the outcome is told in `dialog`'s result line when given (the
        "Write e-mail" dialog keeps its own status-bar message), else in the status bar."""
        service = self.applications_service
        started = time.time()

        def done(app) -> None:
            if app is None:
                return
            new = app.created >= started
            log.info("Application recorded from %s (%s)", origin, "new draft" if new else "already recorded")
            text = t(notice_key if new else "applications.notice.already_recorded", name=app.display_title)
            if not self._applications_tell_in_dialog(dialog, text):
                self.show_notice(text)
            if self.applications_panel is not None:
                self.applications_panel.refresh()
            self._applications_refresh_card_box()

        self._in_background(lambda: work(service), done, self._applications_job_failed,
                            name=f"record application ({origin})")

    @staticmethod
    def _applications_tell_in_dialog(dialog, text: str) -> bool:
        label = getattr(dialog, "result_label", None)
        if label is None:
            return False
        try:
            if not dialog.isVisible():
                return False
            current = label.text()
            label.setText(f"{current}\n\n{text}" if current else text)
            label.show()
            return True
        except RuntimeError:  # the dialog was closed and deleted meanwhile
            return False

    # --- start-up: archive clean-up and reminders ----------------------------------------

    def _applications_show_reminders(self, counts: dict) -> None:
        text = reminder_text(counts)
        if not text:
            return
        log.info("Applications reminders: %s", counts)
        self._applications_notice_text = text
        self.show_notice(text)


def applications_reminders(service, now: datetime | None = None) -> dict:
    """{"follow_ups", "today", "tomorrow"}: what the start-up notice tells (runs on a worker)."""
    moment = now or datetime.now().astimezone()
    today = moment.date()
    counts = {"follow_ups": len(service.due_follow_ups(today)), "today": 0, "tomorrow": 0}
    for item, _app in service.upcoming_appointments(moment, days=UPCOMING_NOTICE_DAYS, limit=None):
        day = item.start_dt.astimezone().date()
        if day == today:
            counts["today"] += 1
        elif day == today + timedelta(days=1):
            counts["tomorrow"] += 1
    return counts


def reminder_text(counts: dict) -> str:
    """The notice ("" when there is nothing to tell)."""
    parts = []
    if counts.get("follow_ups"):
        parts.append(t("applications.startup.follow_ups", count=counts["follow_ups"]))
    if counts.get("today"):
        parts.append(t("applications.startup.today", count=counts["today"]))
    if counts.get("tomorrow"):
        parts.append(t("applications.startup.tomorrow", count=counts["tomorrow"]))
    return t("applications.startup.notice", details=", ".join(parts)) if parts else ""
