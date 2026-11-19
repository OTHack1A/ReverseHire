"""ChatSession: one conversation with the user's installed assistant CLI.

Each question is one CLI process (``claude -p`` or ``codex exec``) started on a
worker thread; follow-up questions resume the CLI's own session id. Callbacks
are plain callables called from that worker thread (an integrator bridges them
to Qt signals). ``send`` never blocks and nothing raises into the caller: every
problem becomes an ``on_error`` call with a readable message.

Order of the callbacks of one question: any number of ``on_text`` / ``on_tool``
/ ``on_notice``, then exactly one of ``on_done`` or ``on_error`` (always last).
"""

from __future__ import annotations

import collections
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import events as ev
from .commands import (McpLaunch, build_claude_command, build_codex_command, claude_environment,
                       codex_prompt, write_claude_mcp_config)
from .detect import CLAUDE, CODEX, CliInfo
from .process import batch_unsafe_arguments, child_environment, current_python, kill_tree, popen_kwargs
from .stream import TextChunk, parser_for

log = logging.getLogger("reversehire.chat")

DEFAULT_TIMEOUT_S = 900          # analyze_company may read several sites
ASSISTANT_FOLDER = "assistant"   # inside the data folder
WORKSPACE_FOLDER = "workspace"   # the CLI's working directory: always empty
CLAUDE_MCP_FILE = "claude-mcp.json"
_STDERR_KEEP = 8000


@dataclass
class ChatCallbacks:
    on_text: Callable[[str], None] | None = None                # partial answer text
    on_tool: Callable[[ev.ToolNotice], None] | None = None      # a ReverseHire tool started/finished
    on_notice: Callable[[ev.Notice], None] | None = None        # non-fatal information
    on_done: Callable[[ev.ChatAnswer], None] | None = None      # final answer (last call)
    on_error: Callable[[ev.ChatError], None] | None = None      # failure (last call)


def assistant_folder(data_dir: Path) -> Path:
    return Path(data_dir) / ASSISTANT_FOLDER


def default_mcp_env(data_dir: Path) -> dict[str, str]:
    """Environment of the MCP server: the same data folder as the app, and the
    package folder on PYTHONPATH (runs from a source checkout too). No secrets."""
    from .. import __file__ as package_init

    return {"REVERSEHIRE_HOME": str(Path(data_dir)),
            "PYTHONPATH": str(Path(package_init).resolve().parent.parent)}


class ChatSession:
    """One conversation. Create it with a detected CliInfo (``detect.check_cli``)."""

    def __init__(self, cli: CliInfo, *, data_dir: str | Path, callbacks: ChatCallbacks | None = None,
                 language: str = "en", timeout_s: float = DEFAULT_TIMEOUT_S, python: str | None = None,
                 mcp_env: dict[str, str] | None = None, model: str | None = None, max_turns: int = 12,
                 popen: Callable = subprocess.Popen,
                 parser_factory: Callable[[str], object] | None = None) -> None:
        """`parser_factory(cli_kind)` returns the stream parser of each question (default:
        ``stream.parser_for``); a subclass changes the command with `_prepare` and the
        parsing with this factory, never the process handling."""
        self.cli = cli
        self.parser_factory = parser_factory
        self.last_parser = None   # the parser of the last question (usage, tools used)
        self.data_dir = Path(data_dir)
        self.callbacks = callbacks or ChatCallbacks()
        self.language = language
        self.timeout_s = float(timeout_s)
        self.python = python or current_python()
        self.mcp_env = dict(mcp_env) if mcp_env is not None else default_mcp_env(self.data_dir)
        self.model = model
        self.max_turns = max_turns
        self._popen = popen
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._session_id: str | None = None
        self._cancelled = False
        self._timed_out = False

    # --- public API ----------------------------------------------------------------------
    @property
    def session_id(self) -> str | None:
        """The CLI's own session id, resumed by the next question (None: new conversation)."""
        return self._session_id

    @property
    def busy(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def work_dir(self) -> Path:
        return assistant_folder(self.data_dir) / WORKSPACE_FOLDER

    @property
    def mcp_config_path(self) -> Path:
        return assistant_folder(self.data_dir) / CLAUDE_MCP_FILE

    def send(self, text: str) -> bool:
        """Asks a question. Returns True when the question was started; False when it was
        refused at once (empty, busy: ``on_error`` was already called)."""
        try:
            question = (text or "").strip()
            if not question:
                self._deliver_end(ev.make_error(ev.ERR_EMPTY, self.cli.kind))
                return False
            with self._lock:
                if self.busy:
                    busy = ev.make_error(ev.ERR_BUSY, self.cli.kind)
                else:
                    busy = None
                    self._cancelled = False
                    self._timed_out = False
                    self._thread = threading.Thread(target=self._run, args=(question,),
                                                    name="reversehire-chat", daemon=True)
                    self._thread.start()
            if busy is not None:
                self._deliver_end(busy)
                return False
            return True
        except Exception as exc:  # never into the caller
            log.exception("Chat: could not start the question")
            self._deliver_end(ev.make_error(ev.ERR_INTERNAL, self.cli.kind, detail=str(exc)))
            return False

    def cancel(self) -> None:
        """Stops the running question (the process tree is terminated). Does not block."""
        self._cancelled = True
        proc = self._proc
        if proc is not None:
            threading.Thread(target=kill_tree, args=(proc,), name="reversehire-chat-kill",
                             daemon=True).start()

    def reset(self) -> None:
        """Forgets the conversation: the next question starts a new CLI session."""
        self._session_id = None

    def wait(self, timeout: float | None = None) -> bool:
        """Waits for the running question (tests, shutdown). True when nothing runs anymore."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            return not thread.is_alive()
        return True

    def close(self, timeout: float = 10.0) -> None:
        self.cancel()
        self.wait(timeout)

    # --- worker --------------------------------------------------------------------------
    def _run(self, question: str) -> None:
        started = time.monotonic()
        outcome: ev.ChatAnswer | ev.ChatError
        parser = None
        try:
            parser, outcome = self._run_turn(question, started)
        except Exception as exc:
            log.exception("Chat: unexpected error during a question to %s", self.cli.kind)
            outcome = ev.make_error(ev.ERR_INTERNAL, self.cli.kind, detail=str(exc),
                                    duration_s=time.monotonic() - started)
        finally:
            self._proc = None
        if parser is not None and parser.session_id and not (
                isinstance(outcome, ev.ChatError) and outcome.code == ev.ERR_UNSAFE):
            self._session_id = parser.session_id
        tools = ",".join(parser.tools_used) if parser is not None else ""
        log.info("Chat question ended: cli=%s outcome=%s exit=%s duration=%.1fs tools=[%s] malformed=%d",
                 self.cli.kind, outcome.code if isinstance(outcome, ev.ChatError) else "answer",
                 outcome.exit_code, time.monotonic() - started, tools,
                 parser.malformed if parser is not None else 0)
        self._deliver_end(outcome)

    def _prepare(self, question: str) -> tuple[list[str], dict, str]:
        kind = self.cli.kind
        resume = self._session_id
        mcp = McpLaunch(python=self.python, env=self.mcp_env)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        prefix = self.cli.argv_prefix()
        if not prefix:
            raise FileNotFoundError("no executable")
        if kind == CLAUDE:
            write_claude_mcp_config(self.mcp_config_path, mcp)
            argv = build_claude_command(prefix, self.mcp_config_path, self.language, resume_session=resume,
                                        model=self.model, max_turns=self.max_turns)
            return argv, child_environment(claude_environment()), question
        if kind == CODEX:
            argv = build_codex_command(prefix, mcp, resume_session=resume, model=self.model)
            return argv, child_environment(), codex_prompt(question, self.language, first_turn=resume is None)
        raise ValueError(f"unknown CLI: {kind!r}")

    def _run_turn(self, question: str, started: float):
        kind = self.cli.kind
        parser = self.parser_factory(kind) if self.parser_factory is not None else parser_for(kind)
        self.last_parser = parser
        try:
            argv, env, stdin_text = self._prepare(question)
        except (OSError, ValueError) as exc:
            return parser, ev.make_error(ev.ERR_START_FAILED, kind, detail=str(exc))
        unsafe = batch_unsafe_arguments(argv)
        if unsafe:
            chars = sorted({c for a in unsafe for c in a if c in '%!^&|<>"\r\n'})
            return parser, ev.make_error(ev.ERR_UNSAFE_ARGS, kind, detail=" ".join(repr(c) for c in chars))
        log.info("Chat question started: cli=%s (%s) resume=%s", kind, Path(argv[0]).name,
                 "yes" if self._session_id else "no")
        try:
            proc = self._popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               cwd=str(self.work_dir), env=env, text=True, encoding="utf-8",
                               errors="replace", bufsize=1, **popen_kwargs())
        except (OSError, ValueError) as exc:
            return parser, ev.make_error(ev.ERR_START_FAILED, kind, detail=str(exc),
                                         duration_s=time.monotonic() - started)
        self._proc = proc
        if self._cancelled:
            kill_tree(proc)
        stderr_lines: collections.deque[str] = collections.deque(maxlen=200)
        readers = [threading.Thread(target=_feed_stdin, args=(proc, stdin_text), daemon=True),
                   threading.Thread(target=_drain, args=(proc.stderr, stderr_lines), daemon=True)]
        for reader in readers:
            reader.start()
        timer = threading.Timer(self.timeout_s, self._on_timeout, args=(proc,))
        timer.daemon = True
        timer.start()
        try:
            for line in proc.stdout:
                for event in parser.feed(line):
                    self._dispatch(event)
                if parser.abort is not None:
                    kill_tree(proc)
                    break
        except (OSError, ValueError) as exc:  # pipe closed by a kill
            log.debug("Chat: stdout closed (%s)", exc)
        finally:
            timer.cancel()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            proc.wait(timeout=10)
        for reader in readers:
            reader.join(timeout=5)
        stderr = "".join(stderr_lines)[-_STDERR_KEEP:]
        return parser, parser.outcome(proc.returncode, stderr, time.monotonic() - started,
                                      cancelled=self._cancelled, timed_out=self._timed_out,
                                      timeout_s=self.timeout_s)

    def _on_timeout(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None and not self._cancelled:
            self._timed_out = True
            log.warning("Chat: %s exceeded %.0f s, stopping it", self.cli.kind, self.timeout_s)
            kill_tree(proc)

    # --- callbacks -----------------------------------------------------------------------
    def _dispatch(self, event) -> None:
        if isinstance(event, TextChunk):
            self._call(self.callbacks.on_text, event.text)
        elif isinstance(event, ev.ToolNotice):
            self._call(self.callbacks.on_tool, event)
        elif isinstance(event, ev.Notice):
            self._call(self.callbacks.on_notice, event)

    def _deliver_end(self, outcome) -> None:
        if isinstance(outcome, ev.ChatAnswer):
            self._call(self.callbacks.on_done, outcome)
        else:
            self._call(self.callbacks.on_error, outcome)

    @staticmethod
    def _call(callback, value) -> None:
        if callback is None:
            return
        try:
            callback(value)
        except Exception:
            log.exception("Chat: a callback raised an exception")


def _feed_stdin(proc: subprocess.Popen, text: str) -> None:
    try:
        proc.stdin.write(text)
        proc.stdin.close()
    except (OSError, ValueError):
        pass


def _drain(stream, keep: collections.deque) -> None:
    try:
        for line in stream:
            keep.append(line)
    except (OSError, ValueError):
        pass
