"""Which requests the map page may make (pure Python, no Qt, unit-testable).

The page is loaded from the package's own files. Allowed:
  * file:// URLs inside the bundled ``web`` folder (and the local tile folder, if the
    tile template is a file:// one);
  * the Qt WebChannel script (qrc:/qtwebchannel/qwebchannel.js);
  * data: URLs (inline images);
  * tile requests to the one host of the configured tile template.
Everything else (other hosts, other schemes, files elsewhere on the disk) is blocked.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .settings import tile_origin


@dataclass(frozen=True)
class Decision:
    allow: bool
    is_tile: bool = False
    reason: str = ""


def _norm(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def _is_within(path: str, root: str) -> bool:
    root = root.rstrip("\\/")
    return path == root or path.startswith(root + os.sep)


def file_url_to_path(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme != "file":
        return None
    path = unquote(parts.path)
    if parts.netloc and parts.netloc != "localhost":
        path = "//" + parts.netloc + path  # UNC share
    elif os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path


class RequestPolicy:
    def __init__(self, assets_dir: str | Path):
        self._assets = _norm(assets_dir)
        self._tile_host: tuple[str, str] | None = None
        self._tile_dir: str | None = None

    def set_tile_template(self, template: str | None) -> None:
        """Declare the one tile source the page may use (None = no tiles)."""
        self._tile_host = None
        self._tile_dir = None
        origin = tile_origin(template) if template else None
        if origin is None:
            return
        scheme, where = origin
        if scheme == "file":
            local = file_url_to_path(where)
            if local:
                self._tile_dir = _norm(Path(local).parent if not local.endswith(("/", "\\")) else local)
        else:
            self._tile_host = (scheme, where)

    @property
    def assets_dir(self) -> str:
        return self._assets

    def is_bundled_file(self, url: str) -> bool:
        path = file_url_to_path(url)
        return path is not None and _is_within(_norm(path), self._assets)

    def decide(self, url: str) -> Decision:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme == "file":
            if self.is_bundled_file(url):
                return Decision(True, reason="bundled")
            path = file_url_to_path(url)
            if self._tile_dir and path and _is_within(_norm(path), self._tile_dir):
                return Decision(True, is_tile=True, reason="local tiles")
            return Decision(False, reason="file outside the bundled assets")
        if scheme == "qrc":
            if parts.path.rstrip("/").lower() == "/qtwebchannel/qwebchannel.js":
                return Decision(True, reason="webchannel")
            return Decision(False, reason="qrc resource not allowed")
        if scheme in ("data", "about", "blob"):
            return Decision(True, reason=scheme)
        if scheme in ("http", "https") and self._tile_host is not None:
            host = (parts.hostname or "").lower()
            default_port = 443 if scheme == "https" else 80
            if (scheme, f"{host}:{parts.port or default_port}") == self._tile_host:
                return Decision(True, is_tile=True, reason="tile host")
        return Decision(False, reason=f"{scheme or 'unknown'} request blocked")
