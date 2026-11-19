"""Events delivered by a ChatSession to its callbacks (plain data, no Qt)."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import messages

# ChatError.code values
ERR_BUSY = "busy"
ERR_EMPTY = "empty"
ERR_START_FAILED = "start_failed"
ERR_TIMEOUT = "timeout"
ERR_CANCELLED = "cancelled"
ERR_NOT_LOGGED_IN = "not_logged_in"
ERR_CLI_FAILED = "cli_failed"
ERR_CLI_REPORTED = "cli_reported"
ERR_USAGE_LIMIT = "usage_limit"            # the user's plan reached its usage limit (reset time known)
ERR_USAGE_LIMIT_NO_TIME = "usage_limit_no_time"
ERR_NO_ANSWER = "no_answer"
ERR_UNSAFE = "unsafe"
ERR_UNSAFE_ARGS = "unsafe_args"
ERR_INTERNAL = "internal"

# Notice.code values
NOTICE_MCP_FAILED = "mcp_failed"
NOTICE_RETRYING = "retrying"


@dataclass(frozen=True)
class ToolNotice:
    """The assistant called a tool (``phase`` "started", then "finished")."""

    tool: str                 # e.g. "find_companies"
    server: str               # "reversehire" (anything else is refused)
    phase: str = "started"    # "started" | "finished"
    is_error: bool = False    # only meaningful when phase == "finished"
    call_id: str = ""


@dataclass(frozen=True)
class Notice:
    """Non-fatal information during a turn (MCP server failed, API retry)."""

    code: str
    key: str
    params: dict = field(default_factory=dict)

    @property
    def message(self) -> str:
        return messages.text(self.key, **self.params)


@dataclass(frozen=True)
class ChatAnswer:
    """Final answer of a turn. ``text`` is authoritative (chunks may be partial)."""

    text: str
    cli: str
    session_id: str | None
    duration_s: float
    tools: tuple[str, ...] = ()
    exit_code: int | None = None


@dataclass(frozen=True)
class ChatError:
    """A turn that ended without an answer. ``message`` is readable and translated;
    ``detail`` is extra text from the CLI (may be shown, is never logged)."""

    code: str
    key: str
    params: dict = field(default_factory=dict)
    detail: str = ""
    cli: str = ""
    duration_s: float = 0.0
    exit_code: int | None = None

    @property
    def message(self) -> str:
        return messages.text(self.key, **self.params)


def make_error(error_code: str, cli: str, /, *, detail: str = "", exit_code: int | None = None,
               duration_s: float = 0.0, **params) -> ChatError:
    code = error_code
    name = messages.CLI_NAMES.get(cli, cli)
    base = {"cli": name, "login_command": messages.LOGIN_COMMANDS.get(cli, cli)}
    base.update(params)
    if "detail" not in base:
        base["detail"] = _short(detail)
    return ChatError(code=code, key=f"chat.error.{code}", params=base, detail=detail, cli=cli,
                     duration_s=duration_s, exit_code=exit_code)


def make_notice(code: str, cli: str, **params) -> Notice:
    base = {"cli": messages.CLI_NAMES.get(cli, cli)}
    base.update(params)
    return Notice(code=code, key=f"chat.notice.{code}", params=base)


def _short(text: str, limit: int = 300) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
