"""The main window's part for the regional register (a mixin of MainWindow, kept apart to
keep main_window.py readable): the Register manager window, the "Register manager…"
button of tab 2 and its Search-menu entry, the start-up update check.

Public API for the other parts of the window
--------------------------------------------
``window.register_manager`` -> registers.RegisterManager
    Created lazily (``registers.shared_manager``: one per data folder, shared with the
    discovery source "regional_register"); data under ``<data folder>/registers``.
``window.open_register_manager()`` -> RegisterManagerWindow
    Shows (or raises) the ONE register window: official sources, imported/bought lists,
    every company (see gui/register_manager.py for the design).
``window.register_candidates(area=None, limit=200)`` -> list[models.Company]
    Register companies as discovery candidates (``RegisterCompany.to_candidate()``).
    ``area``: None (the regions of the settings), a region key ("emilia-romagna"), a
    province code ("FC") or a dict {"region"|"province"|"municipality"|"ateco"|"text"}.
    Reads the database: call it on a worker (``self._in_background``).
``window.register_signals_for(company_or_card)`` -> list[registers.Signal]
    Dated facts the register knows about a company, found by its tax code / VAT number
    (``ids["tax_code"]``, ``ids["vat"]``, ``fields["cf"]``, a card's ``vat`` / ``tax_code``),
    or by ``fields["register_key"]`` for a register candidate: kind "hiring" (hired with
    an incentive, source RNA), "aid" (latest public aid, source RNA), "legality" (AGCM
    rating). ``Signal.title`` is the ready text with its source, e.g. "Hired with
    incentives (latest 2026-05-04) (source: RNA)". Database read: call it on a worker
    for many companies; one company is a few indexed queries.

Hooks called by MainWindow._ext (each optional; a failure is logged, never raised):
_registers_init, _registers_build, _registers_retranslate, _registers_settings_applied,
_registers_started (start-up update check), _registers_shutdown, _registers_dialogs.
"""

from __future__ import annotations

import logging
import threading

from ..i18n import t

log = logging.getLogger(__name__)

CHECK_TIMEOUT_S = 300
SIGNAL_SOURCES = {"hired": "rna", "aid": "rna", "legality": "agcm"}


class RegistersMixin:
    """Mixed into MainWindow (see the module docstring)."""

    # --- hooks ----------------------------------------------------------------------------

    def _registers_init(self) -> None:
        self._register_manager = None
        self.register_window = None
        self._register_check_cancel = threading.Event()

    def _registers_build(self) -> None:
        panel = self.search_panel
        panel.on_open_register = self.open_register_manager
        panel.register_manager_getter = lambda: self.register_manager

    def _registers_retranslate(self) -> None:
        window = self._open_register_window()
        if window is not None:
            window.retranslate()

    def _registers_settings_applied(self) -> None:
        if self._register_manager is not None:
            self._register_manager.settings = self._settings

    def _registers_started(self) -> None:
        if not getattr(self._settings, "register_auto_check", True):
            return
        manager = self.register_manager
        cancel = self._register_check_cancel

        def work():
            reminder = manager.reminder()
            if reminder.get("never") or not reminder.get("due"):
                return []        # never downloaded, or checked recently: no network at all
            checks = manager.check_updates(cancel=cancel)
            return [c.source for c in checks if c.state in ("new", "changed")]

        def done(changed) -> None:
            if changed and not self._closed:
                from ..registers.texts import source_title

                self.show_notice(t("registers.notice.update", sources=", ".join(source_title(s) for s in changed)))

        self._in_background(work, done, lambda error: log.info("Register update check skipped: %s",
                                                                type(error).__name__),
                            name="register update check", timeout=CHECK_TIMEOUT_S, cancel=cancel)

    def _registers_shutdown(self) -> None:
        self._register_check_cancel.set()
        window = self._open_register_window()
        if window is not None:
            window.close()

    def _registers_dialogs(self) -> list:
        window = self._open_register_window()
        return [window] if window is not None else []

    # --- public API -----------------------------------------------------------------------

    @property
    def register_manager(self):
        if self._register_manager is None:
            from ..registers import shared_manager

            self._register_manager = shared_manager(self._settings)
        return self._register_manager

    def _open_register_window(self):
        window = getattr(self, "register_window", None)
        if window is None:
            return None
        try:
            if window._closed:
                return None
            window.isVisible()          # raises when the C++ object is gone
        except RuntimeError:
            self.register_window = None
            return None
        return window

    def open_register_manager(self):
        """The Register manager window (non-modal, one at a time)."""
        window = self._open_register_window()
        if window is not None:
            window.raise_()
            window.activateWindow()
            return window
        from .register_manager import RegisterManagerWindow

        dialogs = self.dialogs
        log.info("Register manager opened")
        window = RegisterManagerWindow(
            self, self.register_manager, store=self._archive, runner=self._in_background,
            progress=self._register_progress, notice=self.show_notice,
            ask=lambda text: dialogs.ask(text), choose_file=lambda title, filters: dialogs.open_file(title, filters),
            choose_save=lambda title, default: dialogs.save_file(title, default, "CSV (*.csv)"),
            on_changed=self._register_changed)
        self.register_window = window
        window.show()
        return window

    def _register_progress(self, owner: str, state) -> None:
        if self._closed:
            return
        if state is None:
            self.status_progress.finish(owner)
        else:
            self.status_progress.show(owner, state)

    def _register_changed(self) -> None:
        try:
            self.search_panel.refresh_imports()
        except Exception:  # noqa: BLE001
            log.debug("Search tab register summary not refreshed", exc_info=True)

    def register_candidates(self, area=None, limit: int = 200) -> list:
        """See the module docstring (runs on the caller's worker)."""
        from ..registers.lists import UNCODED_KEPT

        db = self.register_manager.db
        filters: dict = {"only_active": True}
        if isinstance(area, dict):
            filters.update({k: v for k, v in area.items()
                            if k in ("region", "province", "municipality", "text") and v})
            if area.get("ateco"):
                filters["ateco_prefixes"] = tuple(area["ateco"])
                filters["keep_uncoded"] = UNCODED_KEPT
        elif isinstance(area, str) and len(area.strip()) == 2:
            filters["province"] = area.strip().upper()
        elif isinstance(area, str) and area.strip():
            filters["region"] = area.strip()
        result = []
        regions = [filters.pop("region")] if "region" in filters else (
            [] if "province" in filters or "municipality" in filters else self.register_manager.regions())
        for region in regions or [None]:
            kwargs = dict(filters)
            if region:
                kwargs["region"] = region
                kwargs["include_unplaced"] = False
            records = db.find(limit=max(1, int(limit)) - len(result), **kwargs)
            result += [record.to_candidate(filters.get("province", "")) for record in records]
            if len(result) >= limit:
                break
        return result[:limit]

    def register_signals_for(self, company_or_card) -> list:
        """See the module docstring."""
        from ..registers.model import Signal

        record = self._register_record_of(company_or_card)
        if record is None:
            return []
        signals = []
        for badge in record.badges():
            kind = badge.get("kind", "")
            source = SIGNAL_SOURCES.get(kind, "")
            title = badge.get("text", "")
            if source:
                title = t("registers.signal.with_source", text=title, source=source.upper())
            if kind == "hired":
                for day in badge.get("dates") or [badge.get("date", "")]:
                    signals.append(Signal("hiring", str(day or ""), title))
            else:
                signals.append(Signal(kind, str(badge.get("date") or ""), title))
        return signals

    def _register_record_of(self, item):
        from ..registers.model import company_cf

        db = self.register_manager.db
        fields = getattr(item, "fields", None) or {}
        ids = getattr(item, "ids", None) or {}
        key = fields.get("register_key") if isinstance(fields, dict) else None
        if key:
            found = db.get(str(key))
            if found is not None:
                return found
        candidates = []
        for value in (ids.get("tax_code") if isinstance(ids, dict) else None,
                      ids.get("vat") if isinstance(ids, dict) else None,
                      fields.get("cf") if isinstance(fields, dict) else None,
                      fields.get("vat") if isinstance(fields, dict) else None,
                      getattr(item, "tax_code", None), getattr(item, "vat", None)):
            if isinstance(value, dict):
                value = value.get("value")
            code = company_cf(value)
            if code and code not in candidates:
                candidates.append(code)
        for code in candidates:
            found = db.get(code)
            if found is not None:
                return found
            matches = db.find(text=code, only_active=False, limit=1)   # the VAT number of another key
            if matches:
                return matches[0]
        return None
