""""Paste figures…" of the company card: the user pastes the text he copied by hand from a
company-information page; the figures found (financials/paste.py) are shown in an editable
grid, one row per fiscal year, with the source the user states. Nothing is saved until the
user presses Save AND confirms; ranges ("10–50 mln") stay ranges.

The dialog is non-blocking for the rest of the window (open(), not exec()); the reading of
the pasted text is a pure, fast function (no network, no database), the saving goes through
the `on_save` callback of the card view.
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..financials.paste import Figure, format_figure, parse_figure, parse_pasted
from ..i18n import t
from . import table_filter
from .widgets import wrapped_label

log = logging.getLogger(__name__)

FIELDS = ("revenue", "profit", "employees")
COLUMNS = ("year",) + FIELDS

TEXTS = {
    "fin.paste.title": "Paste figures",
    "fin.paste.intro": "Open the company's page on a company-information site, select its text, copy it and "
                       "paste it here. The app only reads what you paste; check every figure before saving.",
    "fin.paste.placeholder": "Paste the page text here (e.g. \"Fatturato 2023: € 12.345.678\")",
    "fin.paste.read": "Read the figures",
    "fin.paste.found": "Figures found for {n} year(s). Check and correct them in the grid.",
    "fin.paste.none": "No figure with a year was found in the pasted text.",
    "fin.paste.source": "Source (site or document)",
    "fin.paste.source_placeholder": "e.g. reportaziende.it, page of 18/11/2026",
    "fin.paste.source_needed": "Write where the figures come from.",
    "fin.paste.add_row": "Add a year",
    "fin.paste.remove_row": "Remove the year",
    "fin.paste.save": "Save…",
    "fin.paste.cancel": "Cancel",
    "fin.paste.confirm": "Save the figures of {n} year(s) ({years}) on the card of {name}? Source: {source}.",
    "fin.paste.bad_cell": "Year {year}, {field}: not readable ({text}).",
    "fin.paste.bad_year": "Row {row}: the year is not readable.",
    "fin.paste.nothing": "There is no figure to save.",
    "fin.paste.column.year": "Year",
    "fin.paste.column.revenue": "Revenue",
    "fin.paste.column.profit": "Net profit",
    "fin.paste.column.employees": "Employees",
    "fin.paste.range_tip": "A range such as 10000000–50000000 is kept as a range.",
}


def tr(key: str, **params) -> str:
    text = t(key, **params)
    if text != key:
        return text
    text = TEXTS.get(key, key)
    try:
        return text.format(**params) if params else text
    except (KeyError, IndexError, ValueError):
        return text


class PasteFiguresDialog(QDialog):
    """See the module docstring. `on_save(rows, source, currency, scope) -> ""` or an error
    text; `rows` is a list of {"year": int, "revenue"/"profit"/"employees": Figure | None}.
    `ask(title, text) -> bool` confirms the save (a message box by default)."""

    def __init__(self, parent: QWidget | None, company_name: str,
                 on_save: Callable[[list, str, str, str], str], *,
                 ask: Callable[[str, str], bool] | None = None):
        super().__init__(parent)
        self._name = company_name
        self._on_save = on_save
        self._ask = ask or self._ask_box
        self.currency_found = ""
        self.setWindowTitle(tr("fin.paste.title") + (f" – {company_name}" if company_name else ""))
        self.intro = wrapped_label(tr("fin.paste.intro"), role="muted")
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText(tr("fin.paste.placeholder"))
        self.read_button = QPushButton(tr("fin.paste.read"))
        self.read_button.clicked.connect(lambda: self.read_text())
        self.status = wrapped_label(role="muted")
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setObjectName("finPasteTable")
        table_filter.attach(self.table)
        self.table.setHorizontalHeaderLabels([tr(f"fin.paste.column.{c}") for c in COLUMNS])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setToolTip(tr("fin.paste.range_tip"))
        self.add_button = QPushButton(tr("fin.paste.add_row"))
        self.add_button.clicked.connect(lambda: self.add_row())
        self.remove_button = QPushButton(tr("fin.paste.remove_row"))
        self.remove_button.clicked.connect(lambda: self.remove_row())
        self.source = QLineEdit()
        self.source.setPlaceholderText(tr("fin.paste.source_placeholder"))
        self.currency = QLineEdit("EUR")
        self.currency.setMaxLength(3)
        self.scope = QComboBox()
        for scope in ("separate", "consolidated", "unknown"):
            self.scope.addItem(t(f"fin.scope.{scope}") if t(f"fin.scope.{scope}") != f"fin.scope.{scope}"
                               else scope, scope)
        form = QFormLayout()
        form.addRow(tr("fin.paste.source"), self.source)
        form.addRow(t("fin.dialog.currency") if t("fin.dialog.currency") != "fin.dialog.currency" else "Currency",
                    self.currency)
        form.addRow(t("fin.dialog.scope") if t("fin.dialog.scope") != "fin.dialog.scope" else "Scope", self.scope)
        self.error_label = wrapped_label(role="error")
        self.error_label.hide()
        self.save_button = QPushButton(tr("fin.paste.save"))
        self.save_button.setProperty("role", "accent")
        self.save_button.clicked.connect(lambda: self.save())
        self.cancel_button = QPushButton(tr("fin.paste.cancel"))
        self.cancel_button.clicked.connect(self.reject)
        rows = QHBoxLayout()
        rows.addWidget(self.read_button)
        rows.addStretch(1)
        rows.addWidget(self.add_button)
        rows.addWidget(self.remove_button)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.intro)
        layout.addWidget(self.text, 2)
        layout.addLayout(rows)
        layout.addWidget(self.status)
        layout.addWidget(self.table, 2)
        layout.addLayout(form)
        layout.addWidget(self.error_label)
        layout.addLayout(buttons)
        metrics = self.fontMetrics()
        self.resize(metrics.averageCharWidth() * 90, metrics.height() * 34)

    # --- reading

    def read_text(self, text: str | None = None) -> int:
        """Reads the pasted text into the grid (replacing it); returns the years found."""
        if text is not None:
            self.text.setPlainText(text)
        result = parse_pasted(self.text.toPlainText())
        self.table.setRowCount(0)
        for year in result.years:
            self.add_row(year.year, {name: year.get(name) for name in FIELDS})
        if result.currency:
            self.currency_found = result.currency
            self.currency.setText(result.currency)
        self.status.setText(tr("fin.paste.found", n=len(result.years)) if result.years else tr("fin.paste.none"))
        log.info("Paste figures: %d year(s) read from %d characters", len(result.years),
                 len(self.text.toPlainText()))
        return len(result.years)

    def add_row(self, year: int | None = None, figures: dict | None = None) -> int:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(str(year) if year else ""))
        for column, name in enumerate(FIELDS, start=1):
            figure = (figures or {}).get(name)
            item = QTableWidgetItem(format_figure(figure, whole=name == "employees"))
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, column, item)
        return row

    def remove_row(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def cell(self, row: int, column: int) -> str:
        item = self.table.item(row, column)
        return item.text().strip() if item is not None else ""

    def rows(self) -> list[dict]:
        """The grid read back; raises ValueError with a readable message."""
        out = []
        for row in range(self.table.rowCount()):
            year_text = self.cell(row, 0)
            values = {name: self.cell(row, c) for c, name in enumerate(FIELDS, start=1)}
            if not year_text and not any(values.values()):
                continue
            if not (year_text.isdigit() and 1900 <= int(year_text) <= 2100):
                raise ValueError(tr("fin.paste.bad_year", row=row + 1))
            entry: dict = {"year": int(year_text)}
            for name, text in values.items():
                try:
                    entry[name] = parse_figure(text)
                except ValueError:
                    raise ValueError(tr("fin.paste.bad_cell", year=year_text,
                                        field=tr(f"fin.paste.column.{name}"), text=text)) from None
            if any(isinstance(entry.get(n), Figure) for n in FIELDS):
                out.append(entry)
        return out

    # --- saving

    def _error(self, text: str) -> bool:
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text))
        return False

    def save(self) -> bool:
        try:
            rows = self.rows()
        except ValueError as error:
            return self._error(str(error))
        if not rows:
            return self._error(tr("fin.paste.nothing"))
        source = " ".join(self.source.text().split())
        if not source:
            self.source.setFocus()
            return self._error(tr("fin.paste.source_needed"))
        years = ", ".join(str(r["year"]) for r in sorted(rows, key=lambda r: r["year"]))
        if not self._ask(tr("fin.paste.title"), tr("fin.paste.confirm", n=len(rows), years=years,
                                                   name=self._name, source=source)):
            return False
        error = self._on_save(rows, source, self.currency.text().strip().upper() or "EUR",
                              self.scope.currentData() or "separate")
        if error:
            return self._error(error)
        self._error("")
        self.accept()
        return True

    def _ask_box(self, title: str, text: str) -> bool:
        answer = QMessageBox.question(self, title, text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes
