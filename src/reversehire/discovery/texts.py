"""Interface texts of the discovery layer, with English fallbacks.

The locale files are maintained separately: until a key is added there, `text()`
shows the English text below instead of the bare key.
"""

from __future__ import annotations

from ..i18n import t

FALLBACK: dict[str, str] = {
    # sources
    "discovery.source.osm_extract": "Local map data",
    "discovery.source.overpass": "Overpass (OpenStreetMap online)",
    "discovery.source.wikidata": "Wikidata",
    "discovery.source.overture": "Overture Maps places",
    "discovery.source.imports": "Imported lists",
    "discovery.source.regional_register": "Regional register (official lists and your imported lists)",
    "discovery.source.own": "My companies (favourites)",
    # status lines
    "discovery.status.ok": "{label}: {count} companies ({new} new) in {seconds} s",
    "discovery.status.ok_first": "{label}: {count} companies in {seconds} s",
    "discovery.status.empty": "{label}: no companies in the area ({seconds} s)",
    "discovery.status.not_needed": "{label}: skipped (not needed)",
    "discovery.status.skipped": "{label}: skipped ({detail})",
    "discovery.status.unavailable": "{label}: not available ({detail})",
    "discovery.status.failed": "{label}: failed ({detail})",
    "discovery.status.cancelled": "{label}: cancelled",
    "discovery.status.disabled": "{label}: off",
    # pipeline
    "pipeline.source_status": "Company sources: {line}",
    "pipeline.map_index": "Preparing the local map data ({percent}%)…",
    "pipeline.discovery_waiting": "Still waiting for the online company sources: {seconds} s so far. Results from the sources that already answered are kept.",
    "error.discovery_failed": "No company source could answer:\n{details}\nThe search has been saved and can be resumed; you can also add the websites you know by hand.",
    "pipeline.discovery_failed_manual": "No company source could answer: only the {n} websites added by hand are evaluated.",
    # search panel
    "discovery.box_title": "Where the companies come from",
    "discovery.region": "Region:",
    "discovery.area": "Area:",
    "discovery.area.radius": "Radius around the point",
    "discovery.area.province": "Whole province",
    "discovery.area.region": "Whole region",
    "discovery.map_download": "Download / refresh map data",
    "discovery.map_cancel": "Stop download",
    "discovery.map_none": "Map data not downloaded yet (about {size} MB, reused for 30 days).",
    "discovery.map_ready": "Map data: {features} companies and places, {age} days old ({size} MB file).",
    "discovery.map_progress": "Downloading map data: {done} of {total} MB…",
    "discovery.map_indexing": "Preparing map data ({percent}%)…",
    "discovery.map_done": "Map data ready: {features} companies in {seconds} s.",
    "discovery.map_failed": "Map data could not be prepared: {detail}",
    "discovery.ateco_title": "Company categories of interest (ATECO)",
    "discovery.ateco_help": "Proposed from your profile. They are categories to look for, never the code of a specific company (that comes only from a register list you import).",
    "discovery.ateco_search": "Search a category or code…",
    "discovery.ateco_add": "Add",
    "discovery.ateco_remove": "Remove",
    "discovery.import": "Import company list…",
    "discovery.import_title": "Import a company list",
    "discovery.import_kind.register": "Registro Imprese list",
    "discovery.import_kind.startup": "Innovative start-up register",
    "discovery.import_kind.generic": "Generic list",
    "discovery.import_column": "Column for {field}:",
    "discovery.import_none": "(none)",
    "discovery.import_done": "{n} companies imported from {name}.",
    "discovery.import_failed": "The list could not be imported: {detail}",
    "discovery.imports_count": "Imported lists: {lists} ({n} companies).",
    "discovery.status_title": "Last search:",
    # settings
    "settings.discovery": "Company discovery",
    "settings.request_pause": "Pause between requests to the same service (s):",
    "settings.request_pause_error": "The pause must be a number between {min} and {max} seconds.",
    "settings.paid_registry": "Paid company register (optional)",
    "settings.paid_provider": "Provider:",
    "settings.paid_key": "API key:",
    "settings.paid_note": "Used only when you press the paid-register button on a company card and confirm the cost shown. No request is ever made with this key otherwise.",
}

SOURCE_FIELD_LABELS = {
    "name": "name", "site": "website", "address": "address", "town": "town", "province": "province",
    "postcode": "postcode", "cf": "tax code (CF)", "vat": "VAT number", "rea": "REA",
    "ateco": "ATECO code", "ateco_text": "activity", "pec": "PEC", "email": "e-mail", "phone": "phone",
    "legal_form": "legal form", "employees": "employees", "start_date": "start date",
    "lat": "latitude", "lon": "longitude",
}


def text(key: str, **params) -> str:
    value = t(key, **params)
    if value != key:
        return value
    template = FALLBACK.get(key, key)
    try:
        return template.format(**params) if params else template
    except (KeyError, IndexError, ValueError):
        return template


def source_label(name: str) -> str:
    return text(f"discovery.source.{name}")


def status_line(status, first: bool = False) -> str:
    """One line for a SourceStatus (see sources.SourceStatus)."""
    label = source_label(status.name)
    seconds = f"{status.seconds:.1f}"
    detail = status.detail or status.reason or "-"
    if status.state == "ok":
        key = "discovery.status.ok_first" if first or status.new == status.count else "discovery.status.ok"
        return text(key, label=label, count=f"{status.count:,}", new=f"{status.new:,}", seconds=seconds)
    if status.state == "empty":
        return text("discovery.status.empty", label=label, seconds=seconds)
    if status.state == "skipped" and status.reason == "not_needed":
        return text("discovery.status.not_needed", label=label)
    if status.state in ("skipped", "unavailable", "failed", "cancelled", "disabled"):
        return text(f"discovery.status.{status.state}", label=label, detail=detail)
    return f"{label}: {status.state}"
