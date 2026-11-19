"""The advanced OCR reader in the main window: the one-time download offer, the
download itself (worker thread, determinate bar, cancellable, resumable), removal,
"Read again with the advanced reader", the Tools menu entries and the state shown
in Settings -> General. Nothing slow runs on the GUI thread; errors go to the log
and to the non-blocking status-bar notice.
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtGui import QAction

from .. import config
from ..i18n import t
from .progress_bar import BarState

log = logging.getLogger(__name__)

BAR_OWNER = "ocr_models"
DOWNLOAD_TIMEOUT_S = 3600.0  # a slow connection; the cancel event stops it sooner
SOURCE_NAME = "Hugging Face"


def megabytes(size: int) -> str:
    return f"{size / 1_000_000:.0f} MB"


def device_text(provider: str) -> str:
    return t(f"settings.ocr.device.{'dml' if provider == 'dml' else 'cpu'}")


class AdvancedReader:
    """Owned by the main window (`window.ocr_reader`)."""

    def __init__(self, window):
        self.window = window
        self._cancel: threading.Event | None = None
        self._reread_after = False
        self.download_action = QAction(window, triggered=lambda: self.start_download())
        self.cancel_action = QAction(window, triggered=lambda: self.cancel_download())
        self.remove_action = QAction(window, triggered=lambda: self.remove())
        self.settings_action = QAction(window, triggered=lambda: window.open_settings("general"))
        self.menu = None
        panel = window.profile_panel
        panel.on_advanced_reread = self.reread
        panel.on_advanced_accept = self.accept_offer
        panel.on_advanced_decline = self.decline_offer

    # --- state

    @property
    def downloading(self) -> bool:
        return self._cancel is not None

    def status(self):
        from ..cv import ocr_models

        return ocr_models.advanced_status()

    def installed(self) -> bool:
        from ..cv import ocr_models

        return ocr_models.find_advanced() is not None

    def state_text(self) -> str:
        """One line for Settings: not installed / installed, size, GPU or CPU."""
        if self.downloading:
            return t("settings.ocr.state_downloading")
        try:
            status = self.status()
        except Exception:  # noqa: BLE001 - never break the settings page
            log.warning("Advanced OCR state not readable", exc_info=True)
            return t("settings.ocr.state_missing", size="?")
        if status.installed:
            return t("settings.ocr.state_installed", size=megabytes(status.size_bytes),
                     device=device_text(status.provider))
        return t("settings.ocr.state_missing", size=megabytes(status.download_bytes))

    def refresh(self) -> None:
        installed = self.installed()
        self.download_action.setEnabled(not installed and not self.downloading)
        self.cancel_action.setEnabled(self.downloading)
        self.remove_action.setEnabled(installed and not self.downloading)
        dialog = getattr(self.window, "settings_dialog", None)
        if dialog is not None and hasattr(dialog, "refresh_ocr"):
            dialog.refresh_ocr()

    # --- menus and texts

    def add_menu(self, mirror) -> None:
        panel = self.window.profile_panel
        cv_menu = self.window.cv_menu
        first = cv_menu.actions()[0] if cv_menu.actions() else None
        reread = mirror(panel.advanced_button)
        cv_menu.insertAction(first, reread) if first is not None else cv_menu.addAction(reread)
        for button in (panel.advanced_download_button, panel.advanced_never_button):
            cv_menu.addAction(mirror(button))
        self.menu = self.window.tools_menu.addMenu("")
        for action in (self.download_action, self.cancel_action, self.remove_action):
            self.menu.addAction(action)
        self.menu.addSeparator()
        self.menu.addAction(self.settings_action)
        self.menu.aboutToShow.connect(self.refresh)
        self.refresh()

    def retranslate(self) -> None:
        if self.menu is not None:
            self.menu.setTitle(t("ocr.menu"))
        self.download_action.setText(t("ocr.menu.download"))
        self.cancel_action.setText(t("ocr.menu.cancel"))
        self.remove_action.setText(t("ocr.menu.remove"))
        self.settings_action.setText(t("ocr.menu.settings"))

    # --- after a CV was read

    def cv_read(self, reading) -> None:
        """Offers or fetches the advanced reader when the reading needed it and it is missing."""
        from ..cv.reading import ADVANCED_MISSING, ADVANCED_USED

        state = getattr(reading, "advanced", "")
        if state == ADVANCED_USED:
            self.window.show_notice(t("ocr.used"))
        if state != ADVANCED_MISSING or self.downloading:
            return
        mode = getattr(self.window._settings, "ocr_advanced", config.OCR_ADVANCED_DEFAULT)
        if mode == "never":
            return
        if self.installed():
            # the folder is there but did not load (checksum or engine failure): no loop
            self.window.show_notice(t("ocr.damaged"))
            return
        if mode == "always":
            self.start_download(reread=True)
            return
        self.offer()

    def offer(self) -> None:
        from ..cv import ocr_models

        self.window.profile_panel.offer_advanced(megabytes(ocr_models.advanced_download_bytes()), SOURCE_NAME)

    def accept_offer(self) -> None:
        self._save_mode("always")
        self.start_download(reread=True)

    def decline_offer(self) -> None:
        self._save_mode("never")

    def _save_mode(self, mode: str) -> None:
        settings = self.window._settings
        if getattr(settings, "ocr_advanced", None) == mode:
            return
        settings.ocr_advanced = mode
        try:
            config.save(settings)
        except OSError:
            log.warning("Settings not saved (advanced OCR choice)", exc_info=True)

    # --- read again

    def reread(self) -> None:
        """"Read again with the advanced reader": offers the download when it is missing."""
        panel = self.window.profile_panel
        if not panel.can_read_again():
            return
        if not self.installed():
            self.offer()
            return
        self.window.load_cv(panel.current_profile().cv_path, advanced="force")

    # --- download and removal

    def start_download(self, reread: bool = False) -> bool:
        """Starts the download in a worker; False when one is already running."""
        from ..cv import ocr_models

        if self.downloading or self.installed():
            return False
        self._reread_after = self._reread_after or reread
        cancel = threading.Event()
        self._cancel = cancel
        window = self.window
        settings = window._settings
        window.profile_panel.hide_advanced_offer()
        total_bytes = ocr_models.advanced_download_bytes()
        self._show_progress(0, total_bytes)

        def progress(done: int, total: int) -> None:
            window._queue.put(("ok", lambda value: self._show_progress(*value), (done, total)))

        def work():
            return ocr_models.download_advanced(progress=progress, cancel=cancel, settings=settings)

        window._in_background(work, self._downloaded, self._download_failed, name="advanced OCR download",
                              timeout=DOWNLOAD_TIMEOUT_S, cancel=cancel)
        self.refresh()
        return True

    def cancel_download(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def _show_progress(self, done: int, total: int) -> None:
        if not self.downloading or self.window._closed:
            return
        fraction = min(1.0, done / total) if total else None
        text = t("ocr.download_progress", done=f"{done / 1_000_000:.0f}", total=f"{total / 1_000_000:.0f}")
        self.window.status_progress.show(BAR_OWNER, BarState(fraction, f"{round((fraction or 0) * 100)} %", text))

    def _finish_bar(self) -> None:
        self._cancel = None
        self.window.status_progress.finish(BAR_OWNER, self.window._search_final_state())

    def _downloaded(self, _folder) -> None:
        from ..cv import ocr

        self._finish_bar()
        ocr.reset_engines()
        status = self.status()
        self.window.show_notice(t("ocr.downloaded", size=megabytes(status.size_bytes)))
        self.refresh()
        reread, self._reread_after = self._reread_after, False
        if reread and self.window.profile_panel.can_read_again():
            self.reread()

    def _download_failed(self, error: BaseException) -> None:
        from ..cv.ocr_models import DownloadCancelled
        from .common import WorkerTimeout

        self._finish_bar()
        self._reread_after = False
        if isinstance(error, (DownloadCancelled, WorkerTimeout)):
            self.window.show_notice(t("ocr.download_cancelled"))
        else:
            log.warning("Advanced OCR download failed: %s", error)
            self.window.show_notice(t("ocr.download_failed", error=str(error) or type(error).__name__))
        self.refresh()

    def remove(self) -> bool:
        from ..cv import ocr, ocr_models

        status = self.status()
        if not status.installed or self.downloading:
            return False
        if not self.window.dialogs.ask(t("ocr.confirm_remove", size=megabytes(status.size_bytes)),
                                       t("ocr.menu"), warning=True):
            return False
        ocr.reset_engines()  # release the loaded sessions first
        try:
            ocr_models.remove_advanced(status.folder)
        except Exception as error:  # noqa: BLE001 - files in use, permissions
            log.warning("Advanced OCR models not removed", exc_info=True)
            self.window.show_notice(t("ocr.remove_failed", error=str(error)))
            self.refresh()
            return False
        self.window.show_notice(t("ocr.removed"))
        self.refresh()
        return True
