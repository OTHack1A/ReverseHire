"""Resumable, verified downloads of register files through the app's PoliteGateway.

Every file is stored under `<data>/registers/raw/<source>/` with a `manifest.json`
recording, per file: URL, download date, bytes, ETag, Last-Modified, SHA-256 and the
period it covers. A refresh first sends a cheap HEAD and skips files whose ETag /
Last-Modified / size did not change.

Download steps for one file:
1. HEAD (polite, same pacing as every other request) -> size, ETag, Last-Modified;
2. GET into "<name>.new" through the gateway, resumed from "<name>.new.part" with an
   HTTP Range when a previous attempt was interrupted or cancelled (the partial file is
   discarded when the server's version changed in between);
3. checks: size against Content-Length and the size announced by the source, SHA-256
   when the source publishes one, ZIP structure (central directory; CRC of every member
   for files below ZIP_CRC_LIMIT);
4. only then "<name>.new" replaces "<name>": a failed or damaged download never
   destroys the previous good copy.
Errors never propagate: each file gets a FileResult with a state and an error key.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..discovery.gateway import Cancelled, GatewayError, SourceBlocked
from .model import RegisterProgress, notify, now_iso

log = logging.getLogger(__name__)

MANIFEST = "manifest.json"
ZIP_CRC_LIMIT = 64 * 1024 * 1024
HASH_CHUNK = 1024 * 1024

# File states
DOWNLOADED, UNCHANGED, CANCELLED, FAILED = "downloaded", "unchanged", "cancelled", "failed"
# Error keys (texts: registers.error.<key>)
E_NETWORK, E_BLOCKED, E_CHECKSUM, E_DISK = "network", "blocked", "checksum", "disk"


@dataclass
class RemoteFile:
    url: str
    name: str                 # local file name
    size: int | None = None   # size announced by the source (listing / API), when known
    sha256: str = ""          # checksum announced by the source, when known
    period: str = ""          # e.g. "2025-07" (RNA) or the publication date
    rows: int | None = None   # rows announced by the source, when known


@dataclass
class FileResult:
    name: str
    url: str
    state: str
    bytes: int = 0
    path: str = ""
    error: str = ""
    detail: str = ""


@dataclass
class DownloadResult:
    source: str
    state: str = "ok"               # ok | unchanged | partial | failed | cancelled
    files: list[FileResult] = field(default_factory=list)
    bytes_downloaded: int = 0
    error: str = ""
    detail: str = ""

    @property
    def paths(self) -> list[Path]:
        return [Path(f.path) for f in self.files if f.path and f.state in (DOWNLOADED, UNCHANGED)]

    def to_dict(self) -> dict:
        return asdict(self)


class Manifest:
    """`manifest.json` of one source folder (atomic writes, tolerant reads)."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.path = self.folder / MANIFEST
        self.data: dict = {"files": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                value.setdefault("files", {})
                self.data = value
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            log.warning("Register manifest %s unreadable: starting a new one", self.path)

    @property
    def files(self) -> dict:
        return self.data.setdefault("files", {})

    def entry(self, name: str) -> dict:
        value = self.files.get(name)
        return value if isinstance(value, dict) else {}

    def set_entry(self, name: str, value: dict) -> None:
        self.files[name] = value
        self.save()

    def remove(self, name: str) -> None:
        if self.files.pop(name, None) is not None:
            self.save()

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value
        self.save()

    def save(self) -> None:
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            log.warning("Register manifest %s could not be written", self.path, exc_info=True)

    def local_paths(self) -> list[Path]:
        """Downloaded files listed in the manifest that still exist, in manifest order."""
        result = []
        for name in self.files:
            path = self.folder / name
            if path.is_file():
                result.append(path)
        return result


def sha256_of(path: Path, cancel=None) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            chunk = handle.read(HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _header(headers: dict, name: str) -> str:
    for key, value in (headers or {}).items():
        if key.lower() == name:
            return str(value)
    return ""


def remote_state(gateway, url: str, cancel=None) -> dict:
    """HEAD the URL: {"status", "size", "etag", "last_modified"} ("status" 0 on errors)."""
    try:
        reply = gateway.request("HEAD", url, cancel=cancel, retry_backoff=False)
    except Cancelled:
        raise
    except GatewayError as error:
        return {"status": 0, "error": str(error), "blocked": isinstance(error, SourceBlocked)}
    length = _header(reply.headers, "content-length")
    return {"status": reply.status, "size": int(length) if length.isdigit() else None,
            "etag": _header(reply.headers, "etag"), "last_modified": _header(reply.headers, "last-modified")}


def is_changed(entry: dict, state: dict, compare: tuple[str, ...] = ("etag", "last_modified", "size")) -> bool:
    """True when the remote state differs from the manifest entry on any field both know."""
    if not entry:
        return True
    known = False
    for key in compare:
        remote = state.get(key)
        local = entry.get("bytes") if key == "size" else entry.get(key)
        if remote in (None, "") or local in (None, ""):
            continue
        known = True
        if str(remote) != str(local):
            return True
    if not known:
        log.debug("No ETag, Last-Modified or size to compare: treated as unchanged")
    return False


def verify_file(path: Path, *, expected_size: int | None = None, sha256: str = "",
                cancel=None) -> tuple[bool, str, str]:
    """(ok, detail, sha256 of the file). Size, published checksum and ZIP structure."""
    try:
        size = path.stat().st_size
    except OSError as error:
        return False, f"cannot read the file: {error}", ""
    if expected_size is not None and size != expected_size:
        return False, f"size {size} bytes instead of {expected_size}", ""
    digest = sha256_of(path, cancel)
    if sha256 and digest.lower() != sha256.lower():
        return False, "SHA-256 differs from the published one", digest
    if path.suffix.lower() == ".zip" or path.name.lower().endswith(".zip.new"):
        try:
            with zipfile.ZipFile(path) as archive:
                if not archive.namelist():
                    return False, "empty ZIP archive", digest
                if size <= ZIP_CRC_LIMIT:
                    bad = archive.testzip()
                    if bad:
                        return False, f"damaged member {bad}", digest
        except (zipfile.BadZipFile, OSError, EOFError) as error:
            return False, f"not a valid ZIP file ({error})", digest
    return True, "", digest


def fetch_file(gateway, remote: RemoteFile, folder: Path, manifest: Manifest, source_id: str, *,
               progress=None, cancel=None, compare: tuple[str, ...] = ("etag", "last_modified", "size"),
               force: bool = False, max_rate_bps: float | None = None) -> FileResult:
    """Download one file (see the module docstring). Never raises."""
    folder = Path(folder)
    target = folder / remote.name
    staging = folder / (remote.name + ".new")
    part = staging.with_name(staging.name + ".part")
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return FileResult(remote.name, remote.url, FAILED, error=E_DISK, detail=str(error))
    try:
        notify(progress, RegisterProgress(source_id, "checking", file=remote.name))
        state = remote_state(gateway, remote.url, cancel)
        if state.get("blocked"):
            return FileResult(remote.name, remote.url, FAILED, error=E_BLOCKED, detail=state.get("error", ""))
        if state.get("status") not in (200, 204, 206) and state.get("status") not in (0, 403, 405, 501):
            return FileResult(remote.name, remote.url, FAILED, error=E_NETWORK,
                              detail=f"HTTP {state.get('status')}")
        entry = manifest.entry(remote.name)
        if (not force and target.is_file() and entry and entry.get("url") == remote.url
                and not is_changed(entry, state, compare)):
            return FileResult(remote.name, remote.url, UNCHANGED, path=str(target))
        version = state.get("etag") or state.get("last_modified") or str(state.get("size") or "")
        partial = manifest.get("partial", {}) or {}
        if part.exists() and partial.get(remote.name) not in (None, version):
            log.info("Register %s: %s changed on the server, partial download discarded", source_id, remote.name)
            part.unlink(missing_ok=True)
        partial[remote.name] = version
        manifest.set("partial", partial)
        expected = remote.size if remote.size is not None else state.get("size")

        def on_bytes(done: int, total: int | None) -> None:
            notify(progress, RegisterProgress(source_id, "download", file=remote.name, done_bytes=done,
                                              total_bytes=total or expected))

        kwargs = {"cancel": cancel, "progress": on_bytes}
        if max_rate_bps is not None:
            kwargs["max_rate_bps"] = max_rate_bps
        gateway.download(remote.url, staging, **kwargs)
        ok, detail, digest = verify_file(staging, expected_size=expected, sha256=remote.sha256, cancel=cancel)
        if not ok:
            log.warning("Register %s: %s failed the check (%s)", source_id, remote.name, detail)
            staging.unlink(missing_ok=True)
            return FileResult(remote.name, remote.url, FAILED, error=E_CHECKSUM, detail=detail)
        os.replace(staging, target)
        size = target.stat().st_size
        manifest.set_entry(remote.name, {
            "url": remote.url, "date": now_iso(), "bytes": size, "etag": state.get("etag", ""),
            "last_modified": state.get("last_modified", ""), "sha256": digest, "period": remote.period,
            "rows": remote.rows})
        partial.pop(remote.name, None)
        manifest.set("partial", partial)
        return FileResult(remote.name, remote.url, DOWNLOADED, bytes=size, path=str(target))
    except Cancelled:
        return FileResult(remote.name, remote.url, CANCELLED, detail="cancelled")
    except SourceBlocked as error:
        return FileResult(remote.name, remote.url, FAILED, error=E_BLOCKED, detail=str(error))
    except GatewayError as error:
        return FileResult(remote.name, remote.url, FAILED, error=E_NETWORK, detail=str(error))
    except OSError as error:
        log.warning("Register %s: %s could not be written", source_id, remote.name, exc_info=True)
        return FileResult(remote.name, remote.url, FAILED, error=E_DISK, detail=str(error))
    except Exception as error:  # noqa: BLE001 - never raise to the caller
        log.warning("Register %s: unexpected error downloading %s", source_id, remote.name, exc_info=True)
        return FileResult(remote.name, remote.url, FAILED, error=E_NETWORK, detail=f"{type(error).__name__}: {error}")


def folder_bytes(folder: Path) -> int:
    total = 0
    try:
        for path in Path(folder).rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


__all__ = ["CANCELLED", "DOWNLOADED", "DownloadResult", "FAILED", "FileResult", "Manifest", "RemoteFile",
           "UNCHANGED", "fetch_file", "folder_bytes", "is_changed", "remote_state", "sha256_of", "verify_file"]
