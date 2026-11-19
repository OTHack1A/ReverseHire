"""Finds the user's installed assistant CLIs (Claude Code, Codex) and checks them.

No model call is made: the checks are ``--version`` and the CLIs' own sign-in
status commands (``claude auth status``, ``codex login status``). The account
e-mail / organisation printed by those commands is never kept, logged or shown.

Every system access (PATH lookup, file test, running a command, Store package
lookup) can be replaced, so the tests run on a fake filesystem.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import messages
from .process import IS_WINDOWS, run_quick

log = logging.getLogger("reversehire.chat")

CLAUDE, CODEX = "claude", "codex"
KINDS = (CLAUDE, CODEX)

# Status values of CliInfo.status
NOT_INSTALLED = "not_installed"   # nothing found (or the override path does not exist)
FOUND = "found"                   # found and runs, sign-in state unknown
NOT_LOGGED_IN = "not_logged_in"   # runs, but the CLI says it is not signed in
READY = "ready"                   # runs and is signed in
BROKEN = "broken"                 # found but `--version` fails

CODEX_APPX_PACKAGE = "OpenAI.Codex"


@dataclass
class CliInfo:
    """Result of the detection of one CLI."""

    kind: str                       # "claude" | "codex"
    status: str                     # one of the status constants above
    path: str | None = None         # executable found (may be a .cmd shim)
    version: str | None = None
    hint_key: str = ""              # i18n key of the human hint
    hint_params: dict = field(default_factory=dict)
    auth_method: str | None = None  # e.g. "claude.ai", "ChatGPT", "API key" (never the account)
    source: str | None = None       # where it was found: override, PATH, npm, store, ...
    launcher: list[str] | None = None  # argv prefix to run it (default: [path]); tests use a fake

    @property
    def name(self) -> str:
        return messages.CLI_NAMES.get(self.kind, self.kind)

    @property
    def usable(self) -> bool:
        """True when a question can be tried (signed in, or sign-in state unknown)."""
        return self.status in (READY, FOUND)

    @property
    def hint(self) -> str:
        return messages.text(self.hint_key, **self.hint_params) if self.hint_key else ""

    def argv_prefix(self) -> list[str]:
        if self.launcher:
            return list(self.launcher)
        return [self.path] if self.path else []


# --- candidate locations -----------------------------------------------------------------

def _home(env: dict) -> Path:
    return Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())


def candidate_paths(kind: str, env: dict | None = None, windows: bool = IS_WINDOWS) -> list[tuple[str, Path]]:
    """Common install locations, in order, as (source label, path)."""
    env = dict(os.environ if env is None else env)
    home = _home(env)
    appdata = Path(env["APPDATA"]) if env.get("APPDATA") else None
    local = Path(env["LOCALAPPDATA"]) if env.get("LOCALAPPDATA") else None
    out: list[tuple[str, Path]] = []
    if windows:
        if kind == CLAUDE:
            out.append(("native", home / ".local" / "bin" / "claude.exe"))
            out.append(("local", home / ".claude" / "local" / "claude.exe"))
            out.append(("local", home / ".claude" / "local" / "claude.cmd"))
            if appdata:
                out.append(("npm", appdata / "npm" / "claude.cmd"))
            if local:
                out.append(("programs", local / "Programs" / "claude" / "claude.exe"))
        else:
            if appdata:
                out.append(("npm", appdata / "npm" / "codex.cmd"))
            out.append(("native", home / ".local" / "bin" / "codex.exe"))
            out.append(("cargo", home / ".cargo" / "bin" / "codex.exe"))
            if local:
                out.append(("alias", local / "Microsoft" / "WindowsApps" / "codex.exe"))
    else:
        name = kind
        out.append(("native", home / ".local" / "bin" / name))
        if kind == CLAUDE:
            out.append(("local", home / ".claude" / "local" / "claude"))
        else:
            out.append(("cargo", home / ".cargo" / "bin" / "codex"))
        out.append(("npm", home / ".npm-global" / "bin" / name))
        out.append(("system", Path("/usr/local/bin") / name))
        out.append(("homebrew", Path("/opt/homebrew/bin") / name))
    return out


def codex_store_location(run: Callable = run_quick) -> Path | None:
    """codex.exe inside the Codex desktop app (Store package; the folder changes with
    every update and cannot be listed, but the exe runs)."""
    if not IS_WINDOWS:
        return None
    script = f"(Get-AppxPackage -Name {CODEX_APPX_PACKAGE} | Select-Object -First 1).InstallLocation"
    try:
        done = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("Store lookup for Codex failed: %s", exc)
        return None
    folder = (done.stdout or "").strip().splitlines()
    if done.returncode != 0 or not folder or not folder[0].strip():
        return None
    return Path(folder[0].strip()) / "app" / "resources" / "codex.exe"


def find_cli(kind: str, override: str | os.PathLike | None = None, *, env: dict | None = None,
             which: Callable[[str], str | None] = shutil.which,
             is_file: Callable[[Path], bool] = lambda p: Path(p).is_file(),
             store_lookup: Callable[[], Path | None] | None = None,
             windows: bool = IS_WINDOWS) -> tuple[str | None, str | None]:
    """(path, source) of the CLI, or (None, None). An override wins and is not replaced
    by other locations when it is missing (the caller reports it)."""
    if override:
        path = Path(os.path.expandvars(os.path.expanduser(str(override)).strip().strip('"')))
        return (str(path), "override") if is_file(path) else (None, "override")
    found = which(kind)
    if found:
        return found, "PATH"
    for source, path in candidate_paths(kind, env, windows):
        if is_file(path):
            return str(path), source
    if kind == CODEX:
        lookup = store_lookup if store_lookup is not None else codex_store_location
        path = lookup()
        if path is not None and is_file(path):
            return str(path), "store"
    return None, None


# --- version and sign-in -----------------------------------------------------------------

_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?(?:[-.+][0-9A-Za-z.\-]+)?")


def parse_version(output: str) -> str | None:
    match = _VERSION_RE.search(output or "")
    return match.group(0) if match else None


def parse_claude_auth(output: str) -> tuple[bool | None, str | None]:
    """(logged_in, auth_method) from ``claude auth status``; e-mail/org are dropped."""
    text = (output or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
        except ValueError:
            data = None
        if isinstance(data, dict) and "loggedIn" in data:
            method = data.get("authMethod")
            return bool(data["loggedIn"]), str(method) if method else None
    low = text.lower()
    if "not logged in" in low or "not signed in" in low:
        return False, None
    if "logged in" in low:
        return True, None
    return None, None


def parse_codex_login(output: str, returncode: int) -> tuple[bool | None, str | None]:
    """(logged_in, auth_method) from ``codex login status``."""
    low = (output or "").strip().lower()
    if "not logged in" in low or "not signed in" in low:
        return False, None
    match = re.search(r"logged in using (?:an? )?([a-z0-9 _-]+)", low)
    if match and returncode == 0:
        method = match.group(1).strip()
        return True, {"chatgpt": "ChatGPT", "api key": "API key"}.get(method, method)
    if returncode == 0 and "logged in" in low:
        return True, None
    if returncode != 0 and low:
        return False, None
    return None, None


def check_cli(kind: str, override: str | os.PathLike | None = None, *, env: dict | None = None,
              which: Callable[[str], str | None] = shutil.which,
              is_file: Callable[[Path], bool] = lambda p: Path(p).is_file(),
              store_lookup: Callable[[], Path | None] | None = None,
              run: Callable = run_quick, check_login: bool = True,
              windows: bool = IS_WINDOWS) -> CliInfo:
    """Detects one CLI and returns its status with a human hint. Never raises."""
    if kind not in KINDS:
        raise ValueError(f"unknown CLI kind: {kind!r}")
    name = messages.CLI_NAMES[kind]
    try:
        path, source = find_cli(kind, override, env=env, which=which, is_file=is_file,
                                store_lookup=store_lookup, windows=windows)
    except Exception as exc:  # defensive: detection must never break the settings page
        log.warning("Detection of %s failed: %s", kind, exc)
        path, source = None, None
    if path is None:
        if override:
            return CliInfo(kind, NOT_INSTALLED, source="override", hint_key="chat.cli.path_missing",
                           hint_params={"cli": name, "path": str(override)})
        return CliInfo(kind, NOT_INSTALLED, hint_key="chat.cli.not_installed", hint_params={"cli": name})
    info = CliInfo(kind, FOUND, path=path, source=source)
    try:
        done = run([path, "--version"], timeout=20)
        output = (done.stdout or "") + (done.stderr or "")
        if done.returncode != 0:
            raise OSError(f"exit code {done.returncode}")
        info.version = parse_version(output) or output.strip()[:40] or None
    except (OSError, subprocess.SubprocessError) as exc:
        info.status = BROKEN
        info.hint_key, info.hint_params = "chat.cli.not_runnable", {"cli": name, "detail": str(exc)[:200]}
        log.info("%s found (%s) but does not run: %s", kind, source, exc)
        return info
    version = info.version or "?"
    logged_in: bool | None = None
    if check_login:
        argv = [path, "auth", "status"] if kind == CLAUDE else [path, "login", "status"]
        try:
            done = run(argv, timeout=20)
            output = (done.stdout or "") + "\n" + (done.stderr or "")
            if kind == CLAUDE:
                logged_in, info.auth_method = parse_claude_auth(output)
            else:
                logged_in, info.auth_method = parse_codex_login(output, done.returncode)
        except (OSError, subprocess.SubprocessError) as exc:
            log.info("Sign-in check of %s failed: %s", kind, exc)
    if logged_in is True:
        info.status = READY
        info.hint_key, info.hint_params = "chat.cli.ready", {"cli": name, "version": version}
    elif logged_in is False:
        info.status = NOT_LOGGED_IN
        info.hint_key = "chat.cli.not_logged_in"
        info.hint_params = {"cli": name, "login_command": messages.LOGIN_COMMANDS[kind]}
    else:
        info.hint_key, info.hint_params = "chat.cli.login_unknown", {"cli": name, "version": version}
    log.info("Assistant CLI %s: %s (version %s, found via %s)", kind, info.status, version, source)
    return info


def detect_all(overrides: dict[str, str | None] | None = None, **kwargs) -> dict[str, CliInfo]:
    """Both CLIs: {"claude": CliInfo, "codex": CliInfo}. Slow (runs commands): call it
    on a worker thread."""
    overrides = overrides or {}
    return {kind: check_cli(kind, overrides.get(kind), **kwargs) for kind in KINDS}
