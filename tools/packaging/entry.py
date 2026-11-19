"""Entry script of the ready-made builds (PyInstaller): the same as `python -m reversehire`.

It also answers `--self-test`, `--version` and `-m reversehire.mcp_server` (the
assistant strip and AI clients start the MCP server with the program's own
executable): see reversehire.app.run.
"""

import sys

from reversehire.app import run

if __name__ == "__main__":
    sys.exit(run())
