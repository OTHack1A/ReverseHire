"""Dialog to set the starting point: paste from Google Maps, or type latitude and
longitude, optionally moved by an azimuth and a distance."""

from __future__ import annotations

import webbrowser

from PySide6.QtWidgets import (
    QDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from ..i18n import t
from .common import format_number
from .coordinates import DISTANCE_MAX_KM, StartPoint, compute_start_point, parse_coordinates
from .theme import section, set_invalid, set_role
from .widgets import wrapped_label

GOOGLE_MAPS_URL = "https://www.google.com/maps"


def _format_coordinate(value: float) -> str:
    return f"{value:.6f}"


class StartPointDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, opener=webbrowser.open,
                 initial: tuple[float, float] | None = None):
        super().__init__(parent)
        self._opener = opener
        self.result_point: StartPoint | None = None

        self.maps_box = QGroupBox()
        self.maps_help = wrapped_label(role="muted")
        self.maps_button = QPushButton()
        self.maps_button.clicked.connect(self.open_google_maps)
        self.paste_label = QLabel()
        self.paste_field = QLineEdit()
        self.paste_field.textChanged.connect(self._pasted)
        self.paste_status = wrapped_label(role="muted")
        maps_row = QHBoxLayout()
        maps_row.setContentsMargins(0, 0, 0, 0)
        maps_row.addWidget(self.maps_button)
        maps_row.addStretch(1)
        paste_row = QHBoxLayout()
        paste_row.setContentsMargins(0, 0, 0, 0)
        paste_row.addWidget(self.paste_label)
        paste_row.addWidget(self.paste_field, 1)
        maps_layout = QVBoxLayout(self.maps_box)
        maps_layout.addWidget(self.maps_help)
        maps_layout.addLayout(maps_row)
        maps_layout.addLayout(paste_row)
        maps_layout.addWidget(self.paste_status)

        self.coords_box = QGroupBox()
        self.lat_label, self.lon_label = QLabel(), QLabel()
        self.azimuth_label, self.distance_label = QLabel(), QLabel()
        self.lat_field, self.lon_field = QLineEdit(), QLineEdit()
        self.azimuth_field, self.distance_field = QLineEdit(), QLineEdit()
        self.offset_help = wrapped_label(role="muted")
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        for row, (label, field) in enumerate(((self.lat_label, self.lat_field),
                                              (self.lon_label, self.lon_field),
                                              (self.azimuth_label, self.azimuth_field),
                                              (self.distance_label, self.distance_field))):
            grid.addWidget(label, row, 0)
            grid.addWidget(field, row, 1)
            field.textChanged.connect(self._recompute)
        grid.setColumnStretch(1, 1)
        coords_layout = QVBoxLayout(self.coords_box)
        coords_layout.addLayout(grid)
        coords_layout.addWidget(self.offset_help)

        self.result_label = wrapped_label()
        self.use_button = QPushButton()
        self.use_button.setProperty("role", "accent")
        self.use_button.setDefault(True)
        self.use_button.clicked.connect(self.accept_point)
        self.cancel_button = QPushButton()
        self.cancel_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addStretch(1)
        buttons.addWidget(self.use_button)
        buttons.addWidget(self.cancel_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.maps_box)
        layout.addWidget(self.coords_box)
        layout.addWidget(self.result_label)
        layout.addLayout(buttons)

        self.retranslate()
        if initial is not None:
            self.lat_field.setText(_format_coordinate(initial[0]))
            self.lon_field.setText(_format_coordinate(initial[1]))
        self._recompute()
        # Wide enough for the explanations to wrap into a few readable lines. No
        # setMinimumWidth: an explicit minimum would replace the layout's own.
        width = max(self.sizeHint().width(), self.fontMetrics().averageCharWidth() * 72)
        self.resize(width, self.heightForWidth(width) if self.hasHeightForWidth() else self.sizeHint().height())

    def retranslate(self) -> None:
        self.setWindowTitle(t("start.title"))
        self.maps_box.setTitle(section(t("start.maps_section")))
        self.maps_help.setText(t("start.maps_help"))
        self.maps_button.setText(t("start.open_maps"))
        self.paste_label.setText(t("start.paste"))
        self.paste_field.setPlaceholderText(t("start.paste_placeholder"))
        self.coords_box.setTitle(section(t("start.coords_section")))
        self.lat_label.setText(t("start.lat"))
        self.lon_label.setText(t("start.lon"))
        self.azimuth_label.setText(t("start.azimuth"))
        self.distance_label.setText(t("start.distance"))
        self.lat_field.setPlaceholderText("44.493700")
        self.lon_field.setPlaceholderText("11.343000")
        self.azimuth_field.setPlaceholderText("0–360")
        self.distance_field.setPlaceholderText("km")
        self.offset_help.setText(t("start.offset_help"))
        self.use_button.setText(t("start.use"))
        self.cancel_button.setText(t("common.cancel"))
        self._recompute()

    def _fit_height(self) -> None:
        """Grows the dialog when messages wrap onto more lines (top-level windows
        do not follow their labels' height-for-width by themselves)."""
        layout = self.layout()
        if layout is None:
            return
        if layout.hasHeightForWidth():
            needed = layout.totalHeightForWidth(self.width())
        else:
            needed = layout.totalMinimumSize().height()
        if self.height() < needed:
            self.resize(self.width(), needed)

    def open_google_maps(self) -> None:
        self._opener(GOOGLE_MAPS_URL)

    def _pasted(self, text: str) -> None:
        if not text.strip():
            self.paste_status.setText("")
            set_invalid(self.paste_field, False)
            return
        pair = parse_coordinates(text)
        if pair is None:
            self.paste_status.setText(t("start.paste_invalid"))
            set_role(self.paste_status, "error")
            set_invalid(self.paste_field, True)
            self._fit_height()
            return
        set_invalid(self.paste_field, False)
        self.paste_status.setText(t("start.paste_ok"))
        self._fit_height()
        set_role(self.paste_status, "success")
        self.lat_field.setText(_format_coordinate(pair[0]))
        self.lon_field.setText(_format_coordinate(pair[1]))

    def _compute(self) -> tuple[StartPoint | None, list[str]]:
        return compute_start_point(self.lat_field.text(), self.lon_field.text(),
                                   self.azimuth_field.text(), self.distance_field.text())

    def _recompute(self, *_args) -> None:
        point, errors = self._compute()
        fields = {"start.err_lat": self.lat_field, "start.err_lon": self.lon_field,
                  "start.err_azimuth": self.azimuth_field, "start.err_distance": self.distance_field}
        for key, field in fields.items():
            set_invalid(field, key in errors)
        if point is None:
            messages = [t(e, max=format_number(DISTANCE_MAX_KM)) for e in errors]
            self.result_label.setText("\n".join(messages))
            set_role(self.result_label, "muted" if errors == ["start.empty"] else "error")
            self.use_button.setEnabled(False)
            self._fit_height()
            return
        if point.moved:
            text = t("start.result_moved", lat=_format_coordinate(point.lat), lon=_format_coordinate(point.lon),
                     km=format_number(point.distance_km, 2), azimuth=format_number(point.azimuth, 1),
                     lat0=_format_coordinate(point.origin_lat), lon0=_format_coordinate(point.origin_lon))
        else:
            text = t("start.result", lat=_format_coordinate(point.lat), lon=_format_coordinate(point.lon))
        self.result_label.setText(text)
        set_role(self.result_label, "success")
        self.use_button.setEnabled(True)
        self._fit_height()

    def accept_point(self) -> bool:
        point, errors = self._compute()
        if point is None:
            return False
        self.result_point = point
        self.accept()
        return True
