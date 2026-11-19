"""Smoke test of a ready-made build (used by .github/workflows/release.yml).

    python tools/packaging/smoke.py <executable> [--expect-program-folder auto|none]

1. Runs `<executable> --self-test --self-test-report <file>` (see reversehire.selftest)
   with a time limit, prints the report and fails unless every check passed and
   the program runs frozen.
2. Starts `<executable> -m reversehire.mcp_server` (as the assistant strip and AI
   clients do), sends the MCP "initialize" request on stdin and fails unless a
   JSON-RPC result comes back on stdout.

Both runs use a new temporary data folder (REVERSEHIRE_HOME); nothing is
downloaded. Exit code 0 when both passed.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SELF_TEST_TIMEOUT_S = 420
MCP_TIMEOUT_S = 120


def self_test(executable: Path, expect_program_folder: str) -> bool:
    report = Path(tempfile.mkdtemp(prefix="rh-smoke-")) / "selftest.json"
    env = dict(os.environ, REVERSEHIRE_HOME=tempfile.mkdtemp(prefix="rh-smoke-home-"))
    started = time.monotonic()
    try:
        completed = subprocess.run([str(executable), "--self-test", "--self-test-report", str(report)],
                                   env=env, timeout=SELF_TEST_TIMEOUT_S, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as expired:
        print(f"SELF-TEST TIMEOUT after {SELF_TEST_TIMEOUT_S} s")
        print((expired.stdout or "")[-4000:] if isinstance(expired.stdout, str) else "")
        return False
    seconds = time.monotonic() - started
    print(f"self-test exit code {completed.returncode} after {seconds:.1f} s")
    if completed.returncode != 0:
        print("--- stdout (end) ---\n" + completed.stdout[-6000:])
        stderr = completed.stderr
        if "Fatal Python error" in stderr:  # faulthandler: the Python stacks come first
            start = stderr.index("Fatal Python error")
            print("--- stderr (crash report) ---\n" + stderr[start:start + 12000])
        else:
            print("--- stderr (end) ---\n" + stderr[-6000:])
    if not report.is_file():
        print("no self-test report was written")
        return False
    data = json.loads(report.read_text(encoding="utf-8"))
    print(json.dumps(data, indent=2))
    ok = completed.returncode == 0 and data.get("ok") is True and data.get("frozen") is True
    folder = data.get("program_folder")
    if expect_program_folder == "none" and folder != "None":
        print(f"expected no program folder (app bundle), got {folder}")
        ok = False
    if expect_program_folder == "auto" and Path(str(folder)).resolve() != executable.resolve().parent:
        print(f"expected the program folder {executable.resolve().parent}, got {folder}")
        ok = False
    return ok


def _reader(stream, lines: queue.Queue) -> None:
    for line in iter(stream.readline, ""):
        lines.put(line)
    lines.put(None)


def mcp_handshake(executable: Path) -> bool:
    env = dict(os.environ, REVERSEHIRE_HOME=tempfile.mkdtemp(prefix="rh-smoke-mcp-"))
    process = subprocess.Popen([str(executable), "-m", "reversehire.mcp_server"], env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace")
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(process.stdout, lines), daemon=True).start()
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "release-smoke-test", "version": "1"}}}
    ok = False
    try:
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + MCP_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                break
            line = line.strip()
            if not line.startswith("{"):
                continue
            answer = json.loads(line)
            if answer.get("id") == 1:
                server = answer.get("result", {}).get("serverInfo", {})
                print(f"MCP initialize answered: {json.dumps(server)}")
                ok = "result" in answer
                break
        if not ok:
            print("MCP server did not answer the initialize request")
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)
        if not ok:
            print("--- MCP stderr (end) ---\n" + (process.stderr.read() or "")[-4000:])
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--expect-program-folder", choices=("auto", "none"), default="auto")
    args = parser.parse_args(argv)
    if not args.executable.is_file():
        print(f"not found: {args.executable}")
        return 2
    first = self_test(args.executable, args.expect_program_folder)
    second = mcp_handshake(args.executable)
    print(f"SMOKE self-test={'PASS' if first else 'FAIL'} mcp={'PASS' if second else 'FAIL'}")
    return 0 if first and second else 1


if __name__ == "__main__":
    sys.exit(main())
