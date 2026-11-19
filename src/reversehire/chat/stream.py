"""Parsers of the JSON-lines streams of Claude Code (``--output-format stream-json``)
and Codex (``exec --json``).

A parser gets one line at a time (``feed``) and returns the events to deliver:
:class:`TextChunk`, :class:`~.events.ToolNotice`, :class:`~.events.Notice`.
Unknown event types are ignored, malformed lines are logged (length only, never
the content) and skipped. When the stream shows something ReverseHire does not
allow (a non-ReverseHire tool, a permissive permission mode) ``abort`` is set
and the session stops the process. ``outcome`` turns the end of the run into a
:class:`~.events.ChatAnswer` or a :class:`~.events.ChatError`.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from dataclasses import dataclass

from . import events as ev
from .commands import CLAUDE_PERMISSION_MODE, CLAUDE_TOOL_PREFIX, SERVER_NAME

log = logging.getLogger("reversehire.chat")

_AUTH_RE = re.compile(r"not logged in|not signed in|/login|codex login|invalid api key|"
                      r"authentication[_ ]failed|unauthori[sz]ed|\b401\b|oauth token|expired token|"
                      r"login required|please (?:log|sign) in", re.IGNORECASE)


@dataclass(frozen=True)
class TextChunk:
    text: str


_USAGE_RE = re.compile(r"usage limit|limit reached|hit your (?:usage )?limit|rate[ _-]?limit|quota exceeded|"
                       r"out of (?:extra )?usage|too many requests|\b429\b|insufficient[_ ]quota|credit balance",
                       re.IGNORECASE)
_RESET_EPOCH_RE = re.compile(r"\|(\d{10})\b")
_RESET_TEXT_RE = re.compile(r"\bresets?\s+(?:at\s+)?([^.;\n|]{1,40})", re.IGNORECASE)


def usage_limit_reset(text: str) -> str | None:
    """None when `text` is not a usage-limit message; else the reset time as the CLI gave
    it ("" when unknown): "Claude AI usage limit reached|1763542800" -> "19/11 10:00"."""
    if not text or not _USAGE_RE.search(text):
        return None
    epoch = _RESET_EPOCH_RE.search(text)
    if epoch:
        from datetime import datetime

        try:
            return datetime.fromtimestamp(int(epoch.group(1))).strftime("%d/%m %H:%M")
        except (OverflowError, OSError, ValueError):
            return ""
    when = _RESET_TEXT_RE.search(text)
    return when.group(1).strip() if when else ""


def looks_like_auth_error(text: str) -> bool:
    return bool(text) and bool(_AUTH_RE.search(text))


class _BaseParser:
    cli = ""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.abort: ev.ChatError | None = None
        self.tools_used: list[str] = []
        self.malformed = 0
        self._chunks: list[str] = []
        self._tool_names: dict[str, str] = {}
        self._notified: set[str] = set()

    # -- helpers ---------------------------------------------------------------------
    def _decode(self, line: str):
        line = line.strip()
        if not line:
            return None
        try:
            obj = json.loads(line)
        except ValueError:
            self.malformed += 1
            log.warning("%s: skipped a malformed stream line (%d characters)", self.cli, len(line))
            return None
        if not isinstance(obj, dict):
            self.malformed += 1
            log.warning("%s: skipped a stream line that is not an object", self.cli)
            return None
        return obj

    def _text(self, text: str, new_block: bool = False) -> list:
        if not text:
            return []
        if new_block and self._chunks and not self._chunks[-1].endswith("\n"):
            text = "\n\n" + text
        self._chunks.append(text)
        return [TextChunk(text)]

    def _tool_started(self, server: str, tool: str, call_id: str) -> list:
        if call_id and call_id in self._tool_names:
            return []
        if server != SERVER_NAME:
            self._refuse(f"tool {server}/{tool}" if server else f"tool {tool}")
            return []
        if call_id:
            self._tool_names[call_id] = tool
        self.tools_used.append(tool)
        log.info("%s called the ReverseHire tool %s", self.cli, tool)
        return [ev.ToolNotice(tool=tool, server=server, phase="started", call_id=call_id)]

    def _tool_finished(self, call_id: str, is_error: bool, tool: str | None = None) -> list:
        tool = tool or self._tool_names.get(call_id)
        if not tool:
            return []
        return [ev.ToolNotice(tool=tool, server=SERVER_NAME, phase="finished", is_error=is_error,
                              call_id=call_id)]

    def _notice_once(self, code: str, **params) -> list:
        if code in self._notified:
            return []
        self._notified.add(code)
        return [ev.make_notice(code, self.cli, **params)]

    def _refuse(self, detail: str) -> None:
        if self.abort is None:
            log.warning("%s stopped by ReverseHire: %s", self.cli, detail)
            self.abort = ev.make_error(ev.ERR_UNSAFE, self.cli, detail=detail)

    @property
    def streamed_text(self) -> str:
        return "".join(self._chunks)

    def _failure(self, detail: str, exit_code: int | None, duration: float, code: str) -> ev.ChatError:
        params = {"code": exit_code} if exit_code is not None else {"code": "?"}
        reset = usage_limit_reset(detail)
        if reset is not None:   # the plan's limit, said in plain words (not "reported an error")
            code = ev.ERR_USAGE_LIMIT if reset else ev.ERR_USAGE_LIMIT_NO_TIME
            params["when"] = reset
        elif looks_like_auth_error(detail):
            code = ev.ERR_NOT_LOGGED_IN
        return ev.make_error(code, self.cli, detail=detail, exit_code=exit_code, duration_s=duration,
                             **params)

    def _answer(self, text: str, exit_code: int | None, duration: float) -> ev.ChatAnswer:
        return ev.ChatAnswer(text=text.strip(), cli=self.cli, session_id=self.session_id,
                             duration_s=duration, tools=tuple(self.tools_used), exit_code=exit_code)

    def _common_end(self, cancelled: bool, timed_out: bool, timeout_s: float, exit_code, duration):
        if self.abort is not None:
            return dataclasses.replace(self.abort, duration_s=duration, exit_code=exit_code)
        if cancelled:
            return ev.make_error(ev.ERR_CANCELLED, self.cli, exit_code=exit_code, duration_s=duration)
        if timed_out:
            return ev.make_error(ev.ERR_TIMEOUT, self.cli, exit_code=exit_code, duration_s=duration,
                                 seconds=int(timeout_s))
        return None


class ClaudeStreamParser(_BaseParser):
    """``claude -p --output-format stream-json --verbose --include-partial-messages``."""

    cli = "claude"
    # False for runs started without the ReverseHire MCP server on purpose (suggestions):
    # its absence is then expected, not worth a warning or a notice.
    expects_mcp_server = True

    def __init__(self) -> None:
        super().__init__()
        self.result: dict | None = None
        self._deltas_seen = False
        self._new_message = False

    def feed(self, line: str) -> list:
        obj = self._decode(line)
        if obj is None:
            return []
        try:
            return self._handle(obj)
        except (AttributeError, TypeError, KeyError, ValueError) as exc:
            self.malformed += 1
            log.warning("claude: skipped an unexpected %r event (%s)", obj.get("type"), type(exc).__name__)
            return []

    def _handle(self, obj: dict) -> list:
        kind = obj.get("type")
        if obj.get("session_id") and not self.session_id:
            self.session_id = str(obj["session_id"])
        if kind == "system":
            return self._system(obj)
        if kind == "stream_event":
            return self._stream_event(obj.get("event") or {})
        if kind == "assistant":
            return self._assistant(obj.get("message") or {})
        if kind == "user":
            return self._user(obj.get("message") or {})
        if kind == "result":
            self.result = obj
            if obj.get("session_id"):
                self.session_id = str(obj["session_id"])
            return []
        if isinstance(kind, str) and kind.startswith("hook"):
            log.warning("claude: a hook event appeared although settings are ignored (%s)", kind)
        else:
            log.debug("claude: ignored event %r", kind)
        return []

    def _system(self, obj: dict) -> list:
        subtype = obj.get("subtype")
        if subtype == "init":
            if obj.get("session_id"):
                self.session_id = str(obj["session_id"])
            mode = obj.get("permissionMode")
            if mode is not None and mode != CLAUDE_PERMISSION_MODE:
                self._refuse(f"permission mode {mode}")
            for tool in obj.get("tools") or []:
                name = tool if isinstance(tool, str) else (tool or {}).get("name", "")
                if not str(name).startswith(CLAUDE_TOOL_PREFIX):
                    self._refuse(f"tool {name}")
            servers = {}
            for server in obj.get("mcp_servers") or []:
                if isinstance(server, dict):
                    servers[server.get("name")] = server.get("status")
            for name in servers:
                if name != SERVER_NAME:
                    self._refuse(f"MCP server {name}")
            status = servers.get(SERVER_NAME)
            if status != "connected" and not self.expects_mcp_server:
                return []  # a run without the ReverseHire server on purpose (suggestions)
            if status != "connected":
                log.warning("claude: ReverseHire MCP server status %s", status or "missing")
                return self._notice_once(ev.NOTICE_MCP_FAILED, detail=status or "not loaded")
            return []
        if subtype == "api_retry":
            detail = obj.get("error") or obj.get("error_status") or "retry"
            if isinstance(detail, dict):
                detail = detail.get("type") or detail.get("message") or "retry"
            return self._notice_once(ev.NOTICE_RETRYING, detail=str(detail)[:80])
        return []

    def _stream_event(self, event: dict) -> list:
        etype = event.get("type")
        if etype == "message_start":
            self._new_message = True
            return []
        if etype == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                return self._claude_tool(block)
            return []
        if etype == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                self._deltas_seen = True
                first = self._new_message
                self._new_message = False
                return self._text(str(delta.get("text") or ""), new_block=first)
        return []

    def _claude_tool(self, block: dict) -> list:
        name = str(block.get("name") or "")
        call_id = str(block.get("id") or "")
        if name.startswith(CLAUDE_TOOL_PREFIX):
            return self._tool_started(SERVER_NAME, name[len(CLAUDE_TOOL_PREFIX):], call_id)
        server = name.split("__")[1] if name.startswith("mcp__") and name.count("__") >= 2 else ""
        return self._tool_started(server, name, call_id)

    def _assistant(self, message: dict) -> list:
        out: list = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                out += self._claude_tool(block)
            elif block.get("type") == "text" and not self._deltas_seen:
                out += self._text(str(block.get("text") or ""), new_block=True)
        return out

    def _user(self, message: dict) -> list:
        out: list = []
        content = message.get("content")
        if not isinstance(content, list):
            return out
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                out += self._tool_finished(str(block.get("tool_use_id") or ""), bool(block.get("is_error")))
        return out

    def outcome(self, exit_code: int | None, stderr: str, duration: float, *, cancelled: bool = False,
                timed_out: bool = False, timeout_s: float = 0) -> ev.ChatAnswer | ev.ChatError:
        end = self._common_end(cancelled, timed_out, timeout_s, exit_code, duration)
        if end is not None:
            return end
        result = self.result
        if result is not None:
            if result.get("is_error") or result.get("subtype") not in (None, "success"):
                errors = result.get("errors")
                detail = str(result.get("result") or "") or (
                    "; ".join(map(str, errors)) if isinstance(errors, list) and errors else "") or \
                    str(result.get("subtype") or "") or stderr
                return self._failure(detail, exit_code, duration, ev.ERR_CLI_REPORTED)
            text = str(result.get("result") or "") or self.streamed_text
            if text.strip():
                return self._answer(text, exit_code, duration)
            return ev.make_error(ev.ERR_NO_ANSWER, self.cli, exit_code=exit_code, duration_s=duration)
        if exit_code not in (0, None):
            return self._failure(stderr, exit_code, duration, ev.ERR_CLI_FAILED)
        if self.streamed_text.strip():
            return self._answer(self.streamed_text, exit_code, duration)
        if looks_like_auth_error(stderr):
            return self._failure(stderr, exit_code, duration, ev.ERR_NOT_LOGGED_IN)
        return ev.make_error(ev.ERR_NO_ANSWER, self.cli, detail=stderr, exit_code=exit_code,
                             duration_s=duration)


class CodexStreamParser(_BaseParser):
    """``codex exec --json`` (thread.* / turn.* / item.* events)."""

    cli = "codex"
    # Items that would mean Codex used something other than the ReverseHire tools.
    REFUSED_ITEMS = {"command_execution", "file_change", "web_search", "patch_apply", "exec_command",
                     "local_shell_call", "image_generation"}

    def __init__(self) -> None:
        super().__init__()
        self.last_message: str | None = None
        self.turn_completed = False
        self.turn_failed: str | None = None
        self.last_error: str | None = None

    def feed(self, line: str) -> list:
        obj = self._decode(line)
        if obj is None:
            return []
        try:
            return self._handle(obj)
        except (AttributeError, TypeError, KeyError, ValueError) as exc:
            self.malformed += 1
            log.warning("codex: skipped an unexpected %r event (%s)", obj.get("type"), type(exc).__name__)
            return []

    def _handle(self, obj: dict) -> list:
        kind = obj.get("type")
        if kind == "thread.started":
            if obj.get("thread_id"):
                self.session_id = str(obj["thread_id"])
            return []
        if kind in ("item.started", "item.updated", "item.completed"):
            return self._item(kind, obj.get("item") or {})
        if kind == "turn.completed":
            self.turn_completed = True
            return []
        if kind == "turn.failed":
            error = obj.get("error") or {}
            self.turn_failed = str(error.get("message") if isinstance(error, dict) else error) or "turn failed"
            return []
        if kind == "error":
            message = str(obj.get("message") or "")
            if message.lower().startswith(("reconnecting", "retrying")):
                return self._notice_once(ev.NOTICE_RETRYING, detail=message[:80])
            self.last_error = message or self.last_error
            return []
        log.debug("codex: ignored event %r", kind)
        return []

    def _item(self, kind: str, item: dict) -> list:
        itype = item.get("type") or item.get("item_type") or (item.get("details") or {}).get("type")
        call_id = str(item.get("id") or "")
        if itype in self.REFUSED_ITEMS:
            self._refuse(str(itype))
            return []
        if itype == "mcp_tool_call":
            server = str(item.get("server") or "")
            tool = str(item.get("tool") or item.get("name") or "")
            out = []
            if call_id not in self._tool_names:
                out += self._tool_started(server, tool, call_id)
            if kind == "item.completed" and self.abort is None:
                failed = item.get("status") == "failed" or bool(item.get("error"))
                out += self._tool_finished(call_id, failed, tool)
            return out
        if itype == "agent_message" and kind == "item.completed":
            text = str(item.get("text") or "")
            if text:
                self.last_message = text
                return self._text(text, new_block=True)
            return []
        if itype == "error" and kind == "item.completed":
            log.info("codex reported a non-fatal error item")
        return []

    def outcome(self, exit_code: int | None, stderr: str, duration: float, *, cancelled: bool = False,
                timed_out: bool = False, timeout_s: float = 0) -> ev.ChatAnswer | ev.ChatError:
        end = self._common_end(cancelled, timed_out, timeout_s, exit_code, duration)
        if end is not None:
            return end
        if self.turn_failed:
            return self._failure(self.turn_failed, exit_code, duration, ev.ERR_CLI_REPORTED)
        if self.last_message and (self.turn_completed or exit_code in (0, None)):
            return self._answer(self.last_message, exit_code, duration)
        if exit_code not in (0, None):
            return self._failure(self.last_error or stderr, exit_code, duration, ev.ERR_CLI_FAILED)
        if self.last_error:
            return self._failure(self.last_error, exit_code, duration, ev.ERR_CLI_REPORTED)
        if looks_like_auth_error(stderr):
            return self._failure(stderr, exit_code, duration, ev.ERR_NOT_LOGGED_IN)
        return ev.make_error(ev.ERR_NO_ANSWER, self.cli, detail=stderr, exit_code=exit_code,
                             duration_s=duration)


def parser_for(cli: str) -> ClaudeStreamParser | CodexStreamParser:
    if cli == "claude":
        return ClaudeStreamParser()
    if cli == "codex":
        return CodexStreamParser()
    raise ValueError(f"unknown CLI: {cli!r}")
