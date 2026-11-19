"""Interface texts of the regional register, with English fallbacks.

Keys follow the discovery convention ("registers.<area>.<name>"). Until a key is added
to the locale files, `text()` shows the English default below instead of the bare key.
"""

from __future__ import annotations

from ..numbers import format_int, format_number  # noqa: F401 - marks of the interface language
from ..i18n import t

FALLBACK: dict[str, str] = {
    # register as a whole
    "registers.title": "Regional register",
    "registers.discovery_source": "Regional register (official open data)",
    "registers.attribution_line": "Contains data from {sources}.",
    # sources
    "registers.source.rna.title": "RNA – Registro Nazionale Aiuti di Stato (open data)",
    "registers.source.rna.licence": "CC BY 4.0",
    "registers.source.rna.attribution": "Registro Nazionale Aiuti di Stato – Incentivi Italia, open data (CC BY 4.0)",
    "registers.source.rna.note": "Companies that received state aid, de minimis aid, guarantees or tax credits. Region only, sector as a hint, hiring incentives as a signal.",
    "registers.source.gleif.title": "GLEIF golden copy (LEI register)",
    "registers.source.gleif.licence": "CC0 1.0",
    "registers.source.gleif.attribution": "GLEIF Golden Copy, Global Legal Entity Identifier Foundation (CC0)",
    "registers.source.gleif.note": "Companies with an LEI code: legal name, registered address, postcode, legal form, founding date, status.",
    "registers.source.opencoesione.title": "OpenCoesione – soggetti (cohesion funds)",
    "registers.source.opencoesione.licence": "CC BY 4.0",
    "registers.source.opencoesione.attribution": "OpenCoesione – Dipartimento per le politiche di coesione (CC BY 4.0)",
    "registers.source.opencoesione.note": "Recipients of cohesion funds: municipality, address, full 6-digit ATECO code, employee class.",
    "registers.source.agcm.title": "AGCM – companies with a legality rating",
    "registers.source.agcm.licence": "CC BY 4.0",
    "registers.source.agcm.attribution": "Autorità Garante della Concorrenza e del Mercato, rating di legalità (CC BY 4.0)",
    "registers.source.agcm.note": "Companies with a legality rating (stars), municipality and province. Statistical value only: later revocations must be checked on later lists.",
    "registers.source.ri_bought.title": "Registro Imprese (bought by the user)",
    "registers.source.ri_bought.licence": "Bought by the user: personal use only",
    "registers.source.ri_bought.attribution": "Registro Imprese list bought by the user (not redistributed)",
    "registers.source.ri_bought.note": "A list you bought from the Registro Imprese (\"Elenco\" CSV/XLSX), imported into the same register.",
    "registers.source.user.title": "Your edits",
    "registers.source.csv_import.title": "Imported register file",
    "registers.source.derived.title": "Derived (other companies of the same municipality)",
    "registers.update.daily": "updated daily",
    "registers.update.weekly": "updated weekly",
    "registers.update.monthly": "updated monthly",
    "registers.update.twice_yearly": "updated about twice a year",
    "registers.update.manual": "updated when you import a new file",
    # fields and badges
    "registers.field.province_approx": "{province} (presumed, from the tax office code)",
    "registers.field.ateco_indicative": "{code} (indicative: sector declared for an aid)",
    "registers.badge.legality": "Legality rating {stars}",
    "registers.badge.legality_until": "Legality rating {stars}, valid until {date}",
    "registers.badge.legality_revoked": "Legality rating {stars} (revoked or suspended)",
    "registers.badge.aid": "Received public aid ({count}, latest {date})",
    "registers.badge.hired": "Hired with incentives (latest {date})",
    # coverage
    "registers.coverage.known": "The free registers list {count} companies here. Official counts suggest about {low}–{high} active companies ({basis}), so this covers roughly {pct_low}–{pct_high} %. The complete list is the paid Registro Imprese \"Elenco\" (5 € + 0.02–0.12 € per company).",
    "registers.coverage.unknown": "The free registers list {count} companies here. The total number of companies is unknown for this choice; in the study they covered roughly a quarter to a third of the machinery companies of a province. The complete list is the paid Registro Imprese \"Elenco\" (5 € + 0.02–0.12 € per company).",
    # progress
    "registers.progress.listing": "{source}: reading the list of files…",
    "registers.progress.checking": "{source}: checking for updates…",
    "registers.progress.download": "{source}: downloading {file} ({done} of {total} MB)…",
    "registers.progress.download_unknown": "{source}: downloading {file} ({done} MB)…",
    "registers.progress.parse": "{source}: reading {file} ({rows} companies kept)…",
    "registers.progress.merge": "{source}: merging {rows} companies into the register…",
    "registers.progress.done": "{source}: {rows} companies in the register.",
    "registers.result.periods": "Months loaded: {first}–{last} ({n}).",
    "registers.result.skipped": "{n} month(s) not available yet and skipped: {months}.",
    "registers.progress.cancelled": "{source}: stopped. Downloaded parts are kept and resumed next time.",
    # results and errors
    "registers.result.unchanged": "{source}: no new data since the last download.",
    "registers.result.updated": "{source}: {rows} companies updated.",
    "registers.error.network": "{source}: the server could not be reached ({detail}). Try again later.",
    "registers.error.blocked": "{source}: the server asked to slow down. Try again later.",
    "registers.error.checksum": "{source}: the downloaded file {file} is damaged (size or checksum differs) and was discarded.",
    "registers.error.parse": "{source}: the file {file} could not be read ({detail}).",
    "registers.error.not_downloaded": "{source}: nothing downloaded yet.",
    "registers.error.import": "The list could not be imported: {detail}",
    "registers.error.disk": "Not enough disk space or the data folder cannot be written ({detail}).",
    # reminder / maintenance
    "registers.reminder.due": "The regional register was last refreshed {days} days ago. Check for updates?",
    "registers.reminder.never": "The regional register has not been downloaded yet.",
    "registers.disk.usage": "Downloaded files: {raw} MB; register: {db} MB.",
    "registers.disk.purged": "{mb} MB of downloaded files removed. The register itself is kept.",
    # editing
    "registers.edit.locked": "Changed by you: a refresh never overwrites it.",
    "registers.edit.hidden": "Hidden: a refresh will not bring it back.",
    "registers.edit.undone": "Undone: {action}.",
    "registers.edit.nothing_to_undo": "Nothing to undo.",
    "registers.edit.possible_duplicate": "A company with a similar name is already in the register ({name}, {place}). It is not merged automatically: check the tax code.",
}

# Field labels for tables and exports.
FIELD_LABELS: dict[str, str] = {
    "cf": "Tax code (CF)", "name": "Name", "vat": "VAT number", "legal_form": "Legal form",
    "address": "Address", "postcode": "Postcode", "municipality": "Municipality",
    "municipality_istat": "ISTAT municipality code", "province": "Province", "region": "Region",
    "ateco": "ATECO code", "ateco_text": "Activity", "nace_hints": "Sector hints (aids)",
    "size_class": "Size", "employees_class": "Employees", "founded": "Founded", "lei": "LEI",
    "website": "Website", "status": "Status", "legality_rating": "Legality rating",
    "legality_until": "Rating valid until", "legality_state": "Rating state", "lat": "Latitude",
    "lon": "Longitude", "pec": "PEC", "rea": "REA", "note": "Note", "email": "E-mail", "phone": "Phone",
    "aid_count": "Public aids", "last_aid_date": "Latest aid", "aid_measures": "Aid measures",
}


def field_label(name: str) -> str:
    """The translated label of a register field ("registers.field.<name>"), English
    from FIELD_LABELS until the key is in the locale files."""
    key = f"registers.field.{name}"
    value = t(key)
    return value if value != key else FIELD_LABELS.get(name, name)


def text(key: str, **params) -> str:
    value = t(key, **params)
    if value != key:
        return value
    template = FALLBACK.get(key, key)
    try:
        return template.format(**params) if params else template
    except (KeyError, IndexError, ValueError):
        return template


def source_title(source_id: str) -> str:
    return text(f"registers.source.{source_id}.title")


def progress_text(event) -> str:
    """One status line for a RegisterProgress event (download / parse / merge...)."""
    source = source_title(event.source)
    mb = 1024 * 1024
    if event.phase == "download":
        if event.total_bytes:
            return text("registers.progress.download", source=source, file=event.file,
                        done=format_number(event.done_bytes / mb), total=format_number(event.total_bytes / mb))
        return text("registers.progress.download_unknown", source=source, file=event.file,
                    done=format_number(event.done_bytes / mb))
    if event.phase == "parse":
        return text("registers.progress.parse", source=source, file=event.file, rows=format_int(event.rows))
    if event.phase == "merge":
        return text("registers.progress.merge", source=source, rows=format_int(event.rows))
    if event.phase in ("listing", "checking", "done", "cancelled"):
        return text(f"registers.progress.{event.phase}", source=source, rows=format_int(event.rows))
    return source


__all__ = ["FALLBACK", "FIELD_LABELS", "field_label", "progress_text", "source_title", "text"]
