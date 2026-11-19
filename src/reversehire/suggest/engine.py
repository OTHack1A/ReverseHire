"""`suggest_companies`: one request (first N, or "Expand" = the next N) to the assistant.

Blocking by design (seconds to minutes): call it on a worker thread. It reports
progress through plain callbacks, honours a cancel event (the CLI process tree is
killed) and a timeout, and never raises: every problem is a `SuggestionBatch` with
`error` set (code + readable message) and logged.

Expand: pass the `offset_token` of the previous batch. The token is a JSON-serialisable
dict: the CLI session id (resumed, so the model sees what it already said) and the
names and website hosts already given; the exclusion list is ALWAYS sent again as well
(resuming alone does not stop a model from repeating itself, and a resumed session may
have expired).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..numbers import format_int, format_number  # noqa: F401 - marks of the interface language
from ..chat import events as ev
from ..chat.session import ChatCallbacks
from .brief import ProfileBrief
from .prompt import DEFAULT_N, MAX_N, Suggestion, build_prompt, host_key, is_known, parse_answer
from .runner import DEFAULT_TIMEOUT_S, SuggestSession, Usage
from .texts import text

log = logging.getLogger("reversehire.suggest")

TOKEN_VERSION = 1
MAX_EXCLUDED_SENT = 300   # names sent in the exclusion list (most recent kept)
POLL_S = 0.1

# SuggestionBatch.error codes
ERR_NO_CLI = "no_cli"
ERR_MALFORMED = "malformed"
ERR_EMPTY = "empty"
ERR_CLI = "cli"
ERR_CANCELLED = "cancelled"
ERR_INTERNAL = "internal"


@dataclass
class SuggestCallbacks:
    on_progress: Callable[[str], None] | None = None     # readable status line
    on_text: Callable[[str], None] | None = None         # raw partial answer (debug views only)


@dataclass
class SuggestionBatch:
    suggestions: list[Suggestion] = field(default_factory=list)  # new, de-duplicated, ranked
    duplicates: list[str] = field(default_factory=list)          # names dropped as already known
    dropped: list[str] = field(default_factory=list)             # parse problems (short reasons)
    more_available: bool | None = None
    offset_token: dict = field(default_factory=dict)             # pass back for "Expand"
    usage: Usage = field(default_factory=Usage)
    notices: list[str] = field(default_factory=list)             # cost / usage honesty lines
    error: str = ""                                              # "" = success
    message: str = ""                                            # readable error
    detail: str = ""                                             # CLI detail (may be shown, not logged)
    cli: str = ""
    web_search: bool = False
    prompt_chars: int = 0

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict:
        return {"suggestions": [s.to_dict() for s in self.suggestions], "duplicates": list(self.duplicates),
                "dropped": list(self.dropped), "more_available": self.more_available,
                "offset_token": dict(self.offset_token), "usage": self.usage.to_dict(),
                "notices": list(self.notices), "error": self.error, "message": self.message, "cli": self.cli,
                "web_search": self.web_search}


def usage_notices(usage: Usage, cli_name: str) -> list[str]:
    """The per-call honesty lines: whose plan was used and what the CLI reported."""
    lines = [text("suggest.notice.plan", cli=cli_name)]
    if usage.input_tokens is not None or usage.output_tokens is not None:
        lines.append(text("suggest.notice.tokens", cli=cli_name, input=format_int(usage.input_tokens or 0),
                          output=format_int(usage.output_tokens or 0)))
    if usage.cost_usd is not None:
        lines.append(text("suggest.notice.cost", cli=cli_name, cost=f"{usage.cost_usd:.4f}"))
    if usage.input_tokens is None and usage.output_tokens is None and usage.cost_usd is None:
        lines.append(text("suggest.notice.no_usage", cli=cli_name))
    return lines


def _token_names(token: dict | None) -> tuple[list[str], set[str], str | None, int]:
    token = token if isinstance(token, dict) else {}
    names = [str(x) for x in token.get("given") or [] if str(x or "").strip()]
    hosts = {str(x) for x in token.get("hosts") or [] if x}
    session = token.get("session_id")
    return names, hosts, str(session) if session else None, int(token.get("round") or 0)


def _safe_call(callback, value) -> None:
    if callback is None:
        return
    try:
        callback(value)
    except Exception:  # noqa: BLE001
        log.exception("Suggest: a callback raised an exception")


def suggest_companies(brief: ProfileBrief, *, cli, n: int = DEFAULT_N, exclude: list[str] | tuple = (),
                      offset_token: dict | None = None, language: str = "en",
                      callbacks: SuggestCallbacks | None = None, timeout: float = DEFAULT_TIMEOUT_S,
                      data_dir: str | Path | None = None, allow_web_search: bool = False,
                      model: str | None = None, cancel: threading.Event | None = None,
                      resume: bool = True, popen=None) -> SuggestionBatch:
    """Asks the assistant CLI `cli` (a ``chat.CliInfo``) for the next `n` companies.

    `exclude`: names already known (the user's cards, the current results); companies
    named in earlier batches come from `offset_token`. Returns a SuggestionBatch."""
    callbacks = callbacks or SuggestCallbacks()
    batch = SuggestionBatch(web_search=bool(allow_web_search))
    try:
        return _suggest(brief, cli, n, list(exclude or ()), offset_token, language, callbacks, timeout,
                        data_dir, allow_web_search, model, cancel, resume, popen, batch)
    except Exception as exc:  # noqa: BLE001 - never into the caller
        log.exception("Suggest: unexpected error")
        batch.error, batch.message, batch.detail = ERR_INTERNAL, text("suggest.error.internal"), str(exc)
        return batch


def _suggest(brief, cli, n, exclude, offset_token, language, callbacks, timeout, data_dir, allow_web_search,
             model, cancel, resume, popen, batch: SuggestionBatch) -> SuggestionBatch:
    if cli is None or not getattr(cli, "usable", False):
        batch.error, batch.message = ERR_NO_CLI, text("suggest.error.no_cli")
        return batch
    batch.cli = cli.kind
    cli_name = cli.name
    if data_dir is None:
        from .. import config

        data_dir = config.data_folder()
    given, hosts, session_id, round_index = _token_names(offset_token)
    n = max(1, min(MAX_N, int(n or DEFAULT_N)))
    known_names = list(dict.fromkeys([*exclude, *given]))
    sent_names = known_names[-MAX_EXCLUDED_SENT:]
    question = build_prompt(brief.preview_text(), brief.direction_ids, n=n, exclude=sent_names,
                            language=language, web_search=allow_web_search, round_index=round_index)
    batch.prompt_chars = len(question)

    finished = threading.Event()
    outcome: dict = {}

    def on_done(answer: ev.ChatAnswer) -> None:
        outcome["answer"] = answer
        finished.set()

    def on_error(error: ev.ChatError) -> None:
        outcome["error"] = error
        finished.set()

    session_kwargs = {"popen": popen} if popen is not None else {}
    session = SuggestSession(cli, data_dir=data_dir, language=language, timeout_s=timeout, model=model,
                             allow_web_search=allow_web_search,
                             resume_session=session_id if resume else None,
                             callbacks=ChatCallbacks(on_text=callbacks.on_text, on_done=on_done,
                                                     on_error=on_error),
                             **session_kwargs)
    _safe_call(callbacks.on_progress, text("suggest.progress.asking", cli=cli_name))
    started = time.monotonic()
    session.send(question)
    while not finished.wait(POLL_S):
        if cancel is not None and cancel.is_set():
            session.cancel()
            break
    session.wait(timeout + 60)
    finished.wait(5)
    parser = session.last_parser
    duration = time.monotonic() - started
    batch.usage = parser.usage(duration) if parser is not None and hasattr(parser, "usage") else Usage(
        cli=cli.kind, duration_s=duration)
    batch.notices = usage_notices(batch.usage, cli_name)
    new_session = session.session_id or session_id
    error: ev.ChatError | None = outcome.get("error")
    answer: ev.ChatAnswer | None = outcome.get("answer")
    if cancel is not None and cancel.is_set() and answer is None:
        batch.error, batch.message = ERR_CANCELLED, text("suggest.error.cancelled")
        batch.offset_token = _token(given, hosts, new_session, round_index)
        return batch
    if error is not None or answer is None:
        if error is not None and error.code == ev.ERR_CANCELLED:
            batch.error, batch.message = ERR_CANCELLED, text("suggest.error.cancelled")
        else:
            batch.error = ERR_CLI
            batch.message = text("suggest.error.cli", message=error.message if error is not None else "?")
            batch.detail = error.detail if error is not None else ""
        # the CLI's own words (an error text, never the brief or the answer) help the next diagnosis
        log.info("Suggestion request failed: cli=%s code=%s detail=%s", cli.kind, error.code if error else "none",
                 " ".join(str(getattr(error, "detail", "") or "").split())[:160])
        batch.offset_token = _token(given, hosts, new_session, round_index)
        return batch
    _safe_call(callbacks.on_progress, text("suggest.progress.parsing", cli=cli_name))
    country = (brief.area.country or "IT").upper()
    parsed = parse_answer(answer.text, direction_ids=brief.direction_ids, default_country=country)
    batch.dropped = list(parsed.dropped)
    batch.more_available = parsed.more_available
    if not parsed.found_json or (parsed.raw_items and not parsed.suggestions):
        batch.error, batch.message = ERR_MALFORMED, text("suggest.error.malformed")
        log.info("Suggestion answer not usable: json=%s items=%d (answer of %d characters)", parsed.found_json,
                 parsed.raw_items, len(answer.text))
        batch.offset_token = _token(given, hosts, new_session, round_index)
        return batch
    seen_names = list(known_names)
    seen_hosts = set(hosts)
    position = len(given)
    for suggestion in parsed.suggestions:
        duplicate = is_known(suggestion, seen_names, seen_hosts)
        if duplicate:
            batch.duplicates.append(suggestion.name)
            continue
        position += 1
        suggestion.rank = position
        batch.suggestions.append(suggestion)
        seen_names.append(suggestion.name)
        if suggestion.website:
            seen_hosts.add(host_key(suggestion.website))
        if len(batch.suggestions) >= n:
            break
    if not batch.suggestions:
        batch.error, batch.message = ERR_EMPTY, text("suggest.error.empty")
    names = given + [s.name for s in batch.suggestions]
    batch.offset_token = _token(names, seen_hosts, new_session, round_index + 1)
    log.info("Suggestion request done: cli=%s new=%d duplicates=%d dropped=%d in %.1f s", cli.kind,
             len(batch.suggestions), len(batch.duplicates), len(batch.dropped), duration)
    return batch


def _token(names: list[str], hosts: set[str], session_id: str | None, round_index: int) -> dict:
    return {"version": TOKEN_VERSION, "session_id": session_id or None, "given": list(names),
            "hosts": sorted(h for h in hosts if h), "round": int(round_index)}
