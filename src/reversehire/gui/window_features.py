"""The main window's part for the recruiter analysis, the job search, the "Match with my
CV" values and the maps (a mixin of MainWindow, kept apart to keep main_window.py
readable). Everything slow runs through the window's `_in_background` (deadline,
cancel, central logging); progress goes to the shared bottom bar as "done/estimated";
errors go to the log and to the non-blocking status-bar notice.
"""

from __future__ import annotations

import logging

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from .. import applog, companies, config
from ..i18n import t
from .cv_match import MatchService
from .jobsearch_panel import EMPLOYERS_TIMEOUT_S, JobSearchPanel
from .lazy_map import LazyMap
from .progress_bar import BarState
from .recruiter_analysis import RecruiterAnalysisPanel
from .widgets import wrapped_label

log = logging.getLogger(__name__)

MAX_ANALYSIS_POSTINGS = 200
EMPLOYER_INDEX_SEARCHES = 5  # saved company searches whose companies feed the employer hypotheses
REGISTER_PER_TERM = 60       # job search, spontaneous companies: register rows per word of the search
REGISTER_BY_SECTOR = 150     # ... and through the ATECO divisions the search names


class FeaturesMixin:
    """Mixed into MainWindow (see the module docstring)."""

    # --- set-up -----------------------------------------------------------------------------

    def _init_features(self) -> None:
        self.match_service = MatchService(self._in_background)
        self._cv_analysis = None
        self.paste_dialog = None
        self.employers_dialog = None
        self._register_job_secrets()

    def _register_job_secrets(self) -> None:
        for name in config.JOBSEARCH_KEY_FIELDS:
            applog.register_secret(getattr(self._settings, name, "") or "")

    def _palette_and_dark(self):
        from . import theme

        return theme.PALETTES[self._effective_theme], self._effective_theme == "dark"

    def _build_analysis_panel(self) -> RecruiterAnalysisPanel:
        panel = RecruiterAnalysisPanel(self._in_background, opener=self._opener)
        panel.cv_provider = self._cv_document
        panel.ai_estimate_provider = lambda: getattr(self._cv_analysis, "estimate", None)
        panel.postings_provider = self._analysis_postings
        panel.companies_provider = lambda: companies.list_cards(self._archive)
        panel.answers_provider = self._knockout_answers
        panel.embedder_provider = self._analysis_embedder
        panel.on_progress = lambda done, total, text: self._feature_progress("analysis", done, total, text)
        panel.on_finished = lambda text: self._feature_finished("analysis", text)
        panel.on_open_answers = lambda: self.open_settings("answers")
        self.analysis_panel = panel
        return panel

    def _build_jobs_panel(self) -> JobSearchPanel:
        panel = JobSearchPanel(self._in_background, self.match_service, opener=self._opener,
                               settings_provider=lambda: self._settings)
        panel.store = self._archive
        panel.geocoder_factory = self._create_geocoder
        panel.on_progress = lambda done, total, text: self._feature_progress("jobs", done, total, text)
        panel.on_finished = lambda text: self._feature_finished("jobs", text)
        panel.on_create_card = self.create_card_from_posting
        panel.on_possible_employers = self.open_possible_employers
        panel.on_simulate = self.simulate_posting
        panel.on_paste = self.open_paste_dialog
        panel.embedder_provider = self._get_embedder
        panel.ask = lambda text: self.dialogs.ask(text, warning=True)
        panel.on_edit_pasted = self._edit_pasted_posting
        panel.on_bar = self._jobs_companies_progress
        self._wire_jobs_companies(panel.companies_view)
        self.jobs_panel = panel
        return panel

    # --- job search: companies for a spontaneous application ---------------------------------

    def _wire_jobs_companies(self, view) -> None:
        from .affinity_service import preferences_of

        view.cards_provider = lambda: companies.list_cards(self._archive)
        view.register_provider = self._jobs_register_candidates
        view.map_provider = self._jobs_map_candidates
        view.embedder_provider = self._get_embedder
        view.vector_store = self._archive
        view.affinity_service_getter = lambda: getattr(self, "affinity_service", None)
        view.preferences_provider = lambda: preferences_of(self._settings)
        view.on_progress = self._jobs_companies_progress
        view.on_analyse = self.analyse_spontaneous_company
        view.on_open_card = self._open_card_from_jobs
        view.on_notice = self.show_notice
        self.jobs_suggestions = None
        try:
            from .suggestions_view import SuggestionsView, brief_for_query
        except Exception:  # noqa: BLE001 - the assistant part is optional
            log.warning("Assistant suggestions not available in the job search", exc_info=True)
            view.set_suggestions(None)
            return

        def brief():
            from ..jobsearch.spontaneous import direction_for_query

            query = self.jobs_panel.query
            text = (getattr(query, "role", "") or getattr(query, "raw", "") or "") if query is not None else ""
            direction = direction_for_query(query, preferences_of(self._settings)) if query is not None else None
            return brief_for_query(self, text, directions=[direction] if direction else None)

        self.jobs_suggestions = SuggestionsView(self, brief_provider=brief,
                                                on_added=lambda _card: self._refresh_favourites())
        self.jobs_suggestions.setObjectName("jobsSuggestionsView")
        view.set_suggestions(self.jobs_suggestions)

    def _jobs_companies_progress(self, owner: str, state) -> None:
        if self._closed:
            return
        if state is None:
            self.status_progress.finish(owner, self._search_final_state())
        else:
            self.status_progress.show(owner, state)

    def _jobs_register_candidates(self, query) -> tuple[list, list]:
        """(register companies whose name has a word of the search, register companies of
        the ATECO divisions the search names); worker thread."""
        from ..jobsearch import lexicon
        from ..jobsearch.spontaneous import ateco_prefixes_for

        base: dict = {}
        if getattr(query, "province", None):
            base["province"] = query.province
        elif getattr(query, "region_key", None):
            base["region"] = query.region_key
        terms: list[str] = []
        try:
            extra = [term for term, _kind in query.also_terms()]
        except Exception:  # noqa: BLE001
            extra = []
        for term in list(query.role_words) + extra:
            if len(term) >= 4 and term not in lexicon.GENERIC_WORDS and term not in terms:
                terms.append(term)
        by_text: list = []
        for term in terms[:6]:
            by_text += self.register_candidates(dict(base, text=term), limit=REGISTER_PER_TERM)
        prefixes, _label = ateco_prefixes_for(query)
        by_sector = self.register_candidates(dict(base, ateco=prefixes), limit=REGISTER_BY_SECTOR) \
            if prefixes else []
        return by_text, by_sector

    def _jobs_map_candidates(self, query) -> list:
        """The companies of the local map index (only when it exists already); worker thread."""
        from .suggestions_view import local_map_index

        index = local_map_index(self._settings)
        if index is None:
            return []
        if getattr(query, "lat", None) is not None and getattr(query, "lon", None) is not None:
            return index.radius(query.lat, query.lon, float(query.radius_km or 40.0))
        return index.region()

    def analyse_spontaneous_company(self, candidate):
        """"Analyse now" / "Create card" of a company for a spontaneous application: the card
        is created when needed (with where it came from), then built in the background."""
        from datetime import date

        from .jobsearch_companies import origin_text

        card = companies.get(self._archive, candidate.card_id) if candidate.card_id is not None else None
        if card is None:
            item = candidate.item
            fields = getattr(item, "fields", None) or {}
            ids = getattr(item, "ids", None) or {}
            query = self.jobs_panel.query
            notes = t("jobs.companies.provenance", query=(getattr(query, "raw", "") or "–") if query else "–",
                      origin=origin_text(candidate.origin), date=date.today().isoformat())
            sectors = [str(fields.get("ateco_text"))] if isinstance(fields, dict) and fields.get("ateco_text") \
                else list(getattr(item, "sectors", None) or [])
            try:
                card = companies.add_manual(self._archive, item.name, getattr(item, "site", None),
                                            address=getattr(item, "address", "") or "", lat=item.lat, lon=item.lon,
                                            sectors=sectors, notes=notes, tags=["job search", "spontaneous"])
            except Exception:  # noqa: BLE001
                log.warning("Card of a spontaneous company not created", exc_info=True)
                self.show_notice(t("error.generic"))
                return None
            legal = {k: str(v) for k, v in (("vat", ids.get("vat")), ("tax_code", ids.get("tax_code")),
                                             ("ateco", ids.get("ateco"))) if v}
            if legal:
                try:
                    self._archive.card_update(card.id, legal)
                except Exception:  # noqa: BLE001 - the card exists, the identifiers come with the build
                    log.info("Legal identifiers not copied to the new card", exc_info=True)
            log.info("Company card created for a spontaneous application (%s)", candidate.origin)
        self._refresh_favourites()
        self.companies_panel.select_card(card.id)
        started = self.companies_panel.card_view.build()
        self.show_notice(t("jobs.card.created", name=card.name) + (" " + t("jobs.card.building") if started else ""))
        return card

    def _open_card_from_jobs(self, card_id: int) -> None:
        from .menus import show_tab_of

        self.companies_panel.select_card(card_id)
        show_tab_of(self.companies_panel)
        log.info("Company card opened from the job search")

    def _build_results_view(self) -> QWidget:
        """The results table with its map view (the map is built on first use)."""
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        self.results_map_button = QPushButton()
        self.results_map_button.clicked.connect(lambda: self.toggle_results_map())
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        row.addWidget(self.results_map_button)
        self.results_map = LazyMap(lambda: self._settings, self._palette_and_dark)
        self.results_map.pointActivated.connect(self._results_point)
        self.results_views = QStackedWidget()
        self.results_views.addWidget(self.results_table)
        self.results_views.addWidget(self.results_map)
        layout.addLayout(row)
        layout.addWidget(self.results_views, 1)
        return holder

    def _build_search_map(self) -> None:
        """Tab 2: the start point and the radius on a map (shown on request)."""
        panel = self.search_panel
        grid = panel.place_box.layout()
        self.search_map_button = QPushButton()
        self.search_map_button.clicked.connect(lambda: self.toggle_search_map())
        self.search_map_note = wrapped_label(role="muted")
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.search_map_button)
        row.addWidget(self.search_map_note, 1)
        self.search_map = LazyMap(lambda: self._settings, self._palette_and_dark)
        self.search_map.setMinimumHeight(self.fontMetrics().lineSpacing() * 14)
        self.search_map.hide()
        grid.addLayout(row, 4, 1)
        grid.addWidget(self.search_map, 5, 0, 1, 2)
        panel.on_location_changed = self._update_search_map
        panel.radius_field.textChanged.connect(lambda _text: self._update_search_map())

    def wire_search_box(self, box) -> None:
        """The shared search box of a list runs on the window's worker with its embedder."""
        box.runner = self._in_background
        box.embedder_provider = self._get_embedder
        box.store = self._archive

    def _feature_menus(self, mirror) -> None:
        for box in (self.results_table.search_box, self.history.search_box, self.jobs_panel.results_search):
            self.wire_search_box(box)
        analysis, jobs, detail = self.analysis_panel, self.jobs_panel, self.detail
        self.analysis_menu = self.cv_menu.addMenu("")
        for button in (analysis.run_button, analysis.cancel_button, analysis.answers_button):
            self.analysis_menu.addAction(mirror(button))
        self.analysis_menu.addAction(mirror(analysis.compare_help_button, lambda: t("analysis.compare.help_title")))
        for button in (jobs.search_button, jobs.cancel_button, jobs.paste_button):
            self.jobs_menu.addAction(mirror(button))
        self.jobs_menu.addSeparator()
        for button in (jobs.open_button, jobs.card_button, jobs.employers_button, jobs.favourite_button,
                       jobs.simulate_button, jobs.map_button):
            self.jobs_menu.addAction(mirror(button))
        self.jobs_menu.addSeparator()
        for button in (jobs.pasted_button, jobs.pasted_edit_button, jobs.pasted_delete_button):
            self.jobs_menu.addAction(mirror(button))
        self.jobs_menu.addSeparator()
        for button in (jobs.linkedin_button, jobs.indeed_button):
            self.jobs_menu.addAction(mirror(button))
        self.jobs_menu.addSeparator()
        for button in (jobs.open_search_button, jobs.note_search_button, jobs.delete_search_button,
                       jobs.delete_selected_button):
            self.jobs_menu.addAction(mirror(button))
        self.jobs_menu.addSeparator()
        spontaneous = jobs.companies_view
        for button in (spontaneous.analyse_button, spontaneous.card_button, spontaneous.open_card_button,
                       spontaneous.refresh_button):
            self.jobs_menu.addAction(mirror(button))
        if getattr(self, "jobs_suggestions", None) is not None:
            for action in self.jobs_suggestions.actions():
                self.jobs_menu.addAction(action)
        self.jobs_menu.addSeparator()
        self.jobs_settings_action = QAction(self, triggered=lambda: self.open_settings("jobsearch"))
        self.jobs_menu.addAction(self.jobs_settings_action)
        self.results_menu.insertAction(self.results_menu.actions()[0], mirror(detail.simulate_button))
        self.results_menu.addSeparator()
        self.results_menu.addAction(mirror(self.results_map_button))
        self.search_menu.addAction(mirror(self.search_map_button))
        self._build_map_actions()
        for menu in (self.analysis_menu, self.jobs_menu):
            menu.aboutToShow.connect(self._mirrors.refresh)
        # hooks of the existing views
        detail.match_service = self.match_service
        detail.on_simulate = self.simulate_posting
        card = self.companies_panel.card_view
        card.match_service = self.match_service
        card.on_simulate = self.simulate_company_system

    # --- map backgrounds: offline switch, online button, retry -------------------------------

    def _build_map_actions(self) -> None:
        """One set of QActions for the three maps (tab 2, Results, Job search): the banner's
        "Show the online map" / "Retry" buttons and the corner "Work offline" toggle of every
        map run them, and they are in the Search menu (gui/menus.py rule)."""
        self.map_offline_action = QAction(self)
        self.map_offline_action.setCheckable(True)
        self.map_offline_action.setChecked(bool(getattr(self._settings, "map_offline", False)))
        self.map_offline_action.toggled.connect(lambda on: self.set_map_offline(bool(on)))
        self.map_online_action = QAction(self, triggered=lambda: self.set_map_offline(False))
        self.map_retry_action = QAction(self, triggered=lambda: self.retry_maps())
        actions = {"online": self.map_online_action, "offline": self.map_offline_action,
                   "retry": self.map_retry_action}
        for view in self._lazy_maps():
            view.actions = actions
            view.on_offline = self.set_map_offline
        self.search_menu.addSeparator()
        for action in (self.map_online_action, self.map_offline_action, self.map_retry_action):
            self.search_menu.addAction(action)
        self._sync_map_actions()

    def _lazy_maps(self) -> list:
        return [view for view in (getattr(self, "search_map", None), getattr(self, "results_map", None),
                                  getattr(getattr(self, "jobs_panel", None), "map", None)) if view is not None]

    def _sync_map_actions(self) -> None:
        offline = bool(getattr(self._settings, "map_offline", False))
        action = getattr(self, "map_offline_action", None)
        if action is None:
            return
        if action.isChecked() != offline:
            blocked = action.blockSignals(True)
            action.setChecked(offline)
            action.blockSignals(blocked)
        self.map_online_action.setEnabled(offline)
        self.map_retry_action.setEnabled(not offline)

    def set_map_offline(self, offline: bool) -> None:
        """The map's own switch: saved like a change in Settings, applied to every map at once."""
        offline = bool(offline)
        if bool(getattr(self._settings, "map_offline", False)) == offline:
            self._sync_map_actions()
            return
        self._settings.map_offline = offline
        log.info("Settings changed from the map: map_offline=%s", offline)
        self._save_settings()
        self._feature_settings_applied()

    def retry_maps(self) -> None:
        for view in self._lazy_maps():
            view.retry()

    # --- texts and theme ----------------------------------------------------------------------

    def _feature_retranslate(self) -> None:
        self.jobs_menu.setTitle(t("jobsearch.menu.title"))
        self.analysis_menu.setTitle(t("analysis.menu"))
        self.jobs_settings_action.setText(t("jobs.menu.settings"))
        self.map_online_action.setText(t("mapview.show_online"))
        self.map_offline_action.setText(t("mapview.work_offline"))
        self.map_retry_action.setText(t("mapview.retry_maps"))
        self.results_map_button.setText(t("results.show_table") if self.results_views.currentIndex() == 1
                                        else t("results.show_map"))
        self.search_map_button.setText(t("place.hide_map") if self.search_map.isVisibleTo(self.search_panel)
                                       else t("place.show_map"))
        self.analysis_panel.retranslate()
        self.jobs_panel.retranslate()
        for view in (self.results_map, self.search_map):
            view.retranslate()
        self._update_search_map()

    def _feature_theme(self, palette) -> None:
        dark = self._effective_theme == "dark"
        self.analysis_panel.apply_palette(palette)
        self.jobs_panel.apply_palette(palette, dark)
        for view in (self.results_map, self.search_map):
            view.apply_theme(palette, dark)

    # --- the shared progress bar ---------------------------------------------------------------

    def _feature_progress(self, owner: str, done: int, total: int, text: str) -> None:
        if self._closed:
            return
        panel = self.analysis_panel if owner == "analysis" else self.jobs_panel
        if not panel.running:
            return  # a late progress signal after the end: the bar was already released
        from .main_window import counts_text

        if total > 0:
            state = BarState(min(1.0, done / total), counts_text(done, total, True), text)
        else:
            state = BarState(None, "", text)
        self.status_progress.show(owner, state)
        if text:
            self.show_notice(text)

    def _feature_finished(self, owner: str, text: str) -> None:
        if self._closed:
            return
        self.status_progress.finish(owner, self._search_final_state())
        self.show_notice(text)

    # --- CV ----------------------------------------------------------------------------------

    def _feature_cv_read(self, analysis) -> None:
        self._cv_analysis = analysis
        profile = getattr(analysis, "profile", None)
        self.match_service.set_profile(profile)
        panel = self.analysis_panel
        if not panel.role_field.text().strip():
            roles = [r.value for r in (getattr(profile, "roles", None) or [])]
            if roles:
                panel.role_field.setText(roles[0])
        panel.cv_changed()
        self.detail.refresh_matches()
        self.companies_panel.card_view.render()
        if self.jobs_panel.postings:
            self.match_service.request(self.jobs_panel.postings, self.jobs_panel._fill_table)
            self.jobs_panel._fill_table()

    def _cv_document(self):
        """The CV currently loaded, for the simulators (None before a CV is read)."""
        analysis = self._cv_analysis
        if analysis is None or getattr(analysis, "profile", None) is None:
            return None
        from ..recruiter_sim import CvDocument

        profile = analysis.profile
        return CvDocument(profile, getattr(analysis, "reading", None), getattr(profile, "cv_path", None) or None)

    def _knockout_answers(self):
        from ..recruiter_sim import KnockoutAnswers

        return KnockoutAnswers.from_dict(config.clean_knockout_answers(
            getattr(self._settings, "knockout_answers", {}) or {}))

    def _analysis_postings(self) -> list:
        """Postings offered as targets: Results, Job search, pasted ones (label, posting)."""
        items = []
        for company, evaluation in self.results_table.rows:
            for posting in (evaluation.postings if evaluation is not None else []) or []:
                items.append((t("analysis.from.results", title=posting.title, company=company.name), posting))
        for posting in self.jobs_panel.postings:
            items.append((t("analysis.from.jobs", title=posting.title, company=posting.publisher or "–"), posting))
        try:
            from ..jobsearch import Posting

            for row in self._archive.list_postings(origin="pasted")[:50]:
                posting = Posting.from_dict(row["data"])
                items.append((t("analysis.from.pasted", title=posting.title, company=posting.publisher or "–"),
                              posting))
        except Exception:  # noqa: BLE001 - a busy database must not empty the list
            log.warning("Pasted postings not read", exc_info=True)
        return items[:MAX_ANALYSIS_POSTINGS]

    # --- simulate --------------------------------------------------------------------------------

    def simulate_posting(self, posting) -> None:
        """"Simulate screening" of a posting: the Recruiter analysis tab, preconfigured."""
        self.tabs.setCurrentWidget(self.analysis_panel)
        self.analysis_panel.select_posting(posting)
        log.info("Recruiter analysis preconfigured with a posting")
        self.show_notice(t("analysis.preconfigured"))

    def simulate_company_system(self, card_id: int, profile_key: str) -> None:
        """"Simulate this system on my CV" of a company card."""
        self.tabs.setCurrentWidget(self.analysis_panel)
        self.analysis_panel.select_company(card_id, profile_key)
        log.info("Recruiter analysis preconfigured with a company (%s)", profile_key)
        self.show_notice(t("analysis.preconfigured"))

    # --- job search actions --------------------------------------------------------------------

    def create_card_from_posting(self, posting):
        """"Create company card": the seed of the ad, added (or found) and built in the background."""
        from ..jobsearch import company_seed_from_posting

        seed = company_seed_from_posting(posting)
        if not seed.get("name"):
            self.show_notice(t("jobs.card.no_name"))
            return None
        fields = {k: v for k, v in seed.items() if k != "extra" and v is not None}
        try:
            card = companies.add_manual(self._archive, **fields)
        except Exception:  # noqa: BLE001
            log.warning("Company card not created from a posting", exc_info=True)
            self.show_notice(t("error.generic"))
            return None
        log.info("Company card created from a posting (%s)",
                 "agency" if seed.get("extra", {}).get("hidden_employer") else "employer")
        self._refresh_favourites()
        self.companies_panel.select_card(card.id)
        started = self.companies_panel.card_view.build()
        self.show_notice(t("jobs.card.created", name=card.name) + (" " + t("jobs.card.building") if started else ""))
        return card

    def open_possible_employers(self, posting) -> None:
        """"Who might be behind this ad?": the hypotheses, computed on a worker."""
        from ..jobsearch import estimate_employers

        archive = self._archive

        def work():
            cards = companies.list_cards(archive)
            index = []
            for search in archive.list_searches()[:EMPLOYER_INDEX_SEARCHES]:
                index += [company for company, _evaluation in archive.results_of(search["id"])]
            return estimate_employers(posting, index, cards)

        def done(estimate) -> None:
            from .jobsearch_dialogs import EmployersDialog

            if self._closed:
                return
            if self.employers_dialog is not None:
                self.employers_dialog.close()
            palette, _dark = self._palette_and_dark()
            dialog = EmployersDialog(self, posting, estimate, palette=palette)
            dialog.finished.connect(lambda _r, d=dialog: self._dialog_closed("employers_dialog", d))
            self.employers_dialog = dialog
            dialog.show()
            self._set_status("status.ready")
            self.jobs_panel.add_hypotheses(posting, estimate.hypotheses)
            log.info("Employer hypotheses shown (%d, other %.0f%%)", len(estimate.hypotheses),
                     100 * estimate.other_share)

        def failed(error) -> None:
            log.warning("Employer hypotheses failed: %s", type(error).__name__)
            self.show_notice(t("jobs.employers.failed"))

        self.show_notice(t("jobs.employers.computing"))
        self._in_background(work, done, failed, name="possible employers", timeout=EMPLOYERS_TIMEOUT_S)

    def open_paste_dialog(self):
        """"Paste posting / company page…" (text copied by hand, saved only on confirmation)."""
        from .jobsearch_dialogs import PasteDialog

        if self.paste_dialog is not None:
            self.paste_dialog.raise_()
            self.paste_dialog.activateWindow()
            return self.paste_dialog
        palette, _dark = self._palette_and_dark()
        dialog = PasteDialog(self, on_save_posting=self._save_pasted_posting,
                             on_save_company=self._save_pasted_company, palette=palette)
        dialog.finished.connect(lambda _r, d=dialog: self._dialog_closed("paste_dialog", d))
        self.paste_dialog = dialog
        dialog.show()
        return dialog

    def _save_pasted_posting(self, posting) -> int | None:
        replaced = getattr(self.paste_dialog, "replaces_posting_id", None) if self.paste_dialog else None
        try:
            posting_id = self._archive.save_posting(posting.to_dict(), origin="pasted")
            if replaced is not None and replaced != posting_id:
                self._archive.delete_posting(replaced)   # "Edit": the corrected posting replaces the old one
        except Exception:  # noqa: BLE001
            log.warning("Pasted posting not saved", exc_info=True)
            self.show_notice(t("error.generic"))
            return None
        log.info("Pasted posting saved (source %s)%s", posting.source, ", replacing the old one" if replaced else "")
        self.show_notice(t("jobs.paste.saved_posting", title=posting.title))
        panel = self.jobs_panel
        if panel.showing_pasted:
            panel.open_pasted()
        else:
            panel._update_pasted_buttons()
        return posting_id

    def _edit_pasted_posting(self, row: dict):
        """"Edit" of a pasted posting: the paste window again, with the posting's text; the
        reviewed posting replaces the old one when saved."""
        data = dict(row.get("data") or {})
        dialog = self.open_paste_dialog()
        text = "\n".join(str(data.get(k) or "") for k in ("title", "company", "location", "salary", "contract",
                                                            "description") if data.get(k))
        dialog.paste_box.setPlainText(text)
        if data.get("url") and hasattr(dialog, "url_field"):
            dialog.url_field.setText(str(data["url"]))
        dialog.replaces_posting_id = row.get("id")
        return dialog

    def _save_pasted_company(self, fields: dict):
        site = (fields.get("website") or "").strip() or None
        if site and "://" not in site:
            site = "https://" + site
        notes = t("jobs.paste.card_note", source=fields.get("source", ""), url=fields.get("source_url", "") or "–")
        try:
            card = companies.add_manual(self._archive, fields["company"], site,
                                        address=fields.get("headquarters", "") or "",
                                        description=fields.get("overview", "") or "", notes=notes,
                                        tags=["pasted"])
        except Exception:  # noqa: BLE001
            log.warning("Pasted company not saved", exc_info=True)
            self.show_notice(t("error.generic"))
            return None
        log.info("Pasted company page saved as a card")
        self._refresh_favourites()
        self.show_notice(t("jobs.paste.saved_company", name=card.name))
        return card

    # --- maps -------------------------------------------------------------------------------

    def toggle_results_map(self) -> None:
        showing_map = self.results_views.currentIndex() == 1
        if showing_map:
            self.results_views.setCurrentIndex(0)
        else:
            self.results_map.ensure_soon()
            self.results_views.setCurrentIndex(1)
            self._update_results_map()
        log.info("Results: %s view", "table" if showing_map else "map")
        self.results_map_button.setText(t("results.show_map") if showing_map else t("results.show_table"))

    def _update_results_map(self) -> None:
        from ..mapview import MapPoint
        from .results_table import status_of, status_text

        rows = self.results_table.visible_rows()
        points = []
        for row in rows:
            company, evaluation = row
            index = self.results_table.row_index(row)
            if company.lat is None or company.lon is None:
                continue
            tooltip = status_text(status_of(evaluation))
            if company.distance_km is not None:
                tooltip += f" · {company.distance_km:.1f} km"
            points.append(MapPoint(f"r{index}", company.lat, company.lon, company.name, "company", tooltip=tooltip))
        center, radius = None, None
        if self._current_search is not None:
            try:
                search = self._archive.read_search(self._current_search)
            except Exception:  # noqa: BLE001
                search = None
            if search is not None and search.get("lat") is not None:
                center, radius = (search["lat"], search["lon"]), search.get("radius_km")
                points.append(MapPoint("home", center[0], center[1], search.get("place") or "", "home"))
        self.results_map.set_data(points, center=center, radius_km=radius)
        row = self.results_table.selected()
        if row is not None and row in rows:
            self.results_map.select(f"r{self.results_table.row_index(row)}")

    def _results_point(self, point_id: str) -> None:
        if point_id.startswith("r") and point_id[1:].isdigit():
            self.results_table.select(int(point_id[1:]))

    def _results_changed(self) -> None:
        if self.results_map.created and self.results_views.currentIndex() == 1:
            self._update_results_map()

    def toggle_search_map(self) -> None:
        visible = self.search_map.isVisibleTo(self.search_panel)
        if visible:
            self.search_map.hide()
        else:
            self.search_map.show()
            self.search_map.ensure_soon()
            self._update_search_map()
        log.info("Location tab: map %s", "hidden" if visible else "shown")
        self.search_map_button.setText(t("place.show_map") if visible else t("place.hide_map"))

    def _update_search_map(self) -> None:
        from ..mapview import MapPoint

        panel = self.search_panel
        place = panel.valid_place()
        radius = panel.radius()
        if place is None:
            self.search_map_note.setText(t("place.map_note_none"))
            self.search_map.set_data([])
            return
        self.search_map_note.setText(t("place.map_note", place=place.name,
                                       radius=f"{radius:g}" if radius else "–"))
        self.search_map.set_data([MapPoint("home", place.lat, place.lon, place.name, "home")],
                                 center=(place.lat, place.lon), radius_km=radius)

    def _feature_settings_applied(self) -> None:
        self._register_job_secrets()
        for view in (self.results_map, self.search_map, self.jobs_panel.map):
            view.apply_settings(self._settings)
        self._sync_map_actions()

    def _feature_dialogs(self) -> list:
        suggestions = getattr(self, "jobs_suggestions", None)
        return [self.paste_dialog, self.employers_dialog, getattr(suggestions, "preview_dialog", None)]
