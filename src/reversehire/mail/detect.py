"""Which mail programs this computer has, read-only (study, section E.5).

Windows: registry only (mailto UserChoice → ProgId → Application; classic Outlook COM
registration and mail profile; new Outlook package; Thunderbird; default browser).
Linux: ``xdg-settings`` and ``which``. macOS: application folders. Nothing is ever
written, no mail program is started, no account or mailbox is read.

All lookups go through a ``Probe`` of callables, so tests use a fake registry and
file system.
"""

import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from reversehire.mail.system import platform_name

log = logging.getLogger("reversehire.mail")

HKCU, HKLM, HKCR = "HKCU", "HKLM", "HKCR"

KIND_OUTLOOK_NEW = "outlook_new"
KIND_OUTLOOK_CLASSIC = "outlook_classic"
KIND_THUNDERBIRD = "thunderbird"
KIND_BROWSER = "browser"
KIND_OTHER = "other"
KIND_NONE = "none"        # no handler registered
KIND_UNKNOWN = "unknown"  # could not be determined (macOS)

_BROWSER_PROGIDS = {
    "chromehtml": "Google Chrome", "msedgehtm": "Microsoft Edge", "firefoxurl": "Firefox",
    "firefoxhtml": "Firefox", "bravehtml": "Brave", "operastable": "Opera", "vivaldihtm": "Vivaldi",
    "ie.http": "Internet Explorer", "safarihtml": "Safari",
}
_LINUX_BROWSERS = ("firefox", "chrome", "chromium", "brave", "opera", "vivaldi", "epiphany", "edge")
_NEW_OUTLOOK_PACKAGE = "Microsoft.OutlookForWindows"
_APPX_PACKAGES = r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\Repository\Packages"
_OUTLOOK_PROFILE_KEYS = (r"Software\Microsoft\Office\16.0\Outlook\Profiles",
                         r"Software\Microsoft\Office\15.0\Outlook\Profiles",
                         r"Software\Microsoft\Office\Outlook\Profiles")


# --- probe ----------------------------------------------------------------------------

def _winreg_root(hive: str):
    import winreg

    return {HKCU: winreg.HKEY_CURRENT_USER, HKLM: winreg.HKEY_LOCAL_MACHINE,
            HKCR: winreg.HKEY_CLASSES_ROOT}[hive]


def _real_read_value(hive: str, path: str, name: str = "") -> str | None:
    try:
        import winreg

        with winreg.OpenKey(_winreg_root(hive), path) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except (ImportError, OSError):
        return None
    return None if value is None else str(value)


def _real_subkeys(hive: str, path: str) -> list[str]:
    try:
        import winreg

        names = []
        with winreg.OpenKey(_winreg_root(hive), path) as key:
            index = 0
            while True:
                try:
                    names.append(winreg.EnumKey(key, index))
                except OSError:
                    break
                index += 1
        return names
    except (ImportError, OSError):
        return []


def _real_run(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",  # noqa: S603
                                errors="replace", timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


@dataclass(frozen=True)
class Probe:
    """Read-only lookups (replace them in tests)."""

    read_value: Callable[[str, str, str], str | None] = _real_read_value  # (hive, key path, value name)
    subkeys: Callable[[str, str], list[str]] = _real_subkeys
    exists: Callable[[str], bool] = os.path.exists
    which: Callable[[str], str | None] = shutil.which
    run: Callable[[list[str]], str | None] = _real_run                   # stdout or None
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    platform: str = ""                                                   # "" = this computer


# --- result ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MailEnvironment:
    """What was found. ``*_kind`` values are the KIND_* constants."""

    platform: str
    mailto_kind: str = KIND_NONE
    mailto_name: str = ""             # e.g. "Outlook", "Thunderbird", "Microsoft Edge"
    eml_kind: str = KIND_NONE         # program set for .eml files
    default_browser: str = ""
    outlook_classic_installed: bool = False
    outlook_classic_com: bool = False      # Outlook.Application registered
    outlook_classic_profile: bool = False  # at least one mail profile exists
    outlook_new_installed: bool = False
    outlook_new_preferred: bool = False    # "UseNewOutlook" toggle set in classic Outlook
    outlook_new_path: str = ""           # olk.exe app execution alias (opens .eml drafts)
    thunderbird_path: str = ""
    xdg_email_path: str = ""
    xdg_open_path: str = ""
    apple_mail: bool = False
    notes: tuple[str, ...] = ()            # technical notes for the diagnostics/log

    @property
    def outlook_classic_usable(self) -> bool:
        """COM automation will find a configured Outlook (no profile wizard expected)."""
        return self.outlook_classic_com and self.outlook_classic_profile


def _classify(progid: str, app_name: str, aumid: str, command: str) -> tuple[str, str]:
    """(kind, display name) of a protocol/file handler."""
    low_progid, low_command = progid.lower(), command.lower()
    name = app_name if app_name and not app_name.startswith("@") else ""
    if aumid.lower().startswith(_NEW_OUTLOOK_PACKAGE.lower()) or "olk.exe" in low_command:
        return KIND_OUTLOOK_NEW, name or "Outlook (new)"
    if "outlook.exe" in low_command or low_progid.startswith("outlook."):
        return KIND_OUTLOOK_CLASSIC, name or "Outlook"
    if "thunderbird" in low_progid or "thunderbird" in low_command:
        return KIND_THUNDERBIRD, name or "Thunderbird"
    for prefix, browser in _BROWSER_PROGIDS.items():
        if low_progid.startswith(prefix):
            return KIND_BROWSER, name or browser
    for exe, browser in (("chrome.exe", "Google Chrome"), ("msedge.exe", "Microsoft Edge"),
                         ("firefox.exe", "Firefox"), ("brave.exe", "Brave")):
        if exe in low_command:
            return KIND_BROWSER, name or browser
    if not progid and not command:
        return KIND_NONE, ""
    return KIND_OTHER, name or progid or _exe_name(command)


def _exe_name(command: str) -> str:
    exe = _command_exe(command)
    return os.path.splitext(os.path.basename(exe))[0] if exe else ""


def _command_exe(command: str) -> str:
    command = (command or "").strip()
    if command.startswith('"'):
        return command[1:].split('"', 1)[0]
    match = re.match(r"(.+?\.exe)\b", command, re.IGNORECASE)
    return match.group(1) if match else command.split(" ", 1)[0]


def _windows_handler(probe: Probe, user_choice_key: str, fallback_key: str) -> tuple[str, str]:
    """Handler of a protocol ("mailto", "https") or file type (".eml"): the per-user
    choice first, then the machine-wide registration under HKCR."""

    def from_progid(progid: str) -> tuple[str, str]:
        app_name = probe.read_value(HKCR, progid + r"\Application", "ApplicationName") or ""
        aumid = probe.read_value(HKCR, progid + r"\Application", "AppUserModelID") or ""
        command = probe.read_value(HKCR, progid + r"\shell\open\command", "") or ""
        return _classify(progid, app_name, aumid, command)

    progid = probe.read_value(HKCU, user_choice_key, "ProgId") or ""
    if progid:
        return from_progid(progid)
    if fallback_key.startswith("."):  # file type: HKCR\.eml (default) = ProgId
        progid = probe.read_value(HKCR, fallback_key, "") or ""
        return from_progid(progid) if progid else (KIND_NONE, "")
    command = probe.read_value(HKCR, fallback_key + r"\shell\open\command", "") or ""
    return _classify("", "", "", command)


def _detect_windows(probe: Probe) -> MailEnvironment:
    notes = []
    mailto_kind, mailto_name = _windows_handler(
        probe, r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\mailto\UserChoice", "mailto")
    eml_kind, _eml_name = _windows_handler(
        probe, r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.eml\UserChoice", ".eml")
    _browser_kind, browser = _windows_handler(
        probe, r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice", "https")

    com = bool(probe.read_value(HKCR, r"Outlook.Application\CurVer", ""))
    exe = probe.read_value(HKLM, r"Software\Microsoft\Windows\CurrentVersion\App Paths\OUTLOOK.EXE", "") or ""
    installed = com or bool(exe and probe.exists(exe.strip('"')))
    profile = any(probe.subkeys(HKCU, key) for key in _OUTLOOK_PROFILE_KEYS)
    preferred = (probe.read_value(HKCU, r"Software\Microsoft\Office\16.0\Outlook\Preferences",
                                  "UseNewOutlook") or "") == "1"
    if com and not profile:
        notes.append("classic Outlook is registered but has no mail profile")

    packages = probe.subkeys(HKCU, _APPX_PACKAGES)
    local = probe.env.get("LOCALAPPDATA", "")
    alias = os.path.join(local, "Microsoft", "WindowsApps", "olk.exe") if local else ""
    olk = alias if alias and probe.exists(alias) else (probe.which("olk") or "")
    new_installed = (any(p.startswith(_NEW_OUTLOOK_PACKAGE + "_") for p in packages) or bool(olk)
                     or mailto_kind == KIND_OUTLOOK_NEW)

    thunderbird = ""
    command = probe.read_value(HKLM, r"Software\Clients\Mail\Mozilla Thunderbird\shell\open\command", "") or ""
    candidates = [_command_exe(command)] if command else []
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        if probe.env.get(variable):
            candidates.append(os.path.join(probe.env[variable], "Mozilla Thunderbird", "thunderbird.exe"))
    for candidate in candidates:
        if candidate and probe.exists(candidate):
            thunderbird = candidate
            break
    thunderbird = thunderbird or (probe.which("thunderbird") or "")

    return MailEnvironment(
        platform="windows", mailto_kind=mailto_kind, mailto_name=mailto_name, eml_kind=eml_kind,
        default_browser=browser, outlook_classic_installed=installed, outlook_classic_com=com,
        outlook_classic_profile=profile, outlook_new_installed=new_installed,
        outlook_new_preferred=preferred, outlook_new_path=olk, thunderbird_path=thunderbird, notes=tuple(notes))


def _linux_kind(desktop_file: str) -> tuple[str, str]:
    low = (desktop_file or "").lower()
    name = re.sub(r"\.desktop$", "", desktop_file or "").split(".")[-1].replace("userapp-", "")
    if not low:
        return KIND_NONE, ""
    if "thunderbird" in low:
        return KIND_THUNDERBIRD, "Thunderbird"
    if any(browser in low for browser in _LINUX_BROWSERS):
        return KIND_BROWSER, name
    return KIND_OTHER, name


def _detect_linux(probe: Probe) -> MailEnvironment:
    mailto_kind, mailto_name = _linux_kind(
        probe.run(["xdg-settings", "get", "default-url-scheme-handler", "mailto"]) or "")
    _kind, browser = _linux_kind(probe.run(["xdg-settings", "get", "default-web-browser"]) or "")
    return MailEnvironment(
        platform="linux", mailto_kind=mailto_kind, mailto_name=mailto_name,
        eml_kind=mailto_kind if mailto_kind in (KIND_THUNDERBIRD, KIND_OTHER) else KIND_UNKNOWN,
        default_browser=browser,
        thunderbird_path=probe.which("thunderbird") or probe.which("thunderbird-esr") or "",
        xdg_email_path=probe.which("xdg-email") or "", xdg_open_path=probe.which("xdg-open") or "")


def _detect_macos(probe: Probe) -> MailEnvironment:
    apple_mail = probe.exists("/System/Applications/Mail.app") or probe.exists("/Applications/Mail.app")
    thunderbird = "/Applications/Thunderbird.app/Contents/MacOS/thunderbird"
    return MailEnvironment(
        platform="macos", mailto_kind=KIND_UNKNOWN, eml_kind=KIND_UNKNOWN, apple_mail=apple_mail,
        thunderbird_path=thunderbird if probe.exists(thunderbird) else "",
        notes=("default mail program not detected on macOS (needs LaunchServices)",))


def detect_environment(probe: Probe | None = None) -> MailEnvironment:
    """Read-only detection; never raises (a failed lookup counts as "not found")."""
    probe = probe or Probe()
    system = probe.platform or platform_name()
    try:
        if system == "windows":
            environment = _detect_windows(probe)
        elif system == "macos":
            environment = _detect_macos(probe)
        elif system == "linux":
            environment = _detect_linux(probe)
        else:
            environment = MailEnvironment(platform=system)
    except Exception as error:  # noqa: BLE001 - detection must never break the dialog
        log.warning("mail client detection failed: %s", error)
        environment = MailEnvironment(platform=system, notes=(f"detection failed: {error}",))
    log.info("mail clients: platform=%s mailto=%s (%s) eml=%s classic_outlook=%s/%s new_outlook=%s olk=%s "
             "thunderbird=%s",
             environment.platform, environment.mailto_kind, environment.mailto_name, environment.eml_kind,
             "com" if environment.outlook_classic_com else "-",
             "profile" if environment.outlook_classic_profile else "no profile",
             environment.outlook_new_installed, bool(environment.outlook_new_path), bool(environment.thunderbird_path))
    return environment
