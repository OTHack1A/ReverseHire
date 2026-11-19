"""The one progress bar of the status bar, shared by its producers.

The CV analysis and the search can run at the same time: each producer shows
its state under its own owner name and the bar shows the producer that started
last; when it finishes, the other one is shown again. The states come from the
workers through the main window's queue (never from a worker thread directly).
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtWidgets import QProgressBar

SCALE = 1000  # the bar runs 0..SCALE: fractions, so that a growing total never moves it back


@dataclass
class BarState:
    """What the bar shows: `fraction` 0..1 or None (busy), its text and tooltip."""

    fraction: float | None
    text: str = ""
    tooltip: str = ""


class StatusProgress:
    def __init__(self, bar: QProgressBar):
        self.bar = bar
        self._owners: dict[str, BarState] = {}
        self._last: BarState | None = None
        self.idle()

    def show(self, owner: str, state: BarState) -> None:
        """Shows (or updates) the state of `owner`; a new owner comes on top, an
        existing one keeps its place."""
        self._owners[owner] = state
        self._render()

    def finish(self, owner: str, final: BarState | None = None) -> None:
        """`owner` is done. With no other producer the bar keeps `final` (e.g. "15/15")
        or goes back to idle."""
        self._owners.pop(owner, None)
        if self._owners:
            self._render()
        elif final is not None:
            self._apply(final)
        else:
            self.idle()

    def active(self) -> str | None:
        return next(reversed(self._owners), None) if self._owners else None

    def state(self, owner: str) -> BarState | None:
        return self._owners.get(owner)

    def idle(self) -> None:
        self._last = None
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setToolTip("")

    def _render(self) -> None:
        owner = self.active()
        if owner is not None:
            self._apply(self._owners[owner])

    def _apply(self, state: BarState) -> None:
        self._last = state
        if state.fraction is None:
            self.bar.setRange(0, 0)  # busy until a total is known
        else:
            self.bar.setRange(0, SCALE)
            self.bar.setValue(round(max(0.0, min(1.0, state.fraction)) * SCALE))
        # QProgressBar replaces only %p, %v and %m in the format (it has no %% escape):
        # a "%" followed by a space, as in "42 %", is shown as it is.
        self.bar.setFormat(state.text)
        self.bar.setTextVisible(bool(state.text) and state.fraction is not None)
        self.bar.setToolTip(state.tooltip)

    def text(self) -> str:
        """The text on the bar ("" when hidden)."""
        return self._last.text if self._last is not None else ""
