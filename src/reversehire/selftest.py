"""Start-up self-test of an installed or ready-made copy (``reversehire --self-test``).

    ReverseHire --self-test [--self-test-report <file.json>]

Checks, without the internet and without touching the user's data:

- the bundled pieces load: locales, ONNX runtime with the two bundled models, the
  face detector (OpenCV), the normal OCR engine (RapidOCR), tokenizers, truststore,
  the MCP server library;
- the web map (Qt WebEngine and the Leaflet page) starts, with tiles served by a
  throw-away loopback server (mapview._selftest, mode "smoke");
- the main window is created, the store is opened and the window closes by itself
  after about a second.

The data folder is a new temporary folder (REVERSEHIRE_HOME), unless REVERSEHIRE_HOME
is already set. The window is created offscreen unless QT_QPA_PLATFORM is set.
The result is one JSON object, printed as a "SELF-TEST {json}" line and written to
the report file when one is given (a windowed Windows executable has no console).
Exit code 0 when every check passed, 1 otherwise.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Callable

FLAG = "--self-test"
REPORT_FLAG = "--self-test-report"
WINDOW_CLOSE_MS = 1000


def report_path(argv: list[str]) -> Path | None:
    """The file named after --self-test-report (or --self-test-report=<file>), if any."""
    for index, arg in enumerate(argv):
        if arg.startswith(REPORT_FLAG + "="):
            return Path(arg.split("=", 1)[1])
        if arg == REPORT_FLAG and index + 1 < len(argv):
            return Path(argv[index + 1])
    return None


def _check(results: dict, name: str, function: Callable[[], object]) -> None:
    started = time.monotonic()
    try:
        detail = function()
        results[name] = {"ok": True, "detail": detail}
    except Exception as error:  # noqa: BLE001 - every failure is reported, never raised
        results[name] = {"ok": False, "error": f"{type(error).__name__}: {error}",
                         "trace": traceback.format_exc(limit=6)}
    results[name]["seconds"] = round(time.monotonic() - started, 2)


def _locales() -> dict:
    from . import i18n

    languages = list(i18n.available_languages())
    if len(languages) < 3:
        raise RuntimeError(f"only {languages} found")
    return {"languages": languages}


def _onnx_models() -> dict:
    import onnxruntime

    from .cv import ocr_models

    if not ocr_models.bundled_cyrillic_ok():
        raise RuntimeError("bundled Cyrillic OCR model missing or damaged")
    options = onnxruntime.SessionOptions()
    options.log_severity_level = 3  # errors only
    session = onnxruntime.InferenceSession(str(ocr_models.CYRILLIC_MODEL), options,
                                           providers=["CPUExecutionProvider"])
    return {"onnxruntime": onnxruntime.__version__, "inputs": [i.name for i in session.get_inputs()]}


def _face_detector() -> dict:
    from PIL import Image

    from .cv import photo

    if not photo.yunet_model_path().is_file():
        raise RuntimeError("bundled face detection model missing")
    boxes = photo._yunet_faces(Image.new("RGB", (64, 64), "white"))
    if boxes is None:
        raise RuntimeError("the YuNet face detector cannot run (OpenCV)")
    return {"faces": len(boxes)}


def _ocr() -> dict:
    from .cv import ocr

    engine = ocr.get_engine()
    return {"engine": type(engine).__name__, "provider": ocr.engine_provider(engine)}


def _libraries() -> dict:
    import tokenizers
    import truststore  # noqa: F401
    import pypdf
    import docx  # noqa: F401

    from . import mcp_server

    if getattr(mcp_server, "_ServerClass", None) is None:
        raise RuntimeError("the mcp library is missing: the assistant strip cannot work")
    return {"tokenizers": tokenizers.__version__, "pypdf": pypdf.__version__}


def _web_map() -> dict:
    import contextlib
    import io

    from .gui import theme
    from .mapview import _selftest

    theme.ensure_application([sys.argv[0]])
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = _selftest.run("smoke", None, 10)
    line = next((x for x in output.getvalue().splitlines() if x.startswith("RESULT ")), "RESULT {}")
    result = json.loads(line[len("RESULT "):])
    if code != 0:
        raise RuntimeError(f"map check exit code {code}: {result}")
    return {"backend": result.get("backend"), "markers": result.get("stats", {}).get("markers")}


def _main_window() -> dict:
    from . import app, config

    app.main(close_after_ms=WINDOW_CLOSE_MS)
    folder = config.data_folder()
    store = folder / config.STORE_FILE_NAME
    if not store.is_file():
        raise RuntimeError(f"the store was not created in {folder}")
    return {"store_bytes": store.stat().st_size}


def run(argv: list[str]) -> int:
    import faulthandler

    from . import __version__, config

    if sys.stderr is not None:
        faulthandler.enable(all_threads=True)  # a native crash prints the Python stacks
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
    if not os.environ.get(config.HOME_ENV):
        os.environ[config.HOME_ENV] = tempfile.mkdtemp(prefix="reversehire-selftest-")
    config.reset_data_location()
    results: dict = {
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "frozen": config.is_frozen(),
        "program_folder": str(config.program_folder()),
        "data_folder": os.environ[config.HOME_ENV],
        "qt_platform": os.environ.get("QT_QPA_PLATFORM"),
    }
    checks: dict = {}
    results["checks"] = checks
    target = report_path(argv)

    def write_report() -> None:
        if target is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    for name, function in (("locales", _locales), ("onnx_models", _onnx_models),
                           ("face_detector", _face_detector), ("ocr", _ocr), ("libraries", _libraries),
                           ("web_map", _web_map), ("main_window", _main_window)):
        # Written before each check: after a crash the report names the check that was running.
        results["running"] = name
        results["ok"] = False
        write_report()
        _check(checks, name, function)
    results.pop("running", None)
    results["ok"] = all(check["ok"] for check in checks.values())
    write_report()
    try:
        print("SELF-TEST " + json.dumps(results, default=str), flush=True)
    except (OSError, ValueError, AttributeError):
        pass  # no console (windowed executable): the report file has it
    return 0 if results["ok"] else 1
