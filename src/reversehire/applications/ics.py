"""iCalendar (RFC 5545) text for appointments, so the user can open the .ics file with
Outlook, Google Calendar or any calendar program. The application never talks to a
calendar service: it only writes the file.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Iterable

from .model import Application, Appointment
from .texts import Texts

PRODID = "-//ReverseHire//Applications sent//EN"
UID_DOMAIN = "reversehire.invalid"  # stable, never resolves


def escape_text(value: str) -> str:
    """Escapes a TEXT value: backslash, semicolon, comma and line breaks."""
    return (str(value or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n"))


def fold(line: str) -> str:
    """Folds a content line at 75 octets (continuation lines start with one space)."""
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    parts: list[str] = []
    current = b""
    limit = 75
    for char in line:
        encoded = char.encode("utf-8")
        if len(current) + len(encoded) > limit:
            parts.append(current.decode("utf-8"))
            current, limit = b"", 74  # the leading space counts as one octet
        current += encoded
    parts.append(current.decode("utf-8"))
    return "\r\n ".join(parts)


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _summary(appointment: Appointment, application: Application | None, T: Texts) -> str:
    kind = T(f"applications.appointment.kind.{appointment.kind}")
    who = application.display_title if application else ""
    return f"{kind}: {who}" if who else kind


def _event_lines(appointment: Appointment, application: Application | None, T: Texts,
                 stamp: datetime) -> list[str]:
    start, end = appointment.start_dt, appointment.end_dt
    if start is None or end is None:
        raise ValueError("The appointment has no valid start")
    uid = appointment.uid or f"appointment-{appointment.id}"
    description = []
    if appointment.people:
        description.append(f"{T('applications.appointment.people')}: {appointment.people}")
    if application is not None and application.posting_url:
        description.append(application.posting_url)
    if appointment.notes:
        description.append(appointment.notes)
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}@{UID_DOMAIN}",
        f"DTSTAMP:{_utc(stamp)}",
        f"DTSTART:{_utc(start)}",
        f"DTEND:{_utc(end)}",
        f"SUMMARY:{escape_text(_summary(appointment, application, T))}",
    ]
    if appointment.place:
        lines.append(f"LOCATION:{escape_text(appointment.place)}")
    if description:
        lines.append(f"DESCRIPTION:{escape_text(chr(10).join(description))}")
    if appointment.outcome == "cancelled":
        lines.append("STATUS:CANCELLED")
    if appointment.reminder_min is not None and int(appointment.reminder_min) >= 0:
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY",
                  f"DESCRIPTION:{escape_text(_summary(appointment, application, T))}",
                  f"TRIGGER:-PT{int(appointment.reminder_min)}M", "END:VALARM"]
    lines.append("END:VEVENT")
    return lines


def export_ics(appointments: Iterable[Appointment], applications: dict[int, Application] | None = None,
               *, tr: Callable | None = None, now: datetime | None = None) -> str:
    """One VCALENDAR with a VEVENT per appointment (CRLF line ends, folded lines).

    `applications` maps an application id to its record, to name the event. Appointments
    without a valid start are skipped."""
    T = tr if isinstance(tr, Texts) else Texts(tr)
    stamp = now or datetime.now(timezone.utc)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    for appointment in appointments:
        if appointment.start_dt is None:
            continue
        application = (applications or {}).get(appointment.application_id)
        lines += _event_lines(appointment, application, T, stamp)
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"


def to_ics(appointment: Appointment, application: Application | None = None, *,
           tr: Callable | None = None, now: datetime | None = None) -> str:
    """The iCalendar text of one appointment."""
    applications = {appointment.application_id: application} if application else None
    return export_ics([appointment], applications, tr=tr, now=now)
