"""Company suggestions from the user's own assistant, verified by the app (reusable widget).

CONTRACT (stable; embedded by the "Who could be interested in my profile" window and by
the Job search tab)::

    from reversehire.gui.suggestions_view import SuggestionsView, brief_for_query

    view = SuggestionsView(window, brief_provider=lambda: brief_for_query(window, "PLC programmer"),
                           on_added=lambda card: ..., parent=None)
    view.start()          # "Suggest companies with the assistant" (first 10)
    view.expand()         # "Show 10 more" (the batch's offset token)
    view.cancel()         # stops the running request / verification
    view.retranslate(); view.apply_palette(palette)
    view.running          # True while a request or its verification runs
    view.actions()        # the QActions of the view's buttons, for the menus

- `brief_provider()` returns the anonymised ``suggest.ProfileBrief`` to send, or None
  (no CV yet: the view says so and sends nothing).
- Before the FIRST use (``settings.suggest_notice_shown`` False) a non-modal window shows
  exactly what will be sent (``brief.preview_text()``) and the cost notice; "Send" stores
  the acceptance and starts the request. "What is sent" shows the same window any time.
- The list: one row per suggestion with its status badge (VERIFIED / PARTIAL /
  UNVERIFIED / REJECTED), the place, the website, the verification facts with their
  sources and the assistant's sentence in a column labelled as its opinion. UNVERIFIED
  rows are greyed and cannot be added; REJECTED rows are hidden until "Show rejected".
  Header-click sorting; right-click menu with the same actions as the buttons.
- "Add to my companies" (VERIFIED / PARTIAL only) creates the company card with
  ``companies.add_manual`` from ``VerifiedSuggestion.to_company_seed()``: the notes carry
  the provenance ("suggested by the assistant on <date>, verified by: ...") and the
  opinion, labelled as such. `on_added(card)` is called for each card.
- Everything slow runs on ONE worker of the window (``window._in_background``: deadline,
  cancel): CLI detection (``window._check_cli``), the request (``suggest.suggest_companies``)
  and the verification (``suggest.verify_batch``, 8 requests per company, 80 per batch).
  Progress goes to the shared bottom bar as "done/estimated"; errors (no CLI, malformed
  answer, timeout...) are shown in the view and as a non-blocking notice.

`brief_for_query(window, query_text, directions=None, area=None)` builds the brief of the
loaded CV with `query_text` as the first wanted role ("companies for this search").
`default_area(window)` is the area chosen on the Search tab (radius around the start TOWN,
province, region) as a ``suggest.SearchArea``.

Tests replace ``view.suggest_function``, ``view.verify_function`` and
``view.tools_factory(settings, cancel) -> (tools dict, close callable)``.
"""

from __future__ import annotations

import logging
import re
import threading
import weakref
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QHBoxLayout, QHeaderView, QPlainTextEdit,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import i18n, suggest
from ..i18n import t
from . import context_menu, table_filter, table_sort
from .menus import bind_action
from .widgets import wrapped_label

log = logging.getLogger(__name__)

PROGRESS_OWNER = "suggest"
BATCH_SIZE = 10
SUGGEST_TIMEOUT_S = 240          # the assistant's answer
JOB_TIMEOUT_S = 900              # answer + verification of 10 companies
COLUMNS = ("status", "name", "place", "site", "checks", "opinion")
STATUS_RANK = {suggest.VERIFIED: 0, suggest.PARTIAL: 1, suggest.UNVERIFIED: 2, suggest.REJECTED: 3}
LIVE_VIEWS: "weakref.WeakSet[SuggestionsView]" = weakref.WeakSet()   # the idle analysis checks `running`


def running_views(window) -> list:
    """The suggestion views of `window` that are running a request now."""
    out = []
    for view in list(LIVE_VIEWS):
        try:
            if view.window_ref is window and view.running:
                out.append(view)
        except RuntimeError:  # deleted by Qt
            continue
    return out


def stext(key: str, **params) -> str:
    """A text of the suggest package (locale first, English default)."""
    return suggest.text(key, **params)


# --- the brief and the area ------------------------------------------------------------------------


def _settings(window):
    return getattr(window, "_settings", None)


def _town_of(place_name: str) -> str:
    """The town of a geocoded start point ("Piazza Maggiore, Bologna" -> "Bologna"): never a
    street or a number (the brief drops anything address-like anyway)."""
    parts = [p.strip() for p in str(place_name or "").split(",") if p.strip()]
    for part in reversed(parts):
        if not re.search(r"\d", part) and len(part) > 1:
            return part
    return ""


def default_area(window) -> suggest.SearchArea:
    """The area of the Search tab: radius around the start TOWN, province or region (IT)."""
    from ..geo import italy
    from ..geo.regions import provinces_of, region_for

    area = suggest.SearchArea(kind=suggest.AREA_COUNTRY, country="IT")
    panel = getattr(window, "search_panel", None)
    if panel is None:
        return area
    try:
        choices = panel.discovery_choices()
    except Exception:  # noqa: BLE001
        return area
    kind = choices.get("area") or "radius"
    code = str(choices.get("province") or "").upper()
    region = region_for(choices.get("region") or "") if choices.get("region") else None
    if kind == "province" and code:
        found = italy.province(code)
        area.kind, area.province = suggest.AREA_PROVINCE, code
        area.province_name = found.name if found is not None else code
        area.region = italy.region_name(found.region) if found is not None else ""
        return area
    if kind == "region" and region is not None:
        area.kind, area.region = suggest.AREA_REGION, region.name
        area.provinces = [p.code for p in provinces_of(region)]
        return area
    place = panel.valid_place() if hasattr(panel, "valid_place") else None
    radius = panel.radius() if hasattr(panel, "radius") else None
    town = _town_of(getattr(place, "name", "") or panel.place_text())
    if place is not None and radius and town:
        area.kind, area.town, area.radius_km = suggest.AREA_RADIUS, town, float(radius)
        area.lat, area.lon = float(place.lat), float(place.lon)
        return area
    if code:
        found = italy.province(code)
        area.kind, area.province = suggest.AREA_PROVINCE, code
        area.province_name = found.name if found is not None else code
    return area


def _profile(window):
    panel = getattr(window, "profile_panel", None)
    try:
        return panel.current_profile() if panel is not None else None
    except Exception:  # noqa: BLE001
        return None


def _preferences(window):
    from .affinity_service import preferences_of

    try:
        return preferences_of(_settings(window))
    except Exception:  # noqa: BLE001
        log.debug("Affinity preferences not readable", exc_info=True)
        return None


def profile_brief(window, area: suggest.SearchArea | None = None,
                  directions: list[str] | None = None) -> suggest.ProfileBrief | None:
    """The anonymised brief of the loaded CV (None without a CV)."""
    profile = _profile(window)
    if profile is None:
        return None
    brief = suggest.build_profile_brief(profile, _preferences(window), area or default_area(window))
    if directions:
        wanted = [str(d) for d in directions]
        kept = [(i, n) for i, n in brief.directions if i in wanted]
        brief.directions = kept or brief.directions
    return brief


def brief_for_query(window, query_text: str, directions: list[str] | None = None,
                    area: suggest.SearchArea | None = None) -> suggest.ProfileBrief:
    """The brief of the loaded CV with `query_text` as the first wanted role (the Job search's
    "companies for this search"). Without a CV the brief holds only the query and the area.
    A query that contains identifying text of the CV is not added (fail closed)."""
    profile = _profile(window)
    brief = profile_brief(window, area, directions) if profile is not None else None
    if brief is None:
        brief = suggest.ProfileBrief(area=area or default_area(window))
        prefs = _preferences(window)
        if prefs is not None:
            brief.company_types = suggest.build_profile_brief(None, prefs, brief.area).company_types
    query = " ".join(str(query_text or "").split())[:60]
    if query and not (profile is not None and suggest.find_leaks(query, profile)) \
            and query.casefold() not in (r.casefold() for r in brief.roles):
        brief.roles.insert(0, query)
    return brief


def verification_tools(window, settings, cancel: threading.Event):
    """(tools, close): the app's polite helpers for `suggest.verify_batch` — the Fetcher, the
    gateway (Wikidata), the register client (VIES, GLEIF) and the local map index when it was
    already built (never downloaded here)."""
    from ..discovery.gateway import PoliteGateway
    from ..registry.http import RegistryClient
    from ..web.fetch import Fetcher

    store = getattr(window, "_archive", None)
    fetcher = Fetcher(settings, store)
    gateway = PoliteGateway(settings, store)
    registry = RegistryClient(settings, store, cancel=cancel)
    tools = {"fetcher": fetcher, "gateway": gateway, "registry": registry, "place_index": local_map_index(settings)}
    try:   # the regional register (local): evidence for a suggestion without a readable website
        manager = getattr(window, "register_manager", None)
        tools["register_db"] = manager.db if manager is not None else None
    except Exception:  # noqa: BLE001 - the register is optional
        log.debug("Regional register not available for the verification", exc_info=True)

    def close() -> None:
        for item in (fetcher, gateway, registry):
            try:
                item.close()
            except Exception:  # noqa: BLE001
                log.debug("Verification helper not closed", exc_info=True)

    return tools, close


def local_map_index(settings):
    """The offline OpenStreetMap index of the settings' region, only when it exists already."""
    try:
        from ..discovery import osm_extract
        from ..geo.regions import region_for

        region = region_for(getattr(settings, "osm_region", "") or "")
        if region is None or not osm_extract.status(region).ready:
            return None
        return osm_extract.OsmIndex(osm_extract.index_path(region))
    except Exception:  # noqa: BLE001 - the map is optional
        log.debug("Local map index not available", exc_info=True)
        return None


EXCLUDE_MAX = 60   # names of known companies sent so that they are not suggested again


def card_in_area(card, area) -> bool:
    """True when a saved company may lie in the request's area (unknown place = yes for a
    radius or country, no for a province or region it does not name)."""
    import re as _re

    if area is None:
        return True
    kind = getattr(area, "kind", "")
    if kind in (suggest.AREA_PROVINCE, suggest.AREA_REGION):
        from .interest_flow import place_province, saved_in_area

        if place_province(card):   # postcode or coordinates: the same rule as the fast list
            return saved_in_area(area, card)
    address = str(getattr(card, "address", "") or "")
    codes = [area.province] if kind == suggest.AREA_PROVINCE and area.province else         list(getattr(area, "provinces", None) or []) if kind == suggest.AREA_REGION else []
    if kind in (suggest.AREA_PROVINCE, suggest.AREA_REGION):
        names = [n for n in (getattr(area, "province_name", ""), ) if n and kind == suggest.AREA_PROVINCE]
        if any(_re.search(rf"(?<![A-Za-z]){_re.escape(c)}(?![A-Za-z])", address) for c in codes if c):
            return True
        return any(n.casefold() in address.casefold() for n in names)
    if kind == suggest.AREA_RADIUS and area.lat is not None and getattr(card, "lat", None) is not None             and getattr(card, "lon", None) is not None and area.radius_km:
        from ..geo.distance import haversine_km

        return haversine_km(area.lat, area.lon, card.lat, card.lon) <= float(area.radius_km) * 1.5
    return True


# --- the preview window ------------------------------------------------------------------------------


class PreviewDialog(QDialog):
    """Exactly what is sent, and the cost notice (non-modal). `on_send()` when confirmed."""

    def __init__(self, parent, brief: suggest.ProfileBrief, cli_name: str, web_search: bool, *,
                 on_send: Callable[[], None] | None = None, exclude: list[str] | None = None):
        super().__init__(parent)
        self.setObjectName("suggestPreview")
        self._on_send = on_send
        self.exclude = list(exclude or [])
        # the exclusion list is sent too: its count, and the names on request
        self.exclude_label = wrapped_label(role="muted")
        self.exclude_button = QPushButton()
        self.exclude_button.setProperty("role", "small")
        self.exclude_button.setCheckable(True)
        self.exclude_button.toggled.connect(lambda on: self.exclude_text.setVisible(on))
        self.exclude_text = QPlainTextEdit("\n".join(self.exclude))
        self.exclude_text.setReadOnly(True)
        self.exclude_text.hide()
        self.exclude_button.setVisible(bool(self.exclude))
        self._cli_name = cli_name
        self._web = web_search
        self.intro_label = wrapped_label()
        self.text = QPlainTextEdit(brief.preview_text())
        self.text.setReadOnly(True)
        self.text.setMinimumHeight(self.fontMetrics().height() * 10)
        self.web_label = wrapped_label(role="muted")
        self.cost_label = wrapped_label(role="warning")
        self.send_button = QPushButton()
        self.send_button.setProperty("role", "accent")
        self.send_button.clicked.connect(self.send)
        self.close_button = QPushButton()
        self.close_button.clicked.connect(self.close)
        self.send_button.setVisible(on_send is not None)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.send_button)
        buttons.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.intro_label)
        layout.addWidget(self.text, 1)
        exclude_row = QHBoxLayout()
        exclude_row.addWidget(self.exclude_label, 1)
        exclude_row.addWidget(self.exclude_button)
        layout.addLayout(exclude_row)
        layout.addWidget(self.exclude_text)
        layout.addWidget(self.web_label)
        layout.addWidget(self.cost_label)
        layout.addLayout(buttons)
        self.retranslate()
        self.resize(self.fontMetrics().averageCharWidth() * 90, self.fontMetrics().height() * 26)

    def retranslate(self) -> None:
        self.setWindowTitle(stext("suggest.preview.title"))
        self.intro_label.setText(stext("suggest.preview.intro", cli=self._cli_name))
        self.exclude_label.setText(t("interest.suggest.exclude_note", n=len(self.exclude)) if self.exclude
                                   else t("interest.suggest.exclude_none"))
        self.exclude_button.setText(t("interest.suggest.exclude_show"))
        self.web_label.setText(stext("suggest.preview.web_search" if self._web else "suggest.preview.no_web_search"))
        self.cost_label.setText(t("interest.suggest.cost_notice", cli=self._cli_name))
        self.send_button.setText(stext("suggest.preview.confirm"))
        self.close_button.setText(t("interest.close") if self._on_send is None else t("interest.cancel"))

    def send(self) -> None:
        callback, self._on_send = self._on_send, None
        self.close()
        if callback is not None:
            callback()


# --- the view --------------------------------------------------------------------------------------------


def _cli_name(kind: str) -> str:
    try:
        from ..chat.messages import CLI_NAMES

        return CLI_NAMES.get(kind, kind)
    except Exception:  # noqa: BLE001
        return kind


class SuggestionsView(QWidget):
    """See the module docstring (the contract)."""

    def __init__(self, window, *, brief_provider: Callable[[], suggest.ProfileBrief | None],
                 on_added: Callable[[object], None] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("suggestionsView")
        LIVE_VIEWS.add(self)
        self.window_ref = window
        self.brief_provider = brief_provider
        self.on_added = on_added
        self.suggest_function = suggest.suggest_companies
        self.verify_function = suggest.verify_batch
        self.tools_factory: Callable = lambda settings, cancel: verification_tools(window, settings, cancel)
        self.listeners: list[Callable[[], None]] = []   # called when `running` or the list changes
        self.running = False
        self.results: list[suggest.VerifiedSuggestion] = []
        self.added: dict[int, int] = {}                 # result index -> card id
        self.last_batch: suggest.SuggestionBatch | None = None
        self.brief: suggest.ProfileBrief | None = None
        self.preview_dialog: PreviewDialog | None = None
        self.message = ""
        self._message_key: tuple[str, dict] | None = None
        self._cancel: threading.Event | None = None
        self._generation = 0
        self._palette = None
        self._progress = (0, BATCH_SIZE)

        self.intro_label = wrapped_label(role="muted")
        self.suggest_action = QAction(self, triggered=lambda _c=False: self.start())
        self.expand_action = QAction(self, triggered=lambda _c=False: self.expand())
        self.cancel_action = QAction(self, triggered=lambda _c=False: self.cancel())
        self.add_action = QAction(self, triggered=lambda _c=False: self.add_selected())
        self.preview_action = QAction(self, triggered=lambda _c=False: self.show_preview())
        self.site_action = QAction(self, triggered=lambda _c=False: self.open_site())
        self.rejected_action = QAction(self, checkable=True)
        self.rejected_action.toggled.connect(self._rejected_toggled)
        self.suggest_button = self._button(self.suggest_action, "accent")
        self.expand_button = self._button(self.expand_action)
        self.cancel_button = self._button(self.cancel_action)
        self.add_button = self._button(self.add_action)
        self.preview_button = self._button(self.preview_action, "small")
        self.rejected_check = QCheckBox()
        self.rejected_check.toggled.connect(self.rejected_action.setChecked)
        bind_action(self.rejected_check, self.rejected_action)
        self.status_label = wrapped_label(role="muted")
        self.error_label = wrapped_label(role="warning")
        self.error_label.hide()
        self.notices_label = wrapped_label(role="muted")
        self.notices_label.hide()

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setObjectName("suggestionsTable")
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.itemDoubleClicked.connect(lambda _item: self.add_selected())
        table_filter.attach(self.table)   # header-click sorting (no filter row)
        context_menu.install(self.table, lambda: [self.add_action, self.site_action, None, self.expand_action,
                                                  self.rejected_action])

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.suggest_button)
        top.addWidget(self.expand_button)
        top.addWidget(self.cancel_button)
        top.addWidget(self.preview_button)
        top.addStretch(1)
        top.addWidget(self.rejected_check)
        top.addWidget(self.add_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.intro_label)
        layout.addLayout(top)
        layout.addWidget(self.status_label)
        layout.addWidget(self.error_label)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.notices_label)
        self.retranslate()

    def _button(self, action: QAction, role: str | None = None) -> QPushButton:
        button = QPushButton()
        if role:
            button.setProperty("role", role)
        button.clicked.connect(lambda: action.trigger())
        bind_action(button, action)
        return button

    # --- public API ---------------------------------------------------------------------------

    def actions(self) -> list[QAction]:  # noqa: D401 - QWidget.actions is replaced on purpose
        return [self.suggest_action, self.expand_action, self.cancel_action, self.add_action, self.rejected_action,
                self.preview_action]

    def start(self) -> bool:
        """First batch (a new list). False when nothing was started (running, no CV, or the
        preview is waiting for the user's confirmation)."""
        return self._begin(None)

    def expand(self) -> bool:
        """The next batch of the same conversation."""
        if not self.can_expand():
            return False
        return self._begin(self.last_batch.offset_token)

    def can_expand(self) -> bool:
        batch = self.last_batch
        return (not self.running and batch is not None and bool(batch.offset_token)
                and batch.error in ("", suggest.ERR_EMPTY) and batch.more_available is not False
                and bool(self.results))

    def cancel(self) -> None:
        if self._cancel is not None and self.running:
            log.info("Assistant suggestions: cancelled by the user")
            self._cancel.set()
            self._set_status("interest.suggest.cancelling")

    def retranslate(self) -> None:
        self.intro_label.setText(t("interest.suggest.intro"))
        self.suggest_action.setText(stext("suggest.action"))
        self.expand_action.setText(stext("suggest.expand", n=BATCH_SIZE))
        self.cancel_action.setText(t("interest.cancel"))
        self.add_action.setText(t("interest.suggest.add"))
        self.preview_action.setText(t("interest.suggest.preview"))
        self.site_action.setText(t("interest.open_site"))
        hidden = sum(1 for r in self.results if r.status == suggest.REJECTED)
        self.rejected_action.setText(t("interest.suggest.show_rejected", count=hidden))
        for action, button in ((self.suggest_action, self.suggest_button), (self.expand_action, self.expand_button),
                               (self.cancel_action, self.cancel_button), (self.add_action, self.add_button),
                               (self.preview_action, self.preview_button)):
            button.setText(action.text())
        self.rejected_check.setText(self.rejected_action.text())
        headers = [t(f"interest.suggest.column.{c}") for c in COLUMNS]
        self.table.setHorizontalHeaderLabels(headers)
        for index, column in enumerate(COLUMNS):
            item = self.table.horizontalHeaderItem(index)
            if item is not None:
                item.setToolTip(t(f"interest.suggest.column.{column}_tip"))
        if self._message_key is not None:
            key, params = self._message_key
            self.status_label.setText(t(key, **params))
        if self.preview_dialog is not None:
            try:
                self.preview_dialog.retranslate()
            except RuntimeError:
                self.preview_dialog = None
        self._redraw()

    def apply_palette(self, palette) -> None:
        self._palette = palette
        self._redraw()

    # --- start --------------------------------------------------------------------------------------

    def _window_settings(self):
        return _settings(self.window_ref)

    def _kind(self) -> str:
        from ..config import ASSISTANTS, DEFAULT_ASSISTANT

        kind = getattr(self._window_settings(), "assistant", DEFAULT_ASSISTANT)
        return kind if kind in ASSISTANTS else DEFAULT_ASSISTANT

    def _begin(self, token: dict | None) -> bool:
        if self.running:
            return False
        try:
            brief = self.brief_provider()
        except Exception:  # noqa: BLE001
            log.warning("Assistant suggestions: the brief could not be built", exc_info=True)
            brief = None
        if brief is None:
            self._show_error(t("interest.suggest.no_cv"))
            return False
        self.brief = brief
        settings = self._window_settings()
        if not getattr(settings, "suggest_notice_shown", False):
            self.show_preview(on_send=lambda: self._accepted(token))
            return False
        return self._run(brief, token)

    def show_preview(self, on_send: Callable[[], None] | None = None) -> PreviewDialog | None:
        """The window with exactly what is sent (and "Send" when `on_send` is given)."""
        brief = self.brief if on_send is not None else None
        if brief is None:
            try:
                brief = self.brief_provider()
            except Exception:  # noqa: BLE001
                brief = None
        if brief is None:
            self._show_error(t("interest.suggest.no_cv"))
            return None
        if self.preview_dialog is not None:
            try:
                self.preview_dialog.close()
            except RuntimeError:
                pass
        settings = self._window_settings()
        dialog = PreviewDialog(self, brief, _cli_name(self._kind()), bool(getattr(settings, "suggest_allow_web_search",
                                                                                 False)), on_send=on_send,
                               exclude=self._known_names(brief.area))
        dialog.setWindowFlag(Qt.WindowType.Window, True)
        self.preview_dialog = dialog
        dialog.show()
        log.info("Assistant suggestions: preview of the anonymised summary shown")
        return dialog

    def _accepted(self, token: dict | None) -> None:
        settings = self._window_settings()
        if settings is not None:
            settings.suggest_notice_shown = True
            saver = getattr(self.window_ref, "_save_settings", None)
            if callable(saver):
                try:
                    saver()
                except Exception:  # noqa: BLE001
                    log.warning("Settings not saved after the suggestion notice", exc_info=True)
        log.info("Assistant suggestions: notice accepted by the user")
        if self.brief is not None:
            self._run(self.brief, token)

    def _known_names(self, area=None) -> list[str]:
        """The exclusion list sent with the request: the companies suggested already, then
        the saved companies of the AREA (one outside it cannot be suggested anyway), at most
        EXCLUDE_MAX names. Shown in the preview: it is sent together with the summary."""
        names = [r.suggestion.name for r in self.results]
        if area is None:
            area = getattr(self.brief, "area", None)
        try:
            from .. import companies

            names += [c.name for c in companies.list_cards(self.window_ref._archive) if card_in_area(c, area)]
        except Exception:  # noqa: BLE001
            log.debug("Saved companies not read for the exclusion list", exc_info=True)
        return list(dict.fromkeys(n for n in names if n))[:EXCLUDE_MAX]

    def _run(self, brief: suggest.ProfileBrief, token: dict | None) -> bool:
        window = self.window_ref
        settings = self._window_settings()
        if token is None:
            self.results, self.added, self.last_batch = [], {}, None
            self.notices_label.hide()
        self._generation += 1
        generation = self._generation
        cancel = threading.Event()
        self._cancel = cancel
        self.running = True
        self._progress = (0, BATCH_SIZE)
        self._hide_error()
        kind = self._kind()
        override = str(getattr(settings, f"{kind}_path", "") or "") or None
        exclude = self._known_names(brief.area)
        language = i18n.current_language()
        web = bool(getattr(settings, "suggest_allow_web_search", False))
        suggest_function, verify_function, tools_factory = self.suggest_function, self.verify_function, \
            self.tools_factory
        post = self._poster(generation)
        log.info("Assistant suggestions requested (%s, %s)", "expand" if token else "first batch", kind)

        def work():
            info = window._check_cli(kind, override)
            batch = suggest_function(brief, cli=info, n=BATCH_SIZE, exclude=exclude, offset_token=token,
                                     language=language, timeout=SUGGEST_TIMEOUT_S, allow_web_search=web,
                                     cancel=cancel,
                                     callbacks=suggest.SuggestCallbacks(on_progress=lambda s: post(self._step, s)))
            if not batch.ok or not batch.suggestions or cancel.is_set():
                return batch, []
            post(self._step, stext("suggest.progress.verifying", index=1, total=len(batch.suggestions),
                                   name=batch.suggestions[0].name))
            tools, close = tools_factory(settings, cancel)
            try:
                callbacks = suggest.BatchCallbacks(on_progress=lambda s: post(self._step, s),
                                                   on_result=lambda r: post(self._result_arrived, r))
                results = verify_function(batch.suggestions, area=brief.area, cancel=cancel,
                                          max_total_requests=suggest.BATCH_REQUESTS,
                                          max_requests_each=suggest.REQUESTS_EACH, cli=batch.cli,
                                          callbacks=callbacks, language=language, **tools)
            finally:
                close()
            return batch, results

        window._in_background(work, lambda value: self._finished(generation, value),
                              lambda error: self._failed(generation, error), name="assistant suggestions",
                              timeout=JOB_TIMEOUT_S, cancel=cancel)
        self._set_status("interest.suggest.asking", cli=_cli_name(kind))
        self._show_progress(stext("suggest.progress.asking", cli=_cli_name(kind)))
        self._changed()
        return True

    def _poster(self, generation: int):
        queue = getattr(self.window_ref, "_queue", None)

        def post(function, value) -> None:
            if queue is None:
                return
            queue.put(("ok", lambda v: function(v) if generation == self._generation and self.running else None,
                       value))

        return post

    # --- progress and results ----------------------------------------------------------------------------

    def _step(self, text: str) -> None:
        self.status_label.setText(text)
        self._message_key = None
        self._show_progress(text)

    def _result_arrived(self, result: suggest.VerifiedSuggestion) -> None:
        self.results.append(result)
        done, total = self._progress
        self._progress = (done + 1, max(total, done + 1))
        self._redraw()
        self._show_progress(self.status_label.text())
        self._changed()

    def _show_progress(self, text: str) -> None:
        window = self.window_ref
        bar = getattr(window, "status_progress", None)
        if bar is None or getattr(window, "_closed", False):
            return
        from .main_window import counts_text
        from .progress_bar import BarState

        done, total = self._progress
        fraction = min(1.0, done / total) if total else None
        bar.show(PROGRESS_OWNER, BarState(fraction if done else None, counts_text(done, total, True), text))

    def _finish_progress(self) -> None:
        bar = getattr(self.window_ref, "status_progress", None)
        if bar is not None and not getattr(self.window_ref, "_closed", False):
            bar.finish(PROGRESS_OWNER)

    def _finished(self, generation: int, value) -> None:
        if generation != self._generation:
            return
        batch, results = value
        self.running = False
        self._cancel = None
        self._finish_progress()
        self.last_batch = batch
        known = {id(r) for r in self.results}
        for result in results or []:          # results not delivered one by one (already posted ones skipped)
            if id(result) not in known and not any(r.suggestion is result.suggestion for r in self.results):
                self.results.append(result)
        if batch.notices:
            self.notices_label.setText("\n".join(batch.notices))
            self.notices_label.show()
        if not batch.ok and not (batch.error == suggest.ERR_EMPTY and self.results):
            self._show_error(batch.message or stext("suggest.error.internal"))
            log.info("Assistant suggestions ended: %s", batch.error)
        else:
            counts = {s: sum(1 for r in self.results if r.status == s) for s in suggest.STATUSES}
            self._set_status("interest.suggest.done", **counts)
            log.info("Assistant suggestions ended: %d shown (%s)", len(self.results),
                     ", ".join(f"{k}={v}" for k, v in counts.items()))
        self._redraw()
        self._changed()

    def _failed(self, generation: int, error: BaseException) -> None:
        if generation != self._generation:
            return
        self.running = False
        self._cancel = None
        self._finish_progress()
        from .common import WorkerTimeout

        if isinstance(error, WorkerTimeout):
            text = t("interest.suggest.timeout")
        else:
            text = stext("suggest.error.internal")
        log.warning("Assistant suggestions failed: %s", type(error).__name__)
        self._show_error(text)
        self._redraw()
        self._changed()

    def _set_status(self, key: str, **params) -> None:
        self._message_key = (key, params)
        self.status_label.setText(t(key, **params))

    def _show_error(self, text: str) -> None:
        self.message = text
        self.error_label.setText(text)
        self.error_label.show()
        notice = getattr(self.window_ref, "show_notice", None)
        if callable(notice):
            notice(text)
        self._update_buttons()

    def _hide_error(self) -> None:
        self.message = ""
        self.error_label.hide()

    def _changed(self) -> None:
        self._update_buttons()
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001
                log.exception("Suggestions listener failed")

    # --- the list -----------------------------------------------------------------------------------------

    def visible_results(self) -> list[tuple[int, suggest.VerifiedSuggestion]]:
        show_rejected = self.rejected_action.isChecked()
        return [(i, r) for i, r in enumerate(self.results) if show_rejected or r.status != suggest.REJECTED]

    def _rejected_toggled(self, on: bool) -> None:
        if self.rejected_check.isChecked() != on:
            self.rejected_check.setChecked(on)
        self._redraw()

    def _colours(self, status: str):
        palette = self._palette
        if palette is None:
            from .theme import active_palette

            palette = active_palette()
        return {suggest.VERIFIED: (palette.hiring_bg, palette.hiring_fg),
                suggest.PARTIAL: (palette.maybe_bg, palette.maybe_fg),
                suggest.UNVERIFIED: (None, palette.muted),
                suggest.REJECTED: (None, palette.error)}.get(status, (None, None)), palette

    @staticmethod
    def _check_lines(result: suggest.VerifiedSuggestion) -> list[str]:
        marks = {True: "✓", False: "✗", None: "·"}
        return [f"{marks.get(r.ok, '·')} {r.message}" for r in result.reasons]

    def _redraw(self) -> None:
        table = self.table
        selected = set(self.selected_indexes())
        sorter = table_sort.sorter_of(table)
        table.setRowCount(0)
        rows = self.visible_results()
        table.setRowCount(len(rows))
        for row, (index, result) in enumerate(rows):
            (background, foreground), palette = self._colours(result.status)
            badge = stext(f"suggest.status.{result.status}")
            if index in self.added:
                badge += " · " + t("interest.suggest.added")
            place = ", ".join(x for x in (result.town or result.suggestion.municipality,
                                          result.province or result.suggestion.province) if x)
            site = result.site or result.suggestion.website or ""
            checks = self._check_lines(result)
            sources = ", ".join(result.verified_by())
            opinion = result.suggestion.why or ""
            values = {
                "status": (badge, stext(f"suggest.status.{result.status}.help"), STATUS_RANK.get(result.status, 9)),
                "name": (result.suggestion.name, result.legal_name or "", None),
                "place": (place, "", None),
                "site": (site, t("interest.suggest.site_tip") if result.site else t("interest.suggest.site_unchecked"),
                         None),
                "checks": ("; ".join(checks), "\n".join(checks + ([t("interest.suggest.sources", sources=sources)]
                                                                   if sources else [])), None),
                "opinion": (opinion, result.opinion(), None),
            }
            for column, name in enumerate(COLUMNS):
                text, tip, sort = values[name]
                item = QTableWidgetItem(text)
                if tip:
                    item.setToolTip(tip)
                if sort is not None:
                    item.setData(table_sort.SORT_ROLE, float(sort))
                if column == 0:
                    item.setData(table_sort.ROW_ID_ROLE, index)
                if name == "status" and background:
                    item.setBackground(QBrush(QColor(background)))
                if foreground and (name == "status" or result.status in (suggest.UNVERIFIED, suggest.REJECTED)):
                    item.setForeground(QBrush(QColor(foreground)))
                if name == "opinion":
                    item.setForeground(QBrush(QColor(palette.muted)))
                table.setItem(row, column, item)
        if sorter is not None:
            table_sort.resort(table)
        for row in range(table.rowCount()):
            if table_sort.source_row(table, row) in selected:
                table.selectRow(row)
        hidden = sum(1 for r in self.results if r.status == suggest.REJECTED)
        self.rejected_action.setText(t("interest.suggest.show_rejected", count=hidden))
        self.rejected_check.setText(self.rejected_action.text())
        if table.columnWidth(1) < 60:
            table.resizeColumnsToContents()
        self._update_buttons()

    def selected_indexes(self) -> list[int]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [table_sort.source_row(self.table, r) for r in rows]

    def selected_results(self) -> list[tuple[int, suggest.VerifiedSuggestion]]:
        return [(i, self.results[i]) for i in self.selected_indexes() if 0 <= i < len(self.results)]

    def _update_buttons(self) -> None:
        addable = [i for i, r in self.selected_results() if r.addable and i not in self.added]
        self.suggest_action.setEnabled(not self.running)
        self.expand_action.setEnabled(self.can_expand())
        self.cancel_action.setEnabled(self.running)
        self.add_action.setEnabled(bool(addable))
        self.site_action.setEnabled(any((r.site or r.suggestion.website) for _i, r in self.selected_results()))
        self.rejected_action.setEnabled(any(r.status == suggest.REJECTED for r in self.results))
        self.rejected_check.setEnabled(self.rejected_action.isEnabled())
        for action, button in ((self.suggest_action, self.suggest_button), (self.expand_action, self.expand_button),
                               (self.cancel_action, self.cancel_button), (self.add_action, self.add_button)):
            button.setEnabled(action.isEnabled())
        self.cancel_button.setVisible(self.running)

    # --- actions on rows ------------------------------------------------------------------------------------

    def add_selected(self) -> list:
        """Adds the selected VERIFIED / PARTIAL suggestions as company cards (with provenance)."""
        cards = []
        for index, result in self.selected_results():
            card = self.add_result(index)
            if card is not None:
                cards.append(card)
        return cards

    def add_result(self, index: int):
        """Adds one suggestion (by its index in `results`); None when it cannot be added."""
        from .. import companies

        if not 0 <= index < len(self.results) or index in self.added:
            return None
        result = self.results[index]
        seed = result.to_company_seed()
        if seed is None:
            return None
        seed.pop("extra", None)
        name, site = seed.pop("name"), seed.pop("site")
        try:
            card = companies.add_manual(self.window_ref._archive, name, site, **seed)
        except Exception as error:  # noqa: BLE001
            log.warning("Suggested company not added: %s", type(error).__name__)
            self._show_error(t("interest.suggest.add_failed"))
            return None
        self.added[index] = card.id
        log.info("Suggested company added to my companies (card %s, status %s)", card.id, result.status)
        self._redraw()
        if self.on_added is not None:
            try:
                self.on_added(card)
            except Exception:  # noqa: BLE001
                log.exception("on_added failed")
        self._changed()
        return card

    def open_site(self) -> None:
        for _index, result in self.selected_results():
            site = result.site or result.suggestion.website
            if site:
                opener = getattr(self.window_ref, "_opener", None)
                if callable(opener):
                    opener(site if "://" in site else f"https://{site}")
                return

    def close_view(self) -> None:
        """Stops the running request (the owner is closing)."""
        self.cancel()
        self._generation += 1
        if self.running:
            self.running = False
            self._finish_progress()
        if self.preview_dialog is not None:
            try:
                self.preview_dialog.close()
            except RuntimeError:
                pass
