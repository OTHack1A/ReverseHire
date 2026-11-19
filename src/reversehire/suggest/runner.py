"""Runs one suggestion request through the user's own assistant CLI, locked down.

The command lines are DERIVED from the chat's (``chat.commands.build_claude_command`` /
``build_codex_command``), so every safety rule of the chat is inherited (restricted
mode, ignored user settings and hooks, no built-in tools, permission mode dontAsk,
read-only sandbox, no slash commands, prompt on stdin, nothing secret on the command
line); only these values are changed, and the build fails closed when an expected
flag is missing:

Claude Code
  * ``--mcp-config`` points to a file with NO server (``{"mcpServers": {}}``) and
    ``--strict-mcp-config`` stays: the suggestion needs no ReverseHire tool;
  * ``--append-system-prompt``: the suggestion instructions (``prompt.preamble``);
  * ``--max-turns``: 2 (no tools), 8 with web search;
  * web search opt-in only: ``--tools WebSearch --allowedTools WebSearch``
    (WebSearch runs on Anthropic's side and returns search results; it cannot read or
    write local files or run commands; WebFetch stays off).

Codex
  * the ``-c mcp_servers.reversehire.*`` overrides are removed (no MCP server);
  * web search opt-in only: ``web_search='live'`` instead of ``'disabled'``; the
    sandbox stays read-only, approval ``never``, shell/browser/plugin features off.

The stream parsers are the chat's, with two differences: the "no ReverseHire MCP
server" notice is dropped (expected here), and when web search is allowed the web
search tool/item is accepted instead of stopping the run. Any other tool still stops
the run (``ERR_UNSAFE``).

Why web search is OFF by default (see also suggest/__init__.py): the answer must be
verified by the app anyway, so search adds cost and latency to the user's plan without
removing the need for verification; search results are untrusted text that could steer
the model (prompt injection), and a model that "found" a page is not proof the company
exists. The opt-in setting `suggest_allow_web_search` helps when the model's knowledge
is thin (small local companies) — the verification rules do not change.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..chat import events as ev
from ..chat.commands import (CLAUDE_ALLOWED_TOOLS, McpLaunch, build_claude_command, build_codex_command)
from ..chat.detect import CLAUDE, CODEX
from ..chat.process import child_environment
from ..chat.session import ChatSession, assistant_folder
from ..chat.stream import ClaudeStreamParser, CodexStreamParser
from ..chat.commands import claude_environment
from .prompt import preamble

log = logging.getLogger("reversehire.suggest")

SUGGEST_MCP_FILE = "suggest-no-mcp.json"
CLAUDE_WEB_TOOL = "WebSearch"
CODEX_WEB_ITEM = "web_search"
MAX_TURNS_NO_TOOLS = 2
MAX_TURNS_WEB = 8
DEFAULT_TIMEOUT_S = 240


class CommandError(ValueError):
    """The chat command line no longer has a flag the suggestion relies on (fail closed)."""


def _set_value(argv: list[str], flag: str, value: str) -> list[str]:
    try:
        index = argv.index(flag)
    except ValueError:
        raise CommandError(f"flag {flag} missing from the chat command line") from None
    if index + 1 >= len(argv):
        raise CommandError(f"flag {flag} has no value")
    out = list(argv)
    out[index + 1] = value
    return out


def build_suggest_claude_command(launcher: list[str], empty_mcp_config: Path | str, language: str | None, *,
                                 allow_web_search: bool = False, resume_session: str | None = None,
                                 model: str | None = None) -> list[str]:
    """argv of a Claude Code suggestion turn (pure; see the module docstring)."""
    turns = MAX_TURNS_WEB if allow_web_search else MAX_TURNS_NO_TOOLS
    argv = build_claude_command(launcher, empty_mcp_config, language, resume_session=resume_session,
                                model=model, max_turns=turns)
    argv = _set_value(argv, "--append-system-prompt", preamble(language))
    argv = _set_value(argv, "--tools", CLAUDE_WEB_TOOL if allow_web_search else "")
    argv = _set_value(argv, "--allowedTools", CLAUDE_WEB_TOOL if allow_web_search else CLAUDE_ALLOWED_TOOLS)
    for required in ("--restricted", "--strict-mcp-config", "--disable-slash-commands"):
        if required not in argv:
            raise CommandError(f"flag {required} missing from the chat command line")
    return argv


def build_suggest_codex_command(launcher: list[str], *, allow_web_search: bool = False,
                                resume_session: str | None = None, model: str | None = None) -> list[str]:
    """argv of a Codex suggestion turn (pure; see the module docstring)."""
    base = build_codex_command(launcher, McpLaunch(python="unused"), resume_session=resume_session, model=model)
    argv: list[str] = []
    i = 0
    web_seen = False
    while i < len(base):
        item = base[i]
        if item == "-c" and i + 1 < len(base):
            value = base[i + 1]
            if value.startswith("mcp_servers."):
                i += 2
                continue
            if value.startswith("web_search="):
                web_seen = True
                value = "web_search='live'" if allow_web_search else "web_search='disabled'"
            argv += ["-c", value]
            i += 2
            continue
        argv.append(item)
        i += 1
    if not web_seen:
        raise CommandError("web_search override missing from the chat command line")
    for required in ("sandbox_mode='read-only'", "approval_policy='never'"):
        if required not in argv:
            raise CommandError(f"override {required} missing from the chat command line")
    return argv


def codex_prompt(question: str, language: str | None) -> str:
    """Codex has no system-prompt flag: the instructions go before every question."""
    return f"[ReverseHire instructions]\n{preamble(language)}\n\n{question}"


def write_empty_mcp_config(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    tmp.replace(path)
    return path


# --- parsers ---------------------------------------------------------------------------------


@dataclass
class Usage:
    """What the CLI reported about the request (None = not reported)."""

    cli: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cost_usd: float | None = None
    turns: int | None = None
    web_searches: int = 0
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _int(value) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class _WebAllowance:
    allow_web = False
    web_label = ""

    def _refuse(self, detail: str) -> None:  # type: ignore[override]
        if self.allow_web and detail in (f"tool {self.web_label}", self.web_label):
            log.debug("%s: web search allowed by the setting", self.cli)
            return
        super()._refuse(detail)  # type: ignore[misc]

    def _notice_once(self, code: str, **params) -> list:  # type: ignore[override]
        if code == ev.NOTICE_MCP_FAILED:
            return []  # no MCP server on purpose
        return super()._notice_once(code, **params)  # type: ignore[misc]


class SuggestClaudeParser(_WebAllowance, ClaudeStreamParser):
    web_label = CLAUDE_WEB_TOOL
    expects_mcp_server = False   # no ReverseHire server on purpose: no "status missing" warning

    def __init__(self, allow_web: bool = False) -> None:
        super().__init__()
        self.allow_web = allow_web
        self.web_searches = 0

    def _claude_tool(self, block: dict) -> list:
        if self.allow_web and str(block.get("name") or "") == CLAUDE_WEB_TOOL:
            self.web_searches += 1
        return super()._claude_tool(block)

    def usage(self, duration: float) -> Usage:
        result = self.result or {}
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        cached = (_int(usage.get("cache_read_input_tokens")) or 0) + (_int(usage.get("cache_creation_input_tokens")) or 0)
        cost = result.get("total_cost_usd")
        return Usage(cli=CLAUDE, input_tokens=_int(usage.get("input_tokens")),
                     output_tokens=_int(usage.get("output_tokens")),
                     cached_input_tokens=cached if usage else None,
                     cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
                     turns=_int(result.get("num_turns")), web_searches=self.web_searches, duration_s=duration)


class SuggestCodexParser(_WebAllowance, CodexStreamParser):
    web_label = CODEX_WEB_ITEM

    def __init__(self, allow_web: bool = False) -> None:
        super().__init__()
        self.allow_web = allow_web
        self.web_searches = 0
        self.usage_data: dict = {}

    def _handle(self, obj: dict) -> list:
        if obj.get("type") == "turn.completed" and isinstance(obj.get("usage"), dict):
            self.usage_data = dict(obj["usage"])
        item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
        if self.allow_web and obj.get("type") == "item.completed" and item.get("type") == CODEX_WEB_ITEM:
            self.web_searches += 1
        return super()._handle(obj)

    def usage(self, duration: float) -> Usage:
        data = self.usage_data
        return Usage(cli=CODEX, input_tokens=_int(data.get("input_tokens")),
                     output_tokens=_int(data.get("output_tokens")),
                     cached_input_tokens=_int(data.get("cached_input_tokens")),
                     web_searches=self.web_searches, duration_s=duration)


# --- session ----------------------------------------------------------------------------------


class SuggestSession(ChatSession):
    """A ChatSession whose turns are suggestion requests (see the module docstring).

    The process handling (worker thread, timeout, cancel with process-tree kill, unsafe
    argument check for .cmd launchers, stderr tail) is ChatSession's own; only the command
    (`_prepare`) and the parser (the session's ``parser_factory`` hook) differ.
    ``last_parser`` is the parser of the last request (usage figures)."""

    def __init__(self, cli, *, data_dir, callbacks=None, language: str = "en",
                 timeout_s: float = DEFAULT_TIMEOUT_S, model: str | None = None,
                 allow_web_search: bool = False, resume_session: str | None = None, popen=subprocess.Popen):
        super().__init__(cli, data_dir=data_dir, callbacks=callbacks, language=language, timeout_s=timeout_s,
                         model=model, mcp_env={}, popen=popen, parser_factory=lambda _kind: self._make_parser())
        self.allow_web_search = bool(allow_web_search)
        self._session_id = resume_session or None

    @property
    def mcp_config_path(self) -> Path:
        return assistant_folder(self.data_dir) / SUGGEST_MCP_FILE

    def _prepare(self, question: str) -> tuple[list[str], dict, str]:
        kind = self.cli.kind
        self.work_dir.mkdir(parents=True, exist_ok=True)
        prefix = self.cli.argv_prefix()
        if not prefix:
            raise FileNotFoundError("no executable")
        if kind == CLAUDE:
            write_empty_mcp_config(self.mcp_config_path)
            argv = build_suggest_claude_command(prefix, self.mcp_config_path, self.language,
                                                allow_web_search=self.allow_web_search,
                                                resume_session=self._session_id, model=self.model)
            return argv, child_environment(claude_environment()), question
        if kind == CODEX:
            argv = build_suggest_codex_command(prefix, allow_web_search=self.allow_web_search,
                                               resume_session=self._session_id, model=self.model)
            return argv, child_environment(), codex_prompt(question, self.language)
        raise ValueError(f"unknown CLI: {kind!r}")

    def _make_parser(self):
        if self.cli.kind == CLAUDE:
            return SuggestClaudeParser(self.allow_web_search)
        if self.cli.kind == CODEX:
            return SuggestCodexParser(self.allow_web_search)
        raise ValueError(f"unknown CLI: {self.cli.kind!r}")
