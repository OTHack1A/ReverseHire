"""Subprocess helpers: no console window on Windows, process-tree kill, quick runs."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("reversehire.chat")

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

# Characters that cmd.exe interprets even inside double quotes: a .cmd/.bat launcher
# (npm shims) runs through cmd.exe, so no argument may contain them.
BATCH_UNSAFE = set('%!^&|<>"\r\n')

# Environment variables of a Claude Code session that would make a child `claude`
# think it is nested inside another session. Only these are removed.
_NESTED_SESSION_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")


def popen_kwargs() -> dict:
    """Keyword arguments for Popen: own process group, no console window."""
    if IS_WINDOWS:
        return {"creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def is_batch_launcher(path: str | Path) -> bool:
    return str(path).lower().endswith((".cmd", ".bat"))


def batch_unsafe_arguments(argv: list[str]) -> list[str]:
    """Arguments that cannot go through a .cmd launcher (empty list when all are fine)."""
    if not argv or not is_batch_launcher(argv[0]):
        return []
    return [a for a in argv[1:] if BATCH_UNSAFE & set(a)]


def child_environment(extra: dict[str, str] | None = None, base: dict | None = None) -> dict[str, str]:
    """Environment of the CLI: the user's own, minus nested-session markers, plus `extra`."""
    env = dict(os.environ if base is None else base)
    for name in _NESTED_SESSION_VARS:
        env.pop(name, None)
    env.setdefault("NO_COLOR", "1")
    if extra:
        env.update(extra)
    return env


def run_quick(argv: list[str], timeout: float = 15.0, env: dict | None = None,
              cwd: str | None = None) -> subprocess.CompletedProcess:
    """Runs a short command (``--version``, ``auth status``) with UTF-8 text output.

    Raises OSError / subprocess.TimeoutExpired like subprocess.run."""
    kwargs = {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {}
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, env=env, cwd=cwd, stdin=subprocess.DEVNULL, **kwargs)


def kill_tree(proc: subprocess.Popen, grace: float = 3.0) -> None:
    """Terminates `proc` and its children (the CLI starts the MCP server as a child)."""
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
            subprocess.run([str(taskkill), "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           timeout=15, creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL)
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            deadline = time.monotonic() + grace
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Could not stop the process tree of pid %s: %s", proc.pid, exc)
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def current_python() -> str:
    """The interpreter running ReverseHire (pythonw.exe -> python.exe for stdio use)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.is_file():
            return str(candidate)
    return str(exe)
