"""Builds the ReverseHire wheel and raises the build number (cross-platform).

    python tools/build.py --out dist/iso_staging/wheels

1. adds one to the third number of the version (0.1.0 -> 0.1.1 -> ...) in
   src/reversehire/_version.py, the only place that holds it (pyproject.toml reads
   it from there), and writes the build date next to it;
2. builds the wheel into the --out folder (`pip wheel <project> --no-deps`);
3. prints the new version as the last line of the output.

When the wheel cannot be built the previous version is written back, so a failed
build does not use up a number. Options: --root (another copy of the project,
used by the tests), --no-wheel (only raise the number), --date (build date,
default today), --dry-run (show the next version, change nothing).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import re
import subprocess
import sys

for _stream in (sys.stdout, sys.stderr):  # a cp1252 console: never UnicodeEncodeError
    getattr(_stream, "reconfigure", lambda **_k: None)(errors="replace")
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = Path("src") / "reversehire" / "_version.py"
_VERSION = re.compile(r'^__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"\s*$', re.MULTILINE)
_BUILD_DATE = re.compile(r'^BUILD_DATE\s*=\s*"[^"]*"\s*$', re.MULTILINE)


class BuildError(Exception):
    pass


def read_version(root: Path) -> str:
    text = (root / VERSION_FILE).read_text(encoding="utf-8")
    match = _VERSION.search(text)
    if match is None:
        raise BuildError(f"No __version__ = \"X.Y.Z\" line in {root / VERSION_FILE}")
    return ".".join(match.groups())


def next_version(version: str) -> str:
    """"0.1.9" -> "0.1.10": the third number (the build number) plus one."""
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def write_version(root: Path, version: str, build_date: str) -> None:
    path = root / VERSION_FILE
    text = path.read_text(encoding="utf-8")
    text = _VERSION.sub(f'__version__ = "{version}"', text, count=1)
    if _BUILD_DATE.search(text):
        text = _BUILD_DATE.sub(f'BUILD_DATE = "{build_date}"', text, count=1)
    else:
        text = text.rstrip("\n") + f'\nBUILD_DATE = "{build_date}"\n'
    path.write_text(text, encoding="utf-8", newline="\n")


def pip_wheel(root: Path, out: Path) -> Path:
    """`pip wheel <root> --no-deps -w <out>` with this Python; returns the wheel file.
    When setuptools is installed in this Python the build runs without an isolated
    environment (offline); otherwise pip fetches setuptools for the isolated build."""
    out.mkdir(parents=True, exist_ok=True)
    before = set(out.glob("reversehire-*.whl"))
    command = [sys.executable, "-m", "pip", "wheel", str(root), "--no-deps", "-w", str(out)]
    if importlib.util.find_spec("setuptools") is not None:
        command.append("--no-build-isolation")
    result = subprocess.run(command)
    if result.returncode != 0:
        raise BuildError(f"pip wheel failed (exit code {result.returncode})")
    built = sorted(set(out.glob("reversehire-*.whl")) - before or out.glob("reversehire-*.whl"),
                   key=lambda p: p.stat().st_mtime)
    if not built:
        raise BuildError(f"No wheel was written to {out}")
    return built[-1]


def build(root: Path, out: Path | None, *, wheel: bool = True, build_date: str | None = None,
          builder: Callable[[Path, Path], Path] = pip_wheel) -> tuple[str, Path | None]:
    """Raises the build number, builds the wheel; (new version, wheel or None)."""
    root = Path(root).resolve()
    previous_text = (root / VERSION_FILE).read_text(encoding="utf-8")
    version = next_version(read_version(root))
    write_version(root, version, build_date or _dt.date.today().isoformat())
    if not wheel:
        return version, None
    if out is None:
        raise BuildError("--out is required to build the wheel")
    try:
        built = builder(root, Path(out).resolve())
    except BaseException:
        (root / VERSION_FILE).write_text(previous_text, encoding="utf-8", newline="\n")
        raise
    if f"-{version}-" not in Path(built).name:
        (root / VERSION_FILE).write_text(previous_text, encoding="utf-8", newline="\n")
        raise BuildError(f"The wheel {Path(built).name} does not carry the version {version}")
    return version, Path(built)


def main(argv: list[str] | None = None, builder: Callable[[Path, Path], Path] = pip_wheel) -> int:
    parser = argparse.ArgumentParser(description="Raise the build number and build the ReverseHire wheel.")
    parser.add_argument("--out", type=Path, help="folder for the wheel (e.g. dist/iso_staging/wheels)")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help="project folder (default: this one)")
    parser.add_argument("--no-wheel", action="store_true", help="only raise the build number")
    parser.add_argument("--date", help="build date, YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true", help="print the next version, change nothing")
    args = parser.parse_args(argv)
    if args.date:
        try:
            _dt.date.fromisoformat(args.date)
        except ValueError:
            parser.error("--date must be YYYY-MM-DD")
    if not args.no_wheel and not args.dry_run and args.out is None:
        parser.error("--out is required (or use --no-wheel)")
    try:
        if args.dry_run:
            print(next_version(read_version(args.root.resolve())))
            return 0
        version, built = build(args.root, args.out, wheel=not args.no_wheel, build_date=args.date,
                               builder=builder)
    except (BuildError, OSError, ValueError) as error:
        print(f"Build failed: {error}", file=sys.stderr)
        return 1
    if built is not None:
        print(f"Wheel: {built}")
    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
