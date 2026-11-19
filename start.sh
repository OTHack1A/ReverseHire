#!/usr/bin/env bash
# Starts ReverseHire from the source folder (Linux, and macOS through start.command).
# On the first run it creates the virtual environment .venv and installs the
# dependencies (internet needed, a few minutes, no administrator rights).
# The data (settings, saved searches, log) goes to the "userdata" folder next to
# this file when the folder is writable (see docs/INSTALLATION.md).
set -u
cd "$(dirname "$0")" || exit 1

fail() {
    echo
    echo "ERROR: $1"
    echo "Fix the problem, delete the .venv folder if it was created, and run this script again."
    exit 1
}

if [ ! -x ".venv/bin/python" ]; then
    echo "ReverseHire - first start: creating the Python environment in .venv"
    PY=""
    for candidate in python3.14 python3.13 python3.12 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
            PY="$candidate"
            break
        fi
    done
    [ -n "$PY" ] || fail "Python 3.12 or later was not found. Install it with your package manager
(for example: sudo apt install python3 python3-venv) or from https://www.python.org/downloads/"
    echo "Using: $(command -v "$PY") ($("$PY" --version 2>&1))"
    "$PY" -m venv .venv || fail "the virtual environment could not be created.
On Debian and Ubuntu install the venv module first: sudo apt install python3-venv"
    .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt ||
        fail "the dependencies could not be installed (see the messages above)."
    .venv/bin/python -m pip install --disable-pip-version-check -e . ||
        fail "ReverseHire could not be installed (see the messages above)."
    echo "Environment ready."
fi

exec .venv/bin/python -m reversehire "$@"
