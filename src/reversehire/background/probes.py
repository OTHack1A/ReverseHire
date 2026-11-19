"""System probes of the idle scheduler: user-input idle time, AC power, CPU load,
network. Each returns None when it cannot tell (the scheduler then decides what an
unknown value means). Every probe is cheap (no network, at most one short process on
macOS/Linux, cached) and never raises.

Windows: ``GetLastInputInfo`` / ``GetTickCount`` (idle), ``GetSystemPowerStatus`` (AC),
``GetSystemTimes`` deltas (CPU), ``InternetGetConnectedState`` (network), via ctypes.
Linux: ``xprintidle`` when installed (X11; under Wayland there is no portable idle
time: None), ``/sys/class/power_supply`` (AC), ``/proc/stat`` deltas (CPU),
``/sys/class/net/*/operstate`` (network).
macOS: ``ioreg -c IOHIDSystem`` HIDIdleTime (idle), ``pmset -g batt`` (AC), load
average / CPU count (CPU); network unknown.
When the idle time is unknown the scheduler falls back to "the app window has not been
active for N minutes" (reported by the GUI through `app_inactive_seconds`).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
CACHE_S = 2.0


def _quiet_run(argv: list[str], timeout: float = 2.0) -> str:
    kwargs = {"creationflags": 0x08000000} if IS_WINDOWS else {}
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, stdin=subprocess.DEVNULL, **kwargs)
        return done.stdout if done.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# --- Windows ------------------------------------------------------------------------------

def _win_idle_seconds() -> float | None:
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:  # noqa: BLE001
        return None


def _win_on_ac() -> bool | None:
    try:
        import ctypes
        from ctypes import wintypes

        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [("ACLineStatus", wintypes.BYTE), ("BatteryFlag", wintypes.BYTE),
                        ("BatteryLifePercent", wintypes.BYTE), ("SystemStatusFlag", wintypes.BYTE),
                        ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]

        status = SYSTEM_POWER_STATUS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return None
        line = status.ACLineStatus & 0xFF
        if line == 1:
            return True
        if line == 0:
            return False
        return True if (status.BatteryFlag & 0xFF) == 128 else None  # 128: no system battery
    except Exception:  # noqa: BLE001
        return None


def _win_cpu_times() -> tuple[int, int] | None:
    """(busy, total) 100-ns ticks since boot."""
    try:
        import ctypes
        from ctypes import wintypes

        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None

        def value(ft) -> int:
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        total = value(kernel) + value(user)  # kernel time includes idle time
        return total - value(idle), total
    except Exception:  # noqa: BLE001
        return None


def _win_network() -> bool | None:
    try:
        import ctypes
        from ctypes import wintypes

        flags = wintypes.DWORD()
        return bool(ctypes.windll.wininet.InternetGetConnectedState(ctypes.byref(flags), 0))
    except Exception:  # noqa: BLE001
        return None


# --- Linux / macOS ---------------------------------------------------------------------------

def _linux_idle_seconds() -> float | None:
    if not os.environ.get("DISPLAY") or shutil.which("xprintidle") is None:
        return None
    out = _quiet_run(["xprintidle"]).strip()
    return int(out) / 1000.0 if out.isdigit() else None


def _mac_idle_seconds() -> float | None:
    out = _quiet_run(["ioreg", "-c", "IOHIDSystem"])
    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
    return int(match.group(1)) / 1e9 if match else None


def _linux_on_ac() -> bool | None:
    base = Path("/sys/class/power_supply")
    try:
        supplies = list(base.iterdir())
    except OSError:
        return None
    mains = [s for s in supplies if (s / "type").is_file() and (s / "type").read_text(encoding="utf-8", errors="replace").strip() == "Mains"]
    for supply in mains:
        try:
            if (supply / "online").read_text(encoding="utf-8", errors="replace").strip() == "1":
                return True
        except OSError:
            continue
    if mains:
        return False
    batteries = [s for s in supplies if (s / "type").is_file() and (s / "type").read_text(encoding="utf-8", errors="replace").strip() == "Battery"]
    return True if not batteries else None  # a desktop without battery is on mains


def _mac_on_ac() -> bool | None:
    out = _quiet_run(["pmset", "-g", "batt"])
    if "AC Power" in out:
        return True
    if "Battery Power" in out:
        return False
    return None


def _linux_cpu_times() -> tuple[int, int] | None:
    try:
        with open("/proc/stat", encoding="ascii") as handle:
            fields = handle.readline().split()[1:]
        values = [int(v) for v in fields[:8]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        return total - idle, total
    except (OSError, ValueError, IndexError):
        return None


def _linux_network() -> bool | None:
    base = Path("/sys/class/net")
    try:
        interfaces = [p for p in base.iterdir() if p.name != "lo"]
    except OSError:
        return None
    for interface in interfaces:
        try:
            if (interface / "operstate").read_text(encoding="utf-8", errors="replace").strip() == "up":
                return True
        except OSError:
            continue
    return False if interfaces else None


class SystemProbes:
    """The real probes, cached for CACHE_S seconds (the scheduler polls twice a second)."""

    def __init__(self, cache_s: float = CACHE_S):
        self.cache_s = cache_s
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, object]] = {}
        self._cpu_last: tuple[int, int] | None = None

    def _cached(self, name: str, compute):
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(name)
            if hit is not None and now - hit[0] < self.cache_s:
                return hit[1]
        try:
            value = compute()
        except Exception:  # noqa: BLE001 - a probe never raises
            value = None
        with self._lock:
            self._cache[name] = (now, value)
        return value

    def idle_seconds(self) -> float | None:
        if IS_WINDOWS:
            return _win_idle_seconds()  # one cheap call: never cached (pause within a second)
        return self._cached("idle", _mac_idle_seconds if IS_MAC else _linux_idle_seconds)

    def on_ac_power(self) -> bool | None:
        return self._cached("ac", _win_on_ac if IS_WINDOWS else _mac_on_ac if IS_MAC else _linux_on_ac)

    def cpu_load(self) -> float | None:
        """0..1 busy share since the previous call (None on the first call)."""
        return self._cached("cpu", self._cpu)

    def _cpu(self) -> float | None:
        if IS_MAC:
            try:
                return min(1.0, os.getloadavg()[0] / max(1, os.cpu_count() or 1))
            except OSError:
                return None
        sample = _win_cpu_times() if IS_WINDOWS else _linux_cpu_times()
        if sample is None:
            return None
        last, self._cpu_last = self._cpu_last, sample
        if last is None:
            return None
        busy, total = sample[0] - last[0], sample[1] - last[1]
        return max(0.0, min(1.0, busy / total)) if total > 0 else None

    def network_available(self) -> bool | None:
        if IS_WINDOWS:
            return self._cached("net", _win_network)
        if IS_MAC:
            return None
        return self._cached("net", _linux_network)
