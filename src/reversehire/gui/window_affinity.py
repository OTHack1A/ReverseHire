"""The main window's part for the affinity between the CV and each company (card box, list columns, feedback) (a mixin of MainWindow, kept apart to keep
main_window.py readable).

Hooks called by MainWindow._ext (each optional; a failure is logged, never raised):
_affinity_init() before the body is built, _affinity_build() after the tabs exist,
_affinity_menus(mirror), _affinity_retranslate(), _affinity_theme(palette), _affinity_cv_read(analysis),
_affinity_settings_applied(), _affinity_started() at the end of the constructor,
_affinity_shutdown(), _affinity_dialogs() -> list of open dialogs to close at exit.
Everything slow runs through the window's `_in_background` (deadline, cancel, logging);
progress goes to the shared bottom bar (`status_progress`), errors to the log and to
`show_notice`.

What it wires: ``self.affinity_service`` (gui/affinity_service.py, the public API for the
other features), the box at the top of the company card (gui/affinity_box.py), the four
affinity columns of Results, My companies and Favourites (the tables read
``affinity_service``), the Companies menu entries of the box buttons.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QTimer

from ..i18n import t

log = logging.getLogger(__name__)

PROGRESS_OWNER = "affinity"
REDRAW_DELAY_MS = 250  # tables are redrawn at most this often while results arrive


class AffinityMixin:
    """Mixed into MainWindow (see the module docstring)."""

    def _affinity_init(self) -> None:
        from .affinity_service import AffinityService

        service = AffinityService(self._in_background, self._archive, settings_provider=lambda: self._settings,
                                  embedder_provider=self._analysis_embedder, semantic=self._affinity_model_ready)
        service.on_progress = self._affinity_progress
        service.on_finished = self._affinity_finished
        service.on_error = self._affinity_error
        service.listeners.append(self._affinity_changed)
        self.affinity_service = service
        self.affinity_box = None
        self._affinity_error_shown = False
        self._affinity_timer = None

    @staticmethod
    def _affinity_model_ready() -> bool:
        """The semantic mode only when the language model is already on disk (never a download)."""
        try:
            from ..scoring.embedding import PREFERRED_MODEL, find_local_model, model_search_folders

            return find_local_model(PREFERRED_MODEL, model_search_folders()) is not None
        except Exception:  # noqa: BLE001
            return False

    def _affinity_build(self) -> None:
        from .affinity_box import AffinityBox

        service = self.affinity_service
        self.results_table.affinity_service = service
        self.companies_panel.affinity_service = service
        view = self.companies_panel.card_view
        box = AffinityBox(service, opener=self._opener, on_insert=self.insert_talking_points)
        view.add_extra_section(box, first=True)
        view.card_listeners.append(box.set_card)
        box.set_card(view.card)
        self.affinity_box = box
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(REDRAW_DELAY_MS)
        timer.timeout.connect(self.affinity_redraw)
        self._affinity_timer = timer

    def _affinity_menus(self, mirror) -> None:
        box = self.affinity_box
        if box is None:
            return
        self.companies_menu.addSeparator()
        for button in box.buttons():
            self.companies_menu.addAction(mirror(button))

    def _affinity_retranslate(self) -> None:
        if self.affinity_box is not None:
            self.affinity_box.retranslate()

    def _affinity_theme(self, palette) -> None:
        if self.affinity_box is not None:
            self.affinity_box.apply_palette(palette)

    def _affinity_cv_read(self, analysis) -> None:
        self.affinity_service.set_profile(getattr(analysis, "profile", None))

    def _affinity_settings_applied(self) -> None:
        self.affinity_service.settings_changed()

    def _affinity_shutdown(self) -> None:
        if self._affinity_timer is not None:
            self._affinity_timer.stop()
        self.affinity_service.close()

    def _affinity_dialogs(self) -> list:
        return []

    # --- results arriving --------------------------------------------------------------------

    def _affinity_changed(self) -> None:
        """New results or judgements: the card box at once, the tables shortly (coalesced)."""
        if getattr(self, "_closed", False):
            return
        if self.affinity_box is not None:
            self.affinity_box.refresh()
        if self._affinity_timer is not None and not self._affinity_timer.isActive():
            self._affinity_timer.start()

    def affinity_redraw(self) -> None:
        """Redraws the affinity columns of the lists (also called by the tests)."""
        if getattr(self, "_closed", False):
            return
        try:
            self.results_table.refresh()
            self.companies_panel.refresh_affinity()
        except RuntimeError:  # widgets already deleted at exit
            pass

    def _affinity_progress(self, done: int, total: int) -> None:
        from .main_window import counts_text
        from .progress_bar import BarState

        if getattr(self, "_closed", False):
            return
        state = BarState(min(1.0, done / total) if total else None, counts_text(done, total, False),
                         t("affinity.ui.progress", done=done, total=total))
        self.status_progress.show(PROGRESS_OWNER, state)

    def _affinity_finished(self) -> None:
        if getattr(self, "_closed", False) or not hasattr(self, "status_progress"):
            return
        if self.status_progress.state(PROGRESS_OWNER) is not None:
            self.status_progress.finish(PROGRESS_OWNER, self._search_final_state())
        if self._affinity_timer is not None and not self._affinity_timer.isActive():
            self._affinity_timer.start()

    def _affinity_error(self, error: BaseException) -> None:
        log.warning("Affinity: %s", type(error).__name__)
        if not self._affinity_error_shown and not getattr(self, "_closed", False):
            self._affinity_error_shown = True  # one notice per session: the log has the rest
            self.show_notice(t("affinity.ui.error"))

    # --- actions -----------------------------------------------------------------------------

    def insert_talking_points(self, card, text: str):
        """Opens the e-mail draft for `card` and inserts the talking points into its body."""
        dialog = self.open_mail_dialog(card)
        if dialog is not None and hasattr(dialog, "insert_text"):
            dialog.insert_text(text)
        return dialog
