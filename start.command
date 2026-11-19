#!/usr/bin/env bash
# macOS: double-click this file in Finder to start ReverseHire from the source folder.
# It runs start.sh, which creates .venv on the first run (see docs/INSTALLATION.md).
# If macOS refuses to open it: right-click > Open, or run once in Terminal:
#   chmod +x start.command start.sh
cd "$(dirname "$0")" || exit 1
exec /bin/bash ./start.sh "$@"
