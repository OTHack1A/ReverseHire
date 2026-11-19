"""Common interface of the register sources and helpers for streaming big files."""

from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from ..discovery.gateway import Cancelled
from . import texts
from .download import (CANCELLED, DOWNLOADED, FAILED, UNCHANGED, DownloadResult, FileResult, Manifest, RemoteFile,
                       fetch_file, is_changed, remote_state)
from .model import CancelledError, ProgressCallback, RegisterProgress, RegisterRow, notify

log = logging.getLogger(__name__)


@dataclass
class Estimate:
    """What a download would cost: bytes and rows when the server tells, else None."""

    source: str
    files: int = 0
    bytes: int | None = None
    rows: int | None = None
    exact: bool = False
    note: str = ""
    error: str = ""


@dataclass
class UpdateCheck:
    source: str
    state: str                     # new | changed | unchanged | unknown | failed
    files: list[str] = field(default_factory=list)   # new or changed files
    bytes: int | None = None
    detail: str = ""


@dataclass
class ParseError:
    file: str
    detail: str


class RegisterSource:
    """One downloadable register. Subclasses set the metadata and implement
    `remote_files` (what to download, possibly reading a listing page) and `parse`."""

    id = ""
    licence = ""
    attribution = ""
    terms_url = ""
    homepage = ""
    update_frequency = "monthly"   # texts: registers.update.<value>
    update_days = 30
    min_pause_s = 0.0              # e.g. AGCM Crawl-Delay
    compare: tuple[str, ...] = ("etag", "last_modified", "size")
    max_rate_bps: float | None = None   # None = the gateway default

    def __init__(self, gateway=None, options: dict | None = None):
        self.gateway = gateway
        self.options = dict(options or {})
        self.errors: list[ParseError] = []
        self.stats: dict = {}

    # --- metadata

    @property
    def title(self) -> str:
        return texts.source_title(self.id)

    def info(self) -> dict:
        return {"id": self.id, "title": self.title, "licence": self.licence, "attribution": self.attribution,
                "terms_url": self.terms_url, "homepage": self.homepage, "update_frequency": self.update_frequency,
                "update_text": texts.text(f"registers.update.{self.update_frequency}"),
                "note": texts.text(f"registers.source.{self.id}.note")}

    # --- network

    def remote_files(self, manifest: Manifest | None = None, cancel=None) -> list[RemoteFile]:
        raise NotImplementedError

    def estimate(self, manifest: Manifest | None = None, cancel=None) -> Estimate:
        """Size and rows of the files `download` would fetch (from the listing/API; the
        sizes are summed only when the server announced them)."""
        try:
            files = self.remote_files(manifest, cancel)
        except (Cancelled, CancelledError):
            return Estimate(self.id, error="cancelled")
        except Exception as error:  # noqa: BLE001
            log.warning("Register %s: estimate failed", self.id, exc_info=True)
            return Estimate(self.id, error=f"{type(error).__name__}: {error}")
        sizes = [f.size for f in files]
        rows = [f.rows for f in files]
        exact = bool(files) and all(s is not None for s in sizes)
        return Estimate(self.id, files=len(files), bytes=sum(s for s in sizes if s) if exact else None,
                        rows=sum(r for r in rows if r) if rows and all(r is not None for r in rows) else None,
                        exact=exact)

    def download(self, dest: Path, progress: ProgressCallback | None = None, cancel=None,
                 force: bool = False) -> DownloadResult:
        """Fetch the files into `dest` (resumable, verified, manifest). Never raises."""
        dest = Path(dest)
        manifest = Manifest(dest)
        result = DownloadResult(self.id)
        try:
            notify(progress, RegisterProgress(self.id, "listing"))
            files = self.remote_files(manifest, cancel)
        except (Cancelled, CancelledError):
            result.state = "cancelled"
            return result
        except Exception as error:  # noqa: BLE001
            log.warning("Register %s: listing failed", self.id, exc_info=True)
            result.state, result.error, result.detail = "failed", "network", f"{type(error).__name__}: {error}"
            return result
        if not files:
            result.state, result.error, result.detail = "failed", "network", "no file listed by the source"
            return result
        for remote in files:
            if cancel is not None and cancel.is_set():
                result.files.append(FileResult(remote.name, remote.url, CANCELLED))
                continue
            outcome = fetch_file(self.gateway, remote, dest, manifest, self.id, progress=progress, cancel=cancel,
                                 compare=self.compare, force=force, max_rate_bps=self.max_rate_bps)
            result.files.append(outcome)
            result.bytes_downloaded += outcome.bytes if outcome.state == DOWNLOADED else 0
        states = {f.state for f in result.files}
        if CANCELLED in states:
            result.state = "cancelled"
        elif states == {UNCHANGED}:
            result.state = "unchanged"
        elif FAILED in states:
            failed = [f for f in result.files if f.state == FAILED]
            result.state = "failed" if len(failed) == len(result.files) else "partial"
            result.error, result.detail = failed[0].error, failed[0].detail
        self.after_download(manifest, files)
        return result

    def after_download(self, manifest: Manifest, files: list[RemoteFile]) -> None:
        """Hook: e.g. forget files the source no longer selects."""

    def check_update(self, dest: Path, cancel=None) -> UpdateCheck:
        """Cheap check (HEAD / small API call). Never raises."""
        manifest = Manifest(dest)
        try:
            files = self.remote_files(manifest, cancel)
            changed, size = [], 0
            for remote in files:
                entry = manifest.entry(remote.name)
                if not entry:
                    changed.append(remote.name)
                    size += remote.size or 0
                    continue
                if entry.get("url") != remote.url:
                    changed.append(remote.name)
                    size += remote.size or 0
                    continue
                if self.cheap_check_needs_head():
                    state = remote_state(self.gateway, remote.url, cancel)
                    if is_changed(entry, state, self.compare):
                        changed.append(remote.name)
                        size += state.get("size") or 0
            if not manifest.files:
                return UpdateCheck(self.id, "new", changed, size or None)
            return UpdateCheck(self.id, "changed" if changed else "unchanged", changed, size or None)
        except (Cancelled, CancelledError):
            return UpdateCheck(self.id, "unknown", detail="cancelled")
        except Exception as error:  # noqa: BLE001
            log.warning("Register %s: update check failed", self.id, exc_info=True)
            return UpdateCheck(self.id, "failed", detail=f"{type(error).__name__}: {error}")

    def cheap_check_needs_head(self) -> bool:
        """True when the listing alone cannot tell a changed file (HEAD each file)."""
        return True

    # --- parsing

    def local_files(self, dest: Path) -> list[Path]:
        """The downloaded files to parse (those the manifest lists)."""
        return Manifest(dest).local_paths()

    def parse(self, files: list[Path], region_filter: set[str] | None,
              progress: ProgressCallback | None = None, cancel=None) -> Iterator[RegisterRow]:
        raise NotImplementedError

    def parse_error(self, file: Path | str, detail: str) -> None:
        log.warning("Register %s: %s could not be read: %s", self.id, file, detail)
        self.errors.append(ParseError(str(file), detail))


# --- streaming helpers ---------------------------------------------------------------------


class CountingReader(io.RawIOBase):
    """Raw reader that counts the bytes read (progress of a streamed ZIP member)."""

    def __init__(self, handle, on_read=None):
        self._handle = handle
        self.count = 0
        self._on_read = on_read

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        data = self._handle.read(len(buffer))
        size = len(data)
        buffer[:size] = data
        self.count += size
        if self._on_read is not None:
            self._on_read(self.count)
        return size

    def close(self) -> None:
        try:
            self._handle.close()
        finally:
            super().close()


def open_member(path: Path, suffixes: tuple[str, ...]) -> tuple[object, int, str]:
    """(binary stream, uncompressed size, member name) of the first member of a ZIP whose
    name ends with one of `suffixes`; a plain file when `path` is not a ZIP."""
    path = Path(path)
    if zipfile.is_zipfile(path):
        archive = zipfile.ZipFile(path)
        for info in archive.infolist():
            if info.filename.lower().endswith(suffixes):
                return _ZipStream(archive, archive.open(info)), info.file_size, info.filename
        archive.close()
        raise ValueError(f"no {'/'.join(suffixes)} file inside the archive")
    return open(path, "rb"), path.stat().st_size, path.name


class _ZipStream:
    """A ZIP member stream that also closes its archive."""

    def __init__(self, archive, handle):
        self._archive = archive
        self._handle = handle

    def read(self, size: int = -1) -> bytes:
        return self._handle.read(size)

    def close(self) -> None:
        try:
            self._handle.close()
        finally:
            self._archive.close()


def text_stream(path: Path, suffixes: tuple[str, ...], encoding: str = "utf-8", on_bytes=None):
    """(text stream with newline='', uncompressed size, member name) for csv.reader."""
    raw, size, name = open_member(path, suffixes)
    counter = CountingReader(raw, on_bytes)
    buffered = io.BufferedReader(counter, buffer_size=1024 * 1024)
    return io.TextIOWrapper(buffered, encoding=encoding, errors="replace", newline=""), size, name


__all__ = ["CountingReader", "Estimate", "ParseError", "RegisterSource", "UpdateCheck", "open_member",
           "text_stream"]
