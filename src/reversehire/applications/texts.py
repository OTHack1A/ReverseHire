"""Interface texts of the applications package: i18n keys with their English text.

Every widget gets its texts through ``Texts(tr)``: ``tr(key, default)`` is any callable
(the integrator passes one backed by ``reversehire.i18n``); the English text below is the
default, used until the key is added to locales/en.json, it.json and uk.json (same keys,
same ``{placeholders}``). ``default_tr`` is the fallback when no callable is given.
"""

from __future__ import annotations

from typing import Callable

EN: dict[str, str] = {
    # --- tab and columns ---------------------------------------------------------------
    "applications.tab.title": "Applications sent",
    "applications.column.company": "Company",
    "applications.column.role": "Role",
    "applications.column.kind": "Type",
    "applications.column.sent": "Sent",
    "applications.column.status": "Status",
    "applications.column.appointment": "Next appointment",
    "applications.column.follow_up": "Follow-up",
    "applications.follow_up.due": "Due ({days} days)",
    "applications.empty": "No applications recorded yet. Use \"New application\" to add the first one.",
    "applications.summary": "{total} recorded, {sent} sent, {answered} answered, {interviews} with interviews {median}",
    "applications.summary.median": "(median answer time: {days} days)",
    "applications.filter.search": "Exact words: company, role, contact…",
    "applications.filter.search_tip": "Finds the applications that contain the words you type (exact words, accents "
                                      "and case ignored); it does not search by meaning.",
    "applications.filter.all_statuses": "All statuses",
    "applications.filter.all_kinds": "All types",
    # --- detail tabs ---------------------------------------------------------------------
    "applications.tab.details": "Details",
    "applications.tab.letter": "Cover letter",
    "applications.tab.cv": "CV sent",
    "applications.tab.appointments": "Appointments",
    "applications.tab.history": "History",
    # --- enumerations ----------------------------------------------------------------------
    "applications.kind.posting": "Reply to a posting",
    "applications.kind.spontaneous": "Spontaneous application",
    "applications.channel.email": "E-mail",
    "applications.channel.site": "Company site / form",
    "applications.channel.agency": "Agency",
    "applications.channel.linkedin": "LinkedIn",
    "applications.channel.other": "Other",
    "applications.status.draft": "Draft",
    "applications.status.sent": "Sent",
    "applications.status.acknowledged": "Acknowledged",
    "applications.status.interview": "Interview",
    "applications.status.offer": "Offer",
    "applications.status.rejected": "Rejected",
    "applications.status.no_answer": "No answer",
    "applications.status.withdrawn": "Withdrawn",
    "applications.source.manual": "Entered by hand",
    "applications.source.mail_draft": "From an e-mail draft",
    "applications.source.export": "From an exported application",
    "applications.who.user": "You",
    "applications.who.system": "Program",
    "applications.who.mail": "E-mail draft",
    "applications.who.import": "Import",
    # --- fields -----------------------------------------------------------------------------
    "applications.field.company": "Company",
    "applications.field.role": "Role / posting title",
    "applications.field.kind": "Type",
    "applications.field.posting_url": "Posting link",
    "applications.field.channel": "Sent through",
    "applications.field.recipient": "Recipient e-mail",
    "applications.field.contact": "Contact person",
    "applications.field.sent_date": "Date sent",
    "applications.field.status": "Status",
    "applications.field.follow_up": "Follow up on",
    "applications.field.follow_up_date": "Follow-up date",
    "applications.field.cv_language": "CV language",
    "applications.field.notes": "Notes",
    "applications.field.source": "Record",
    "applications.field.subject": "Subject",
    "applications.field.cv_hash": "CV file",
    "applications.field.cover_letter": "Cover letter",
    "applications.field.clear_date": "Clear the date",
    # --- actions -----------------------------------------------------------------------------
    "applications.action.new": "New application...",
    "applications.action.open": "Open",
    "applications.action.mark_sent": "Mark as sent",
    "applications.action.change_status": "Change status",
    "applications.action.add_appointment": "Add appointment",
    "applications.action.calendar": "Add to my calendar (.ics)...",
    "applications.action.open_company": "Open company card",
    "applications.action.open_posting": "Open link",
    "applications.action.open_cv": "Open the CV sent",
    "applications.action.export": "Export this application...",
    "applications.action.export_csv": "Export all to CSV...",
    "applications.action.import_csv": "Import from CSV...",
    "applications.action.undo": "Undo last change",
    "applications.action.delete": "Delete...",
    # --- cover letter ------------------------------------------------------------------------
    "applications.letter.edit": "Edit",
    "applications.letter.copy": "Copy",
    "applications.letter.as_sent": "The text exactly as it was sent. Press Edit to change it.",
    # --- CV sent -----------------------------------------------------------------------------
    "applications.cv.none": "No CV file is archived for this application.",
    "applications.cv.info": "{name} (archived copy {hash})",
    "applications.cv.replace": "Attach or replace PDF...",
    "applications.cv.replace_confirm": "This application was already sent. Replace the archived CV with another file?",
    "applications.cv.choose": "Choose the CV PDF",
    "applications.cv.filter": "PDF (*.pdf *.PDF);;All files (*)",
    "applications.pdf.none": "No preview.",
    "applications.pdf.loading": "Loading the preview...",
    "applications.pdf.missing": "The archived file was not found.",
    "applications.pdf.unreadable": "The PDF cannot be shown.",
    "applications.pdf.previous": "Previous page",
    "applications.pdf.next": "Next page",
    "applications.pdf.page": "Page {page} of {count}",
    "applications.pdf.open_external": "Open externally",
    # --- appointments -----------------------------------------------------------------------
    "applications.appointment.add": "Add",
    "applications.appointment.delete": "Delete",
    "applications.appointment.delete_confirm": "Delete this appointment?",
    "applications.appointment.empty": "No appointment selected. Press Add to record a call, interview or test.",
    "applications.appointment.ics": "Add to my calendar (.ics)",
    "applications.appointment.ics_title": "Save the calendar file",
    "applications.appointment.ics_filter": "Calendar file (*.ics)",
    "applications.appointment.kind": "Type",
    "applications.appointment.kind.phone": "Phone call",
    "applications.appointment.kind.video": "Video call",
    "applications.appointment.kind.onsite": "On-site interview",
    "applications.appointment.kind.test": "Technical test",
    "applications.appointment.kind.other": "Other",
    "applications.appointment.start": "Start",
    "applications.appointment.duration": "Duration",
    "applications.appointment.minutes": "min",
    "applications.appointment.place": "Place or link",
    "applications.appointment.people": "People met",
    "applications.appointment.notes": "Notes",
    "applications.appointment.outcome": "Outcome",
    "applications.appointment.reminder": "Reminder",
    "applications.appointment.no_reminder": "none",
    "applications.appointment.minutes_before": "min before",
    "applications.outcome.pending": "Pending",
    "applications.outcome.positive": "Positive",
    "applications.outcome.neutral": "Neutral",
    "applications.outcome.negative": "Negative",
    "applications.outcome.cancelled": "Cancelled",
    "applications.upcoming.none": "No upcoming appointments.",
    # --- history ----------------------------------------------------------------------------
    "applications.history.when": "When",
    "applications.history.who": "Who",
    "applications.history.what": "What",
    "applications.history.change": "Change",
    # --- delete, export, CSV ----------------------------------------------------------------
    "applications.delete.title": "Delete application",
    "applications.delete.confirm": (
        "Delete the application \"{name}\" with its appointments and history? The archived CV and "
        "attachments are removed too if no other record uses them. This cannot be undone."),
    "applications.export.choose_folder": "Choose the folder to export into",
    "applications.csv.export_title": "Export the applications to CSV",
    "applications.csv.import_title": "Import applications from CSV",
    "applications.csv.filter": "CSV files (*.csv);;All files (*)",
    # --- notices ----------------------------------------------------------------------------
    "applications.notice.created": "Application recorded: {name}",
    "applications.notice.deleted": "Application deleted ({files} archived files removed).",
    "applications.notice.undone": "Last change undone.",
    "applications.notice.cv_replaced": "The archived CV was replaced.",
    "applications.notice.exported": "Exported to {path}",
    "applications.notice.csv_saved": "CSV saved: {path}",
    "applications.notice.csv_imported": "CSV imported: {created} added, {skipped} skipped.",
    "applications.notice.ics_saved": "Calendar file saved: {path}. Open it to add the appointment to your calendar.",
    "applications.error.busy": "The applications file is busy: try again in a moment.",
    "applications.error.invalid": "Not saved: {detail}",
    "applications.error.generic": "The applications tracker could not complete the action ({detail}).",
    # --- new application dialog ---------------------------------------------------------------
    "applications.dialog.title": "New application",
    "applications.dialog.company_hint": "Pick a company or type a new name",
    "applications.dialog.company_required": "Enter the company name.",
    "applications.dialog.cv_pdf": "CV PDF sent",
    "applications.dialog.cv_missing": "The CV file was not found.",
    "applications.dialog.browse": "Browse...",
    "applications.dialog.choose_cv": "Choose the CV PDF",
    "applications.dialog.letter": "Cover letter",
    "applications.dialog.letter_hint": "Paste the cover letter exactly as it was sent",
    "applications.dialog.load_letter": "Load letter from a text file...",
    "applications.dialog.choose_letter": "Choose the cover letter file",
    "applications.dialog.letter_filter": "Text files (*.txt *.md);;All files (*)",
    "applications.dialog.letter_unreadable": "The letter file could not be read.",
    "applications.dialog.duplicate": (
        "There are already {count} application(s) to {name} for the same role in the last 60 days. "
        "Record this one anyway?"),
    "applications.dialog.save": "Save",
    "applications.dialog.cancel": "Cancel",
}


def default_tr(key: str, default: str) -> str:
    """The application's translation of `key` when it has one, else `default`."""
    try:
        from ..i18n import t

        text = t(key)
        return default if text == key else text
    except Exception:  # noqa: BLE001
        return default


class Texts:
    """``T(key, **params)``: the translated text of a key, formatted with the parameters."""

    def __init__(self, tr: Callable[[str, str], str] | None = None):
        self._tr = tr or default_tr

    def __call__(self, key: str, **params) -> str:
        default = EN.get(key, key)
        try:
            text = self._tr(key, default)
        except Exception:  # noqa: BLE001 - a broken translation callable never breaks the widget
            text = default
        if params:
            try:
                return text.format(**params)
            except (KeyError, IndexError, ValueError):
                try:
                    return default.format(**params)
                except (KeyError, IndexError, ValueError):
                    return default
        return text
