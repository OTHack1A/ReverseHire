"""`FinancialChart`: revenue and net profit per fiscal year, drawn with QPainter only.

No chart library is used (QtCharts exists in PySide6 but is not relied upon). One scope and
one currency are drawn at a time (see FinancialSeries.view): consolidated and separate
figures are never mixed. Colours come from the widget palette, so the chart follows the
dark and the light theme; a loss is drawn hatched, so it does not depend on colour alone.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPalette, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..numbers import format_int
from .base import TrFunc, default_tr
from .model import FinancialSeries, SeriesView
from .strings import STRINGS

CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}
MINUS = "−"


def _t(tr: TrFunc, key: str, **params) -> str:
    text = tr(key, STRINGS.get(key, key))
    if params:
        try:
            return text.format(**params)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def format_amount(value: float, currency: str = "", tr: TrFunc | None = None) -> str:
    """"2.5 bn €", "850 M €", "12 k €", "-3 M €" (negatives use a real minus sign)."""
    tr = tr or default_tr
    sign = MINUS if value < 0 else ""
    magnitude = abs(value)
    for limit, key in ((1e9, "fin.unit.billion"), (1e6, "fin.unit.million"), (1e3, "fin.unit.thousand")):
        if magnitude >= limit - 0.5 * (limit / 1000):
            scaled, unit = magnitude / limit, _t(tr, key)
            break
    else:
        scaled, unit = magnitude, ""
    if scaled >= 100 or not unit:
        number = f"{scaled:.0f}"
    elif scaled >= 10:
        number = f"{scaled:.1f}"
    else:
        number = f"{scaled:.2f}"
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    if number == "0":
        sign = ""
    try:  # "2,5 Mln €" in Italian and Ukrainian, "2.5 M €" in English
        from ..numbers import decimal_text

        number = decimal_text(number)
    except Exception:  # noqa: BLE001 - the package used outside the app
        pass
    suffix = CURRENCY_SYMBOLS.get(currency, currency)
    return f"{sign}{number}" + (f" {unit}" if unit else "") + (f" {suffix}" if suffix else "")


def nice_step(span: float, target_ticks: int = 4) -> float:
    """A 1/2/5 x 10^n step giving about `target_ticks` intervals over `span`."""
    if span <= 0 or not math.isfinite(span):
        return 1.0
    raw = span / max(1, target_ticks)
    power = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 2.5, 5, 10):
        if raw <= factor * power:
            return factor * power
    return 10 * power


def _ensure_contrast(color: QColor, base: QColor, minimum: int = 80) -> QColor:
    """Moves the lightness of `color` away from `base` until they differ by `minimum` (0..255)."""
    h, s, l, a = color.getHsl()
    base_l = base.lightness()
    if abs(l - base_l) >= minimum:
        return color
    target = base_l - minimum if base_l >= 128 else base_l + minimum
    l = max(0, min(255, target))
    out = QColor.fromHsl(max(h, 0), s, l, a)
    return out


class FinancialChart(QWidget):
    """Bars of revenue and net profit per year, with legend, caption and empty state."""

    def __init__(self, parent=None, tr: TrFunc | None = None):
        super().__init__(parent)
        self._tr: TrFunc = tr or default_tr
        self._series: FinancialSeries | None = None
        self._scope: str | None = None
        self._last = 5
        self._why = ""
        self._notes: list[str] = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(280, 190)
        self.setAutoFillBackground(False)
        self._refresh_texts()

    # --- public API

    def set_tr(self, tr: TrFunc | None) -> None:
        self._tr = tr or default_tr
        self._refresh_texts()
        self.update()

    def set_series(self, series: FinancialSeries | None, scope: str | None = None, *,
                   why_missing: str = "", notes: list[str] | None = None, last: int = 5) -> None:
        """Shows `series` (one scope: `scope`, default the one with most years)."""
        self._series, self._scope, self._why, self._last = series, scope, why_missing or "", last
        self._notes = list(notes or [])
        self._refresh_texts()
        self.update()

    def set_why_missing(self, text: str) -> None:
        self._why = text or ""
        self.update()

    def view(self) -> SeriesView:
        if self._series is None:
            return SeriesView()
        return self._series.view(self._scope, self._last)

    def available_scopes(self) -> list[str]:
        return self._series.scopes() if self._series is not None else []

    def sizeHint(self) -> QSize:
        return QSize(560, 320)

    def minimumSizeHint(self) -> QSize:
        return QSize(280, 190)

    def render_png(self, width: int = 900, height: int = 520) -> bytes:
        """The chart as PNG bytes (opaque background taken from the palette)."""
        width, height = max(200, int(width)), max(140, int(height))
        image = QImage(width, height, QImage.Format.Format_ARGB32)
        image.fill(self._base_color())
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            self._paint(painter, QRect(0, 0, width, height))
        finally:
            painter.end()
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        buffer.close()
        return bytes(data.data())

    # --- texts

    def _refresh_texts(self) -> None:
        view = self.view()
        self.setAccessibleName(_t(self._tr, "fin.chart.accessible"))
        if view.empty:
            self.setToolTip(self._why or _t(self._tr, "fin.chart.empty"))
            return
        rows = []
        for r in view.rows:
            parts = [str(r.year)]
            if r.revenue is not None:
                parts.append(f"{_t(self._tr, 'fin.chart.revenue')} {format_amount(r.revenue, view.currency, self._tr)}")
            if r.profit is not None:
                parts.append(f"{self._profit_label(r.profit)} {format_amount(r.profit, view.currency, self._tr)}")
            if r.employees is not None:
                parts.append(f"{_t(self._tr, 'fin.chart.employees')} {format_int(int(r.employees))}")
            rows.append(": ".join([parts[0], ", ".join(parts[1:])]))
        self.setToolTip("\n".join(rows))

    def _profit_label(self, value: float | None = None) -> str:
        return _t(self._tr, "fin.chart.loss" if (value is not None and value < 0) else "fin.chart.profit")

    def caption_text(self, view: SeriesView | None = None) -> str:
        view = view or self.view()
        if view.empty:
            return ""
        tr = self._tr
        names = []
        for source in view.sources:
            base = source.split(":")[0]
            key = f"fin.source.{base}"
            names.append(_t(tr, key) if key in STRINGS else _t(tr, "fin.source.other", name=source))
        first, last = view.rows[0].year, view.rows[-1].year
        scope = _t(tr, f"fin.scope.{view.scope}")
        key = "fin.caption.main_one" if first == last else "fin.caption.main"
        text = _t(tr, key, scope=scope, sources=", ".join(names) or "-",
                  currency=view.currency or "?", first=first, last=last)
        staff = next((r for r in reversed(view.rows) if r.employees is not None), None)
        if staff is not None:
            text += " - " + _t(tr, "fin.chart.employees_value", value=format_int(int(staff.employees)), year=staff.year)
        if view.conflict_years:
            text += ". " + _t(tr, "fin.caption.conflict", years=", ".join(str(y) for y in view.conflict_years))
        if view.dropped:
            text += ". " + _t(tr, "fin.chart.dropped", n=view.dropped)
        for note in self._notes:
            text += ". " + note.rstrip(".")
        return text

    # --- colours

    def _base_color(self) -> QColor:
        palette = self.palette()
        color = palette.color(QPalette.ColorRole.Base)
        return color if color.alpha() == 255 else palette.color(QPalette.ColorRole.Window)

    def _colors(self) -> dict:
        palette = self.palette()
        base = self._base_color()
        text = palette.color(QPalette.ColorRole.Text)
        revenue = _ensure_contrast(palette.color(QPalette.ColorRole.Highlight), base)
        h, s, l, _a = revenue.getHsl()
        profit = QColor.fromHsl((max(h, 0) + 150) % 360, max(s, 150), l)
        profit = _ensure_contrast(profit, base)
        grid = QColor(text)
        grid.setAlpha(45)
        muted = QColor(text)
        muted.setAlpha(170)
        return {"base": base, "text": text, "muted": muted, "grid": grid,
                "revenue": revenue, "profit": profit}

    # --- painting

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            self._paint(painter, self.rect())
        finally:
            painter.end()

    def _paint(self, painter: QPainter, rect: QRect) -> None:
        colors = self._colors()
        font = QFont(self.font())
        if rect.width() >= 700 and font.pointSizeF() > 0:
            font.setPointSizeF(max(font.pointSizeF(), 10.0))
        painter.setFont(font)
        painter.setPen(colors["text"])
        view = self.view()
        pad = 10
        area = rect.adjusted(pad, pad, -pad, -pad)
        if view.empty:
            self._paint_empty(painter, area, colors)
            return
        fm = painter.fontMetrics()
        # caption (bottom)
        caption = self.caption_text(view)
        cap_rect = fm.boundingRect(QRect(0, 0, area.width(), 10000),
                                   int(Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft), caption)
        cap_h = min(cap_rect.height(), area.height() // 3)
        caption_box = QRect(area.left(), area.bottom() - cap_h + 1, area.width(), cap_h)
        painter.setPen(colors["muted"])
        painter.drawText(caption_box, int(Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft
                                          | Qt.AlignmentFlag.AlignBottom), caption)
        # legend (top)
        legend_h = self._paint_legend(painter, QRect(area.left(), area.top(), area.width(), fm.height() + 4),
                                      view, colors)
        # plot area
        values = [v for r in view.rows for v in (r.revenue, r.profit) if v is not None]
        vmin, vmax = min(values + [0.0]), max(values + [0.0])
        if vmax == vmin:
            vmax = vmin + 1.0
        step = nice_step(vmax - vmin, 6)
        low, high = math.floor(vmin / step) * step, math.ceil(vmax / step) * step
        ticks = []
        value = low
        while value <= high + step * 0.001 and len(ticks) < 20:
            ticks.append(0.0 if abs(value) < step * 1e-9 else value)
            value += step
        labels = [format_amount(t, view.currency, self._tr) for t in ticks]
        label_w = max(fm.horizontalAdvance(s) for s in labels) + 8
        plot = QRect(area.left() + label_w, area.top() + legend_h + fm.height() // 2 + 8,
                     area.width() - label_w,
                     area.height() - legend_h - cap_h - fm.height() - fm.height() // 2 - 20)
        if plot.width() < 40 or plot.height() < 40:
            return
        scale = plot.height() / (high - low)

        def y_of(v: float) -> float:
            return plot.bottom() - (v - low) * scale

        # grid, y labels
        for tick, label in zip(ticks, labels):
            y = y_of(tick)
            painter.setPen(QPen(colors["grid"], 1))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(colors["muted"])
            painter.drawText(QRectF(area.left(), y - fm.height() / 2, label_w - 6, fm.height()),
                             int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), label)
        # zero line
        if low < 0 < high:
            painter.setPen(QPen(colors["text"], 1.2))
            painter.drawLine(QPointF(plot.left(), y_of(0)), QPointF(plot.right(), y_of(0)))
        # bars
        first, last = view.rows[0].year, view.rows[-1].year
        years = list(range(first, last + 1))
        by_year = {r.year: r for r in view.rows}
        slot = plot.width() / len(years)
        bar_w = max(4.0, min(46.0, slot * 0.34))
        gap = max(2.0, bar_w * 0.12)
        small = QFont(font)
        if small.pointSizeF() > 0:
            small.setPointSizeF(small.pointSizeF() * 0.85)
        show_values = slot >= 92
        zero_y = y_of(0)
        for index, year in enumerate(years):
            center = plot.left() + slot * (index + 0.5)
            painter.setFont(font)
            painter.setPen(colors["text"])
            painter.drawText(QRectF(center - slot / 2, plot.bottom() + 4, slot, fm.height()),
                             int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), str(year))
            record = by_year.get(year)
            if record is None:
                continue
            for offset, value, kind in ((-(bar_w + gap) / 2, record.revenue, "revenue"),
                                        ((bar_w + gap) / 2, record.profit, "profit")):
                if value is None:
                    continue
                x = center + offset - bar_w / 2
                top, bottom = sorted((y_of(value), zero_y))
                bar = QRectF(x, top, bar_w, max(1.0, bottom - top))
                color = colors[kind]
                if kind == "profit" and value < 0:
                    brush = QBrush(color, Qt.BrushStyle.BDiagPattern)
                    painter.setBrush(brush)
                    painter.setPen(QPen(color, 1.5))
                else:
                    painter.setBrush(QBrush(color))
                    painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRect(bar)
                if show_values:
                    painter.setFont(small)
                    painter.setPen(colors["muted"])
                    text = format_amount(value, "", self._tr)
                    above = value >= 0
                    label_rect = QRectF(x - 14, (bar.top() - fm.height() - 1) if above else bar.bottom() + 1,
                                        bar_w + 28, fm.height())
                    painter.drawText(label_rect, int(Qt.AlignmentFlag.AlignHCenter
                                                     | (Qt.AlignmentFlag.AlignBottom if above
                                                        else Qt.AlignmentFlag.AlignTop)), text)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_legend(self, painter: QPainter, box: QRect, view: SeriesView, colors: dict) -> int:
        fm = painter.fontMetrics()
        x = box.left()
        size = max(8, fm.height() - 4)
        has_loss = any(r.profit is not None and r.profit < 0 for r in view.rows)
        has_profit = any(r.profit is not None and r.profit >= 0 for r in view.rows)
        items = []
        if any(r.revenue is not None for r in view.rows):
            items.append(("revenue", _t(self._tr, "fin.chart.revenue"), False))
        if has_profit or not has_loss:
            if any(r.profit is not None for r in view.rows):
                items.append(("profit", _t(self._tr, "fin.chart.profit"), False))
        if has_loss:
            items.append(("profit", _t(self._tr, "fin.chart.loss"), True))
        for kind, label, hatched in items:
            swatch = QRectF(x, box.top() + (box.height() - size) / 2, size, size)
            color = colors[kind]
            if hatched:
                painter.setBrush(QBrush(color, Qt.BrushStyle.BDiagPattern))
                painter.setPen(QPen(color, 1.2))
            else:
                painter.setBrush(QBrush(color))
                painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRect(swatch)
            painter.setPen(colors["text"])
            width = fm.horizontalAdvance(label)
            painter.drawText(QRectF(x + size + 5, box.top(), width + 4, box.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), label)
            x += size + 5 + width + 16
        painter.setBrush(Qt.BrushStyle.NoBrush)
        return box.height()

    def _paint_empty(self, painter: QPainter, area: QRect, colors: dict) -> None:
        text = self._why or _t(self._tr, "fin.chart.empty")
        # the frame of the chart: a border, the axis and faint placeholder bars, so the
        # empty state reads as "a chart without figures yet", not as a blank space
        muted = QColor(colors["muted"])
        frame = QColor(muted)
        frame.setAlpha(140)
        painter.save()
        painter.setPen(QPen(frame, 1, Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(area, 6, 6)
        plot = area.adjusted(40, 24, -24, -36)
        painter.setPen(QPen(frame, 1))
        painter.drawLine(plot.bottomLeft(), plot.bottomRight())
        painter.drawLine(plot.bottomLeft(), plot.topLeft())
        ghost = QColor(muted)
        ghost.setAlpha(22)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ghost)
        slots = 5
        step = plot.width() / slots
        for i, share in enumerate((0.35, 0.5, 0.45, 0.65, 0.8)):
            bar_h = int(plot.height() * share)
            x = int(plot.left() + i * step + step * 0.25)
            painter.drawRect(QRect(x, plot.bottom() - bar_h, int(step * 0.5), bar_h))
        flags = int(Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignCenter)
        box = painter.fontMetrics().boundingRect(plot.adjusted(24, 8, -24, -8), flags, text).adjusted(-12, -8, 12, 8)
        painter.setBrush(self._base_color())
        painter.drawRoundedRect(box, 6, 6)
        painter.restore()
        painter.setPen(colors["muted"])
        painter.drawText(plot.adjusted(24, 8, -24, -8), flags, text)
