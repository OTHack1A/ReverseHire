"""Interface texts of the assistant suggestions, as i18n keys with English defaults.

The locale files are maintained separately: until a key is added there, `text()`
returns the English default below instead of the bare key (same rule as
``discovery/texts.py``).
"""

from __future__ import annotations

FALLBACK: dict[str, str] = {
    # GUI entry point and the brief preview
    "suggest.action": "Suggest companies with the assistant",
    "suggest.expand": "Show {n} more",
    "suggest.preview.title": "What will be sent to your assistant",
    "suggest.preview.intro": "{cli} receives only this anonymised summary, the instructions of the request and "
                             "the names of the companies listed below as already known. Your name, contacts, "
                             "address, employers, dates and the CV text are never sent.",
    "suggest.preview.confirm": "Send and ask for companies",
    "suggest.preview.web_search": "The assistant may search the web (setting \"Allow web search\").",
    "suggest.preview.no_web_search": "The assistant answers from its own knowledge only (no web search).",
    # progress
    "suggest.progress.asking": "Asking {cli} for companies that could want your profile…",
    "suggest.progress.parsing": "Reading the answer of {cli}…",
    "suggest.progress.verifying": "Checking suggestion {index} of {total}: {name}…",
    "suggest.progress.done": "{verified} verified, {partial} partly verified, {unverified} not verified, "
                             "{rejected} rejected.",
    # cost honesty
    "suggest.notice.plan": "This request used your own {cli} plan (your subscription or API key), "
                           "not a ReverseHire service.",
    "suggest.notice.tokens": "{cli} reported {input} input and {output} output tokens for this request.",
    "suggest.notice.cost": "{cli} estimated the cost of this request at {cost} USD (on a subscription it "
                           "counts against your usage limits instead).",
    "suggest.notice.no_usage": "{cli} did not report how much of your plan this request used.",
    # errors of the call
    "suggest.error.no_cli": "No assistant is ready: install and sign in to Claude Code or Codex "
                            "(Settings > Assistant).",
    "suggest.error.malformed": "The assistant answered, but not with the expected list of companies. "
                               "Try again; nothing was added.",
    "suggest.error.empty": "The assistant suggested no new companies for this profile and area.",
    "suggest.error.cli": "The assistant could not answer: {message}",
    "suggest.error.cancelled": "Request cancelled.",
    "suggest.error.internal": "Unexpected problem while asking the assistant (details in the log).",
    # statuses of a suggestion
    "suggest.status.verified": "Verified",
    "suggest.status.partial": "Partly verified",
    "suggest.status.unverified": "Not verified",
    "suggest.status.rejected": "Rejected",
    "suggest.status.verified.help": "The website is reachable, carries the company's name and a register "
                                    "or the map confirms the company.",
    "suggest.status.partial.help": "The website is reachable and carries the company's name; no register "
                                   "or map confirmation yet.",
    "suggest.status.unverified.help": "The app could not confirm that this company exists: shown greyed, "
                                      "never added automatically.",
    "suggest.status.rejected.help": "A check contradicted the suggestion (see the reason).",
    # verification reasons (one line each)
    "suggest.reason.site_ok": "Website {site} reachable and it carries the company's name ({where}).",
    "suggest.reason.site_unreachable": "The suggested website {site} could not be read.",
    "suggest.reason.site_not_allowed": "The suggested website {site} is not a public web address.",
    "suggest.reason.site_parked": "The suggested website {site} is a parked or for-sale domain.",
    "suggest.reason.site_other_company": "The website {site} belongs to another company ({other}).",
    "suggest.reason.site_name_missing": "The website {site} does not carry the company's name.",
    "suggest.reason.site_from_map": "Website taken from the map data (OpenStreetMap): {site}.",
    "suggest.reason.site_from_wikidata": "Website taken from Wikidata item {qid}: {site}.",
    "suggest.reason.no_site": "No website given and none found on the map or Wikidata.",
    "suggest.reason.map_match": "Found on the map data as \"{name}\" in {town}.",
    "suggest.reason.register_match": "Listed in the regional register as \"{name}\" in {town}.",
    "suggest.reason.wikidata_match": "Wikidata item {qid} has this name and the same website.",
    "suggest.reason.vies_match": "VAT number {vat} printed on the site is valid in VIES for \"{name}\".",
    "suggest.reason.vies_other": "VAT number {vat} on the site belongs to \"{name}\", not to this company.",
    "suggest.reason.identity": "Identity check: {status}.",
    "suggest.reason.place_ok": "{town} ({province}) is inside the requested area.",
    "suggest.reason.place_outside": "{town} ({province}) is outside the requested area.",
    "suggest.reason.place_unknown": "The place could not be checked against the requested area.",
    "suggest.reason.budget": "Request budget reached: verification stopped early.",
    "suggest.reason.cancelled": "Verification cancelled.",
    "suggest.reason.duplicate": "Already known: {name}.",
    # provenance written on the card
    "suggest.provenance": "Suggested by the assistant ({cli}) on {date}; verified by: {checks}.",
    "suggest.opinion": "Assistant's opinion (not verified): {why}",
    "suggest.tag": "suggested by assistant",
    # company types
    "suggest.type.machine_builder": "Machine builder",
    "suggest.type.system_integrator": "System integrator",
    "suggest.type.manufacturer": "Manufacturer",
    "suggest.type.software_house": "Software / IT",
    "suggest.type.other": "Other",
}


def text(key: str, **params) -> str:
    """Translated text when the locale has the key, else the English default."""
    try:
        from ..i18n import t

        value = t(key, **params)
    except Exception:  # noqa: BLE001 - texts must never fail
        value = key
    if value != key:
        return value
    template = FALLBACK.get(key, key)
    try:
        return template.format(**params) if params else template
    except (KeyError, IndexError, ValueError):
        return template
