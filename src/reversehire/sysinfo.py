"""Information about this PC for the About dialog (standard library only).

Shown only at run time: never logged, stored or sent anywhere. Every value is
collected in its own guard, so a failure (or a non-Windows system) shows
UNKNOWN instead of raising. Screen and Qt values are passed in by the GUI (Qt is
only used on the GUI thread), so this module does not import Qt; everything else
may be slow (a sleeping disk wakes up) and is collected on a worker thread.

The data folder and the embedding model are in Diagnostics (diagnostics.py), not
here: `data_folder` and `embedding_model` stay available for it.
"""

from __future__ import annotations

import ctypes
import getpass
import importlib.util
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

UNKNOWN = "—"

# Keys in display order; the GUI translates them as "about.key.<key>".
KEYS = (
    "user", "computer", "os", "uptime", "processor", "cores", "memory", "gpu", "screen", "disks",
    "network", "python", "qt", "app_version", "ocr", "mcp",
)

# OCR engines the CV reader may use (checked by package name, never imported).
OCR_PACKAGES = ("rapidocr_onnxruntime", "rapidocr", "pytesseract", "easyocr", "winocr")

_CPU_KEY = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
_WINDOWS_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
_DISPLAY_CLASS_KEY = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
# Virtual adapters that say nothing about the PC.
_VIRTUAL_GPUS = ("microsoft basic display", "microsoft basic render", "microsoft remote display",
                 "indirect display", "virtual display")
_EDITIONS = {"Professional": "Pro", "Core": "Home", "CoreSingleLanguage": "Home Single Language",
             "ProfessionalWorkstation": "Pro for Workstations", "ProfessionalEducation": "Pro Education"}
DRIVE_FIXED = 3

# English words, replaced by the GUI's translated `labels` (see collect).
DEFAULT_LABELS = {
    "available": "available", "not_available": "not available",
    "online": "online", "offline": "offline",
    "uptime": "{days} d {hours} h {minutes} min",
    "disk": "{drive}: {total} total, {free} free",
    "disk_program": "program", "disk_data": "data", "disk_roles": " ({roles})",
    "app_version": "{version} (build {build}, {date})",
    "app_version_source": "{version} (build {build}, source copy, not built)",
}


def _guard(function: Callable[[], str | None]) -> str:
    try:
        value = function()
    except Exception:
        return UNKNOWN
    if value is None:
        return UNKNOWN
    text = str(value).strip()
    return text or UNKNOWN


# --- user, computer, system ---------------------------------------------------------------

def session_user() -> str | None:
    """The user of the operating-system session this process runs in, asked to the
    system (an environment variable may have been changed): GetUserNameW on
    Windows, the password database elsewhere; None when it cannot be read."""
    if sys.platform == "win32":
        try:
            size = ctypes.c_ulong(257)
            buffer = ctypes.create_unicode_buffer(size.value)
            if ctypes.windll.advapi32.GetUserNameW(buffer, ctypes.byref(size)) and buffer.value:
                return buffer.value
        except Exception:
            return None
        return None
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return None


def user_name() -> str | None:
    """The user currently logged in to the system (session_user), getpass as the last resort."""
    return session_user() or getpass.getuser()


def computer_name() -> str | None:
    return os.environ.get("COMPUTERNAME") or platform.node()


def _windows_registry(name: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _WINDOWS_KEY) as key:
            value, _type = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return str(value).strip() or None


def operating_system() -> str | None:
    """"Windows 11 Pro 24H2, build 26100.2314, 64-bit" (edition, feature update and
    build with its update revision); "<system> <release> <version> 64-bit" elsewhere."""
    if sys.platform == "win32":
        version = sys.getwindowsversion()
        name = "Windows 11" if version.major == 10 and version.build >= 22000 else f"Windows {platform.release()}"
        edition = ""
        try:
            edition = platform.win32_edition() or ""
        except Exception:
            pass
        edition = _EDITIONS.get(edition, edition)
        display = ubr = None
        try:
            display = _windows_registry("DisplayVersion")
            ubr = _windows_registry("UBR")
        except Exception:
            pass
        build = f"build {version.build}.{ubr}" if ubr else f"build {version.build}"
        bits = "64-bit" if platform.machine().endswith("64") else platform.architecture()[0]
        head = " ".join(p for p in (name, edition, display) if p)
        return f"{head}, {build}, {bits}"
    bits = "64-bit" if sys.maxsize > 2 ** 32 else "32-bit"
    return f"{platform.system()} {platform.release()} {platform.version()} {bits}"


def uptime_seconds() -> float | None:
    """Seconds since the operating system started."""
    if sys.platform == "win32":
        function = ctypes.windll.kernel32.GetTickCount64
        function.restype = ctypes.c_ulonglong
        return function() / 1000.0
    if sys.platform.startswith("linux"):
        return float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])
    if sys.platform == "darwin":
        import re
        import time

        text = _sysctl("kern.boottime") or ""
        match = re.search(r"sec\s*=\s*(\d+)", text)
        return time.time() - int(match.group(1)) if match else None
    return None


def uptime_text(seconds: float | None, template: str = DEFAULT_LABELS["uptime"]) -> str | None:
    if seconds is None or seconds < 0:
        return None
    minutes_total = int(seconds // 60)
    days, rest = divmod(minutes_total, 24 * 60)
    hours, minutes = divmod(rest, 60)
    return template.format(days=days, hours=hours, minutes=minutes)


# --- processor, memory, graphics ------------------------------------------------------------

def processor_name() -> str | None:
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _CPU_KEY) as key:
                value, _type = winreg.QueryValueEx(key, "ProcessorNameString")
            if str(value).strip():
                return " ".join(str(value).split())
        except Exception:
            pass
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    if sys.platform == "darwin":
        brand = _sysctl("machdep.cpu.brand_string")
        if brand:
            return brand
    return platform.processor() or None


def _sysctl(name: str) -> str | None:
    """`sysctl -n <name>` on macOS and BSD (None when not available)."""
    import subprocess

    try:
        result = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=2)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    text = result.stdout.strip() if result.returncode == 0 else ""
    return text or None


def _physical_cores() -> int | None:
    """Physical cores on Windows (GetLogicalProcessorInformation) and macOS (sysctl);
    None elsewhere."""
    if sys.platform == "darwin":
        value = _sysctl("hw.physicalcpu")
        return int(value) if value and value.isdigit() else None
    if sys.platform != "win32":
        return None

    class _Info(ctypes.Structure):
        _fields_ = [("mask", ctypes.c_size_t), ("relationship", ctypes.c_int),
                    ("reserved", ctypes.c_ulonglong * 2)]

    function = ctypes.windll.kernel32.GetLogicalProcessorInformation
    length = ctypes.c_ulong(0)
    function(None, ctypes.byref(length))
    count = length.value // ctypes.sizeof(_Info)
    if count <= 0:
        return None
    buffer = (_Info * count)()
    if not function(buffer, ctypes.byref(length)):
        return None
    cores = sum(1 for item in buffer if item.relationship == 0)  # RelationProcessorCore
    return cores or None


def cores() -> str | None:
    logical = os.cpu_count()
    physical = None
    try:
        physical = _physical_cores()
    except Exception:
        physical = None
    if physical and logical:
        return f"{physical} / {logical}"
    return str(logical) if logical else None


def _memory_bytes() -> int | None:
    if sys.platform == "win32":
        class _Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                        ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                        ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                        ("avail_extended", ctypes.c_ulonglong)]

        status = _Status()
        status.length = ctypes.sizeof(_Status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.total_phys)
        return None
    if sys.platform == "darwin":  # SC_PHYS_PAGES does not exist on macOS
        value = _sysctl("hw.memsize")
        return int(value) if value and value.isdigit() else None
    if hasattr(os, "sysconf"):
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    return None


def memory() -> str | None:
    total = _memory_bytes()
    if not total:
        return None
    return f"{total / 1024 ** 3:.1f} GB"


def gpu_names() -> list[str]:
    """Names of the graphics adapters, from the driver list of the system (no extra
    package, nothing loaded): the Windows display-adapter class in the registry,
    /sys/class/drm on Linux (vendor only when no name is published). [] elsewhere."""
    names: list[str] = []
    if sys.platform == "win32":
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS_KEY) as root:
            index = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, sub) as key:
                        value, _type = winreg.QueryValueEx(key, "DriverDesc")
                except OSError:
                    continue
                names.append(" ".join(str(value).split()))
    elif sys.platform.startswith("linux"):
        for info in sorted(Path("/proc/driver/nvidia/gpus").glob("*/information")):
            for line in info.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.lower().startswith("model:"):
                    names.append(line.split(":", 1)[1].strip())
    result: list[str] = []
    for name in names:
        if name and name not in result and not any(v in name.lower() for v in _VIRTUAL_GPUS):
            result.append(name)
    return result


def gpu() -> str | None:
    names = gpu_names()
    return "\n".join(names) if names else None


# --- disks -------------------------------------------------------------------------------------

@dataclass
class Disk:
    drive: str           # "C" on Windows, the mount point elsewhere
    total: int           # bytes
    free: int            # bytes
    program: bool = False
    data: bool = False


def _size_text(size: int) -> str:
    gigabytes = size / 1024 ** 3
    return f"{gigabytes / 1024:.2f} TB" if gigabytes >= 1024 else f"{gigabytes:.1f} GB"


def _program_and_data_paths() -> tuple[Path | None, Path | None]:
    program = Path(__file__).resolve().parent
    data = None
    try:
        from .config import data_location

        data = data_location().folder.resolve()
    except Exception:
        data = None
    return program, data


def _mount_point(path: Path) -> str:
    path = path.resolve()
    while not os.path.ismount(path) and path.parent != path:
        path = path.parent
    return str(path)


def fixed_disks(program: Path | None = None, data: Path | None = None) -> list[Disk]:
    """Every fixed (local, not removable) disk with total and free space, marking the
    ones that hold the program and the data folder. Windows: every fixed drive
    letter; elsewhere the mount points of /, the program and the data folder."""
    if program is None and data is None:
        program, data = _program_and_data_paths()
    disks: list[Disk] = []
    if sys.platform == "win32":
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        get_type = ctypes.windll.kernel32.GetDriveTypeW
        letters = [chr(ord("A") + i) for i in range(26) if mask & (1 << i)]
        on = {p.drive.rstrip(":").upper() for p in (program,) if p is not None}
        on_data = {p.drive.rstrip(":").upper() for p in (data,) if p is not None}
        for letter in letters:
            root = f"{letter}:\\"
            if get_type(root) != DRIVE_FIXED:
                continue
            try:
                usage = shutil.disk_usage(root)
            except OSError:
                continue
            disks.append(Disk(letter, usage.total, usage.free, letter in on, letter in on_data))
        return disks
    points: list[str] = []
    for path in (Path("/"), program, data):
        if path is not None:
            point = _mount_point(path)
            if point not in points:
                points.append(point)
    for point in points:
        try:
            usage = shutil.disk_usage(point)
        except OSError:
            continue
        disks.append(Disk(point, usage.total, usage.free,
                          program is not None and _mount_point(program) == point,
                          data is not None and _mount_point(data) == point))
    return disks


def disks_text(disks: list[Disk], labels: dict[str, str] | None = None) -> str | None:
    """One line per disk: "C: 476.3 GB total, 120.5 GB free (program, data)"."""
    words = {**DEFAULT_LABELS, **(labels or {})}
    lines = []
    for disk in disks:
        line = words["disk"].format(drive=disk.drive, total=_size_text(disk.total), free=_size_text(disk.free))
        roles = [words[name] for name, on in (("disk_program", disk.program), ("disk_data", disk.data)) if on]
        if roles:
            line += words["disk_roles"].format(roles=", ".join(roles))
        lines.append(line)
    return "\n".join(lines) if lines else None


# --- network -----------------------------------------------------------------------------------

def network_online() -> bool | None:
    """Whether the system has a network connection, asked to the system (no
    request is sent and no address is read): wininet on Windows, a default route
    on Linux; None when unknown."""
    if sys.platform == "win32":
        flags = ctypes.c_ulong(0)
        return bool(ctypes.windll.wininet.InternetGetConnectedState(ctypes.byref(flags), 0))
    if sys.platform.startswith("linux"):
        for line in Path("/proc/net/route").read_text(encoding="ascii").splitlines()[1:]:
            fields = line.split()
            if len(fields) > 3 and fields[1] == "00000000" and int(fields[3], 16) & 1:
                return True
        return False
    return None


# --- Python, the app, OCR ----------------------------------------------------------------------

def python_version() -> str | None:
    return f"{platform.python_version()} ({platform.python_implementation()}, {platform.architecture()[0]})"


def app_version(labels: dict[str, str] | None = None) -> str | None:
    """"0.1.7 (build 7, 2026-11-18)": the version, its build number and build date."""
    from . import BUILD_DATE, __version__, build_number

    words = {**DEFAULT_LABELS, **(labels or {})}
    if BUILD_DATE:
        return words["app_version"].format(version=__version__, build=build_number(__version__), date=BUILD_DATE)
    return words["app_version_source"].format(version=__version__, build=build_number(__version__))


def data_folder(modes: dict[str, str] | None = None) -> str | None:
    """The data folder as the file system resolves it (links and short names expanded),
    followed by how it was chosen ("portable", "per-user"...; `modes` holds the
    translated words, keyed by config.MODE_*). Shown in Diagnostics."""
    from .config import data_location

    location = data_location()
    words = {"env": "REVERSEHIRE_HOME", "portable": "portable", "user": "per-user",
             "temp": "temporary"}
    words.update(modes or {})
    return f"{location.folder.resolve()} ({words.get(location.mode, location.mode)})"


def embedding_model(downloaded: str = "downloaded", missing: str = "not downloaded yet",
                    found_in: str = "found in {folder}") -> str | None:
    """Model name and whether a complete copy of its files is in one of the model
    folders (``scoring.embedding.model_search_folders``), without loading it."""
    from .scoring.embedding import PREFERRED_MODEL, find_local_model, model_search_folders

    local = find_local_model(PREFERRED_MODEL, model_search_folders())
    if local is None:
        return f"{PREFERRED_MODEL} ({missing})"
    where = found_in.format(folder=local.folder.resolve()) if "{folder}" in found_in else found_in
    return f"{PREFERRED_MODEL} ({downloaded}, {where})"


def ocr_engine(available: str = "available", missing: str = "not available") -> str | None:
    for name in OCR_PACKAGES:
        try:
            if importlib.util.find_spec(name) is not None:
                return f"{available} ({name})"
        except (ImportError, ValueError):
            continue
    return missing


# --- all values --------------------------------------------------------------------------------

def collect(*, screen: str | None = None, qt: str | None = None, mcp: str | None = None,
            labels: dict[str, str] | None = None) -> dict[str, str]:
    """All values, keyed by KEYS, as display strings (UNKNOWN when unavailable).

    `screen`, `qt` and `mcp` are given by the caller (the GUI); `labels` holds the
    translated words and templates (keys of DEFAULT_LABELS). Blocking (disks):
    call it on a worker thread."""
    words = {**DEFAULT_LABELS, **(labels or {})}

    def online() -> str | None:
        state = network_online()
        return None if state is None else words["online"] if state else words["offline"]

    getters: dict[str, Callable[[], str | None]] = {
        "user": user_name,
        "computer": computer_name,
        "os": operating_system,
        "uptime": lambda: uptime_text(uptime_seconds(), words["uptime"]),
        "processor": processor_name,
        "cores": cores,
        "memory": memory,
        "gpu": gpu,
        "screen": lambda: screen,
        "disks": lambda: disks_text(fixed_disks(), words),
        "network": online,
        "python": python_version,
        "qt": lambda: qt,
        "app_version": lambda: app_version(words),
        "ocr": lambda: ocr_engine(words["available"], words["not_available"]),
        "mcp": lambda: mcp,
    }
    return {key: _guard(getters[key]) for key in KEYS}


def as_text(values: dict[str, str], names: dict[str, str]) -> str:
    """Plain "name: value" lines for the Copy button (one line per key: a value of
    several lines, such as the disks, is joined with "; ")."""
    return "\n".join(f"{names.get(key, key)}: {'; '.join(str(values.get(key, UNKNOWN)).splitlines()) or UNKNOWN}"
                     for key in KEYS)
