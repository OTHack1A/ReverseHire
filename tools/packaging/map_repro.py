"""Which application-wide event filter makes the web map crash? (diagnosis helper)

    python tools/packaging/map_repro.py            # every mode, each in its own process
    python tools/packaging/map_repro.py --mode plain

Each mode installs some event filters on the QApplication, then runs the map check
of mapview._selftest ("smoke": loopback tiles, offscreen unless QT_QPA_PLATFORM is
set). Modes: none, plain (an empty Python filter), activity, hover, table_filter,
theme, window_interest, all, main_window (the real main window, shown); "_noquick"
variants block the import of PySide6.QtQuick. Prints one "MODE <name>: exit <code>" line per mode
(-11 / 139 = segmentation fault).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

MODES = ("none", "plain", "activity", "hover", "table_filter", "theme", "window_interest", "all",
         "main_window", "plain_noquick", "main_window_noquick")


def install(mode: str, app) -> list:
    from PySide6.QtCore import QObject

    keep = []
    mode = mode.removesuffix("_noquick")
    if mode == "plain":
        class Plain(QObject):
            def eventFilter(self, obj, event):  # noqa: N802
                return False
        keep.append(Plain(app))
        app.installEventFilter(keep[-1])
    if mode in ("activity", "all"):
        from reversehire.gui import activity
        keep.append(activity.install(app))
    if mode in ("hover", "all"):
        from reversehire.gui import hover
        keep.append(hover.install(app))
    if mode in ("table_filter", "all"):
        from reversehire.gui import table_filter
        keep.append(table_filter.install(app))
    if mode in ("theme", "all"):
        from reversehire.gui import theme
        theme.apply_theme(app, "dark")
    if mode in ("window_interest", "all"):
        from reversehire.gui.window_interest import _ActivityFilter
        keep.append(_ActivityFilter(lambda: None, app))
        app.installEventFilter(keep[-1])
    if mode == "main_window":
        import tempfile

        from reversehire import config
        from reversehire.gui.main_window import MainWindow

        os.environ.setdefault(config.HOME_ENV, tempfile.mkdtemp(prefix="rh-repro-"))
        config.reset_data_location()
        window = MainWindow(config.load())
        window.show()
        keep.append(window)
    return keep


def run_one(mode: str) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
    import faulthandler

    faulthandler.enable(all_threads=True)
    from reversehire.gui import theme

    if mode.endswith("_noquick"):
        class _NoQuick:
            def find_spec(self, name, path=None, target=None):
                if name == "PySide6.QtQuick":
                    raise ImportError("blocked by map_repro")
                return None
        sys.meta_path.insert(0, _NoQuick())
    app = theme.ensure_application([sys.argv[0]])
    keep = install(mode, app)  # noqa: F841 - the filters must stay alive
    from reversehire.mapview import _selftest

    code = _selftest.run("smoke", None, 10)
    sys.stdout.flush()
    os._exit(code)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=MODES)
    args = parser.parse_args(argv)
    if args.mode:
        return run_one(args.mode)
    failed = 0
    for mode in MODES:
        try:
            completed = subprocess.run([sys.executable, __file__, "--mode", mode], capture_output=True,
                                       text=True, encoding="utf-8", errors="replace", timeout=180)
            code = completed.returncode
            tail = completed.stdout.strip().splitlines()[-1:] if completed.stdout else []
        except subprocess.TimeoutExpired:
            code, tail = "timeout", []
        print(f"MODE {mode}: exit {code} {' '.join(tail)[:300]}", flush=True)
        if code != 0 and code != "timeout":
            stderr = completed.stderr
            start = stderr.find("Current thread's C stack")
            print("    " + stderr[start:start + 3000].replace("\n", "\n    ") if start >= 0
                  else "    " + stderr[-1500:].replace("\n", "\n    "), flush=True)
        failed += code != 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
