"""Adds (or replaces) interface texts in the three locale files at once.

Usage::

    python tools/locale_keys.py fragment.json [--replace]

`fragment.json` maps each key to its three texts::

    {"affinity.box.title": {"en": "Affinity", "it": "Affinità", "uk": "Спорідненість"}}

Every key must have the three languages with the same {placeholders}. New keys are
appended at the end of each file; an existing key keeps its place and is changed only
with --replace. The three files are written under a lock file, so several developers
(or agents) can run the script at the same time without losing each other's keys.
"""

from __future__ import annotations

import json
import os
import string
import sys

for _stream in (sys.stdout, sys.stderr):  # a cp1252 console: never UnicodeEncodeError
    getattr(_stream, "reconfigure", lambda **_k: None)(errors="replace")
import time
from pathlib import Path

LOCALES = Path(__file__).resolve().parent.parent / "src" / "reversehire" / "locales"
CODES = ("en", "it", "uk")
LOCK = LOCALES / ".locale_keys.lock"


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def check(fragment: dict) -> list[str]:
    errors = []
    for key, texts in fragment.items():
        if not isinstance(texts, dict) or set(texts) != set(CODES):
            errors.append(f"{key}: needs exactly en, it, uk")
            continue
        if any(not isinstance(v, str) or not v.strip() for v in texts.values()):
            errors.append(f"{key}: empty text")
            continue
        names = {code: _placeholders(texts[code]) for code in CODES}
        if not names["en"] == names["it"] == names["uk"]:
            errors.append(f"{key}: different placeholders {names}")
    return errors


def _acquire(timeout: float = 60.0) -> int:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() > deadline:
                raise SystemExit(f"Lock {LOCK} held for more than {timeout:.0f} s: remove it if stale")
            time.sleep(0.2)


def merge(fragment: dict, replace: bool = False) -> tuple[int, int]:
    errors = check(fragment)
    if errors:
        raise SystemExit("\n".join(errors))
    handle = _acquire()
    added = changed = 0
    try:
        files = {code: json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8")) for code in CODES}
        for key, texts in fragment.items():
            exists = key in files["en"]
            if exists and not replace:
                continue
            for code in CODES:
                files[code][key] = texts[code]
            if exists:
                changed += 1
            else:
                added += 1
        for code in CODES:
            path = LOCALES / f"{code}.json"
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(files[code], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(path)
    finally:
        os.close(handle)
        LOCK.unlink(missing_ok=True)
    return added, changed


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    fragment = json.loads(Path(argv[0]).read_text(encoding="utf-8-sig"))
    added, changed = merge(fragment, replace="--replace" in argv[1:])
    print(f"{added} keys added, {changed} replaced")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
