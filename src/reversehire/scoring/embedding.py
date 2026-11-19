"""Local multilingual embeddings (fastembed/ONNX), with a vector cache.

Chosen model: ``intfloat/multilingual-e5-small`` (384 dimensions, ~100 languages,
~470 MB ONNX file). fastembed 0.9 does not include it in its registry, so we
register it as a custom model pointing at the ``onnx/model.onnx`` file published
in the official Hugging Face repository (MEAN pooling + normalization, as in the
model card). If registration or loading fails we fall back to
``sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`` (in the fastembed
registry, ~220 MB, 50 languages, but weaker at retrieval).

Where the model comes from (``model_search_folders``), first match wins:
``<data folder>/models``, the folder in ``REVERSEHIRE_MODELS``, ``<repo>/models``
of a source checkout, ``models`` next to the installed program
(``%LOCALAPPDATA%\\Programs\\ReverseHire\\models`` on Windows, or next to its ``venv``)
and, last, ``models`` in the data folder of older versions (``%APPDATA%\\ReverseHire``,
``~/.reversehire``). A
folder may hold the Hugging Face cache layout (``models--org--name/snapshots/...``,
as copied by the installer) or a plain folder named after the model. When the
files are found the model is loaded straight from that snapshot
(``specific_model_path`` + ``local_files_only``): no network request and no
write into that folder, so a read-only copy works. Only when no folder has the
files is the model downloaded into ``<data folder>/models``.

When no model can be loaded at all, ``ModelLoadError`` carries a cause
(``classify_load_error``: runtime DLL missing, blocked by Windows security
policy, no connection, certificate refused, damaged files, out of memory,
folder not writable, unknown) and a diagnostic text without any CV content.
Callers then switch to ``LexicalEmbedder``: hashed word and character n-grams,
deterministic, multilingual-agnostic, no downloads, with its own similarity
ranges, so the scoring code keeps working with reduced accuracy.
``ResilientEmbedder`` does that switch by itself and retries the real model
after ``RETRY_AFTER_S``.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import ssl
import sys
import threading
import time
import unicodedata
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

import numpy as np

log = logging.getLogger(__name__)

# --- constants --------------------------------------------------------------

PREFERRED_MODEL = "intfloat/multilingual-e5-small"
FALLBACK_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Registration of the preferred model as a fastembed custom model.
_E5_SMALL_HF = "intfloat/multilingual-e5-small"
_E5_SMALL_FILE = "onnx/model.onnx"
_E5_SMALL_DIM = 384
_E5_SMALL_GB = 0.47

# Typical range of cosine similarities per model: (low, high).
# "low" ~ texts from different fields, "high" ~ texts on the same topic.
# Values measured with the real-model test (accountant profile, "query:" on
# both sides, best block; see test_scoring_real_model.py):
#   e5-small: Italian accounting firm 0.869, workshop 0.813, pizzeria 0.786,
#             English accounting firm 0.808 (the IT->EN comparison is weak).
#   MiniLM:   Italian firm 0.423, English firm 0.267, workshop 0.157, pizzeria 0.153.
SIMILARITY_RANGES: dict[str, tuple[float, float]] = {
    PREFERRED_MODEL: (0.80, 0.88),
    FALLBACK_MODEL: (0.15, 0.50),
}
# Short titles (posting title vs CV role): with e5-small even unrelated
# titles sit at 0.82-0.88 (e.g. "Magazziniere"/"Contabile" 0.876), related ones
# at 0.88-0.90, so the range is higher and narrower.
TITLE_RANGES: dict[str, tuple[float, float]] = {
    PREFERRED_MODEL: (0.86, 0.93),
    FALLBACK_MODEL: (0.45, 0.85),
}
# Posting requirement sentence vs CV evidence line (both "query:"), see
# scoring/requirements.py; measured with test_scoring_real_model.py.
REQUIREMENT_RANGES: dict[str, tuple[float, float]] = {
    PREFERRED_MODEL: (0.85, 0.93),  # related 0.88-0.93, unrelated 0.81-0.87 (one at 0.868)
    FALLBACK_MODEL: (0.25, 0.65),
}
DEFAULT_RANGE = (0.0, 1.0)

# Texts longer than this are truncated before embedding
# (e5 accepts at most 512 tokens, ~2000 characters of Italian text).
MAX_TEXT_CHARS = 2000
# Length of the blocks long texts are split into (see split_into_blocks).
CHARS_PER_BLOCK = 400
BATCH_SIZE = 32
MAX_MEMORY_ENTRIES = 5000  # vectors also kept in memory (besides the Store)
CACHE_NAMESPACE = "embedding"
MODELS_SUBFOLDER = "models"

VALID_KINDS = ("query", "passage")

# Files of each model: (Hugging Face repo the files come from, ONNX file).
MODEL_SOURCES: dict[str, tuple[str, str]] = {
    PREFERRED_MODEL: (_E5_SMALL_HF, _E5_SMALL_FILE),
    FALLBACK_MODEL: ("Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q", "model_optimized.onnx"),
}
# Besides the ONNX file, fastembed needs these to build the tokenizer.
REQUIRED_FILES = ("tokenizer.json", "tokenizer_config.json")
MODELS_ENV = "REVERSEHIRE_MODELS"
INSTALLED_PROGRAM_SUBPATH = ("Programs", "ReverseHire")  # under %LOCALAPPDATA%

# After a failed load, the same embedder does not try again before this delay
# (each attempt can take a minute without a connection).
RETRY_AFTER_S = 300.0
# Reachability probe used to tell "no connection" from "certificate refused".
PROBE_URL = "https://huggingface.co/api/models/" + _E5_SMALL_HF
PROBE_TIMEOUT_S = 8.0

# --- causes of a failed model load (i18n keys "model_cause.<cause>.short/advice") ---
CAUSE_RUNTIME = "runtime"          # Visual C++ runtime / onnxruntime DLL cannot be loaded
CAUSE_BLOCKED = "blocked"          # Smart App Control / Application Control policy
CAUSE_NETWORK = "network"          # files not on disk and no connection (or proxy)
CAUSE_CERTIFICATE = "certificate"  # files not on disk and the TLS certificate was refused
CAUSE_DAMAGED = "damaged"          # files on disk but incomplete or corrupt
CAUSE_MEMORY = "memory"
CAUSE_NOT_WRITABLE = "not_writable"  # the download folder cannot be written
CAUSE_UNKNOWN = "unknown"
CAUSES = (CAUSE_RUNTIME, CAUSE_BLOCKED, CAUSE_NETWORK, CAUSE_CERTIFICATE, CAUSE_DAMAGED,
          CAUSE_MEMORY, CAUSE_NOT_WRITABLE, CAUSE_UNKNOWN)
VC_REDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"


@runtime_checkable
class Embedder(Protocol):
    """Turns texts into normalized vectors (one row per text)."""

    def embed(self, texts: list[str], kind: str = "passage") -> np.ndarray: ...


# --- helpers --------------------------------------------------


def normalize_rows(matrix: np.ndarray) -> np.ndarray:
    """Normalize rows to unit norm (zero rows stay zero), as float32."""
    m = np.asarray(matrix, dtype=np.float32)
    if m.ndim == 1:
        m = m.reshape(1, -1)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (m / norms).astype(np.float32)


def truncate(text: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    """Truncate to max_chars, preferably at a space."""
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    cut = text.rfind(" ", 0, max_chars)
    return text[: cut if cut > max_chars // 2 else max_chars]


def split_into_blocks(text: str, max_chars: int = CHARS_PER_BLOCK) -> list[str]:
    """Split a text into blocks of whole sentences of at most ~max_chars.

    Sentences longer than the limit are cut at a space.
    """
    import re

    text = text.replace("\r", "\n")
    sentences: list[str] = []
    for row in text.split("\n"):
        row = " ".join(row.split())
        if not row:
            continue
        for sentence in re.split(r"(?<=[.!?;])\s+", row):
            sentence = sentence.strip()
            while len(sentence) > max_chars:
                part = truncate(sentence, max_chars)
                sentences.append(part)
                sentence = sentence[len(part):].strip()
            if sentence:
                sentences.append(sentence)

    blocks: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + 1 + len(sentence) > max_chars:
            blocks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence
    if current:
        blocks.append(current)
    return blocks


def cache_key(model: str, kind: str, text: str) -> str:
    return hashlib.sha256(f"{model}\x1f{kind}\x1f{text}".encode("utf-8")).hexdigest()


def _uses_prefixes(model: str) -> bool:
    return "e5" in model.lower()


# --- where the model files are ---------------------------------------------------


def source_checkout_models() -> Path | None:
    """``<repo>/models`` when running from a source checkout (pyproject.toml at the root)."""
    root = Path(__file__).resolve().parents[3]
    return root / MODELS_SUBFOLDER if (root / "pyproject.toml").is_file() else None


def installed_program_models() -> list[Path]:
    """``models`` next to the installed program: the default install folder and the
    folder that holds the running ``venv`` (installer layout ``<install>/venv``)."""
    folders: list[Path] = []
    local = os.environ.get("LOCALAPPDATA") if sys.platform == "win32" else None
    if local:
        folders.append(Path(local).joinpath(*INSTALLED_PROGRAM_SUBPATH, MODELS_SUBFOLDER))
    prefix = Path(sys.prefix)
    if prefix.name.lower() == "venv":
        folders.append(prefix.parent / MODELS_SUBFOLDER)
    return folders


def legacy_data_models() -> list[Path]:
    """``models`` in the data folders of older versions (``%APPDATA%\\ReverseHire``,
    ``~/.reversehire``): searched last, read-only, so a model downloaded there is not
    downloaded again after the data folder moved."""
    from ..config import legacy_data_folders

    return [folder / MODELS_SUBFOLDER for folder in legacy_data_folders()]


def model_search_folders(data_models: str | Path | None = None) -> list[Path]:
    """Folders searched for the model files, in order, without duplicates."""
    if data_models is None:
        from ..config import data_folder

        data_models = data_folder() / MODELS_SUBFOLDER
    candidates: list[Path | None] = [Path(data_models)]
    explicit = os.environ.get(MODELS_ENV, "").strip().strip('"')
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(source_checkout_models())
    candidates.extend(installed_program_models())
    candidates.extend(legacy_data_models())
    result: list[Path] = []
    seen: set[str] = set()
    for folder in candidates:
        if folder is None:
            continue
        key = os.path.normcase(os.path.abspath(folder))
        if key not in seen:
            seen.add(key)
            result.append(folder)
    return result


@dataclass(frozen=True)
class LocalModel:
    """A complete copy of a model's files found on disk."""

    model: str
    folder: Path      # the searched folder (fastembed cache_dir)
    snapshot: Path    # the folder with the ONNX file and the tokenizer


def _repo_folder_name(repo: str) -> str:
    return "models--" + repo.replace("/", "--")


def _complete_snapshot(path: Path, model_file: str) -> bool:
    try:
        return ((path / model_file).is_file() and (path / model_file).stat().st_size > 0
                and all((path / name).is_file() for name in REQUIRED_FILES))
    except OSError:
        return False


def _snapshots(repo_dir: Path) -> list[Path]:
    """Snapshot folders of a Hugging Face cache repo: the one in refs/main first."""
    found: list[Path] = []
    try:
        revision = (repo_dir / "refs" / "main").read_text(encoding="utf-8").strip()
        if revision:
            found.append(repo_dir / "snapshots" / revision)
    except OSError:
        pass
    try:
        others = sorted((p for p in (repo_dir / "snapshots").iterdir() if p.is_dir()),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        others = []
    return found + [p for p in others if p not in found]


def find_local_model(model: str, folders: list[Path]) -> LocalModel | None:
    """The first folder holding a complete copy of `model` (read only, never writes)."""
    repo, model_file = MODEL_SOURCES.get(model, (model, "model.onnx"))
    short = model.split("/")[-1]
    for folder in folders:
        try:
            if not folder.is_dir():
                continue
        except OSError:
            continue
        candidates = _snapshots(folder / _repo_folder_name(repo))
        if repo.lower() != repo:  # older fastembed versions kept other casings
            candidates += _snapshots(folder / _repo_folder_name(repo.lower()))
        candidates += [folder / short, folder]  # plain copies of the model folder
        for snapshot in candidates:
            if _complete_snapshot(snapshot, model_file):
                return LocalModel(model, folder, snapshot)
    return None


def folder_report(folders: list[Path]) -> list[tuple[Path, bool, str | None]]:
    """(folder, exists, model found there or None) for every searched folder."""
    rows = []
    for folder in folders:
        try:
            exists = folder.is_dir()
        except OSError:
            exists = False
        found = None
        if exists:
            for name in (PREFERRED_MODEL, FALLBACK_MODEL):
                if find_local_model(name, [folder]) is not None:
                    found = name
                    break
        rows.append((folder, exists, found))
    return rows


# --- system certificates and plain HTTP for downloads ------------------------------

_certificates_lock = threading.Lock()
_certificates_injected = False


def use_system_certificates() -> bool:
    """Make every HTTPS client of the process trust the Windows certificate store
    (``truststore``), as browsers do: needed behind antivirus HTTPS scanning or a
    company proxy, where the bundled certifi list refuses the certificate.
    Idempotent; False when truststore is not installed."""
    global _certificates_injected
    with _certificates_lock:
        if _certificates_injected:
            return True
        try:
            import truststore

            truststore.inject_into_ssl()
        except Exception as error:  # missing package or unsupported platform
            log.info("System certificate store not used (%s)", type(error).__name__)
            return False
        _certificates_injected = True
        log.info("HTTPS uses the system certificate store")
        return True


def _prefer_plain_http() -> None:
    """Download through huggingface_hub's own httpx client (which honours the system
    certificates) instead of the Xet transfer client, which has its own TLS stack.
    An explicit HF_HUB_DISABLE_XET in the environment wins."""
    if os.environ.get("HF_HUB_DISABLE_XET") is not None:
        return
    try:
        from huggingface_hub import constants

        constants.HF_HUB_DISABLE_XET = True
    except Exception:
        pass


# --- classification of load failures -------------------------------------------------

_BLOCKED_WINERRORS = {4551, 4553, 1260, 225}  # Application Control, group policy, virus
_MEMORY_WORDS = ("bad_alloc", "bad allocation", "failed to allocate", "not enough memory",
                 "out of memory", "cannot allocate memory", "paging file is too small")
_BLOCKED_WORDS = ("application control", "smart app control", "blocked by group policy",
                  "code integrity", "this program is blocked", "blocked this file",
                  "operation did not complete successfully because the file contains a virus")
_RUNTIME_WORDS = ("dll load failed", "vcruntime", "msvcp140", "onnxruntime_pybind11_state",
                  "the specified module could not be found", "specified procedure could not be found",
                  "no module named 'onnxruntime'", "no module named 'fastembed'")
_CERTIFICATE_WORDS = ("certificate verify failed", "certificate_verify_failed",
                      "self-signed certificate", "self signed certificate",
                      "unable to get local issuer", "certificate has expired", "invalidcertificate",
                      "unknownissuer", "tls handshake", "sslerror", "ssl:")
_DAMAGED_WORDS = ("protobuf", "corrupt", "invalid_graph", "invalid model", "no_suchfile",
                  "jsondecodeerror", "expecting value", "files have been corrupted",
                  "do not match the stored metadata", "could not find tokenizer",
                  "load model from", "unexpected end", "truncated")
_NOT_WRITABLE_WORDS = ("access is denied", "permission denied", "read-only file system",
                       "[winerror 5]", "errno 13")
_NETWORK_WORDS = ("connecterror", "connecttimeout", "readtimeout", "timed out", "timeout",
                  "proxy", "name resolution", "getaddrinfo", "nodename nor servname",
                  "no connection", "network is unreachable", "connection refused",
                  "connection reset", "max retries", "offline", "localentrynotfound",
                  "could not load model", "cannot reach", "couldn't connect",
                  "failed to establish", "11001", "10060", "10061")


def _chain(error: BaseException) -> list[BaseException]:
    """The error, its causes and contexts, and the errors listed in ModelLoadError."""
    seen: list[BaseException] = []
    pending = [error]
    while pending:
        current = pending.pop(0)
        if current is None or any(current is e for e in seen) or len(seen) > 30:
            continue
        seen.append(current)
        pending.extend([current.__cause__, current.__context__])
        pending.extend(e for _model, e in getattr(current, "attempts", []) or [])
    return seen


def error_text(error: BaseException) -> str:
    """One line per error of the chain: "Class: message"."""
    return "\n".join(f"{type(e).__name__}: {e}" for e in _chain(error))


def classify_load_error(error: BaseException) -> str:
    """One of CAUSES, from the exception chain (class, winerror and message words)."""
    chain = _chain(error)
    text = error_text(error).lower()
    winerrors = {getattr(e, "winerror", None) for e in chain}
    if any(isinstance(e, MemoryError) for e in chain) or any(w in text for w in _MEMORY_WORDS):
        return CAUSE_MEMORY
    if winerrors & _BLOCKED_WINERRORS or any(w in text for w in _BLOCKED_WORDS):
        return CAUSE_BLOCKED
    if any(w in text for w in _RUNTIME_WORDS) or (
            any(isinstance(e, ImportError) for e in chain) and "dll" in text):
        return CAUSE_RUNTIME
    if any(isinstance(e, ssl.SSLCertVerificationError) for e in chain) or any(
            w in text for w in _CERTIFICATE_WORDS):
        return CAUSE_CERTIFICATE
    if any(isinstance(e, PermissionError) for e in chain) or any(w in text for w in _NOT_WRITABLE_WORDS):
        return CAUSE_NOT_WRITABLE
    if any(w in text for w in _DAMAGED_WORDS):
        return CAUSE_DAMAGED
    if any(isinstance(e, (ConnectionError, TimeoutError)) for e in chain) or any(
            w in text for w in _NETWORK_WORDS):
        return CAUSE_NETWORK
    return CAUSE_UNKNOWN


def probe_hub(timeout: float = PROBE_TIMEOUT_S) -> str | None:
    """None if huggingface.co answers, else CAUSE_CERTIFICATE or CAUSE_NETWORK."""
    try:
        import httpx

        httpx.head(PROBE_URL, timeout=timeout, follow_redirects=True)
        return None
    except Exception as error:
        cause = classify_load_error(error)
        return CAUSE_CERTIFICATE if cause == CAUSE_CERTIFICATE else CAUSE_NETWORK


class HubUnreachable(ConnectionError):
    """The model is not on disk and huggingface.co does not answer (`cause`:
    CAUSE_NETWORK or CAUSE_CERTIFICATE): the download is not even tried."""

    def __init__(self, cause: str):
        super().__init__(f"model files not found locally and huggingface.co is not reachable ({cause})")
        self.cause = cause


class ModelLoadError(RuntimeError):
    """No embedding model could be loaded; `cause` is one of CAUSES."""

    def __init__(self, message: str, cause: str, attempts: list[tuple[str, BaseException]],
                 folders: list[Path], download_folder: Path | None = None):
        super().__init__(message)
        self.cause = cause
        self.attempts = attempts
        self.folders = folders
        self.download_folder = download_folder

    def details(self) -> str:
        """Diagnostic text for the user to send (exceptions, folders; never CV text)."""
        lines = [f"Cause: {self.cause}", f"{type(self).__name__}: {self}"]
        for model, error in self.attempts:
            lines.append(f"- {model}: {type(error).__name__}: {str(error)[:500]}")
        lines.append("Model folders searched:")
        for folder, exists, found in folder_report(self.folders):
            state = f"model found ({found})" if found else ("exists, no model" if exists else "missing")
            lines.append(f"  {folder.resolve() if exists else folder}  [{state}]")
        if self.download_folder is not None:
            lines.append(f"Download folder: {self.download_folder}")
        return "\n".join(lines)


def failure_details(error: BaseException) -> str:
    """`ModelLoadError.details()` or "Class: message" lines for any other error."""
    if isinstance(error, ModelLoadError):
        return error.details()
    return f"Cause: {classify_load_error(error)}\n{error_text(error)}"


def failure_cause(error: BaseException) -> str:
    return error.cause if isinstance(error, ModelLoadError) else classify_load_error(error)


# --- real embedder ---------------------------------------------------


class FastembedEmbedder:
    """fastembed-based embedder; the model is loaded on the first call."""

    def __init__(
        self,
        store=None,
        model: str = PREFERRED_MODEL,
        models_folder: str | Path | None = None,
        fallback: str | None = FALLBACK_MODEL,
        engine=None,
        *,
        search_folders: list[str | Path] | None = None,
        probe: Callable[[], str | None] | None = probe_hub,
        clock: Callable[[], float] = time.monotonic,
    ):
        """``engine``: ready-made object with ``embed(texts, batch_size)`` (for tests).

        ``models_folder`` is where a missing model is downloaded (default
        ``<data folder>/models``); ``search_folders`` replaces the folders searched
        for local copies (default ``model_search_folders``); ``probe`` tells a
        missing connection from a refused certificate after a failed download.
        """
        self._store = store
        self._requested_model = model
        self._fallback = fallback
        self._folder = Path(models_folder) if models_folder else None
        self._search = [Path(f) for f in search_folders] if search_folders is not None else None
        self._probe = probe
        self._clock = clock
        self._engine = engine
        self._model_name: str | None = model if engine is not None else None
        self._lock = threading.Lock()
        self._memory: OrderedDict[str, np.ndarray] = OrderedDict()
        self._failure: ModelLoadError | None = None
        self._failed_at = 0.0
        self.source_folder: Path | None = None  # where the loaded model's files are
        self.offline_load: bool | None = None   # True: loaded from local files, no network

    # Name of the model actually in use (the requested one until it is loaded).
    @property
    def model_name(self) -> str:
        return self._model_name or self._requested_model

    @property
    def similarity_range(self) -> tuple[float, float]:
        return SIMILARITY_RANGES.get(self.model_name, DEFAULT_RANGE)

    @property
    def title_range(self) -> tuple[float, float]:
        return TITLE_RANGES.get(self.model_name, self.similarity_range)

    @property
    def requirement_range(self) -> tuple[float, float]:
        return REQUIREMENT_RANGES.get(self.model_name, self.similarity_range)

    @property
    def loaded(self) -> bool:
        return self._engine is not None

    reduced = False  # the real model (see LexicalEmbedder)

    @property
    def last_error(self) -> ModelLoadError | None:
        """The last load failure (None once the model is loaded)."""
        return self._failure

    def _download_folder(self) -> Path:
        if self._folder is None:
            from ..config import data_folder

            self._folder = data_folder() / MODELS_SUBFOLDER
        return self._folder

    def _models_folder(self) -> Path:
        folder = self._download_folder()
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def search_folders(self) -> list[Path]:
        if self._search is not None:
            return list(self._search)
        return model_search_folders(self._download_folder())

    def local_model(self, name: str | None = None) -> LocalModel | None:
        """The local copy that would be used for `name` (default: the requested model)."""
        return find_local_model(name or self._requested_model, self.search_folders())

    @staticmethod
    def _register_e5_small() -> None:
        from fastembed import TextEmbedding
        from fastembed.common.model_description import ModelSource, PoolingType

        known_names = {m["model"] for m in TextEmbedding.list_supported_models()}
        if PREFERRED_MODEL in known_names:
            return
        TextEmbedding.add_custom_model(
            model=PREFERRED_MODEL,
            pooling=PoolingType.MEAN,
            normalization=True,
            sources=ModelSource(hf=_E5_SMALL_HF),
            dim=_E5_SMALL_DIM,
            model_file=_E5_SMALL_FILE,
            description="multilingual-e5-small (official ONNX), ~100 languages",
            license="mit",
            size_in_gb=_E5_SMALL_GB,
        )

    def _load(self):
        with self._lock:
            if self._engine is not None:
                return self._engine
            if self._failure is not None and self._clock() - self._failed_at < RETRY_AFTER_S:
                raise self._failure
            try:
                return self._try_load()
            except ModelLoadError as error:
                self._failure = error
                self._failed_at = self._clock()
                raise

    def _try_load(self):
        """Local copies first (no network), then a download; ModelLoadError if all fail."""
        folders = self.search_folders()
        candidates = [self._requested_model]
        if self._fallback and self._fallback != self._requested_model:
            candidates.append(self._fallback)
        attempts: list[tuple[str, BaseException]] = []
        try:
            from fastembed import TextEmbedding
        except BaseException as error:  # DLL blocked or missing, broken install
            if not isinstance(error, Exception):
                raise
            attempts.append(("fastembed", error))
            raise self._failed(attempts, folders, downloaded=False) from error

        downloaded = False
        probed: dict[str, str | None] = {}
        for name in candidates:
            local = find_local_model(name, folders)
            if local is None and self._probe is not None:
                # fastembed retries a failed download for about 40 s per model: when
                # huggingface.co is not reachable at all, fail at once instead.
                if "cause" not in probed:
                    use_system_certificates()
                    try:
                        probed["cause"] = self._probe()
                    except Exception:
                        probed["cause"] = None
                if probed["cause"] is not None:
                    downloaded = True
                    attempts.append((name, HubUnreachable(probed["cause"])))
                    continue
            try:
                if name == PREFERRED_MODEL:
                    self._register_e5_small()
                if local is not None:
                    start = self._clock()
                    engine = TextEmbedding(model_name=name, cache_dir=str(local.folder),
                                           specific_model_path=str(local.snapshot),
                                           local_files_only=True)
                    log.info("Embedding model %s loaded offline from %s in %.1f s",
                             name, local.folder, self._clock() - start)
                    self.source_folder, self.offline_load = local.folder, True
                else:
                    downloaded = True
                    folder = self._models_folder()
                    use_system_certificates()
                    _prefer_plain_http()
                    log.info("Embedding model %s not found in %s: downloading into %s",
                             name, ", ".join(str(f) for f in folders), folder)
                    engine = TextEmbedding(model_name=name, cache_dir=str(folder))
                    self.source_folder, self.offline_load = folder, False
                self._engine = engine
                self._model_name = name
                self._failure = None
                return engine
            except Exception as error:  # network, corrupted files, blocked DLL, memory
                log.warning("Embedding model %s not loaded (%s: %s)", name,
                            type(error).__name__, str(error)[:300], exc_info=True)
                attempts.append((name, error))
        raise self._failed(attempts, folders, downloaded)

    def _failed(self, attempts, folders, downloaded: bool) -> ModelLoadError:
        causes = [e.cause if isinstance(e, HubUnreachable) else classify_load_error(e)
                  for _n, e in attempts]
        cause = next((c for c in causes if c not in (CAUSE_UNKNOWN, CAUSE_NETWORK)),
                     causes[0] if causes else CAUSE_UNKNOWN)
        probe_known = any(isinstance(e, HubUnreachable) for _n, e in attempts)
        if (downloaded and not probe_known and cause in (CAUSE_NETWORK, CAUSE_UNKNOWN)
                and self._probe is not None):
            try:
                probed = self._probe()
            except Exception:
                probed = None
            cause = probed or cause
        last = attempts[-1][1] if attempts else None
        message = f"Could not load an embedding model ({cause}): {last}"
        return ModelLoadError(message, cause, attempts, folders,
                              self._folder if downloaded else None)

    def _from_cache(self, key: str) -> np.ndarray | None:
        if key in self._memory:
            self._memory.move_to_end(key)
            return self._memory[key]
        if self._store is not None:
            value = self._store.cache_read(CACHE_NAMESPACE, key)
            if value is not None:
                vector = np.asarray(value, dtype=np.float32)
                self._to_memory(key, vector)
                return vector
        return None

    def _to_memory(self, key: str, vector: np.ndarray) -> None:
        self._memory[key] = vector
        self._memory.move_to_end(key)
        while len(self._memory) > MAX_MEMORY_ENTRIES:
            self._memory.popitem(last=False)

    def embed(self, texts: list[str], kind: str = "passage") -> np.ndarray:
        if kind not in VALID_KINDS:
            raise ValueError(f"invalid kind: {kind!r}")
        if not texts:
            return np.zeros((0, _E5_SMALL_DIM), dtype=np.float32)
        engine = self._load()
        name = self.model_name
        cleaned_texts = [truncate(t or "") for t in texts]
        keys = [cache_key(name, kind, t) for t in cleaned_texts]

        results: list[np.ndarray | None] = [self._from_cache(k) for k in keys]
        missing_indices = [i for i, v in enumerate(results) if v is None]
        if missing_indices:
            prefix = f"{kind}: " if _uses_prefixes(name) else ""
            to_compute = [prefix + cleaned_texts[i] for i in missing_indices]
            vectors = np.vstack(list(engine.embed(to_compute, batch_size=BATCH_SIZE)))
            vectors = normalize_rows(vectors)
            for i, vector in zip(missing_indices, vectors):
                results[i] = vector
                self._to_memory(keys[i], vector)
                if self._store is not None:
                    self._store.cache_write(CACHE_NAMESPACE, keys[i], vector.tolist())
        return normalize_rows(np.vstack(results))


def create_embedder(store=None, models_folder: str | Path | None = None) -> Embedder:
    """Local multilingual embedder; downloads nothing until embed is called."""
    return FastembedEmbedder(store=store, models_folder=models_folder)


# --- built-in lexical fallback ------------------------------------------------------

LEXICAL_MODEL = "builtin/lexical-ngrams"
LEXICAL_DIM = 4096
WORD_WEIGHT = 1.0
BIGRAM_WEIGHT = 0.5
TRIGRAM_WEIGHT = 0.35  # character 3-grams of each word: inflections and compounds
MIN_WORD_CHARS = 3
# Measured with the texts of tests/test_scoring_real_model.py (accountant profile,
# best block): Italian accounting firm 0.434, workshop 0.198, pizzeria 0.070,
# English accounting firm 0.044 (no shared words: the fallback is weak across
# languages). Titles: "Contabile"/"Addetto contabilità" 0.28, unrelated 0.00-0.06.
LEXICAL_SIMILARITY_RANGE = (0.10, 0.40)
LEXICAL_TITLE_RANGE = (0.05, 0.40)
LEXICAL_REQUIREMENT_RANGE = (0.05, 0.40)
# Very common words of the supported languages (and English) that carry no topic.
_STOPWORDS = frozenset("""
the and for with from that this are was were you your our their have has not but all any can
will more other into about over also such its per una uno delle della dello degli dei del nel
nella nelle con che non sono come anche alla alle allo agli dal dalla dai più per tra fra suo sua
loro questo questa quello quella essere stato una для та що від або при його які яка якщо також
""".split())


def fold_text(text: str) -> str:
    """Lower case without accents (NFKD, combining marks removed)."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _bucket(feature: str) -> int:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "little") % LEXICAL_DIM


_FEATURE_WEIGHTS = {"w": WORD_WEIGHT, "b": BIGRAM_WEIGHT, "c": TRIGRAM_WEIGHT}


def lexical_features(text: str) -> dict[str, float]:
    """Features (words, word pairs, character 3-grams of each word) with their
    weight × (1 + log count)."""
    words = [w for w in re.findall(r"\w+", fold_text(text))
             if len(w) >= MIN_WORD_CHARS and w not in _STOPWORDS and not w.isdigit()]
    counts: Counter = Counter()
    for word in words:
        counts["w:" + word] += 1
        padded = f"_{word}_"
        for i in range(len(padded) - 2):
            counts["c:" + padded[i:i + 3]] += 1
    for first, second in zip(words, words[1:]):
        counts[f"b:{first} {second}"] += 1
    return {f: _FEATURE_WEIGHTS[f[0]] * (1.0 + math.log(n)) for f, n in counts.items()}


class LexicalEmbedder:
    """Deterministic hashed n-gram vectors (sublinear TF, L2-normalised).

    No model, no download, no network: the fallback when the real model cannot
    be loaded. Its similarities are lower than the model's, so it has its own
    ranges; scores are less accurate ("reduced accuracy" mode).
    """

    model_name = LEXICAL_MODEL
    similarity_range = LEXICAL_SIMILARITY_RANGE
    title_range = LEXICAL_TITLE_RANGE
    requirement_range = LEXICAL_REQUIREMENT_RANGE
    loaded = True
    reduced = True

    def embed(self, texts: list[str], kind: str = "passage") -> np.ndarray:
        if kind not in VALID_KINDS:
            raise ValueError(f"invalid kind: {kind!r}")
        matrix = np.zeros((len(texts), LEXICAL_DIM), dtype=np.float32)
        for row, text in enumerate(texts):
            for feature, weight in lexical_features(truncate(text or "")).items():
                matrix[row, _bucket(feature)] += weight
        return normalize_rows(matrix)


class ResilientEmbedder:
    """The real embedder, or the lexical fallback while the real one cannot load.

    After a failure the real model is tried again every `retry_after_s`, so it
    is used again as soon as it loads (e.g. once the files are copied).
    Properties (`model_name`, ranges, `reduced`) follow the embedder in use.
    """

    def __init__(self, primary, fallback=None, retry_after_s: float = RETRY_AFTER_S,
                 clock: Callable[[], float] = time.monotonic,
                 on_change: Callable[[bool, BaseException | None], None] | None = None):
        self.primary = primary
        self.fallback = fallback if fallback is not None else LexicalEmbedder()
        self._retry_after = retry_after_s
        self._clock = clock
        self._failed_at: float | None = None
        self._lock = threading.Lock()
        self.last_error: BaseException | None = None
        self._on_change = on_change

    def _current(self):
        return self.fallback if self._failed_at is not None else self.primary

    @property
    def reduced(self) -> bool:
        return self._failed_at is not None

    @property
    def model_name(self) -> str:
        return getattr(self._current(), "model_name", "")

    @property
    def similarity_range(self) -> tuple[float, float]:
        return getattr(self._current(), "similarity_range", DEFAULT_RANGE)

    @property
    def title_range(self) -> tuple[float, float]:
        return getattr(self._current(), "title_range", self.similarity_range)

    @property
    def requirement_range(self) -> tuple[float, float]:
        return getattr(self._current(), "requirement_range", self.similarity_range)

    @property
    def loaded(self) -> bool:
        return bool(getattr(self.primary, "loaded", True)) or self.reduced

    def embed(self, texts: list[str], kind: str = "passage") -> np.ndarray:
        with self._lock:
            due = self._failed_at is None or self._clock() - self._failed_at >= self._retry_after
        if due:
            try:
                vectors = self.primary.embed(texts, kind)
            except Exception as error:
                with self._lock:
                    first = self._failed_at is None
                    self._failed_at = self._clock()
                    self.last_error = error
                if first:
                    log.warning("Embedding model not available, reduced accuracy (%s)",
                                failure_cause(error), exc_info=True)
                    self._notify(True, error)
            else:
                with self._lock:
                    recovered = self._failed_at is not None
                    self._failed_at = None
                    self.last_error = None
                if recovered:
                    log.info("Embedding model loaded again: full accuracy")
                    self._notify(False, None)
                return vectors
        return self.fallback.embed(texts, kind)

    def _notify(self, reduced: bool, error: BaseException | None) -> None:
        if self._on_change is not None:
            try:
                self._on_change(reduced, error)
            except Exception:
                log.exception("Embedder change handler failed")
