"""Everything that touches the operating system, as replaceable callables.

``System`` bundles the side effects used by the mail methods: start a program, open a
URL or file with its default handler, open a URL in the browser, show a file in the
file manager, put text in the clipboard. Tests build a ``System`` of fakes; the real
one is ``System.default()``. Launching is fire-and-forget: a process is started and
watched for at most ``LAUNCH_WAIT`` seconds, only to catch an immediate failure.

Safety switch: with the environment variable REVERSEHIRE_MAIL_DRY_RUN=1 the real
callables refuse to do anything (they raise LaunchError); so does the real classic
Outlook COM factory (methods._default_outlook_factory).
"""

import os
import subprocess
import sys
import threading
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

DRY_RUN_ENV = "REVERSEHIRE_MAIL_DRY_RUN"
LAUNCH_WAIT = 1.0      # seconds a started program is watched for an immediate failure
CLIPBOARD_TIMEOUT = 3.0


class LaunchError(RuntimeError):
    """A program, URL or file could not be opened."""


def _check_dry_run() -> None:
    if os.environ.get(DRY_RUN_ENV) == "1":
        raise LaunchError(f"{DRY_RUN_ENV}=1: nothing is launched")


def platform_name(value: str | None = None) -> str:
    """"windows", "macos", "linux" or "other"."""
    value = value or sys.platform
    if value.startswith("win"):
        return "windows"
    if value == "darwin":
        return "macos"
    if value.startswith("linux") or "bsd" in value:
        return "linux"
    return "other"


def _no_window_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if platform_name() == "windows" else 0


def spawn(command: Sequence[str] | str, check_exit: bool = False, wait: float = LAUNCH_WAIT) -> None:
    """Starts a program without waiting for it. With ``check_exit``, a non-zero exit code
    within ``wait`` seconds is reported as LaunchError (for helpers such as xdg-email
    and osascript; not for explorer.exe, which returns 1 on success).

    The program's output is discarded (never read): an error text could quote the
    message body, which must not reach the log."""
    _check_dry_run()
    try:
        process = subprocess.Popen(  # noqa: S603 - argument list built by this package
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, shell=False)
    except OSError as error:
        raise LaunchError(str(error)) from error
    try:
        code = process.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        return  # still running: fine (fire and forget)
    if check_exit and code != 0:
        raise LaunchError(f"exit code {code}")


def open_target(target: str) -> None:
    """Opens a URL (mailto:) or a file with the default program of the system."""
    _check_dry_run()
    system = platform_name()
    if system == "windows":
        try:
            os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
        except OSError as error:
            raise LaunchError(str(error)) from error
    elif system == "macos":
        spawn(["open", target], check_exit=True)
    else:
        spawn(["xdg-open", target], check_exit=True)


def open_browser(url: str) -> None:
    """Opens an http(s) URL in the default browser."""
    _check_dry_run()
    try:
        opened = webbrowser.open(url, new=2)
    except webbrowser.Error as error:
        raise LaunchError(str(error)) from error
    if not opened:
        raise LaunchError("no browser could be started")


def reveal(path: Path) -> None:
    """Opens the file manager with ``path`` selected (Linux: the folder is opened)."""
    _check_dry_run()
    path = Path(path)
    system = platform_name()
    if system == "windows":
        # explorer wants the quotes around the path only: /select,"C:\a b\cv.pdf"
        spawn(f'explorer /select,"{path}"')
    elif system == "macos":
        spawn(["open", "-R", str(path)], check_exit=True)
    else:
        spawn(["xdg-open", str(path.parent)], check_exit=True)


def _run_input(command: list[str], data: bytes) -> bool:
    try:
        result = subprocess.run(command, input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,  # noqa: S603
                                timeout=CLIPBOARD_TIMEOUT, check=False, creationflags=_no_window_flags())
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def copy_to_clipboard(textual: str) -> bool:
    """Puts text in the clipboard. Uses Qt when a QGuiApplication exists (call it from
    the GUI thread then), otherwise clip / pbcopy / wl-copy / xclip / xsel."""
    _check_dry_run()
    try:
        from PySide6.QtGui import QGuiApplication

        app = QGuiApplication.instance()
        if app is not None:
            app.clipboard().setText(textual)
            return True
    except ImportError:
        pass
    system = platform_name()
    if system == "windows":
        return _run_input(["clip"], ("\ufeff" + textual).encode("utf-16-le"))
    if system == "macos":
        return _run_input(["pbcopy"], textual.encode("utf-8"))
    for command in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        if _run_input(command, textual.encode("utf-8")):
            return True
    return False


@dataclass(frozen=True)
class System:
    """The side effects used by the mail methods (replace any of them in tests)."""

    spawn: Callable[..., None] = spawn                    # (command, check_exit=False) -> None
    open_target: Callable[[str], None] = open_target      # mailto: URL or file path
    open_browser: Callable[[str], None] = open_browser    # http(s) URL
    reveal: Callable[[Path], None] = reveal               # show a file in the file manager
    clipboard: Callable[[str], bool] = copy_to_clipboard  # True when the text was copied
    platform: str = ""                                    # "" = this computer

    @classmethod
    def default(cls) -> "System":
        return cls()

    @property
    def os_name(self) -> str:
        return self.platform or platform_name()


def run_with_timeout(function: Callable[[], object], timeout: float) -> tuple[bool, object, BaseException | None]:
    """Runs ``function`` in a daemon thread and waits at most ``timeout`` seconds.

    Returns (finished, result, error). When not finished the thread keeps running in
    the background (for example Outlook still starting) and its result is dropped.
    """
    box: dict = {}

    def target() -> None:
        try:
            box["result"] = function()
        except BaseException as error:  # noqa: BLE001 - reported to the caller
            box["error"] = error

    thread = threading.Thread(target=target, name="reversehire-mail", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return False, None, None
    return True, box.get("result"), box.get("error")
