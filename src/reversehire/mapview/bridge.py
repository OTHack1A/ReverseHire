"""The Python side of the QWebChannel: commands to the page, events from the page.

Only QtCore is needed, so this module imports without Qt WebEngine and a test can
call the slots directly (the "fake bridge" of the page).
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal, Slot

log = logging.getLogger("reversehire.mapview")


class MapBridge(QObject):
    """Registered in the channel as "bridge".

    Python -> page: ``command`` carries one JSON document per message (the page parses it
    with JSON.parse; data is never evaluated as code).
    Page -> Python: the slots below; every argument is validated by the receiver.
    """

    command = Signal(str)

    pageReady = Signal()
    markerClicked = Signal(str)
    tileStats = Signal(int, int)  # tiles loaded, tile errors
    pageMessage = Signal(str, str)  # level, text

    @Slot()
    def ready(self) -> None:
        self.pageReady.emit()

    @Slot(str)
    def markerActivated(self, point_id: str) -> None:
        self.markerClicked.emit(str(point_id))

    @Slot(int, int)
    def reportTiles(self, loaded: int, errors: int) -> None:
        self.tileStats.emit(int(loaded), int(errors))

    @Slot(str, str)
    def log(self, level: str, text: str) -> None:
        self.pageMessage.emit(str(level)[:10], str(text)[:500])
