"""Model files of the OCR engines: the bundled Cyrillic recogniser and the optional
"advanced reader", plus the choice of the onnxruntime execution provider.

Normal engine (always available, no network): RapidOCR's own PP-OCRv6 small models
(inside the rapidocr wheel) plus the PP-OCRv5 Cyrillic mobile recogniser bundled in
`cv/data/` (8 MB, Apache-2.0, notice in `cv/data/ocr_models_LICENSE.txt`), so that
Ukrainian scans are read.

Advanced reader (optional, downloaded once on the user's request into
`<models folder>/ocr/advanced-v1/`): PP-OCRv6 medium detector, PP-OCRv6 medium Latin
recogniser and the same Cyrillic recogniser (copied from the bundled file, so the
download is about 139 MB of the 147 MB). Files come from the pinned Hugging Face
revisions below, through the app's polite gateway (identifying User-Agent, resumable,
cancellable), and every file is checked against its SHA-256. The folder is looked for
in the same places as the other models (`scoring.embedding.model_search_folders`: the
data folder's `models`, REVERSEHIRE_MODELS, `<repo>/models`, the installed program's
`models`). Explicit model paths are always passed to RapidOCR: without them it would
download missing models from modelscope.cn.

Execution provider: plain `onnxruntime` (CPU) is the dependency. On Windows the
optional `onnxruntime-directml` package provides "DmlExecutionProvider"; it is
detected at run time and checked with one small session, and anything unexpected
falls back to the CPU with one log line. CUDA is not used (the CUDA 13 builds do not
support older GPUs, and DirectML covers any DX12 GPU).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DATA_FOLDER = Path(__file__).with_name("data")
CYRILLIC_MODEL = DATA_FOLDER / "ocr_cyrillic_rec.onnx"
CYRILLIC_KEYS = DATA_FOLDER / "ocr_cyrillic_keys.txt"
LICENSE_FILE = DATA_FOLDER / "ocr_models_LICENSE.txt"
CYRILLIC_SHA256 = "5371ee1ddaa7983cc62d0818d99e982b6804638c85e4f960d59a574094e172e5"

ADVANCED_SUBFOLDER = Path("ocr") / "advanced-v1"
MANIFEST = "manifest.json"
MODEL_FILE = "inference.onnx"
CONFIG_FILE = "inference.yml"
KEYS_FILE = "keys.txt"
LICENSE_NAME = "LICENSE-Apache-2.0.txt"
SOURCE_HOST = "huggingface.co"

# provider override for diagnostics and support: "cpu" or "dml"
PROVIDER_ENV = "REVERSEHIRE_OCR_PROVIDER"
DML = "DmlExecutionProvider"


@dataclass(frozen=True)
class ModelFile:
    """One file of the advanced reader: where it goes, where it comes from, its digest."""

    part: str        # sub-folder: "det", "rec_latin", "rec_cyrillic"
    name: str        # "inference.onnx" or "inference.yml"
    repo: str        # Hugging Face repository
    revision: str    # pinned commit
    size: int
    sha256: str

    @property
    def url(self) -> str:
        return f"https://{SOURCE_HOST}/{self.repo}/resolve/{self.revision}/{self.name}"

    @property
    def relative(self) -> str:
        return f"{self.part}/{self.name}"


_DET = ("PaddlePaddle/PP-OCRv6_medium_det_onnx", "61323801669c338b7891481ec7bac61ce31b576a")
_LAT = ("PaddlePaddle/PP-OCRv6_medium_rec_onnx", "50c7eacafc52fa7bcf4194e8cd08e46f8558504b")
_CYR = ("PaddlePaddle/cyrillic_PP-OCRv5_mobile_rec_onnx", "2cef88145434beb8afa9dd82d77d799eb1ad7b29")

ADVANCED_FILES: tuple[ModelFile, ...] = (
    ModelFile("det", MODEL_FILE, *_DET, 62_032_837,
              "eb13b44b25bb36f89528b68720af8a61d9cf381176107f465db1757b65d086e1"),
    ModelFile("rec_latin", MODEL_FILE, *_LAT, 76_554_979,
              "9c09abf0957f7968c7586464b7397b84ad2387a0497a351af40e9acc71b673ba"),
    ModelFile("rec_latin", CONFIG_FILE, *_LAT, 150_580,
              "991b700facf5b50a7de193468207d5f4255b538dde0d312ae3b7c7a9b6873129"),
    ModelFile("rec_cyrillic", MODEL_FILE, *_CYR, 8_048_799, CYRILLIC_SHA256),
    ModelFile("rec_cyrillic", CONFIG_FILE, *_CYR, 6_991,
              "5c76cc91fa98410178a09f498db10050d0ec1634a660053d3005ab7be581f501"),
)
ADVANCED_BYTES = sum(f.size for f in ADVANCED_FILES)
LICENCE_NAME = "Apache-2.0"


def advanced_download_bytes(bundled: bool | None = None) -> int:
    """Bytes really downloaded: the Cyrillic recogniser is copied from the bundled file."""
    if bundled is None:
        bundled = bundled_cyrillic_ok()
    return sum(f.size for f in ADVANCED_FILES if not (bundled and f.part == "rec_cyrillic"
                                                       and f.name == MODEL_FILE))


class ModelError(Exception):
    """A model file is missing, damaged or could not be downloaded."""


class DownloadCancelled(ModelError):
    """The user cancelled the download (the partial files are kept for a resume)."""


# --- hashing (cached per file state, so readiness checks stay cheap) -----------------------

_hash_cache: dict[tuple[str, int, int], str] = {}
_hash_lock = threading.Lock()


def file_sha256(path: Path, cancel: threading.Event | None = None) -> str:
    stat = path.stat()
    key = (os.path.normcase(str(path.resolve())), stat.st_size, stat.st_mtime_ns)
    with _hash_lock:
        cached = _hash_cache.get(key)
    if cached:
        return cached
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("cancelled")
            chunk = handle.read(4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    value = digest.hexdigest()
    with _hash_lock:
        _hash_cache[key] = value
    return value


# --- bundled Cyrillic recogniser -------------------------------------------------------------

def bundled_cyrillic_ok() -> bool:
    """The bundled Cyrillic model and its character list are present (size checked)."""
    try:
        return (CYRILLIC_MODEL.is_file() and CYRILLIC_KEYS.is_file()
                and CYRILLIC_MODEL.stat().st_size == ADVANCED_FILES[3].size)
    except OSError:
        return False


# --- the advanced reader on disk ---------------------------------------------------------------

def search_folders() -> list[Path]:
    """Model folders searched, in order (same rule as the scoring and translation models)."""
    try:
        from ..scoring.embedding import model_search_folders

        return list(model_search_folders())
    except Exception:  # noqa: BLE001 - the data folder is always a valid answer
        from ..config import data_folder

        return [data_folder() / "models"]


def default_target() -> Path:
    """Where a new download goes: the data folder's models folder."""
    return search_folders()[0] / ADVANCED_SUBFOLDER


def _complete(folder: Path) -> bool:
    try:
        if not (folder / MANIFEST).is_file():
            return False
        for f in ADVANCED_FILES:
            path = folder / f.relative
            if not path.is_file() or path.stat().st_size != f.size:
                return False
        return all((folder / part / KEYS_FILE).is_file() for part in ("rec_latin", "rec_cyrillic"))
    except OSError:
        return False


def find_advanced(folders: list[Path] | None = None) -> Path | None:
    """The first complete advanced-reader folder (all files with the right sizes)."""
    for root in folders if folders is not None else search_folders():
        folder = Path(root) / ADVANCED_SUBFOLDER
        if _complete(folder):
            return folder
    return None


def verify_folder(folder: Path, cancel: threading.Event | None = None) -> list[str]:
    """Files of `folder` whose SHA-256 does not match (empty = all good)."""
    bad = []
    for f in ADVANCED_FILES:
        path = folder / f.relative
        try:
            if path.stat().st_size != f.size or file_sha256(path, cancel) != f.sha256:
                bad.append(f.relative)
        except OSError:
            bad.append(f.relative)
    return bad


@dataclass
class AdvancedPaths:
    det: Path
    rec_latin: Path
    keys_latin: Path
    rec_cyrillic: Path
    keys_cyrillic: Path


_verified: dict[str, bool] = {}


def advanced_paths(folders: list[Path] | None = None) -> AdvancedPaths | None:
    """Paths of a complete and checksum-verified advanced reader, or None.

    The full SHA-256 check runs once per folder and process (about half a second)."""
    folder = find_advanced(folders)
    if folder is None:
        return None
    key = os.path.normcase(str(folder.resolve()))
    ok = _verified.get(key)
    if ok is None:
        bad = verify_folder(folder)
        ok = not bad
        if bad:
            log.warning("Advanced OCR models in %s are damaged (%s): not used", folder, ", ".join(bad))
        _verified[key] = ok
    if not ok:
        return None
    return AdvancedPaths(folder / "det" / MODEL_FILE, folder / "rec_latin" / MODEL_FILE,
                         folder / "rec_latin" / KEYS_FILE, folder / "rec_cyrillic" / MODEL_FILE,
                         folder / "rec_cyrillic" / KEYS_FILE)


def advanced_ready(folders: list[Path] | None = None) -> bool:
    return advanced_paths(folders) is not None


def folder_bytes(folder: Path) -> int:
    total = 0
    for path in folder.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            pass
    return total


@dataclass
class AdvancedStatus:
    """What Settings and Diagnostics show about the advanced reader."""

    installed: bool
    folder: Path | None
    size_bytes: int
    provider: str           # "dml" or "cpu"
    download_bytes: int     # bytes a download would fetch


def advanced_status(folders: list[Path] | None = None, provider: str | None = None) -> AdvancedStatus:
    folder = find_advanced(folders)
    return AdvancedStatus(folder is not None, folder, folder_bytes(folder) if folder is not None else 0,
                          provider or best_provider(), advanced_download_bytes())


def keys_from_yaml(text: str) -> str:
    """The character list of a PaddleX inference.yml as a RapidOCR keys file (one per line)."""
    import yaml

    data = yaml.safe_load(text)
    try:
        characters = data["PostProcess"]["character_dict"]
    except (KeyError, TypeError) as error:
        raise ModelError("inference.yml has no character list") from error
    if not isinstance(characters, list) or not characters or not all(isinstance(c, str) for c in characters):
        raise ModelError("inference.yml has an unexpected character list")
    return "\n".join(characters)


# --- download --------------------------------------------------------------------------------

# fetch(url, target, cancel, progress): downloads `url` to `target` (resuming its own
# "<target>.part" when present); progress(done_bytes, total_bytes_or_None).
Fetcher = Callable[[str, Path, threading.Event | None, Callable[[int, int | None], None]], None]
Progress = Callable[[int, int], None]  # (bytes done, bytes total) over the whole download


def gateway_fetcher(settings=None, gateway=None, **download_options) -> tuple[Fetcher, Callable[[], None]]:
    """A fetcher through the app's polite gateway (User-Agent, pacing, resumable, throttled),
    and the function that closes it (a given `gateway` is left open)."""
    from ..discovery.gateway import Cancelled, GatewayError, PoliteGateway

    own = gateway is None
    gateway = gateway if gateway is not None else PoliteGateway(settings, None)

    def fetch(url, target, cancel, progress) -> None:
        try:
            gateway.download(url, target, cancel=cancel, progress=progress, **download_options)
        except Cancelled as error:
            raise DownloadCancelled("cancelled") from error
        except GatewayError as error:
            raise ModelError(f"download failed: {error}") from error

    return fetch, (gateway.close if own else (lambda: None))


def download_advanced(target: Path | None = None, *, fetch: Fetcher | None = None,
                      progress: Progress | None = None, cancel: threading.Event | None = None,
                      settings=None) -> Path:
    """Downloads (or completes) the advanced reader in `target` and returns the folder.

    Files already present with the right SHA-256 are kept; an interrupted file is
    resumed by the fetcher from its ".part"; a file with the wrong digest is deleted
    and ModelError raised (nothing half-checked is ever used: the manifest, which
    marks the folder complete, is written last). DownloadCancelled when `cancel` is set.
    Never called during a CV analysis: only from an explicit user action."""
    target = Path(target) if target is not None else default_target()
    own_close = None
    if fetch is None:
        fetch, own_close = gateway_fetcher(settings)
    try:
        target.mkdir(parents=True, exist_ok=True)
        (target / MANIFEST).unlink(missing_ok=True)  # incomplete until the end
        bundled = bundled_cyrillic_ok()
        todo: list[ModelFile] = []
        for f in ADVANCED_FILES:
            path = target / f.relative
            if path.is_file() and path.stat().st_size == f.size and file_sha256(path, cancel) == f.sha256:
                continue
            if bundled and f.part == "rec_cyrillic" and f.name == MODEL_FILE:
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(CYRILLIC_MODEL, path)
                if file_sha256(path) == f.sha256:
                    continue
                path.unlink(missing_ok=True)
            todo.append(f)
        total = sum(f.size for f in todo)
        base = 0

        def report(done: int) -> None:
            if progress is not None:
                try:
                    progress(min(total, done), total)
                except Exception:  # noqa: BLE001 - a broken callback must not stop the download
                    log.debug("Download progress callback failed", exc_info=True)

        report(0)
        for f in todo:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("cancelled")
            path = target / f.relative
            path.parent.mkdir(parents=True, exist_ok=True)
            log.info("Downloading the advanced OCR model file %s (%d bytes)", f.url, f.size)
            start = base
            fetch(f.url, path, cancel, lambda done, _total, start=start: report(start + done))
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("cancelled")
            if not path.is_file():
                raise ModelError(f"{f.relative}: the download produced no file")
            digest = file_sha256(path, cancel)
            if path.stat().st_size != f.size or digest != f.sha256:
                path.unlink(missing_ok=True)
                raise ModelError(f"{f.relative}: checksum mismatch (got {digest[:12]}, expected {f.sha256[:12]})")
            base += f.size
            report(base)
        for part in ("rec_latin", "rec_cyrillic"):
            text = (target / part / CONFIG_FILE).read_text(encoding="utf-8")
            (target / part / KEYS_FILE).write_text(keys_from_yaml(text), encoding="utf-8", newline="\n")
        # the det folder needs no character list; keep its yml out (not needed, not fetched)
        if LICENSE_FILE.is_file():
            shutil.copyfile(LICENSE_FILE, target / LICENSE_NAME)
        manifest = {
            "name": "advanced-v1",
            "licence": LICENCE_NAME,
            "files": [{"path": f.relative, "size": f.size, "sha256": f.sha256, "url": f.url,
                       "repository": f.repo, "revision": f.revision} for f in ADVANCED_FILES],
        }
        (target / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _verified.pop(os.path.normcase(str(target.resolve())), None)
        log.info("Advanced OCR models ready in %s", target)
        return target
    finally:
        if own_close is not None:
            own_close()


def remove_advanced(folder: Path | None = None) -> Path | None:
    """Deletes the advanced reader's folder (the one found, or `folder`); returns it."""
    folder = folder if folder is not None else find_advanced()
    if folder is None:
        return None
    folder = Path(folder)
    if folder.name != ADVANCED_SUBFOLDER.name or folder.parent.name != ADVANCED_SUBFOLDER.parent.name:
        raise ModelError(f"not an advanced OCR folder: {folder}")
    shutil.rmtree(folder)
    _verified.pop(os.path.normcase(str(folder.resolve())), None)
    log.info("Advanced OCR models removed from %s", folder)
    return folder


# --- execution provider ---------------------------------------------------------------------

_provider: str | None = None
_provider_lock = threading.Lock()


def onnxruntime_distributions() -> list[str]:
    """Installed onnxruntime distributions ("onnxruntime", "onnxruntime-directml", ...)."""
    from importlib import metadata

    names = []
    for name in ("onnxruntime", "onnxruntime-directml", "onnxruntime-gpu", "onnxruntime-openvino"):
        try:
            metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
        except Exception:  # noqa: BLE001
            continue
        names.append(name)
    return names


def available_providers() -> list[str]:
    try:
        import onnxruntime

        return list(onnxruntime.get_available_providers())
    except Exception:  # noqa: BLE001 - onnxruntime missing or broken
        return []


def _dml_smoke_test() -> None:
    """One small DirectML session on the bundled Cyrillic model, one inference."""
    import numpy as np
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.log_severity_level = 3
    session = ort.InferenceSession(str(CYRILLIC_MODEL), options, providers=[DML, "CPUExecutionProvider"])
    if DML not in session.get_providers():
        raise RuntimeError(f"session providers: {session.get_providers()}")
    name = session.get_inputs()[0].name
    session.run(None, {name: np.zeros((1, 3, 48, 160), dtype=np.float32)})


def best_provider(*, refresh: bool = False) -> str:
    """"dml" when DirectML is installed and a test session works, else "cpu" (cached)."""
    global _provider
    with _provider_lock:
        if _provider is not None and not refresh:
            return _provider
        forced = os.environ.get(PROVIDER_ENV, "").strip().lower()
        choice = "cpu"
        if forced == "cpu":
            choice = "cpu"
        elif sys.platform == "win32" and DML in available_providers() and CYRILLIC_MODEL.is_file():
            try:
                _dml_smoke_test()
                choice = "dml"
            except Exception as error:  # noqa: BLE001 - any failure means CPU
                log.info("DirectML is installed but a test session failed (%s): OCR runs on the CPU",
                         error)
        _provider = choice
        return choice


def reset_provider() -> None:
    global _provider
    with _provider_lock:
        _provider = None
