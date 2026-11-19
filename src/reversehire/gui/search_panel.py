"""Step 3-4: where to search (address, coordinates or a set starting point), radius,
where the companies come from (sources, region, area, local map data, imported
lists), the company categories of interest (ATECO), companies analysed in parallel,
manual websites and the start button.

The discovery widgets keep their choices in the Store (discovery/prefs.py), so the
search uses them even when the caller passes nothing; with `settings` given, the
ticked sources and the region are also written to it (`on_settings_changed`)."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QBoxLayout, QCheckBox, QComboBox, QCompleter, QDialog, QFrame, QGridLayout, QScrollArea, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QProgressBar, QPushButton, QSlider, QSpinBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..numbers import format_int, format_number  # noqa: F401 - marks of the interface language
from ..geo.geocoding import Place
from ..i18n import current_language, t
from .common import format_km, parse_number
from .theme import section, set_role
from . import table_filter
from .widgets import wrapped_label

log = logging.getLogger(__name__)

RADIUS_SLIDER_MIN = 0.5
RADIUS_SLIDER_MAX = 50.0  # the slider only: the field accepts any positive value
SLIDER_STEPS_PER_KM = 2   # 0.5 km steps
PARALLEL_MIN = 1
PARALLEL_MAX = 16
PARALLEL_DEFAULT = 8
# Sources the user can switch (the own list is always used). "regional_register" is the
# ONE register checkbox: official lists and the lists the user imported (registers/).
SWITCHABLE_SOURCES = ("osm_extract", "regional_register", "overpass", "wikidata", "overture")
IMPORT_FIELDS = ("name", "site", "address", "town", "province", "postcode", "cf", "vat", "rea",
                 "ateco", "pec", "email", "phone", "employees")
EXTRACT_SIZE_MB = 205  # Emilia-Romagna, shown before the first download


def _text(key: str, **params) -> str:
    from ..discovery.texts import text

    return text(key, **params)


class _Bridge(QObject):
    """Carries worker-thread news to the GUI thread."""

    message = Signal(object)


class SearchPanel(QWidget):
    def __init__(self, on_check: Callable[[str], None], on_start: Callable[[], None],
                 on_set_start_point: Callable[[], None], on_parallel_change: Callable[[int], None],
                 initial_radius: float = 15.0, initial_parallel: int = PARALLEL_DEFAULT,
                 parent: QWidget | None = None, *, store=None, settings=None,
                 on_settings_changed: Callable | None = None,
                 choose_file: Callable[[], str | None] | None = None):
        super().__init__(parent)
        self.verified_place: Place | None = None
        self._verified_text = ""
        self._syncing = False
        self._result_key: str | None = None
        self._store = store
        self._settings = settings
        self._on_settings_changed = on_settings_changed
        self._choose_file = choose_file
        self._profile = None
        self._proposed: list = []            # AtecoCode proposed for the profile
        self._map_cancel: threading.Event | None = None
        self._map_thread: threading.Thread | None = None
        self._bridge = _Bridge()
        self._bridge.message.connect(self._on_worker_message)
        self._statuses: dict[str, str] = {}
        # Called when the verified place, the start point or the radius change (the map of
        # this tab follows them); set by the main window.
        self.on_location_changed: Callable[[], None] = lambda: None

        self.place_box = QGroupBox()
        self.address_label = QLabel()
        self.address_field = QLineEdit()
        self.address_field.returnPressed.connect(lambda: on_check(self.place_text()))
        self.address_field.textChanged.connect(self._place_changed)
        self.check_button = QPushButton()
        self.check_button.clicked.connect(lambda: on_check(self.place_text()))
        self.start_point_button = QPushButton()
        self.start_point_button.clicked.connect(lambda: on_set_start_point())
        self.result_label = wrapped_label(role="muted")

        self.radius_label = QLabel()
        self.radius_field = QLineEdit(format_km(initial_radius))
        self.radius_field.setMaxLength(8)
        self.radius_field.setMaximumWidth(self.fontMetrics().averageCharWidth() * 10 + 16)
        self.radius_field.textChanged.connect(self._radius_field_changed)
        self.km_label = QLabel()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(int(RADIUS_SLIDER_MIN * SLIDER_STEPS_PER_KM),
                             int(RADIUS_SLIDER_MAX * SLIDER_STEPS_PER_KM))
        self.slider.setValue(self._slider_value(initial_radius))
        self.slider.valueChanged.connect(self._slider_moved)
        self.radius_note = wrapped_label(role="muted")

        self.parallel_label = QLabel()
        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(PARALLEL_MIN, PARALLEL_MAX)
        self.parallel_spin.setValue(max(PARALLEL_MIN, min(PARALLEL_MAX, int(initial_parallel))))
        self.parallel_spin.valueChanged.connect(on_parallel_change)

        address_row = QHBoxLayout()
        address_row.setContentsMargins(0, 0, 0, 0)
        address_row.addWidget(self.address_field, 1)
        address_row.addWidget(self.check_button)
        address_row.addWidget(self.start_point_button)
        radius_row = QHBoxLayout()
        radius_row.setContentsMargins(0, 0, 0, 0)
        radius_row.addWidget(self.radius_field)
        radius_row.addWidget(self.km_label)
        radius_row.addWidget(self.slider, 1)
        radius_row.addSpacing(18)
        radius_row.addWidget(self.parallel_label)
        radius_row.addWidget(self.parallel_spin)

        grid = QGridLayout(self.place_box)
        grid.addWidget(self.address_label, 0, 0)
        grid.addLayout(address_row, 0, 1)
        grid.addWidget(self.result_label, 1, 1)
        grid.addWidget(self.radius_label, 2, 0)
        grid.addLayout(radius_row, 2, 1)
        grid.addWidget(self.radius_note, 3, 1)
        grid.setColumnStretch(1, 1)

        self._build_discovery_box()
        self._build_ateco_box()

        self.urls_box = QGroupBox()
        self.urls_help = wrapped_label(role="muted")
        self.urls_text = QPlainTextEdit()
        self.urls_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.urls_text.setPlaceholderText("www.example.com")
        urls_layout = QVBoxLayout(self.urls_box)
        urls_layout.addWidget(self.urls_help)
        urls_layout.addWidget(self.urls_text, 1)

        self.start_button = QPushButton()
        self.start_button.setProperty("role", "accent")
        self.start_button.clicked.connect(lambda: on_start())
        self.requirements_label = wrapped_label(role="muted")
        # Why the last click on Start did not start (next to the button: the place
        # result at the top of the tab is out of view down here).
        self.start_note = wrapped_label(role="warning")
        self.start_note.hide()
        notes = QVBoxLayout()
        notes.setContentsMargins(0, 0, 0, 0)
        notes.addWidget(self.start_note)
        notes.addWidget(self.requirements_label)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.addWidget(self.start_button, 0, Qt.AlignmentFlag.AlignTop)
        bottom.addLayout(notes, 1)

        # Side by side when there is room, one above the other on a narrow window.
        self.middle = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        middle = self.middle
        middle.setContentsMargins(0, 0, 0, 0)
        middle.addWidget(self.discovery_box, 1)
        middle.addWidget(self.ateco_box, 1)

        # The content scrolls when the window is small: the discovery boxes must not push
        # the window's minimum height beyond a small laptop screen.
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addWidget(self.place_box)
        layout.addLayout(middle)
        layout.addWidget(self.urls_box, 1)
        layout.addLayout(bottom)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        # last resort only: the boxes wrap first (see _adapt_layout)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.scroll)
        self._load_prefs()

    # --- discovery widgets

    def _build_discovery_box(self) -> None:
        from ..geo.regions import REGIONS

        self.discovery_box = QGroupBox()
        self.region_label = QLabel()
        self.region_combo = QComboBox()
        for region in REGIONS.values():
            self.region_combo.addItem(region.name, region.key)
        self.region_combo.currentIndexChanged.connect(self._region_changed)
        self.area_label = QLabel()
        self.area_combo = QComboBox()
        for code in ("radius", "province", "region"):
            self.area_combo.addItem(code, code)
        self.area_combo.currentIndexChanged.connect(self._area_changed)
        self.province_combo = QComboBox()
        self.province_combo.currentIndexChanged.connect(lambda _i: (self._save_prefs(), self.refresh_coverage()))

        self.source_checks: dict[str, QCheckBox] = {}
        self.source_status: dict[str, QLabel] = {}
        sources_grid = QGridLayout()
        sources_grid.setContentsMargins(0, 0, 0, 0)
        for row, name in enumerate(SWITCHABLE_SOURCES):
            box = QCheckBox()
            box.toggled.connect(lambda _on: self._sources_changed())
            status = QLabel()
            set_role(status, "muted")
            status.setWordWrap(True)
            self.source_checks[name] = box
            self.source_status[name] = status
            sources_grid.addWidget(box, row, 0)
            sources_grid.addWidget(status, row, 1)
        sources_grid.setColumnStretch(1, 1)

        self.map_label = wrapped_label(role="muted")
        self.map_button = QPushButton()
        self.map_button.clicked.connect(self.toggle_map_download)
        self.map_progress = QProgressBar()
        self.map_progress.setRange(0, 100)
        self.map_progress.hide()
        self.import_button = QPushButton()
        self.import_button.clicked.connect(lambda: self.import_list())
        self.register_button = QPushButton()   # "Register manager…": the one register window
        self.register_button.clicked.connect(lambda: self.open_register())
        self.register_dialog = None
        # Set by the main window: opens its Register manager; the register it shares.
        self.on_open_register: Callable[[], object] | None = None
        self.register_manager_getter: Callable[[], object] | None = None
        self._import_threads: list[threading.Thread] = []
        self.imports_label = wrapped_label(role="muted")
        # how much of the real companies the free registers cover (stated honestly)
        self.coverage_label = wrapped_label(role="muted")
        self.coverage_label.setObjectName("registerCoverage")
        self.coverage_label.hide()

        # two rows (region; area + province): one row was wider than a small window
        place_row = QGridLayout()
        place_row.setContentsMargins(0, 0, 0, 0)
        place_row.addWidget(self.region_label, 0, 0)
        place_row.addWidget(self.region_combo, 0, 1, 1, 2)
        place_row.addWidget(self.area_label, 1, 0)
        place_row.addWidget(self.area_combo, 1, 1)
        place_row.addWidget(self.province_combo, 1, 2)
        place_row.setColumnStretch(1, 1)
        place_row.setColumnStretch(2, 1)
        for combo in (self.region_combo, self.area_combo, self.province_combo):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(8)
        map_row = QHBoxLayout()
        map_row.setContentsMargins(0, 0, 0, 0)
        map_row.addWidget(self.map_button)
        map_row.addWidget(self.map_progress, 1)
        import_row = QHBoxLayout()
        import_row.setContentsMargins(0, 0, 0, 0)
        import_row.addWidget(self.import_button)
        import_row.addWidget(self.register_button)
        import_row.addWidget(self.imports_label, 1)

        layout = QVBoxLayout(self.discovery_box)
        layout.addLayout(place_row)
        layout.addLayout(sources_grid)
        layout.addWidget(self.map_label)
        layout.addLayout(map_row)
        layout.addLayout(import_row)
        layout.addWidget(self.coverage_label)

    def _build_ateco_box(self) -> None:
        self.ateco_box = QGroupBox()
        self.ateco_help = wrapped_label(role="muted")
        self.ateco_list = QListWidget()
        self.ateco_list.itemChanged.connect(lambda _item: self._ateco_changed())
        self.ateco_field = QLineEdit()
        self.ateco_field.returnPressed.connect(self.add_ateco_from_field)
        self.ateco_add_button = QPushButton()
        self.ateco_add_button.clicked.connect(self.add_ateco_from_field)
        self._completer: QCompleter | None = None
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.ateco_field, 1)
        row.addWidget(self.ateco_add_button)
        layout = QVBoxLayout(self.ateco_box)
        layout.addWidget(self.ateco_help)
        layout.addWidget(self.ateco_list, 1)
        layout.addLayout(row)

    def _ensure_completer(self) -> None:
        language = current_language()
        if self._completer is not None and getattr(self, "_completer_language", None) == language:
            return
        from ..discovery import ateco

        self._completer_language = language
        if language == "en":  # English titles only with the English interface
            entries = [f"{row.code} — {row.title_en or row.title_it}" for row in ateco._table().values()]
        else:
            entries = [f"{row.code} — {row.title_it} / {row.title_en}" for row in ateco._table().values()]
        self._completer = QCompleter(entries, self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.ateco_field.setCompleter(self._completer)

    # --- store and settings

    def store(self):
        if self._store is None:
            from ..pipeline import open_store

            self._store = open_store()
        return self._store

    def _prefs(self):
        from ..discovery import prefs

        try:
            return prefs.read(self.store())
        except Exception:
            log.warning("Discovery choices not readable", exc_info=True)
            return prefs.DiscoveryPrefs()

    def _write_prefs(self, value) -> None:
        from ..discovery import prefs

        try:
            prefs.write(self.store(), value)
        except Exception:
            log.warning("Discovery choices not saved", exc_info=True)

    def _load_prefs(self) -> None:
        from ..discovery import source_names
        from ..discovery.prefs import effective_region, effective_sources

        value = self._prefs()
        self._syncing = True
        try:
            region = effective_region(self._settings, self.store())
            index = self.region_combo.findData(region)
            self.region_combo.setCurrentIndex(index if index >= 0 else self.region_combo.findData("emilia-romagna"))
            enabled = set(source_names(effective_sources(self._settings, self.store())))
            for name, box in self.source_checks.items():
                box.setChecked(name in enabled)
            self._fill_provinces()
            self.area_combo.setCurrentIndex(max(0, self.area_combo.findData(value.area)))
            if value.province:
                position = self.province_combo.findData(value.province)
                if position >= 0:
                    self.province_combo.setCurrentIndex(position)
            self.province_combo.setEnabled(value.area == "province")
            if value.ateco_custom:
                self._fill_ateco([], value.ateco)
        finally:
            self._syncing = False
        self.refresh_map_status()
        self.refresh_imports()

    def _save_prefs(self) -> None:
        if self._syncing:
            return
        value = self._prefs()
        value.area = self.area_combo.currentData() or "radius"
        value.province = self.province_combo.currentData() or ""
        value.region = self.region_combo.currentData() or ""
        from ..discovery import sources_value

        # the register is on by default: its "off" is kept explicitly ("-regional_register")
        value.sources = [n for n in sources_value(
            [n for n, box in self.source_checks.items() if box.isChecked()]).split(",") if n]
        self._write_prefs(value)

    def _sources_changed(self) -> None:
        if self._syncing:
            return
        self._save_prefs()
        if self._settings is not None:
            from ..discovery import source_names, sources_value

            current = [n for n in source_names(getattr(self._settings, "discovery_sources", ""))
                       if n not in SWITCHABLE_SOURCES and n not in ("imports", "own")]
            chosen = [n for n, box in self.source_checks.items() if box.isChecked()]
            self._settings.discovery_sources = sources_value(chosen + current) or "osm_extract"
            if self._on_settings_changed is not None:
                self._on_settings_changed(self._settings)

    def _region_changed(self, _index: int) -> None:
        if self._syncing:
            return
        self._fill_provinces()
        self._save_prefs()
        if self._settings is not None:
            self._settings.osm_region = self.region_combo.currentData() or "emilia-romagna"
            if self._on_settings_changed is not None:
                self._on_settings_changed(self._settings)
        self.refresh_map_status()

    def _area_changed(self, _index: int) -> None:
        self.province_combo.setEnabled(self.area_combo.currentData() == "province")
        self._save_prefs()
        self.refresh_coverage()

    def _fill_provinces(self) -> None:
        from ..discovery import osm_extract
        from ..geo.regions import provinces_of, region_for

        region = region_for(self.region_combo.currentData())
        provinces = [(p.code, p.name) for p in provinces_of(region)]
        if not provinces and region is not None:
            try:
                if osm_extract.status(region).ready:
                    provinces = osm_extract.OsmIndex(osm_extract.index_path(region)).provinces()
            except Exception:
                provinces = []
        was = self._syncing
        self._syncing = True
        try:
            current = self.province_combo.currentData()
            self.province_combo.clear()
            for code, name in provinces:
                self.province_combo.addItem(f"{name} ({code})", code)
            position = self.province_combo.findData(current)
            if position >= 0:
                self.province_combo.setCurrentIndex(position)
            elif current:   # another region: no province of it is chosen for the user
                self.province_combo.setCurrentIndex(-1)
        finally:
            self._syncing = was

    # --- values for the search

    def discovery_choices(self) -> dict:
        """{"area", "province", "ateco", "sources", "region"} as chosen in the panel."""
        return {"area": self.area_combo.currentData() or "radius",
                "province": self.province_combo.currentData() or "",
                "ateco": self.chosen_ateco(),
                "sources": [n for n, box in self.source_checks.items() if box.isChecked()],
                "region": self.region_combo.currentData() or ""}

    # --- ATECO categories

    def set_profile(self, profile) -> None:
        """Propose the ATECO categories of the profile (unless the user changed the list)."""
        from ..discovery.ateco import ateco_for_profile

        self._profile = profile
        try:
            self._proposed = ateco_for_profile(profile) if profile is not None else []
        except Exception:
            log.warning("ATECO proposals not computed", exc_info=True)
            self._proposed = []
        value = self._prefs()
        if value.ateco_custom:
            self._fill_ateco(self._proposed, value.ateco)
        else:
            self._fill_ateco(self._proposed, [c.code for c in self._proposed])

    def _fill_ateco(self, proposed: list, chosen: list[str]) -> None:
        from ..discovery.ateco import describe

        was = self._syncing
        self._syncing = True
        try:
            self.ateco_list.clear()
            seen = set()
            for code in [c.code for c in proposed] + [c for c in chosen]:
                if code in seen:
                    continue
                seen.add(code)
                entry = next((c for c in proposed if c.code == code), None) or describe(code)
                if entry is None:
                    continue
                # the ISTAT table has both titles: English with the English interface
                english = current_language() == "en"
                shown, other = (entry.name_en, entry.name_it) if english else (entry.name_it, entry.name_en)
                item = QListWidgetItem(f"{entry.code}  {shown or other}")
                tip = (other or shown) + (f"\n{entry.why}" if getattr(entry, "why", "") else "")
                item.setToolTip(tip)
                item.setData(Qt.ItemDataRole.UserRole, entry.code)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if code in chosen else Qt.CheckState.Unchecked)
                self.ateco_list.addItem(item)
        finally:
            self._syncing = was

    def chosen_ateco(self) -> list[str]:
        result = []
        for i in range(self.ateco_list.count()):
            item = self.ateco_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                result.append(item.data(Qt.ItemDataRole.UserRole))
        return result

    def _ateco_changed(self) -> None:
        if self._syncing:
            return
        value = self._prefs()
        value.ateco = self.chosen_ateco()
        value.ateco_custom = True
        self._write_prefs(value)

    def add_ateco(self, code: str) -> bool:
        from ..discovery.ateco import describe

        entry = describe(code)
        if entry is None:
            return False
        for i in range(self.ateco_list.count()):
            item = self.ateco_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == entry.code:
                item.setCheckState(Qt.CheckState.Checked)
                return True
        chosen = self.chosen_ateco() + [entry.code]
        self._fill_ateco(self._proposed, chosen)
        self._ateco_changed()
        return True

    def add_ateco_from_field(self) -> None:
        from ..discovery.ateco import search

        text = self.ateco_field.text().strip()
        if not text:
            return
        code = text.split(" ")[0]
        if not self.add_ateco(code):
            found = search(text, limit=1)
            if found:
                self.add_ateco(found[0].code)
        self.ateco_field.clear()

    # --- local map data

    def _region(self):
        from ..geo.regions import region_for

        return region_for(self.region_combo.currentData())

    def refresh_map_status(self) -> None:
        from ..discovery import osm_extract

        region = self._region()
        try:
            state = osm_extract.status(region)
        except Exception:
            state = None
        if state is None or not state.ready:
            size = round(state.pbf_bytes / 1e6) if state is not None and state.pbf_exists else EXTRACT_SIZE_MB
            self.map_label.setText(_text("discovery.map_none", size=size))
        else:
            self.map_label.setText(_text("discovery.map_ready", features=format_int(state.features),
                                         age=f"{state.index_age_days or 0:.0f}",
                                         size=round(state.pbf_bytes / 1e6)))

    @property
    def map_busy(self) -> bool:
        return self._map_thread is not None and self._map_thread.is_alive()

    def toggle_map_download(self) -> None:
        if self.map_busy:
            if self._map_cancel is not None:
                self._map_cancel.set()
            return
        self.start_map_download()

    def start_map_download(self, force: bool = True) -> None:
        """Download (or refresh) and index the region's map data on a worker thread."""
        from ..discovery import osm_extract
        from ..discovery.gateway import PoliteGateway

        region = self._region()
        if region is None:
            return
        cancel = threading.Event()
        self._map_cancel = cancel
        bridge = self._bridge
        settings = self._settings

        def progress(phase: str, done: int, total: int) -> None:
            bridge.message.emit(("map_progress", phase, done, total))

        def job() -> None:
            gateway = PoliteGateway(settings)
            try:
                state = osm_extract.status(region)
                fresh = state.pbf_exists and (state.pbf_age_days or 0) <= osm_extract.MAX_AGE_DAYS
                outcome = osm_extract.download(region, gateway, cancel=cancel, progress=progress,
                                               force=force and not fresh)
                report = None
                if outcome != "reused" or not osm_extract.status(region).ready or force:
                    report = osm_extract.build_index(osm_extract.pbf_path(region), osm_extract.index_path(region),
                                                     cancel=cancel, progress=progress, region_key=region.key)
                log.info("Map data worker finished (%s): %s", region.key, outcome)
                bridge.message.emit(("map_done", report))
            except Exception as error:  # Cancelled, network, disk: shown to the user
                log.warning("Map data worker stopped (%s): %s", region.key, type(error).__name__)
                bridge.message.emit(("map_failed", error))
            finally:
                gateway.close()

        log.info("Map data worker started (%s)", region.key)
        self._map_thread = threading.Thread(target=job, daemon=True, name="map_data")
        self.map_progress.setRange(0, 100)
        self.map_progress.setValue(0)
        self.map_progress.show()
        self.map_button.setText(_text("discovery.map_cancel"))
        self._map_thread.start()

    def _on_worker_message(self, message) -> None:
        kind = message[0]
        if kind == "coverage":
            self._show_coverage(message[1])
            return
        if kind == "imports_info":
            self._show_imports_info(message[1])
            return
        if kind == "import_done":
            self._show_import_done(message[1])
            return
        if kind == "map_progress":
            _, phase, done, total = message
            percent = int(100 * done / total) if total else 0
            if total:
                self.map_progress.setRange(0, 100)
                self.map_progress.setValue(percent)
            else:
                self.map_progress.setRange(0, 0)  # size not announced by the server: busy bar
            if phase == "download":
                self.map_label.setText(_text("discovery.map_progress", done=round(done / 1e6),
                                             total=round(total / 1e6) if total else "?"))
            else:
                self.map_label.setText(_text("discovery.map_indexing", percent=percent))
        elif kind == "map_done":
            report = message[1]
            self.map_progress.hide()
            self.map_button.setText(_text("discovery.map_download"))
            self.refresh_map_status()
            self._fill_provinces()
            if report is not None:
                self.map_label.setText(self.map_label.text() + " " + _text(
                    "discovery.map_done", features=format_int(report.features), seconds=f"{report.seconds:.0f}"))
        elif kind == "map_failed":
            from ..discovery.gateway import Cancelled

            error = message[1]
            self.map_progress.hide()
            self.map_button.setText(_text("discovery.map_download"))
            self.refresh_map_status()
            if not isinstance(error, Cancelled):
                self.map_label.setText(_text("discovery.map_failed", detail=str(error) or type(error).__name__))

    # --- imports

    def register_manager(self):
        """The regional register (the window's shared one, else the data folder's)."""
        if self.register_manager_getter is not None:
            return self.register_manager_getter()
        from ..registers import shared_manager

        return shared_manager(self._settings)

    def _in_thread(self, name: str, work: Callable[[], object], kind: str) -> None:
        """Runs `work` on a thread; its result comes back as the message (kind, result)."""
        bridge = self._bridge

        def job() -> None:
            try:
                result = work()
            except Exception as error:  # noqa: BLE001 - shown in the panel, logged
                log.warning("Search tab: %s failed", name, exc_info=True)
                result = error
            try:
                bridge.message.emit((kind, result))
            except RuntimeError:   # the panel was closed meanwhile
                pass

        thread = threading.Thread(target=job, daemon=True, name=name)
        self._import_threads = [th for th in self._import_threads if th.is_alive()] + [thread]
        thread.start()

    def wait_imports(self, timeout: float = 15.0) -> None:
        """Waits for the register work of this panel and shows its outcome (tests)."""
        from PySide6.QtWidgets import QApplication

        for thread in list(self._import_threads):
            thread.join(timeout)
        for _ in range(3):
            QApplication.processEvents()

    def refresh_imports(self) -> None:
        """The summary of the imported lists (the lists of an older version are moved
        into the register first, once), read on a worker."""
        try:
            manager, store = self.register_manager(), self.store()
        except Exception:
            log.warning("Regional register not available", exc_info=True)
            return

        def work():
            from ..registers import lists

            lists.migrate_imported_lists(store, manager.db)
            return lists.lists(manager.db)

        self._in_thread("register summary", work, "imports_info")
        self.refresh_coverage()

    def refresh_coverage(self) -> None:
        """The coverage note of the free registers for the chosen province (or region),
        read on a thread; shown under the register's controls."""
        try:
            manager = self.register_manager()
        except Exception:  # noqa: BLE001
            return
        province = self.province_combo.currentData() or ""
        box = self.source_checks.get("regional_register")
        if manager is None or (box is not None and not box.isChecked()):
            self.coverage_label.hide()
            return

        def work():
            if not manager.db.count():
                return None      # nothing downloaded yet: no note
            return manager.db.coverage_note(province, "")

        self._in_thread("register coverage", work, "coverage")

    def _show_coverage(self, note) -> None:
        text = note.get("text", "") if isinstance(note, dict) else ""
        self.coverage_label.setText(text)
        self.coverage_label.setVisible(bool(text))
        status = self.source_status.get("regional_register")
        if status is not None:
            status.setToolTip(text)

    def _show_imports_info(self, infos) -> None:
        if isinstance(infos, Exception):
            return
        if infos:
            self.imports_label.setText(_text("discovery.imports_count", lists=len(infos),
                                             n=format_int(sum(item['count'] for item in infos))))
        else:
            self.imports_label.setText("")

    def import_list(self, path: str | None = None) -> bool:
        """Ask for a CSV/XLSX file, show the preview with the column mapping, store it."""
        from ..discovery import imports

        if path is None:
            path = self._choose_file() if self._choose_file is not None else self._ask_file()
        if not path:
            return False
        try:
            preview = imports.preview(path)
        except Exception as error:
            self.imports_label.setText(_text("discovery.import_failed", detail=str(error)))
            return False
        dialog = ImportDialog(self, preview)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        return self.save_import(preview, dialog.mapping(), dialog.kind(), source=dialog.source(),
                                source_date=dialog.source_date())

    def save_import(self, preview, mapping: dict, kind: str, *, source: str = "", source_date: str = "") -> bool:
        """Store the list in the regional register (one source "list:<id>"), on a worker;
        the outcome is shown next to the buttons (`wait_imports` in tests)."""
        from ..discovery import imports

        rows = imports.records(preview, mapping)
        if not rows:
            self.imports_label.setText(_text("discovery.import_failed", detail="no company with a name"))
            return False
        manager = self.register_manager()
        name = Path(preview.path).name
        date = source_date or file_date(preview.path)

        def work():
            from ..registers import lists

            report = lists.import_list(manager.db, name, kind, rows, source=source, source_date=date,
                                       path=str(preview.path))
            return report, name, lists.lists(manager.db)

        log.info("Search tab: company list imported into the register (%d rows)", len(rows))
        self.imports_label.setText(_text("discovery.register.loading"))
        self._in_thread("register import", work, "import_done")
        return True

    def _show_import_done(self, result) -> None:
        if isinstance(result, Exception):
            self.imports_label.setText(_text("discovery.import_failed", detail=str(result) or type(result).__name__))
            return
        report, name, infos = result
        if report.state != "ok":
            self.imports_label.setText(_text("discovery.import_failed", detail=report.detail or report.state))
            return
        self._show_imports_info(infos)
        self.imports_label.setText(self.imports_label.text() + " " + _text(
            "discovery.import_done", n=report.rows, name=name))
        window = self.register_dialog
        if window is not None and not getattr(window, "_closed", True):
            window.reload()

    def open_register(self):
        """"Register manager…": the one register window (the main window's, when set)."""
        if self.on_open_register is not None:
            return self.on_open_register()
        from .register_manager import RegisterManagerWindow

        window = self.register_dialog
        if window is not None and not getattr(window, "_closed", True):
            window.raise_()
            window.activateWindow()
            return window
        choose = self._choose_file
        window = RegisterManagerWindow(self, self.register_manager(), store=self.store(),
                                       choose_file=(lambda _title, _filters: choose()) if choose else None,
                                       on_changed=self.refresh_imports)
        self.register_dialog = window
        window.show()
        return window

    def _ask_file(self) -> str | None:
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(self, _text("discovery.import_title"), "",
                                              "CSV / Excel (*.csv *.txt *.xlsx);;*.*")
        return path or None

    # --- statuses from the search

    def show_source_status(self, event) -> None:
        """Show a "pipeline.source_status" event next to its source (call it from the
        window's event handler)."""
        parameters = getattr(event, "parameters", {}) or {}
        name = parameters.get("source")
        if name in self.source_status:
            self.source_status[name].setText(parameters.get("line") or getattr(event, "message", ""))
            self._statuses[name] = parameters.get("state", "")

    def show_statuses(self, statuses) -> None:
        from ..discovery.texts import status_line

        for status in statuses or []:
            if status.name in self.source_status:
                self.source_status[status.name].setText(status_line(status))

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._profile is None:
            try:  # the profile of the window's profile tab, when there is one
                profile = self.window().profile_panel.current_profile()
            except Exception:
                profile = None
            if profile is not None:
                self.set_profile(profile)
        if not self.map_busy:  # the index being built is locked by the worker
            self.refresh_map_status()

    # --- values

    def place_text(self) -> str:
        return self.address_field.text().strip()

    def set_place_text(self, text: str) -> None:
        self.address_field.setText(text)

    def radius(self) -> float | None:
        """Radius in km, or None if it is not a positive number."""
        value = parse_number(self.radius_field.text())
        if value is None or value <= 0:
            return None
        return value

    def set_radius(self, km: float) -> None:
        self.radius_field.setText(format_km(km))

    def set_radius_text(self, text: str) -> None:
        self.radius_field.setText(text)

    def slider_km(self) -> float:
        return self.slider.value() / SLIDER_STEPS_PER_KM

    def parallel(self) -> int:
        return self.parallel_spin.value()

    def manual_urls(self) -> list[str]:
        return [r.strip() for r in self.urls_text.toPlainText().splitlines() if r.strip()]

    def valid_place(self) -> Place | None:
        """The verified place, if the text has not changed since."""
        if self.verified_place is not None and self._verified_text == self.place_text():
            return self.verified_place
        return None

    def result_text(self) -> str:
        return self.result_label.text()

    def show_place(self, text: str, place: Place | None) -> None:
        if place is None:
            self.verified_place = None
            self._result_key = "error.place_not_found"
            self.result_label.setText(t("error.place_not_found"))
            set_role(self.result_label, "error")
            self.on_location_changed()
            return
        self.verified_place = place
        self._verified_text = text
        self._result_key = "place.found"
        self.result_label.setText(self._describe(place))
        set_role(self.result_label, "success")
        self.on_location_changed()

    def set_start_point(self, place: Place) -> None:
        """A point from the starting-point dialog: already verified, no geocoding needed."""
        text = f"{place.lat:.6f}, {place.lon:.6f}"
        self._syncing = True
        try:
            self.address_field.setText(text)
        finally:
            self._syncing = False
        self.show_place(text, place)

    def show_checking(self) -> None:
        self._result_key = "place.checking"
        self.result_label.setText(t("place.checking"))
        set_role(self.result_label, "muted")

    def _describe(self, place: Place) -> str:
        return t("place.found", name=place.name, lat=f"{place.lat:.5f}", lon=f"{place.lon:.5f}")

    def _place_changed(self, _text: str) -> None:
        if self._syncing:
            return
        if self.verified_place is not None and self.place_text() != self._verified_text:
            self._result_key = "place.needs_check"
            self.result_label.setText(t("place.needs_check"))
            set_role(self.result_label, "muted")

    # --- radius field <-> slider

    @staticmethod
    def _slider_value(km: float) -> int:
        km = min(max(km, RADIUS_SLIDER_MIN), RADIUS_SLIDER_MAX)
        return int(round(km * SLIDER_STEPS_PER_KM))

    def _slider_moved(self, value: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            self.radius_field.setText(format_km(value / SLIDER_STEPS_PER_KM))
        finally:
            self._syncing = False

    def _radius_field_changed(self, _text: str) -> None:
        if self._syncing:
            return
        value = self.radius()
        if value is None:
            return
        self._syncing = True
        try:
            self.slider.setValue(self._slider_value(value))
        finally:
            self._syncing = False

    # --- texts

    def set_requirements(self, text: str) -> None:
        """The note about tab 1, from the window: what the search still needs ("" = nothing)."""
        self.requirements_label.setText(text or "")

    def enable_start(self, enabled: bool) -> None:
        self.start_button.setEnabled(enabled)

    def area(self) -> str:
        return self.area_combo.currentData() or "radius"

    def show_start_problem(self, text: str) -> None:
        """Why Start did not start, shown next to the button ("" hides it)."""
        self.start_note.setText(text)
        self.start_note.setVisible(bool(text))
        if text:
            self.scroll.ensureWidgetVisible(self.start_note)

    def focus_address(self) -> None:
        """The address field, scrolled into view with the keyboard focus."""
        self.scroll.ensureWidgetVisible(self.address_field)
        self.address_field.setFocus(Qt.FocusReason.OtherFocusReason)
        self.address_field.selectAll()

    # --- narrow windows: the two discovery boxes go one above the other

    def _adapt_layout(self) -> None:
        width = self.scroll.viewport().width()
        side_by_side = (self.discovery_box.minimumSizeHint().width() + self.ateco_box.minimumSizeHint().width()
                        + self.middle.spacing() + 24)
        direction = (QBoxLayout.Direction.LeftToRight if width >= side_by_side
                     else QBoxLayout.Direction.TopToBottom)
        if self.middle.direction() != direction:
            self.middle.setDirection(direction)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._adapt_layout()

    def retranslate(self) -> None:
        self.place_box.setTitle(section(t("place.title")))
        self.address_label.setText(t("place.address"))
        self.address_field.setPlaceholderText(t("search.address_placeholder"))
        self.check_button.setText(t("place.check"))
        self.start_point_button.setText(t("start.button"))
        self.start_point_button.setToolTip(t("start.button_tip"))
        self.radius_label.setText(t("place.radius"))
        self.km_label.setText(t("unit.km"))
        self.radius_note.setText(t("place.radius_note"))
        self.parallel_label.setText(t("search.parallel"))
        self.parallel_spin.setToolTip(t("search.parallel_tip"))
        self.parallel_label.setToolTip(t("search.parallel_tip"))
        self.urls_box.setTitle(section(t("place.urls_title")))
        self.urls_help.setText(t("place.urls_instructions"))
        self.start_button.setText(t("place.start"))
        self._retranslate_discovery()
        if self.valid_place() is not None:
            self.result_label.setText(self._describe(self.verified_place))
        elif self._result_key:
            self.result_label.setText(t(self._result_key))
        # Re-formats the radius with the language's decimal separator.
        value = self.radius()
        if value is not None:
            self._syncing = True
            try:
                self.radius_field.setText(format_km(value))
            finally:
                self._syncing = False

    def _retranslate_discovery(self) -> None:
        from ..discovery.texts import source_label

        self.discovery_box.setTitle(section(_text("discovery.box_title")))
        self.region_label.setText(_text("discovery.region"))
        self.area_label.setText(_text("discovery.area"))
        for i in range(self.area_combo.count()):
            self.area_combo.setItemText(i, _text(f"discovery.area.{self.area_combo.itemData(i)}"))
        for name, box in self.source_checks.items():
            box.setText(source_label(name))
        if not self.map_busy:
            self.map_button.setText(_text("discovery.map_download"))
        self.import_button.setText(_text("discovery.import"))
        self.register_button.setText(_text("discovery.register.button"))
        self.register_button.setToolTip(_text("discovery.register.tooltip"))
        if self.register_dialog is not None and not getattr(self.register_dialog, "_closed", True):
            self.register_dialog.retranslate()
        self.ateco_box.setTitle(section(_text("discovery.ateco_title")))
        self.ateco_help.setText(_text("discovery.ateco_help"))
        self.ateco_field.setPlaceholderText(_text("discovery.ateco_search"))
        self.ateco_add_button.setText(_text("discovery.ateco_add"))
        if self.ateco_list.count():  # the category titles follow the interface language
            self._fill_ateco(self._proposed, self.chosen_ateco())
        self.refresh_map_status()
        self.refresh_imports()
        try:
            self._ensure_completer()
        except Exception:
            log.warning("ATECO search list not available", exc_info=True)


def file_date(path) -> str:
    """The date of a list file (its modification day), "" when unknown."""
    import datetime

    try:
        return datetime.date.fromtimestamp(Path(path).stat().st_mtime).isoformat()
    except (OSError, TypeError, ValueError):
        return ""


class ImportDialog(QDialog):
    """Preview of an imported list with the automatic column mapping (editable)."""

    def __init__(self, parent: QWidget | None, preview):
        super().__init__(parent)
        from ..discovery.texts import SOURCE_FIELD_LABELS

        self._preview = preview
        self.setWindowTitle(_text("discovery.import_title"))
        self.kind_combo = QComboBox()
        for kind in ("register", "startup", "generic"):
            self.kind_combo.addItem(_text(f"discovery.import_kind.{kind}"), kind)
        self.kind_combo.setCurrentIndex(max(0, self.kind_combo.findData(preview.kind)))
        self.source_field = QLineEdit(self.kind_combo.currentText())
        self.date_field = QLineEdit(file_date(preview.path))
        self.date_field.setPlaceholderText("2026-01-31")
        source_row = QHBoxLayout()
        source_row.addWidget(QLabel(_text("discovery.register.source_label")))
        source_row.addWidget(self.source_field, 2)
        source_row.addWidget(QLabel(_text("discovery.register.date_label")))
        source_row.addWidget(self.date_field, 1)
        self.combos: dict[str, QComboBox] = {}
        grid = QGridLayout()
        for row, name in enumerate(IMPORT_FIELDS):
            combo = QComboBox()
            combo.addItem(_text("discovery.import_none"), -1)
            for index, header in enumerate(preview.header):
                combo.addItem(header or f"#{index + 1}", index)
            combo.setCurrentIndex(max(0, combo.findData(preview.mapping.get(name, -1))))
            combo.currentIndexChanged.connect(lambda _i: self._refresh_table())
            self.combos[name] = combo
            grid.addWidget(QLabel(_text("discovery.import_column", field=SOURCE_FIELD_LABELS.get(name, name))),
                           row // 2, (row % 2) * 2)
            grid.addWidget(combo, row // 2, (row % 2) * 2 + 1)
        self.table = QTableWidget()
        self.table.setObjectName("importPreviewTable")
        table_filter.attach(self.table)
        self.count_label = QLabel()
        ok = QPushButton(t("common.save"))
        ok.setProperty("role", "accent")
        ok.clicked.connect(self.accept)
        cancel = QPushButton(t("common.cancel"))
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.count_label, 1)
        buttons.addWidget(ok)
        buttons.addWidget(cancel)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(Path(preview.path).name))
        layout.addWidget(self.kind_combo)
        layout.addLayout(source_row)
        layout.addLayout(grid)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        self._refresh_table()
        self.resize(900, 600)

    def mapping(self) -> dict[str, int]:
        return {name: combo.currentData() for name, combo in self.combos.items()
                if combo.currentData() is not None and combo.currentData() >= 0}

    def kind(self) -> str:
        return self.kind_combo.currentData()

    def source(self) -> str:
        return " ".join(self.source_field.text().split())

    def source_date(self) -> str:
        return self.date_field.text().strip()

    def _refresh_table(self) -> None:
        from ..discovery import imports

        mapping = self.mapping()
        fields = [f for f in IMPORT_FIELDS if f in mapping]
        sample = [imports.record_from_row(r, mapping) for r in self._preview.rows[:8]]
        self.table.setColumnCount(len(fields))
        self.table.setRowCount(len(sample))
        self.table.setHorizontalHeaderLabels(fields)
        for r, record in enumerate(sample):
            for c, name in enumerate(fields):
                self.table.setItem(r, c, QTableWidgetItem(str(record.get(name, ""))))
        self.count_label.setText(format_int(len(self._preview.rows)))
