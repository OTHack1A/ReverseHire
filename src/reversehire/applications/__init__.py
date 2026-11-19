"""Tracker of the CVs already sent ("Applications sent").

Logic (no Qt): ``ApplicationService`` with ``model`` records, the content-hash ``archive``
of the files sent, ``ics`` for calendar files. Widgets (import lazily, they need Qt):
``ApplicationsPanel``, ``ApplicationDialog``, ``UpcomingAppointments``, ``PdfPreview``.
Data lives in ``<data folder>/applications/applications.sqlite`` and ``.../files/``.
"""

from .ics import export_ics, to_ics
from .model import (ANSWERED_STATUSES, APPOINTMENT_KINDS, CHANNELS, FINAL_STATUSES, KINDS, OPEN_STATUSES,
                    OUTCOMES, SOURCES, STATUSES, Application, Appointment, Attachment, FollowUp, HistoryEntry,
                    ImportReport, Statistics)
from .service import DEFAULT_FOLLOW_UP_DAYS, DUPLICATE_WINDOW_DAYS, ApplicationService, default_folder
from .texts import EN as TEXTS_EN
from .texts import Texts

__all__ = [
    "ANSWERED_STATUSES", "APPOINTMENT_KINDS", "CHANNELS", "FINAL_STATUSES", "KINDS", "OPEN_STATUSES", "OUTCOMES",
    "SOURCES", "STATUSES", "Application", "Appointment", "Attachment", "FollowUp", "HistoryEntry", "ImportReport",
    "Statistics", "ApplicationService", "DEFAULT_FOLLOW_UP_DAYS", "DUPLICATE_WINDOW_DAYS", "default_folder",
    "export_ics", "to_ics", "TEXTS_EN", "Texts", "ApplicationsPanel", "ApplicationDialog",
    "UpcomingAppointments", "PdfPreview",
]


def __getattr__(name: str):
    """Qt widgets are imported on first use, so the logic stays usable without Qt."""
    if name == "ApplicationsPanel":
        from .panel import ApplicationsPanel

        return ApplicationsPanel
    if name == "ApplicationDialog":
        from .dialog import ApplicationDialog

        return ApplicationDialog
    if name == "UpcomingAppointments":
        from .detail_widgets import UpcomingAppointments

        return UpcomingAppointments
    if name == "PdfPreview":
        from .pdf_preview import PdfPreview

        return PdfPreview
    raise AttributeError(name)
