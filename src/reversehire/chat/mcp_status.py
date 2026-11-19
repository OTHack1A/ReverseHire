"""Can the app's MCP server be started for the assistant CLI?

Checks the interpreter, the ``mcp`` package and the ``reversehire.mcp_server``
module, and reports the ``mcp_enabled`` setting passed in by the caller (this
module reads no settings and imports no GUI).
"""

from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import messages
from .process import current_python, run_quick

log = logging.getLogger("reversehire.chat")

MCP_READY = "ready"
MCP_DISABLED = "disabled"                  # server can start, but access is switched off
MCP_PACKAGE_MISSING = "package_missing"
MCP_IMPORT_FAILED = "import_failed"
MCP_PYTHON_MISSING = "python_missing"

SERVER_MODULE = "reversehire.mcp_server"
_PROBE = ("import importlib.util, sys\n"
          "if importlib.util.find_spec('mcp') is None:\n"
          "    print('NO_MCP'); sys.exit(3)\n"
          f"import {SERVER_MODULE}\n"
          "print('OK')\n")


@dataclass
class McpStatus:
    status: str
    python: str
    hint_key: str
    hint_params: dict = field(default_factory=dict)

    @property
    def can_start(self) -> bool:
        return self.status in (MCP_READY, MCP_DISABLED)

    @property
    def ready(self) -> bool:
        return self.status == MCP_READY

    @property
    def hint(self) -> str:
        return messages.text(self.hint_key, **self.hint_params)


def check_mcp(enabled: bool, python: str | None = None, *, probe: bool = False,
              run: Callable = run_quick, find_spec: Callable = importlib.util.find_spec,
              is_file: Callable[[Path], bool] = lambda p: Path(p).is_file()) -> McpStatus:
    """Status of the MCP server for `python` (default: the running interpreter).

    With the running interpreter and ``probe=False`` this only looks the modules up
    (fast, nothing imported). With another interpreter, or ``probe=True``, it runs
    ``python -c "import reversehire.mcp_server"`` (slow: heavy libraries; use a worker
    thread). Never raises."""
    python = python or current_python()
    same = _same_file(python, sys.executable)
    if not is_file(Path(python)):
        return McpStatus(MCP_PYTHON_MISSING, python, "chat.mcp.python_missing", {"python": python})
    problem: tuple[str, str] | None = None
    if same and not probe:
        try:
            if find_spec("mcp") is None:
                problem = (MCP_PACKAGE_MISSING, "")
            elif find_spec(SERVER_MODULE) is None:
                problem = (MCP_IMPORT_FAILED, "module not found")
        except (ImportError, ValueError) as exc:
            problem = (MCP_IMPORT_FAILED, str(exc))
    else:
        try:
            done = run([python, "-c", _PROBE], timeout=120)
            output = ((done.stdout or "") + (done.stderr or "")).strip()
            if done.returncode == 3 or "NO_MCP" in output:
                problem = (MCP_PACKAGE_MISSING, "")
            elif done.returncode != 0:
                problem = (MCP_IMPORT_FAILED, _last_line(output))
        except (OSError, subprocess.SubprocessError) as exc:
            problem = (MCP_IMPORT_FAILED, str(exc))
    if problem is not None:
        status, detail = problem
        key = "chat.mcp.package_missing" if status == MCP_PACKAGE_MISSING else "chat.mcp.import_failed"
        log.info("MCP server not available for the chat: %s", status)
        return McpStatus(status, python, key, {"python": python, "detail": detail[:300]})
    if not enabled:
        return McpStatus(MCP_DISABLED, python, "chat.mcp.disabled")
    return McpStatus(MCP_READY, python, "chat.mcp.ready")


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except (TypeError, ValueError):
        return False


def _last_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""
