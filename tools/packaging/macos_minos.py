"""Highest minimum macOS version among the Mach-O files of an app bundle.

    python tools/packaging/macos_minos.py dist/ReverseHire.app --max 14.0

Reads LC_BUILD_VERSION (or LC_VERSION_MIN_MACOSX) with `otool -l` for every
Mach-O file and fails when one needs a newer macOS than --max: this is the
real minimum macOS of the build, whatever the wheels' names say.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

MACHO_MAGICS = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xcf"}
# LC_BUILD_VERSION: "minos 14.0" (its "tool ... version" lines are linker versions);
# LC_VERSION_MIN_MACOSX: "version 10.13".
_MINOS = re.compile(r"^\s*minos\s+(\d+(?:\.\d+)*)", re.MULTILINE)
_VERSION_MIN = re.compile(r"^\s*version\s+(\d+(?:\.\d+)*)", re.MULTILINE)


def is_macho(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) in MACHO_MAGICS
    except OSError:
        return False


def minimum(path: Path) -> tuple[int, ...] | None:
    output = subprocess.run(["otool", "-l", str(path)], capture_output=True, text=True,
                            encoding="utf-8", errors="replace").stdout
    versions = []
    for block in re.split(r"Load command \d+", output):
        match = None
        if "cmd LC_BUILD_VERSION" in block:
            match = _MINOS.search(block)
        elif "cmd LC_VERSION_MIN_MACOSX" in block:
            match = _VERSION_MIN.search(block)
        if match:
            versions.append(tuple(int(x) for x in match.group(1).split(".")))
    return max(versions) if versions else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--max", default="14.0")
    args = parser.parse_args(argv)
    limit = tuple(int(x) for x in args.max.split("."))
    found: dict[tuple[int, ...], list[str]] = {}
    for path in args.bundle.rglob("*"):
        if path.is_file() and not path.is_symlink() and is_macho(path):
            version = minimum(path)
            if version is not None:
                found.setdefault(version, []).append(str(path.relative_to(args.bundle)))
    for version in sorted(found):
        print(f"macOS {'.'.join(map(str, version))}: {len(found[version])} files, e.g. {found[version][0]}")
    highest = max(found) if found else None
    print(f"Highest minimum: {'.'.join(map(str, highest)) if highest else 'none found'}")
    too_new = [v for v in found if v > limit]
    for version in too_new:
        print(f"TOO NEW for macOS {args.max}: " + ", ".join(found[version][:10]))
    return 1 if too_new or not found else 0


if __name__ == "__main__":
    sys.exit(main())
