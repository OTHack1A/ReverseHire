"""Checks that explain why the scoring model, the OCR or the online services do not
work on a PC ("? > Diagnostics…" in the app, and the installer's self-test).

Every check returns a `Check` (PASS / WARN / FAIL / INFO / SKIP) and never
raises. Nothing here reads or prints the CV: only versions, folder paths, file
sizes, registry values and HTTP status codes. Network calls and the model load
can be replaced in tests (`http_get`, `load_model`).

Command line (used by tools/installer/install.ps1 after installing):

    python -m reversehire.diagnostics --selftest     # onnxruntime, offline model, OCR
    python -m reversehire.diagnostics                # every check, network included

Exit code of --selftest: 0 all passed, 2 only the model failed (the app still
works, with reduced accuracy), 1 something else failed.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

PASS, WARN, FAIL, INFO, SKIP = "PASS", "WARN", "FAIL", "INFO", "SKIP"

# Order of the checks; the GUI translates the titles as "diagnostics.check.<id>"
# when such a key exists, else shows TITLES.
TITLES = {
    "app": "ReverseHire and Python",
    "data_folder": "Data folder",
    "packaged_copies": "Data folders written by packaged apps",
    "vc_runtime": "Microsoft Visual C++ runtime",
    "smart_app_control": "Smart App Control",
    "onnxruntime": "Scoring engine (onnxruntime)",
    "embedding_model": "Embedding model",
    "model_folders": "Model folders",
    "model_offline": "Model loaded offline and one embedding",
    "ocr": "Text recognition (OCR): engines, models and execution provider",
    "certificates": "System certificate store",
    "net_huggingface": "Internet: huggingface.co (model download)",
    "net_nominatim": "Internet: Nominatim (addresses)",
    "net_overpass": "Internet: Overpass (companies on the map)",
    "net_esco": "Internet: ESCO (skills of occupations)",
}
SELFTEST_IDS = ("app", "data_folder", "vc_runtime", "onnxruntime", "model_folders", "model_offline", "ocr")
VC_RUNTIME_FILES = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")
SAC_KEY = r"SYSTEM\CurrentControlSet\Control\CI\Policy"
SAC_VALUE = "VerifiedAndReputablePolicyState"
SAC_STATES = {0: "off", 1: "on (enforced)", 2: "evaluation"}
HTTP_TIMEOUT_S = 10.0
NOMINATIM_STATUS_URL = "https://nominatim.openstreetmap.org/status?format=json"
ESCO_URL = "https://ec.europa.eu/esco/api/search?text=accountant&language=en&type=occupation&limit=1"
PROBE_TEXT = "query: diagnostics"


@dataclass
class Check:
    id: str
    status: str
    detail: str = ""

    @property
    def title(self) -> str:
        return TITLES.get(self.id, self.id)


def _safe(check_id: str, function: Callable[[], Check]) -> Check:
    try:
        return function()
    except Exception as error:  # a check never stops the others
        log.warning("Diagnostic check %s failed", check_id, exc_info=True)
        return Check(check_id, FAIL, f"{type(error).__name__}: {error}")


# --- individual checks ---------------------------------------------------------------


def check_app() -> Check:
    from . import __version__

    from . import BUILD_DATE, build_number

    built = f", built {BUILD_DATE}" if BUILD_DATE else ""
    return Check("app", INFO, f"ReverseHire {__version__} (build {build_number()}{built}); "
                              f"Python {sys.version.split()[0]} ({sys.executable})")


def real_data_folder() -> Path:
    from .config import data_folder

    return data_folder().resolve()


def check_data_folder() -> Check:
    """Where the data folder is, why, whether it can be written, the database file and
    its journal mode, and a warning for temporary or cloud-synchronised folders."""
    from .config import (MODE_TEMP, STORE_FILE_NAME, cloud_sync_provider, data_location,
                         is_writable_folder)

    location = data_location()
    folder = location.folder.resolve()
    writable = is_writable_folder(folder)
    explicit = os.environ.get("REVERSEHIRE_HOME")
    note = f" (REVERSEHIRE_HOME={explicit})" if explicit else ""
    lines = [f"{folder}{note}; {'writable' if writable else 'NOT writable'}",
             f"chosen because: {location.reason}"]
    if location.program_folder is not None:
        lines.append(f"program folder: {location.program_folder}")
    if location.migrated_from is not None:
        lines.append(f"settings and store copied on this start from {location.migrated_from}")
    store = folder / STORE_FILE_NAME
    if store.is_file():
        lines.append(f"database: {store} ({_size(store)}, journal mode {_journal_mode(store)})")
    status = PASS if writable else FAIL
    if location.mode == MODE_TEMP and writable:
        status = WARN
        lines.append("WARNING: temporary folder, its content may be deleted by the system. "
                     "Set REVERSEHIRE_HOME to a writable folder.")
    provider = cloud_sync_provider(folder)
    if provider:
        status = WARN if status == PASS else status
        lines.append(f"WARNING: the folder is synchronised by {provider}. The sync client may upload "
                     "or replace store.sqlite while ReverseHire writes it (conflicting copies, lost "
                     "searches). Prefer a folder outside {provider}, or pause the sync while the app "
                     "runs; set REVERSEHIRE_HOME to move the data.".replace("{provider}", provider))
    return Check("data_folder", status, "\n".join(lines))


def _journal_mode(store: Path) -> str:
    """Journal mode of a database, read without changing anything."""
    import sqlite3

    try:
        connection = sqlite3.connect(f"{store.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).upper()
        finally:
            connection.close()
    except (sqlite3.Error, OSError, ValueError) as error:
        return f"unknown ({type(error).__name__})"


def packaged_copies() -> list[Path]:
    """`%LOCALAPPDATA%\\Packages\\*\\LocalCache\\Roaming\\ReverseHire` (and `Local`) folders:
    written by a program started from a packaged (MSIX) app, whose AppData writes are
    redirected there; the app started normally does not see them."""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    root = Path(local) / "Packages"
    try:
        found = [p for pattern in ("*/LocalCache/Roaming/ReverseHire", "*/LocalCache/Local/ReverseHire")
                 for p in root.glob(pattern) if p.is_dir()]
        return sorted(found)
    except OSError:
        return []


def check_packaged_copies(package_name: Callable[[], str | None] | None = None) -> Check:
    if sys.platform != "win32":
        return Check("packaged_copies", SKIP, "not Windows")
    from .config import packaged_app_name

    package = (package_name or packaged_app_name)()
    copies = packaged_copies()
    lines = []
    if package:
        lines.append(f"This program runs inside the packaged app {package}: what it writes under "
                     "AppData may be redirected to that app's private folder. The portable data "
                     "folder in the program folder avoids it.")
    if not copies and not package:
        return Check("packaged_copies", PASS, "none")
    for copy in copies:
        has_models = (copy / "models").is_dir()
        lines.append(f"{copy}{' (contains models)' if has_models else ''}")
    if copies:
        lines.insert(1 if package else 0,
                     "These folders were written by a program started from a packaged app, whose "
                     "AppData is redirected; the app does not use them. A models folder there can be "
                     "copied into the data folder:")
    return Check("packaged_copies", WARN, "\n".join(lines))


def check_vc_runtime() -> Check:
    if sys.platform != "win32":
        return Check("vc_runtime", SKIP, "not Windows")
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    missing = [name for name in VC_RUNTIME_FILES if not (system32 / name).is_file()]
    if missing:
        from .scoring.embedding import VC_REDIST_URL

        return Check("vc_runtime", FAIL, f"missing in {system32}: {', '.join(missing)}. "
                                         f"Install {VC_REDIST_URL}")
    return Check("vc_runtime", PASS, f"{', '.join(VC_RUNTIME_FILES)} present in {system32}")


def smart_app_control_state(reader: Callable[[], int | None] | None = None) -> str | None:
    """"off", "on (enforced)", "evaluation", or None when not readable."""
    if reader is None:
        if sys.platform != "win32":
            return None

        def reader():
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, SAC_KEY) as key:
                value, _kind = winreg.QueryValueEx(key, SAC_VALUE)
            return int(value)
    try:
        value = reader()
    except Exception:
        return None
    if value is None:
        return None
    return SAC_STATES.get(value, f"unknown value {value}")


def check_smart_app_control(reader=None) -> Check:
    state = smart_app_control_state(reader)
    if state is None:
        return Check("smart_app_control", SKIP, "state not readable")
    status = WARN if state != "off" else PASS
    note = (" Unsigned program files (DLLs) may be blocked: if the scoring engine fails with "
            "'blocked', check Windows Security > App & browser control.") if status == WARN else ""
    return Check("smart_app_control", status, state + "." + note)


def check_onnxruntime() -> Check:
    from .scoring.embedding import classify_load_error

    try:
        onnxruntime = importlib.import_module("onnxruntime")
    except Exception as error:
        return Check("onnxruntime", FAIL, f"cause: {classify_load_error(error)}; "
                                          f"{type(error).__name__}: {error}")
    providers = ", ".join(onnxruntime.get_available_providers())
    return Check("onnxruntime", PASS, f"version {onnxruntime.__version__}; providers: {providers}")


def _size(path: Path) -> str:
    try:
        return f"{path.stat().st_size / 1024 ** 2:.1f} MB"
    except OSError:
        return "?"


def check_embedding_model() -> Check:
    """The embedding model's name and whether a complete copy is in a model folder
    (moved here from the About dialog), without loading it."""
    from .scoring.embedding import PREFERRED_MODEL, find_local_model, model_search_folders

    local = find_local_model(PREFERRED_MODEL, model_search_folders())
    if local is None:
        return Check("embedding_model", WARN, f"{PREFERRED_MODEL}: not downloaded yet "
                                              "(it is downloaded at the first search)")
    return Check("embedding_model", PASS, f"{PREFERRED_MODEL}: downloaded, found in {local.folder.resolve()}")


def check_model_folders() -> Check:
    from .scoring.embedding import MODEL_SOURCES, find_local_model, model_search_folders

    lines = []
    used = None
    for folder in model_search_folders():
        try:
            exists = folder.is_dir()
        except OSError:
            exists = False
        if not exists:
            lines.append(f"{folder}: missing")
            continue
        found = [find_local_model(name, [folder]) for name in MODEL_SOURCES]
        found = [f for f in found if f is not None]
        if not found:
            lines.append(f"{folder.resolve()}: exists, no complete model")
            continue
        for local in found:
            used = used or local
            files = []
            for item in sorted(local.snapshot.rglob("*")):
                if item.is_file():
                    readonly = "" if os.access(item, os.W_OK) else " read-only"
                    files.append(f"    {item.relative_to(local.snapshot)}  {_size(item)}{readonly}")
            lines.append(f"{local.folder.resolve()}: {local.model} in {local.snapshot}")
            lines.extend(files)
    if used is None:
        return Check("model_folders", FAIL, "no folder holds the model (it would be downloaded "
                                            "at the first search):\n" + "\n".join(lines))
    return Check("model_folders", PASS, f"used: {used.folder.resolve()}\n" + "\n".join(lines))


def load_model_offline():
    """Load the model from local files only (HF_HUB_OFFLINE=1) and embed one text.

    Returns (embedder, vector) ; raises the load error."""
    from .scoring.embedding import CAUSE_NETWORK, FALLBACK_MODEL, FastembedEmbedder, ModelLoadError

    previous = os.environ.get("HF_HUB_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        embedder = FastembedEmbedder(store=None, probe=None)
        if embedder.local_model() is None and embedder.local_model(FALLBACK_MODEL) is None:
            missing = FileNotFoundError("no local copy of the model: it would have to be downloaded "
                                        "(about 470 MB) at the first search")
            raise ModelLoadError(str(missing), CAUSE_NETWORK, [("model files", missing)],
                                 embedder.search_folders())
        vector = embedder.embed([PROBE_TEXT], "query")
        return embedder, vector
    finally:
        if previous is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = previous


def check_model_offline(load_model: Callable | None = None) -> Check:
    from .scoring.embedding import failure_cause, failure_details

    start = time.monotonic()
    try:
        embedder, vector = (load_model or load_model_offline)()
    except Exception as error:
        return Check("model_offline", FAIL, f"cause: {failure_cause(error)}\n{failure_details(error)}")
    seconds = time.monotonic() - start
    folder = getattr(embedder, "source_folder", None)
    return Check("model_offline", PASS,
                 f"{getattr(embedder, 'model_name', '?')} from {folder}; vector of "
                 f"{vector.shape[-1]} dimensions in {seconds:.1f} s")


def check_ocr() -> Check:
    """The normal engine, the bundled Cyrillic model, the advanced reader and the
    onnxruntime execution provider (one line each)."""
    errors = []
    for name in ("rapidocr", "rapidocr_onnxruntime"):
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        except Exception as error:
            errors.append(f"{name}: {type(error).__name__}: {error}")
            continue
        version = getattr(module, "__version__", "")
        if not version:
            try:
                from importlib import metadata

                version = metadata.version(name)
            except Exception:  # noqa: BLE001 - the version is only informative
                version = ""
        lines, status = _ocr_details()
        return Check("ocr", status, "\n".join([f"normal engine: {name} {version}".strip(), *lines]))
    if errors:
        return Check("ocr", FAIL, "; ".join(errors))
    return Check("ocr", FAIL, "rapidocr is not installed")


def _ocr_details() -> tuple[list[str], str]:
    from .cv import ocr_models

    status = PASS
    lines = []
    if ocr_models.bundled_cyrillic_ok():
        lines.append(f"Cyrillic model: bundled ({ocr_models.CYRILLIC_MODEL.name})")
    else:
        lines.append("Cyrillic model: MISSING (Ukrainian scans cannot be read)")
        status = WARN
    found = ocr_models.find_advanced()
    if found is None:
        lines.append(f"advanced engine: not installed (download of "
                     f"{ocr_models.advanced_download_bytes() / 1_000_000:.0f} MB on request)")
    elif ocr_models.advanced_paths() is None:
        lines.append(f"advanced engine: DAMAGED files in {found} (checksum mismatch: download again)")
        status = WARN
    else:
        lines.append(f"advanced engine: installed in {found} "
                     f"({ocr_models.folder_bytes(found) / 1_000_000:.0f} MB)")
    provider = ocr_models.best_provider()
    available = ", ".join(ocr_models.available_providers()) or "none"
    lines.append(f"execution provider: {'DirectML (GPU)' if provider == 'dml' else 'CPU'} "
                 f"(onnxruntime providers: {available})")
    distributions = ocr_models.onnxruntime_distributions()
    if len(distributions) > 1:
        lines.append("several onnxruntime packages installed (" + ", ".join(distributions) +
                     "): they share one folder; keep only one (pip uninstall the others, then reinstall it)")
        status = WARN
    return lines, status


def check_certificates() -> Check:
    from .scoring.embedding import use_system_certificates

    if use_system_certificates():
        store = {"win32": "Windows certificate store", "darwin": "macOS Keychain"}.get(
            sys.platform, "system CA certificates (OpenSSL)")
        return Check("certificates", PASS, f"HTTPS trusts the {store} (truststore)")
    return Check("certificates", WARN, "truststore not available: HTTPS uses the bundled certifi "
                                       "list only (fails behind HTTPS-scanning antivirus or proxies)")


def _default_get(url: str, timeout: float, user_agent: str) -> int:
    import httpx

    response = httpx.get(url, timeout=timeout, follow_redirects=True,
                         headers={"User-Agent": user_agent})
    return response.status_code


def overpass_status_url(server: str) -> str:
    base = server.rstrip("/")
    if base.endswith("/interpreter"):
        return base[: -len("interpreter")] + "status"
    return base


def _local_map_ready(settings) -> bool:
    try:
        from .discovery import osm_extract
        from .geo.regions import region_for

        region = region_for(getattr(settings, "osm_region", "emilia-romagna"))
        return region is not None and osm_extract.status(region).ready
    except Exception:  # noqa: BLE001 - a check never raises
        return False


def _overpass_check(settings, get: Callable) -> Check:
    """The Overpass servers the search really uses (same order as the discovery), the
    first that answers is enough. Overpass is only the fallback of the local map data:
    with the map data downloaded, no answer is a warning, not a failure."""
    try:
        from .discovery.network_sources import endpoints_for

        servers = endpoints_for(settings)
    except Exception:  # noqa: BLE001
        servers = [settings.overpass_server]
    problems = []
    busy = False
    for server in servers[:3]:
        url = overpass_status_url(server)
        start = time.monotonic()
        try:
            code = get(url, HTTP_TIMEOUT_S, settings.user_agent)
        except Exception as error:  # noqa: BLE001
            problems.append(f"{url}: {type(error).__name__}")
            continue
        if code < 400:
            return Check("net_overpass", PASS, f"{url}: HTTP {code} in {time.monotonic() - start:.1f} s")
        busy = busy or code in (429, 503, 504)
        problems.append(f"{url}: HTTP {code}")
    if busy or _local_map_ready(settings):
        return Check("net_overpass", WARN, "no Overpass server answered (" + "; ".join(problems) +
                     "); not needed while the local map data is downloaded")
    return Check("net_overpass", FAIL, "no Overpass server answered (" + "; ".join(problems) + ")")


def network_checks(settings=None, http_get: Callable | None = None) -> list[Check]:
    from .config import Settings
    from .scoring.embedding import PROBE_URL, classify_load_error

    settings = settings or Settings()
    get = http_get or _default_get
    targets = [
        ("net_huggingface", PROBE_URL),
        ("net_nominatim", NOMINATIM_STATUS_URL),
        ("net_esco", ESCO_URL),
    ]
    results = [_overpass_check(settings, get)]
    for check_id, url in targets:
        start = time.monotonic()
        try:
            code = get(url, HTTP_TIMEOUT_S, settings.user_agent)
        except Exception as error:
            cause = classify_load_error(error)
            label = {"certificate": "certificate refused (HTTPS scanning antivirus or proxy?)",
                     "network": "no connection"}.get(cause, cause)
            results.append(Check(check_id, FAIL, f"{url}: {label}; {type(error).__name__}: {str(error)[:200]}"))
            continue
        seconds = time.monotonic() - start
        status = PASS if code < 400 else WARN if code in (429, 503, 504) else FAIL
        results.append(Check(check_id, status, f"{url}: HTTP {code} in {seconds:.1f} s"))
    return results


# --- all checks -------------------------------------------------------------------


def run_checks(settings=None, *, online: bool = True, ids: tuple[str, ...] | None = None,
               http_get: Callable | None = None, load_model: Callable | None = None,
               sac_reader: Callable | None = None,
               progress: Callable[[Check], None] | None = None) -> list[Check]:
    """Runs the checks in order (all of them, or `ids`); network ones only if `online`."""
    plan: list[tuple[str, Callable[[], Check | list[Check]]]] = [
        ("app", check_app),
        ("data_folder", check_data_folder),
        ("packaged_copies", check_packaged_copies),
        ("vc_runtime", check_vc_runtime),
        ("smart_app_control", lambda: check_smart_app_control(sac_reader)),
        ("onnxruntime", check_onnxruntime),
        ("embedding_model", check_embedding_model),
        ("model_folders", check_model_folders),
        ("model_offline", lambda: check_model_offline(load_model)),
        ("ocr", check_ocr),
        ("certificates", check_certificates),
    ]
    if online:
        plan.append(("network", lambda: network_checks(settings, http_get)))
    results: list[Check] = []
    for check_id, function in plan:
        if ids is not None and check_id not in ids:
            continue
        try:
            outcome = function()
        except Exception as error:
            outcome = Check(check_id, FAIL, f"{type(error).__name__}: {error}")
        for check in outcome if isinstance(outcome, list) else [outcome]:
            results.append(check)
            if progress is not None:
                try:
                    progress(check)
                except Exception:
                    pass
    return results


def counts(checks: list[Check]) -> tuple[int, int]:
    """(failed, warnings)."""
    return (sum(1 for c in checks if c.status == FAIL), sum(1 for c in checks if c.status == WARN))


def as_text(checks: list[Check], titles: dict[str, str] | None = None) -> str:
    """Plain text for the Copy button: "[PASS] title: detail" (details indented)."""
    names = titles or {}
    lines = []
    for check in checks:
        title = names.get(check.id) or check.title
        detail_lines = (check.detail or "").splitlines() or [""]
        lines.append(f"[{check.status}] {title}: {detail_lines[0]}".rstrip())
        lines.extend(f"    {line}" for line in detail_lines[1:])
    return "\n".join(lines)


def selftest_exit_code(checks: list[Check]) -> int:
    failed = {c.id for c in checks if c.status == FAIL}
    if not failed:
        return 0
    if failed <= {"model_offline", "model_folders"}:
        return 2
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m reversehire.diagnostics")
    parser.add_argument("--selftest", action="store_true",
                        help="only the installation checks (no network)")
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    if arguments.selftest:
        checks = run_checks(online=False, ids=SELFTEST_IDS)
    else:
        from .config import load

        checks = run_checks(load())
    print(as_text(checks))
    code = selftest_exit_code(checks)
    failed, warnings = counts(checks)
    print(f"RESULT: {'PASS' if code == 0 else 'WARN' if code == 2 else 'FAIL'} "
          f"({failed} failed, {warnings} warnings)")
    return code


if __name__ == "__main__":
    sys.exit(main())
