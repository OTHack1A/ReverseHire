"""Content-addressed archive of the files sent: ``<folder>/<hash[:2]>/<sha256><ext>``.

The exact bytes of a CV are copied here when the record is made, so later edits or the
deletion of the original never change what was sent. The same content is stored once
(deduplication by SHA-256). Copying and hashing read the whole file: call from a worker.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("reversehire.applications")

MAX_FILE_BYTES = 100 * 1024 * 1024
_CHUNK = 1024 * 1024
_HASH = re.compile(r"^[0-9a-f]{64}$")


class ArchiveError(OSError):
    """The file cannot be archived (missing, too large, folder not writable)."""


@dataclass(frozen=True)
class ArchivedFile:
    hash: str
    name: str      # original file name
    size: int
    is_new: bool   # False when the same content was already archived


def is_hash(value: str) -> bool:
    return bool(value) and bool(_HASH.match(value))


class Archive:
    def __init__(self, folder: Path | str):
        self.folder = Path(folder)

    def _path(self, digest: str, suffix: str) -> Path:
        return self.folder / digest[:2] / f"{digest}{suffix}"

    def find(self, digest: str) -> Path | None:
        """The archived file with this hash, or None."""
        if not is_hash(digest):
            return None
        directory = self.folder / digest[:2]
        try:
            for path in directory.iterdir():
                if path.stem == digest and path.is_file():
                    return path
        except OSError:
            return None
        return None

    def store(self, source: Path | str) -> ArchivedFile:
        """Copies `source` into the archive (hashing it on the way). Nothing is left behind on failure."""
        source = Path(source)
        try:
            size = source.stat().st_size
            if not source.is_file():
                raise ArchiveError(f"Not a file: {source}")
            if size > MAX_FILE_BYTES:
                raise ArchiveError(f"File too large to archive ({size} bytes): {source.name}")
            self.folder.mkdir(parents=True, exist_ok=True)
            temp = self.folder / f".tmp-{uuid.uuid4().hex}"
            digest = hashlib.sha256()
            try:
                with source.open("rb") as src, temp.open("wb") as dst:
                    while chunk := src.read(_CHUNK):
                        digest.update(chunk)
                        dst.write(chunk)
                return self._finish(temp, digest.hexdigest(), source.name, size)
            finally:
                with contextlib.suppress(OSError):
                    temp.unlink()
        except ArchiveError:
            raise
        except OSError as error:
            raise ArchiveError(f"Cannot archive {source}: {error}") from error

    def store_bytes(self, data: bytes, name: str) -> ArchivedFile:
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            temp = self.folder / f".tmp-{uuid.uuid4().hex}"
            try:
                temp.write_bytes(data)
                return self._finish(temp, hashlib.sha256(data).hexdigest(), name, len(data))
            finally:
                with contextlib.suppress(OSError):
                    temp.unlink()
        except OSError as error:
            raise ArchiveError(f"Cannot archive {name}: {error}") from error

    def _finish(self, temp: Path, digest: str, name: str, size: int) -> ArchivedFile:
        if self.find(digest) is not None:
            return ArchivedFile(digest, name, size, False)
        suffix = Path(name).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            suffix = ""
        target = self._path(digest, suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temp, target)
        return ArchivedFile(digest, name, size, True)

    def read_bytes(self, digest: str) -> bytes | None:
        path = self.find(digest)
        if path is None:
            return None
        try:
            return path.read_bytes()
        except OSError:
            log.warning("Archived file unreadable: %s", path, exc_info=True)
            return None

    def remove(self, digest: str) -> bool:
        path = self.find(digest)
        if path is None:
            return False
        try:
            path.unlink()
        except OSError:
            log.warning("Archived file not removed: %s", path, exc_info=True)
            return False
        with contextlib.suppress(OSError):
            path.parent.rmdir()  # only when empty
        return True

    def hashes(self) -> set[str]:
        found: set[str] = set()
        try:
            for directory in self.folder.iterdir():
                if directory.is_dir():
                    found.update(p.stem for p in directory.iterdir() if p.is_file() and is_hash(p.stem))
        except OSError:
            pass
        return found

    def collect_garbage(self, referenced: set[str]) -> int:
        """Removes every archived file whose hash is not in `referenced` (and stray temp files)."""
        removed = 0
        for digest in self.hashes() - set(referenced):
            removed += self.remove(digest)
        with contextlib.suppress(OSError):
            for stray in self.folder.glob(".tmp-*"):
                with contextlib.suppress(OSError):
                    stray.unlink()
        return removed
