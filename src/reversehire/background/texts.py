"""Interface texts of the background analysis, as i18n keys with English defaults
(`text()` returns the English default until the locale files have the key)."""

from __future__ import annotations

FALLBACK: dict[str, str] = {
    "background.title": "Analysis while the computer is idle",
    "background.switch": "Analyse companies when the computer is idle",
    "background.pause": "Pause",
    "background.resume": "Resume",
    # states
    "background.state.idle": "Waiting",
    "background.state.running": "Analysing {name}",
    "background.state.paused": "Paused",
    "background.state.stopped": "Off",
    # why it waits / pauses
    "background.reason.running": "The computer is idle: analysing in the background.",
    "background.reason.user_active": "You are using the computer.",
    "background.reason.not_idle": "Waiting until the computer has been idle for {minutes} minutes.",
    "background.reason.idle_unknown": "Idle time cannot be read on this system: waiting until the app "
                                      "has been in the background for {minutes} minutes.",
    "background.reason.on_battery": "Running on battery.",
    "background.reason.cpu_busy": "The computer is busy (CPU {percent}%).",
    "background.reason.offline": "No network connection.",
    "background.reason.foreground": "A search is running in the app.",
    "background.reason.paused_by_user": "Paused by you.",
    "background.reason.budget_spent": "Today's request budget is used up ({used} of {budget}).",
    "background.reason.queue_empty": "Nothing left to analyse.",
    "background.reason.waiting_retry": "Waiting to retry after an error.",
    "background.reason.host_limit": "The remaining companies' sites reached today's per-site limit.",
    "background.reason.stopped": "Background analysis is off.",
    # counters
    "background.counts": "{done} analysed, {queued} in the queue, {failed} failed; {requests} requests today "
                         "of {budget}.",
    "background.next": "Next: {name}",
    # pre-screen reasons
    "background.screen.direction": "fits {direction}",
    "background.screen.direction_guess": "maybe {direction} (to be confirmed when the site is read)",
    "background.screen.type": "type: {type}",
    "background.screen.terms": "matches: {terms}",
    "background.screen.rank.industrial": "production site on the map",
    "background.screen.rank.website": "has a website",
    "background.screen.rank.name": "in the name: {what}",
    "background.screen.rank.map_name": "map and name match your sectors",
    "background.screen.rank.name_cv": "the name recalls the CV: {what}",
    "background.screen.rank.name_ateco": "the name recalls your ATECO categories: {what}",
    "background.screen.rank.wikidata": "well-known company (Wikidata)",
    "background.screen.rank.employees": "{what} employees",
    "background.screen.rank.ateco": "ATECO {what} among your categories",
    "background.screen.type_excluded": "type excluded in your preferences (kept, ordered last)",
    "background.screen.no_signal": "no specific signal",
}


def text(key: str, **params) -> str:
    try:
        from ..i18n import t

        value = t(key, **params)
    except Exception:  # noqa: BLE001
        value = key
    if value != key:
        return value
    template = FALLBACK.get(key, key)
    try:
        return template.format(**params) if params else template
    except (KeyError, IndexError, ValueError):
        return template
