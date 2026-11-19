"""The guided window "Who could be interested in my profile" (non-modal, one at a time).

Four steps, top to bottom:

1. AREA: country / region / province, or a radius around a town (prefilled from the
   Search tab), the career directions and company types (prefilled from the affinity
   settings, ``settings.affinity_preferences``).
2. FAST (offline, seconds): everything already known locally — the saved companies
   (``companies.list_cards``), the regional register (``window.register_candidates``) and
   the local OpenStreetMap index when it was built already (never downloaded here) — is
   ordered by ``background.screen_companies``; each row shows its one-line reason and
   "not analysed yet" until the deep analysis ran. Affinity columns appear when a result
   exists. Header-click sort; right-click menu with the row buttons' actions.
3. ASSISTANT: the reusable ``SuggestionsView`` (gui/suggestions_view.py); a company it
   adds joins the list, marked "suggested by the assistant".
4. DEEP: the best N rows (setting in the window, default 10) are analysed now: card
   (created when missing, ``companies.save_from_result``), the card pipeline
   (``companies.build_cards``: site, identity, registers...) and then the affinity
   (``window.affinity_service.request_cards``); the other rows are queued for the idle
   analysis (``window.interest_enqueue``).

Everything slow runs on the window's workers (``window._in_background``: deadline,
cancel); progress in the shared bottom bar as "done/estimated".
"""

from __future__ import annotations

import copy
import logging
import threading
from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QLineEdit, QListWidget, QListWidgetItem, QMenuBar,
                               QPushButton, QScrollArea, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from .. import suggest
from ..i18n import t
from . import context_menu, table_filter, table_sort
from .affinity_box import AFFINITY_COLUMNS, affinity_cells, apply_cell, column_header
from .menus import bind_action
from .suggestions_view import SuggestionsView, default_area
from .widgets import wrapped_label

log = logging.getLogger(__name__)

PROGRESS_FAST = "interest-fast"
PROGRESS_DEEP = "interest-deep"
REGISTER_LIMIT = 400
MAP_LIMIT = 1500
DEFAULT_DEEP = 10
MAX_DEEP = 50
IDLE_QUEUE_MAX = 200   # companies handed to the idle analysis at once (best first)
FAST_TIMEOUT_S = 180
DEEP_TIMEOUT_S = 3600
RADIUS_TOLERANCE = 1.15
NEAREST_CAPITAL_KM = 60.0   # a saved company farther from every province capital: province unknown
AREA_KINDS = (suggest.AREA_RADIUS, suggest.AREA_PROVINCE, suggest.AREA_REGION, suggest.AREA_COUNTRY)
BASE_COLUMNS = ("name", "source", "place", "reason", "state")
COLUMNS = BASE_COLUMNS + AFFINITY_COLUMNS
SOURCES = ("saved", "register", "map", "assistant")
TYPES = ("machine_builder", "system_integrator", "manufacturer", "software_house")


@dataclass
class FlowRow:
    """One company of the list (`item`: a CompanyCard or a models.Company)."""

    key: str
    name: str
    source: str
    item: object
    reason: str = ""
    score: float = 0.0
    place: str = ""
    card_id: int | None = None
    state: str = "new"          # new | queued | analysing | analysed
    extra: dict = field(default_factory=dict)


# --- helpers (no Qt) ---------------------------------------------------------------------------------


def preferences_for(settings, directions: list[str] | None = None, types: list[str] | None = None):
    """The affinity preferences of the settings with the flow's choices applied (a copy)."""
    from .affinity_service import preferences_of

    prefs = copy.deepcopy(preferences_of(settings))
    if directions is not None:
        for direction in prefs.directions:
            direction.enabled = direction.id in directions
    if types is not None:
        for key in list(prefs.company_types):
            if key in TYPES and key not in types:
                prefs.company_types[key] = 0.0
            elif key in types and not prefs.company_types.get(key):
                prefs.company_types[key] = 1.0
    return prefs


def _place_of(item) -> str:
    address = str(getattr(item, "address", "") or "")
    fields = getattr(item, "fields", None) or {}
    if isinstance(fields, dict):
        town = fields.get("municipality") or fields.get("town") or ""
        province = fields.get("province") or ""
        joined = ", ".join(x for x in (town, province) if x)
        if joined and not address:
            return joined
    return address


def _key_of(item) -> str:
    from .. import companies

    if hasattr(item, "products") and hasattr(item, "key"):
        return str(item.key)
    try:
        return companies.card_key(item)
    except Exception:  # noqa: BLE001
        return f"name:{getattr(item, 'name', '')}"


def _host(site: str | None) -> str:
    from urllib.parse import urlsplit

    text = str(site or "")
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    host = (urlsplit(text).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def register_area(area: suggest.SearchArea):
    """The `area` argument of ``window.register_candidates`` for a flow area."""
    from ..geo.regions import region_for

    if area.kind == suggest.AREA_PROVINCE and area.province:
        return area.province
    if area.kind == suggest.AREA_REGION and area.region:
        region = region_for(area.region)
        return region.key if region is not None else area.region
    return None  # radius / country: the regions of the settings (filtered by distance after)


def map_candidates(settings, area: suggest.SearchArea, index=None) -> list:
    """Companies of the local map index inside the area (only when the index exists)."""
    from .suggestions_view import local_map_index

    index = index if index is not None else local_map_index(settings)
    if index is None:
        return []
    try:
        if area.kind == suggest.AREA_RADIUS and area.lat is not None and area.lon is not None and area.radius_km:
            found = index.radius(area.lat, area.lon, float(area.radius_km))
        elif area.kind == suggest.AREA_PROVINCE and area.province:
            found = index.province(area.province)
        else:
            found = index.region()
    except Exception:  # noqa: BLE001 - the map is optional
        log.info("Interest flow: local map not readable", exc_info=True)
        return []
    if len(found) > MAP_LIMIT:
        # more companies than the pre-screen takes: the likelier employers first (a website,
        # an office / industrial / craft tag) instead of the index order, which cut e.g. an
        # IT company of Cesena out of the list (stable sort: the index order within a group)
        found = sorted(found, key=_map_priority)
        log.info("Interest flow: %d map companies in the area, the %d likeliest are screened", len(found),
                 MAP_LIMIT)
    return found[:MAP_LIMIT]


_WORK_TAGS = ("office", "industrial", "craft", "man_made", "company")


def _map_priority(company) -> tuple[int, int]:
    tags = getattr(company, "osm_tags", None) or {}
    work = any(key in tags for key in _WORK_TAGS)
    return (0 if getattr(company, "site", None) else 1, 0 if work else 1)


def inside_radius(area: suggest.SearchArea, item) -> bool:
    """False only for a company with coordinates outside the radius (unknown = kept)."""
    if area.kind != suggest.AREA_RADIUS or area.lat is None or area.lon is None or not area.radius_km:
        return True
    lat, lon = getattr(item, "lat", None), getattr(item, "lon", None)
    if lat is None or lon is None:
        return True
    from ..geo.distance import haversine_km

    return haversine_km(area.lat, area.lon, float(lat), float(lon)) <= float(area.radius_km) * RADIUS_TOLERANCE


def place_province(item) -> str:
    """The province code of a saved company: from the postcode of its address, else the
    nearest province capital to its coordinates (Italy only); "" when unknown."""
    import re as _re

    from ..geo import italy
    from ..geo.distance import haversine_km
    from ..registers.italy_codes import province_from_postcode

    address = str(getattr(item, "address", "") or "")
    for code in _re.findall(r"(?<!\d)(\d{5})(?!\d)", address):
        found = province_from_postcode(code)
        if found:
            return found
    lat, lon = getattr(item, "lat", None), getattr(item, "lon", None)
    if lat is None or lon is None or not italy.in_italy_box(float(lat), float(lon)):
        return ""
    best = min(italy.PROVINCES.values(), key=lambda p: haversine_km(float(lat), float(lon), p.lat, p.lon))
    return best.code if haversine_km(float(lat), float(lon), best.lat, best.lon) <= NEAREST_CAPITAL_KM else ""


def saved_in_area(area: suggest.SearchArea, item) -> bool:
    """False for a saved company known to lie outside the province / region / radius of the
    flow (a Milan company is not a candidate "in Forlì-Cesena"); unknown place = kept."""
    if area.kind == suggest.AREA_RADIUS:
        return inside_radius(area, item)
    if area.kind not in (suggest.AREA_PROVINCE, suggest.AREA_REGION):
        return True
    code = place_province(item)
    if not code:
        return True
    if area.kind == suggest.AREA_PROVINCE:
        return not area.province or code == area.province.upper()
    from ..geo import italy
    from ..geo.regions import region_for

    region = region_for(area.region) if area.region else None
    province = italy.province(code)
    return region is None or province is None or province.region == region.key


def collect_and_screen(window, settings, area: suggest.SearchArea, profile, prefs, *, index=None,
                       cancel: threading.Event | None = None) -> list[FlowRow]:
    """FAST step (worker thread): every local candidate, ordered by the pre-screen."""
    from .. import background, companies

    candidates: list[tuple[str, object]] = []
    seen: set[str] = set()

    named: set[tuple[str, str]] = set()

    def add(source: str, item) -> None:
        key = _key_of(item)
        # the same company twice on the map (two nodes with one name in one town) is one row
        same = (" ".join(str(getattr(item, "name", "") or "").casefold().split()), _place_of(item).casefold())
        if key in seen or (same[0] and same in named):
            return
        seen.add(key)
        named.add(same)
        candidates.append((source, item))

    try:
        outside = 0
        for card in companies.list_cards(window._archive):
            if saved_in_area(area, card):
                add("saved", card)
            else:
                outside += 1
        if outside:
            log.info("Interest flow: %d saved companies outside the area left out", outside)
    except Exception:  # noqa: BLE001
        log.warning("Interest flow: saved companies not read", exc_info=True)
    if cancel is None or not cancel.is_set():
        try:
            for company in window.register_candidates(register_area(area), REGISTER_LIMIT):
                if inside_radius(area, company):
                    add("register", company)
        except Exception:  # noqa: BLE001 - the register is optional
            log.info("Interest flow: register candidates not read", exc_info=True)
    if cancel is None or not cancel.is_set():
        for company in map_candidates(settings, area, index):
            add("map", company)
    keys = {id(item): _key_of(item) for _source, item in candidates}
    screened = background.screen_companies([item for _source, item in candidates], profile, prefs,
                                           key_of=lambda item: keys.get(id(item), ""))
    rows = []
    for result in screened:
        source, item = candidates[result.index]
        card_id = getattr(item, "id", None) if source == "saved" else None
        rows.append(FlowRow(key=result.key, name=result.name or str(getattr(item, "name", "")), source=source,
                            item=item, reason=result.reason, score=result.score, place=_place_of(item),
                            card_id=card_id))
    log.info("Interest flow: %d companies screened (%s)", len(rows),
             ", ".join(f"{s}={sum(1 for r in rows if r.source == s)}" for s in SOURCES))
    return rows


# --- the window --------------------------------------------------------------------------------------


class InterestFlowWindow(QDialog):
    """See the module docstring."""

    def __init__(self, window):
        super().__init__(window)
        self.setObjectName("interestFlow")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.main = window
        self.rows: list[FlowRow] = []
        self.fast_running = False
        self.deep_running = False
        self._fast_cancel: threading.Event | None = None
        self._deep_cancel: threading.Event | None = None
        self._generation = 0
        self._closed = False
        self._palette = None
        self._centre_town = ""
        self._centre: tuple[float, float] | None = None
        self.index_factory = None   # tests: a fake map index
        settings = window._settings

        # --- 1. area -----------------------------------------------------------------------------
        self.area_box = QGroupBox()
        self.kind_combo = QComboBox()
        for kind in AREA_KINDS:
            self.kind_combo.addItem("", kind)
        self.province_combo = QComboBox()
        self.region_combo = QComboBox()
        self._fill_places()
        self.town_field = QLineEdit()
        self.radius_spin = QDoubleSpinBox()
        self.radius_spin.setRange(1.0, 300.0)
        self.radius_spin.setDecimals(0)
        self.radius_spin.setSuffix(" km")
        self.kind_label, self.province_label, self.region_label = wrapped_label(), wrapped_label(), wrapped_label()
        self.town_label, self.radius_label = wrapped_label(), wrapped_label()
        self.directions_label, self.types_label = wrapped_label(), wrapped_label()
        self.directions_list = QListWidget()
        self.directions_list.setObjectName("interestDirections")
        self.directions_list.setMaximumHeight(self.fontMetrics().height() * 6)
        self.type_checks: dict[str, QCheckBox] = {key: QCheckBox() for key in TYPES}
        self._fill_preferences(settings)
        self._fill_area(default_area(window))
        self.kind_combo.currentIndexChanged.connect(self._kind_changed)
        form = QFormLayout()
        form.addRow(self.kind_label, self.kind_combo)
        form.addRow(self.province_label, self.province_combo)
        form.addRow(self.region_label, self.region_combo)
        form.addRow(self.town_label, self.town_field)
        form.addRow(self.radius_label, self.radius_spin)
        self._form = form
        types_row = QHBoxLayout()
        for box in self.type_checks.values():
            types_row.addWidget(box)
        types_row.addStretch(1)
        prefs_layout = QVBoxLayout()
        prefs_layout.addWidget(self.directions_label)
        prefs_layout.addWidget(self.directions_list)
        prefs_layout.addWidget(self.types_label)
        prefs_layout.addLayout(types_row)
        area_layout = QHBoxLayout(self.area_box)
        area_layout.addLayout(form, 1)
        area_layout.addLayout(prefs_layout, 2)

        # --- 2. fast list ------------------------------------------------------------------------
        self.fast_box = QGroupBox()
        self.fast_action = QAction(self, triggered=lambda _c=False: self.run_fast())
        self.analyse_selected_action = QAction(self, triggered=lambda _c=False: self.analyse_selected())
        self.queue_selected_action = QAction(self, triggered=lambda _c=False: self.queue_selected())
        self.open_card_action = QAction(self, triggered=lambda _c=False: self.open_selected_card())
        self.site_action = QAction(self, triggered=lambda _c=False: self.open_selected_site())
        self.fast_button = self._button(self.fast_action, "accent")
        self.analyse_selected_button = self._button(self.analyse_selected_action)
        self.queue_selected_button = self._button(self.queue_selected_action)
        self.open_card_button = self._button(self.open_card_action)
        self.site_button = self._button(self.site_action)
        self.fast_note = wrapped_label(role="muted")
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setObjectName("interestTable")
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(False)
        if "reason" in COLUMNS:  # the one-line reason takes the free width, not the last column
            self.table.horizontalHeader().setSectionResizeMode(COLUMNS.index("reason"), QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.itemDoubleClicked.connect(lambda _i: self.open_selected_card())
        table_filter.attach(self.table)   # header-click sorting (no filter row)
        context_menu.install(self.table, lambda: [self.open_card_action, self.site_action, None,
                                                  self.analyse_selected_action, self.queue_selected_action])
        fast_buttons = QHBoxLayout()
        fast_buttons.addWidget(self.fast_button)
        fast_buttons.addStretch(1)
        for button in (self.open_card_button, self.site_button, self.analyse_selected_button,
                       self.queue_selected_button):
            fast_buttons.addWidget(button)
        fast_layout = QVBoxLayout(self.fast_box)
        fast_layout.addLayout(fast_buttons)
        fast_layout.addWidget(self.fast_note)
        fast_layout.addWidget(self.table, 1)

        # --- 3. assistant -------------------------------------------------------------------------
        self.assistant_box = QGroupBox()
        self.suggestions = SuggestionsView(window, brief_provider=self.brief, on_added=self._suggestion_added)
        self.suggestions.listeners.append(self._update_buttons)
        assistant_layout = QVBoxLayout(self.assistant_box)
        assistant_layout.addWidget(self.suggestions)

        # --- 4. deep --------------------------------------------------------------------------------
        self.deep_box = QGroupBox()
        self.deep_label = wrapped_label()
        self.deep_spin = QSpinBox()
        self.deep_spin.setRange(1, MAX_DEEP)
        self.deep_spin.setValue(DEFAULT_DEEP)
        self.deep_action = QAction(self, triggered=lambda _c=False: self.run_deep())
        self.cancel_action = QAction(self, triggered=lambda _c=False: self.cancel())
        self.deep_button = self._button(self.deep_action, "accent")
        self.cancel_button = self._button(self.cancel_action)
        self.deep_note = wrapped_label(role="muted")
        deep_row = QHBoxLayout()
        deep_row.addWidget(self.deep_label)
        deep_row.addWidget(self.deep_spin)
        deep_row.addWidget(self.deep_button)
        deep_row.addWidget(self.cancel_button)
        deep_row.addStretch(1)
        deep_layout = QVBoxLayout(self.deep_box)
        deep_layout.addLayout(deep_row)
        deep_layout.addWidget(self.deep_note)

        # --- menu bar: every button's action ---------------------------------------------------
        self.menu_bar = QMenuBar(self)
        self.actions_menu = self.menu_bar.addMenu("")
        for action in (self.fast_action, self.open_card_action, self.site_action, self.analyse_selected_action,
                       self.queue_selected_action):
            self.actions_menu.addAction(action)
        self.actions_menu.addSeparator()
        for action in self.suggestions.actions():
            self.actions_menu.addAction(action)
        self.actions_menu.addSeparator()
        self.actions_menu.addAction(self.deep_action)
        self.actions_menu.addAction(self.cancel_action)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addWidget(self.area_box)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self.fast_box)
        splitter.addWidget(self.assistant_box)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        content_layout.addWidget(splitter, 1)
        content_layout.addWidget(self.deep_box)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        layout = QVBoxLayout(self)
        layout.setMenuBar(self.menu_bar)
        layout.addWidget(scroll)
        service = getattr(window, "affinity_service", None)
        if service is not None:
            service.listeners.append(self._affinity_changed)
        self._kind_changed()
        self.retranslate()
        width = self.fontMetrics().averageCharWidth() * 160
        self.resize(width, self.fontMetrics().height() * 46)

    def _button(self, action: QAction, role: str | None = None) -> QPushButton:
        button = QPushButton()
        if role:
            button.setProperty("role", role)
        button.clicked.connect(lambda: action.trigger())
        bind_action(button, action)
        return button

    # --- area and preferences ------------------------------------------------------------------------

    def _fill_places(self) -> None:
        from ..geo import italy
        from ..geo.regions import REGIONS

        provinces = []
        for region in REGIONS.values():
            provinces += italy.provinces_of_region(region.key)
        for province in sorted({p.code: p for p in provinces}.values(), key=lambda p: p.name):
            self.province_combo.addItem(f"{province.name} ({province.code})", province.code)
        for key, region in REGIONS.items():
            self.region_combo.addItem(region.name, key)

    def _fill_preferences(self, settings) -> None:
        from .affinity_service import preferences_of

        prefs = preferences_of(settings)
        for direction in prefs.directions:
            from .affinity_box import direction_name

            item = QListWidgetItem(direction_name(direction.id, direction.name))
            item.setData(Qt.ItemDataRole.UserRole, direction.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if direction.enabled else Qt.CheckState.Unchecked)
            self.directions_list.addItem(item)
        for key, box in self.type_checks.items():
            box.setChecked(float(prefs.company_types.get(key, 1.0) or 0) > 0)

    def _fill_area(self, area: suggest.SearchArea) -> None:
        from ..geo.regions import region_for

        self.kind_combo.setCurrentIndex(max(0, self.kind_combo.findData(area.kind)))
        if area.province:
            index = self.province_combo.findData(area.province)
            if index >= 0:
                self.province_combo.setCurrentIndex(index)
        if area.region:
            region = region_for(area.region)
            index = self.region_combo.findData(region.key if region is not None else area.region)
            if index >= 0:
                self.region_combo.setCurrentIndex(index)
        self.town_field.setText(area.town)
        self.radius_spin.setValue(float(area.radius_km or getattr(self.main._settings, "radius_km", 15.0) or 15.0))
        if area.lat is not None and area.lon is not None:
            self._centre_town, self._centre = area.town, (area.lat, area.lon)

    def _kind_changed(self, *_args) -> None:
        kind = self.kind_combo.currentData()
        for label, widget, visible in ((self.province_label, self.province_combo, kind == suggest.AREA_PROVINCE),
                                       (self.region_label, self.region_combo, kind == suggest.AREA_REGION),
                                       (self.town_label, self.town_field, kind == suggest.AREA_RADIUS),
                                       (self.radius_label, self.radius_spin, kind == suggest.AREA_RADIUS)):
            label.setVisible(visible)
            widget.setVisible(visible)

    def area(self) -> suggest.SearchArea:
        """The area chosen in the window (a radius around a TOWN, never an address)."""
        from ..geo import italy
        from ..geo.regions import REGIONS, provinces_of

        kind = self.kind_combo.currentData() or suggest.AREA_COUNTRY
        area = suggest.SearchArea(kind=kind, country="IT")
        if kind == suggest.AREA_PROVINCE:
            code = self.province_combo.currentData() or ""
            found = italy.province(code)
            area.province = code
            area.province_name = found.name if found is not None else code
            area.region = italy.region_name(found.region) if found is not None else ""
        elif kind == suggest.AREA_REGION:
            region = REGIONS.get(self.region_combo.currentData() or "")
            if region is not None:
                area.region = region.name
                area.provinces = [p.code for p in provinces_of(region)]
        elif kind == suggest.AREA_RADIUS:
            area.town = " ".join(self.town_field.text().split())
            area.radius_km = float(self.radius_spin.value())
            if self._centre is not None and self._centre_town.casefold() == area.town.casefold():
                area.lat, area.lon = self._centre
        return area

    def directions(self) -> list[str]:
        return [self.directions_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.directions_list.count())
                if self.directions_list.item(i).checkState() == Qt.CheckState.Checked]

    def company_types(self) -> list[str]:
        return [key for key, box in self.type_checks.items() if box.isChecked()]

    def preferences(self):
        return preferences_for(self.main._settings, self.directions(), self.company_types())

    def profile(self):
        try:
            return self.main.profile_panel.current_profile()
        except Exception:  # noqa: BLE001
            return None

    def brief(self) -> suggest.ProfileBrief | None:
        profile = self.profile()
        if profile is None:
            return None
        return suggest.build_profile_brief(profile, self.preferences(), self.area())

    def _resolve_centre(self, area: suggest.SearchArea, index) -> suggest.SearchArea:
        """Worker: the coordinates of the radius centre town (map index, else the geocoder)."""
        if area.kind != suggest.AREA_RADIUS or area.lat is not None or not area.town:
            return area
        place = None
        if index is not None:
            try:
                place = index.find_place(area.town)
            except Exception:  # noqa: BLE001
                place = None
        if place:
            area.lat, area.lon = place[0], place[1]
            return area
        try:
            found = self.main._create_geocoder().geocode(area.town)
        except Exception:  # noqa: BLE001
            found = None
        if found is not None:
            area.lat, area.lon = float(found.lat), float(found.lon)
        return area

    # --- 2. fast --------------------------------------------------------------------------------------

    def run_fast(self) -> bool:
        if self.fast_running or self._closed:
            return False
        window = self.main
        settings = window._settings
        area = self.area()
        profile = self.profile()
        prefs = self.preferences()
        cancel = threading.Event()
        self._fast_cancel = cancel
        self.fast_running = True
        self._generation += 1
        generation = self._generation
        index_factory = self.index_factory
        log.info("Interest flow: fast screening started (area %s)", area.kind)

        def work():
            from .suggestions_view import local_map_index

            index = index_factory() if index_factory is not None else local_map_index(settings)
            resolved = self._resolve_centre(area, index)
            return resolved, collect_and_screen(window, settings, resolved, profile, prefs, index=index, cancel=cancel)

        window._in_background(work, lambda value: self._fast_done(generation, value),
                              lambda error: self._fast_failed(generation, error), name="interest fast screening",
                              timeout=FAST_TIMEOUT_S, cancel=cancel)
        self._bar(PROGRESS_FAST, None, t("interest.fast.running"))
        self.fast_note.setText(t("interest.fast.running"))
        self._update_buttons()
        return True

    def _fast_done(self, generation: int, value) -> None:
        if generation != self._generation or self._closed:
            return
        area, rows = value
        if area.lat is not None and area.town:
            self._centre_town, self._centre = area.town, (area.lat, area.lon)
        self.fast_running = False
        self._finish_bar(PROGRESS_FAST)
        assistant = [r for r in self.rows if r.source == "assistant"]
        known = {r.key for r in assistant}
        self.rows = assistant + [r for r in rows if r.key not in known]
        self.rows.sort(key=lambda r: -r.score)
        self._mark_states()
        self._fast_summary()
        self._redraw()
        service = getattr(self.main, "affinity_service", None)
        cards = [r.item for r in self.rows if r.card_id is not None and hasattr(r.item, "products")]
        if service is not None and cards:
            service.results_for(cards)  # cached results shown; the missing ones computed (no network)

    def _fast_failed(self, generation: int, error: BaseException) -> None:
        if generation != self._generation or self._closed:
            return
        self.fast_running = False
        self._finish_bar(PROGRESS_FAST)
        log.warning("Interest flow: fast screening failed: %s", type(error).__name__)
        self.fast_note.setText(t("interest.fast.failed"))
        self.main.show_notice(t("interest.fast.failed"))
        self._update_buttons()

    def _fast_summary(self) -> None:
        counts = {s: sum(1 for r in self.rows if r.source == s) for s in SOURCES}
        if not self.rows:
            self.fast_note.setText(t("interest.fast.empty"))
        else:
            self.fast_note.setText(t("interest.fast.summary", total=len(self.rows), **counts))

    def _mark_states(self) -> None:
        scheduler = getattr(self.main, "idle_scheduler", None)
        queued = set()
        if scheduler is not None:
            try:
                from ..background import QUEUED

                queued = {j.key for j in scheduler.jobs() if j.state == QUEUED}
            except Exception:  # noqa: BLE001
                queued = set()
        service = getattr(self.main, "affinity_service", None)
        for row in self.rows:
            if row.state in ("analysing",):
                continue
            built = bool(getattr(row.item, "built", False))
            result = service.result_for(row.item) if service is not None and row.card_id is not None else None
            if built and result is not None:
                row.state = "analysed"
            elif row.key in queued:
                row.state = "queued"
            elif row.state != "analysed":
                row.state = "new"

    # --- 3. assistant -----------------------------------------------------------------------------------

    def _suggestion_added(self, card) -> None:
        from .. import background

        screened = background.screen_companies([card], self.profile(), self.preferences(),
                                               key_of=lambda item: str(item.key))
        result = screened[0] if screened else None
        row = FlowRow(key=str(card.key), name=card.name, source="assistant", item=card,
                      reason=(result.reason if result else ""), score=(result.score if result else 0.0) + 1.0,
                      place=card.address or "", card_id=card.id)
        self.rows = [row] + [r for r in self.rows if r.key != row.key]
        self.rows.sort(key=lambda r: -r.score)
        self._redraw()
        changed = getattr(self.main, "_interest_cards_changed", None)
        if callable(changed):
            changed()

    # --- 4. deep -----------------------------------------------------------------------------------------

    def _ensure_card(self, row: FlowRow):
        from .. import companies

        if row.card_id is not None:
            card = companies.get(self.main._archive, row.card_id)
            if card is not None:
                return card
        item = row.item
        card = companies.find(self.main._archive, item) if not hasattr(item, "products") else None
        if card is None:
            card = companies.save_from_result(self.main._archive, item, None)
        row.card_id, row.item = card.id, card
        return card

    def run_deep(self, count: int | None = None, rows: list[FlowRow] | None = None) -> bool:
        """The best `count` rows analysed now, the rest queued for the idle analysis."""
        if self.deep_running or self._closed:
            return False
        if not self.rows and rows is None:
            self.deep_note.setText(t("interest.deep.nothing"))
            return False
        count = int(count or self.deep_spin.value())
        if rows is None:
            pending = [r for r in self.rows if r.state != "analysed"]
            chosen, rest = pending[:count], pending[count:]
        else:
            chosen, rest = rows, []
        if not chosen:
            self.deep_note.setText(t("interest.deep.nothing"))
            return False
        try:
            cards = [self._ensure_card(row) for row in chosen]
        except Exception:  # noqa: BLE001
            log.warning("Interest flow: cards not created", exc_info=True)
            self.main.show_notice(t("interest.deep.failed"))
            return False
        queued = self.queue_rows(self._rows_for_idle(rest)) if rest else 0
        ids = [c.id for c in cards]
        for row in chosen:
            row.state = "analysing"
        window = self.main
        settings = window._settings
        cancel = threading.Event()
        self._deep_cancel = cancel
        self.deep_running = True
        generation = self._generation
        total = len(ids)
        post = self._poster()
        builder = getattr(window, "interest_card_builder", None)
        log.info("Interest flow: deep analysis of %d companies started, %d queued", total, queued)

        def progress(index: int, count_: int, name: str, step: str) -> None:
            post(lambda v: self._deep_progress(*v), (index, count_, name))

        def work():
            if builder is not None:
                return builder(ids, cancel, progress)
            from .. import companies
            from ..web.fetch import Fetcher

            fetcher = Fetcher(settings, window._archive)
            try:
                return companies.build_cards(window._archive, ids, fetcher, settings, cancel=cancel, progress=progress)
            finally:
                fetcher.close()

        window._in_background(work, lambda built: self._deep_done(generation, chosen, built),
                              lambda error: self._deep_failed(generation, chosen, error),
                              name="interest deep analysis", timeout=DEEP_TIMEOUT_S, cancel=cancel)
        self.deep_note.setText(t("interest.deep.running", count=total, queued=queued))
        self._deep_progress(0, total, "")
        self._redraw()
        return True

    def _rows_for_idle(self, rest: list[FlowRow]) -> list[FlowRow]:
        """The rows handed to the idle analysis: at most IDLE_QUEUE_MAX (best first), and only
        after the user agreed once (each one means reading a website while the PC is idle)."""
        rest = list(rest)[:IDLE_QUEUE_MAX]
        settings = self.main._settings
        if not rest or getattr(settings, "idle_queue_agreed", False):
            return rest
        dialogs = getattr(self.main, "dialogs", None)
        text = t("interest.idle.ask_queue", n=len(rest), budget=getattr(settings, "idle_daily_budget", 600),
                 minutes=getattr(settings, "idle_minutes", 5))
        if dialogs is None or not dialogs.ask(text, t("interest.title")):
            log.info("Interest flow: the user did not queue the other companies for the idle analysis")
            return []
        settings.idle_queue_agreed = True
        saver = getattr(self.main, "_save_settings", None)
        if callable(saver):
            try:
                saver()
            except Exception:  # noqa: BLE001
                log.warning("Settings not saved after the idle-queue question", exc_info=True)
        return rest

    def _poster(self):
        queue = getattr(self.main, "_queue", None)

        def post(function, value) -> None:
            if queue is not None:
                queue.put(("ok", lambda v: None if self._closed else function(v), value))

        return post

    def _deep_progress(self, index: int, count: int, name: str) -> None:
        from .main_window import counts_text

        done = min(index, count)
        text = t("interest.deep.progress", done=done, total=count)
        self._bar(PROGRESS_DEEP, (done / count) if count else None, text, counts_text(done, count, False))

    def _deep_done(self, generation: int, chosen: list[FlowRow], built) -> None:
        from .. import companies

        self.deep_running = False
        self._deep_cancel = None
        self._finish_bar(PROGRESS_DEEP)
        cards = []
        for row in chosen:
            card = companies.get(self.main._archive, row.card_id) if row.card_id is not None else None
            if card is not None:
                row.item = card
                cards.append(card)
        service = getattr(self.main, "affinity_service", None)

        def affinity_done() -> None:
            for row in chosen:
                row.state = "analysed" if getattr(row.item, "built", False) else "new"
            self._redraw()

        if service is not None and cards:
            service.request_cards(cards, on_done=affinity_done)
        else:
            affinity_done()
        done = len(built or [])
        self.deep_note.setText(t("interest.deep.done", done=done, total=len(chosen)))
        log.info("Interest flow: deep analysis ended (%d of %d)", done, len(chosen))
        changed = getattr(self.main, "_interest_cards_changed", None)
        if callable(changed):
            changed()
        self._redraw()

    def _deep_failed(self, generation: int, chosen: list[FlowRow], error: BaseException) -> None:
        self.deep_running = False
        self._deep_cancel = None
        self._finish_bar(PROGRESS_DEEP)
        for row in chosen:
            if row.state == "analysing":
                row.state = "new"
        log.warning("Interest flow: deep analysis failed: %s", type(error).__name__)
        self.deep_note.setText(t("interest.deep.failed"))
        self.main.show_notice(t("interest.deep.failed"))
        self._redraw()

    def queue_rows(self, rows: list[FlowRow]) -> int:
        """Queues rows for the idle analysis (cards are created when the job runs)."""
        enqueue = getattr(self.main, "interest_enqueue", None)
        if not callable(enqueue):
            return 0
        count = enqueue(rows)
        for row in rows:
            if row.state == "new":
                row.state = "queued"
        self._redraw()
        return count

    def cancel(self) -> None:
        for event in (self._fast_cancel, self._deep_cancel):
            if event is not None:
                event.set()
        self.suggestions.cancel()
        log.info("Interest flow: cancelled by the user")

    @property
    def running(self) -> bool:
        return self.fast_running or self.deep_running or self.suggestions.running

    # --- selection ------------------------------------------------------------------------------------------

    def selected_rows(self) -> list[FlowRow]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        out = []
        for row in rows:
            index = table_sort.source_row(self.table, row)
            if 0 <= index < len(self.rows):
                out.append(self.rows[index])
        return out

    def analyse_selected(self) -> bool:
        rows = [r for r in self.selected_rows() if r.state != "analysing"]
        return self.run_deep(rows=rows) if rows else False

    def queue_selected(self) -> int:
        return self.queue_rows([r for r in self.selected_rows() if r.state == "new"])

    def open_selected_card(self) -> None:
        rows = [r for r in self.selected_rows() if r.card_id is not None]
        if rows:
            self.main.open_card(rows[0].card_id)

    def open_selected_site(self) -> None:
        for row in self.selected_rows():
            site = getattr(row.item, "site", None) or getattr(row.item, "website", None)
            if site:
                self.main._opener(site if "://" in site else f"https://{site}")
                return

    # --- drawing ------------------------------------------------------------------------------------------------

    def _affinity_changed(self) -> None:
        if not self._closed and self.isVisible():
            self._mark_states()
            self._redraw()

    def apply_palette(self, palette) -> None:
        self._palette = palette
        self.suggestions.apply_palette(palette)
        self._redraw()

    def _redraw(self) -> None:
        from .theme import active_palette

        if self._closed:
            return
        palette = self._palette or active_palette()
        service = getattr(self.main, "affinity_service", None)
        table = self.table
        selected = {r.key for r in self.selected_rows()}
        table.setRowCount(0)
        table.setRowCount(len(self.rows))
        for row_index, row in enumerate(self.rows):
            values = {"name": row.name, "source": t(f"interest.source.{row.source}"), "place": row.place,
                      "reason": row.reason, "state": t(f"interest.state.{row.state}")}
            for column, name in enumerate(BASE_COLUMNS):
                item = QTableWidgetItem(values[name])
                if name == "reason":
                    item.setToolTip(t("interest.fast.reason_tip", reason=row.reason))
                if name == "state" and row.state == "new":
                    item.setForeground(QBrush(QColor(palette.muted)))
                if column == 0:
                    item.setData(table_sort.ROW_ID_ROLE, row_index)
                    item.setData(table_sort.SORT_ROLE, row.name.casefold())
                table.setItem(row_index, column, item)
            result = service.result_for(row.item) if service is not None and row.card_id is not None else None
            cells = affinity_cells(result, palette)
            for offset, name in enumerate(AFFINITY_COLUMNS):
                table.setItem(row_index, len(BASE_COLUMNS) + offset, apply_cell(QTableWidgetItem(), cells[name]))
        table_sort.resort(table)
        for r in range(table.rowCount()):
            index = table_sort.source_row(table, r)
            if 0 <= index < len(self.rows) and self.rows[index].key in selected:
                table.selectRow(r)
        if table.columnWidth(0) < 80 and self.rows:
            table.resizeColumnsToContents()
        self._update_buttons()

    def _update_buttons(self) -> None:
        if self._closed:
            return
        selected = self.selected_rows()
        self.fast_action.setEnabled(not self.fast_running)
        self.deep_action.setEnabled(not self.deep_running and bool(self.rows))
        self.cancel_action.setEnabled(self.running)
        self.analyse_selected_action.setEnabled(not self.deep_running and any(r.state != "analysing"
                                                                              for r in selected))
        self.queue_selected_action.setEnabled(any(r.state == "new" for r in selected))
        self.open_card_action.setEnabled(any(r.card_id is not None for r in selected))
        self.site_action.setEnabled(any(getattr(r.item, "site", None) or getattr(r.item, "website", None)
                                        for r in selected))
        for action, button in ((self.fast_action, self.fast_button), (self.deep_action, self.deep_button),
                               (self.cancel_action, self.cancel_button),
                               (self.analyse_selected_action, self.analyse_selected_button),
                               (self.queue_selected_action, self.queue_selected_button),
                               (self.open_card_action, self.open_card_button), (self.site_action, self.site_button)):
            button.setEnabled(action.isEnabled())

    def _bar(self, owner: str, fraction, tooltip: str, text: str = "") -> None:
        from .progress_bar import BarState

        bar = getattr(self.main, "status_progress", None)
        if bar is not None and not getattr(self.main, "_closed", False):
            bar.show(owner, BarState(fraction, text, tooltip))

    def _finish_bar(self, owner: str) -> None:
        bar = getattr(self.main, "status_progress", None)
        if bar is not None and not getattr(self.main, "_closed", False):
            bar.finish(owner)

    def retranslate(self) -> None:
        self.setWindowTitle(t("interest.title"))
        self.area_box.setTitle(t("interest.area.title"))
        for index, kind in enumerate(AREA_KINDS):
            self.kind_combo.setItemText(index, t(f"interest.area.kind.{kind}"))
        self.kind_label.setText(t("interest.area.kind"))
        self.province_label.setText(t("interest.area.province"))
        self.region_label.setText(t("interest.area.region"))
        self.town_label.setText(t("interest.area.town"))
        self.radius_label.setText(t("interest.area.radius"))
        self.directions_label.setText(t("interest.area.directions"))
        self.types_label.setText(t("interest.area.types"))
        for key, box in self.type_checks.items():
            box.setText(suggest.text(f"suggest.type.{key}"))
        self.fast_box.setTitle(t("interest.fast.title"))
        self.fast_action.setText(t("interest.fast.action"))
        self.analyse_selected_action.setText(t("interest.analyse_selected"))
        self.queue_selected_action.setText(t("interest.queue_selected"))
        self.open_card_action.setText(t("interest.open_card"))
        self.site_action.setText(t("interest.open_site"))
        self.assistant_box.setTitle(t("interest.assistant.title"))
        self.deep_box.setTitle(t("interest.deep.title"))
        self.deep_label.setText(t("interest.deep.count"))
        self.deep_action.setText(t("interest.deep.action"))
        self.cancel_action.setText(t("interest.cancel"))
        self.actions_menu.setTitle(t("interest.menu"))
        for action, button in ((self.fast_action, self.fast_button), (self.deep_action, self.deep_button),
                               (self.cancel_action, self.cancel_button),
                               (self.analyse_selected_action, self.analyse_selected_button),
                               (self.queue_selected_action, self.queue_selected_button),
                               (self.open_card_action, self.open_card_button), (self.site_action, self.site_button)):
            button.setText(action.text())
        headers = []
        for column in COLUMNS:
            if column in AFFINITY_COLUMNS:
                headers.append(column_header(column)[0])
            else:
                headers.append(t(f"interest.column.{column}"))
        self.table.setHorizontalHeaderLabels(headers)
        if self.rows:
            self._fast_summary()
        elif not self.fast_running:
            self.fast_note.setText(t("interest.fast.help"))
        if not self.deep_running:
            self.deep_note.setText(t("interest.deep.help"))
        self.suggestions.retranslate()
        self._redraw()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt name)
        self.cancel()
        self.suggestions.close_view()
        self._finish_bar(PROGRESS_FAST)
        if not self.deep_running:
            self._finish_bar(PROGRESS_DEEP)
        self._closed = True
        service = getattr(self.main, "affinity_service", None)
        if service is not None and self._affinity_changed in service.listeners:
            service.listeners.remove(self._affinity_changed)
        log.info("Interest flow closed")
        super().closeEvent(event)
