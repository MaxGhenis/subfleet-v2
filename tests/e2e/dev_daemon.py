"""An isolated daemon with the fake providers, for developing the desktop app. Not a test.

    PATH=/usr/sbin:/sbin:$PATH uv run --no-sync python -m tests.e2e.dev_daemon [--app PATH]

It builds the state root the end-to-end tests use (`tests/e2e/conftest.py`):
the fake `claude` and `codex` on PATH, an isolated HOME, the synthetic guard,
two Claude and two Codex lanes, a git workspace at `<root>/work`. Then it
starts `subfleetd` there and prints the `SUBFLEET_HOME` a development build
should point at. Messages choose the fake providers' behaviour with
`[fake:<scenario>]` (see `tests/fake/interactive_claude.py` and
`interactive_codex.py`).

`--app` names the development build's executable; the daemon then treats it as
the app for person-only operations (C-25.6), which it does only because this
state root is not `~/.subfleet`. Ctrl-C stops the daemon, cleans up the
processes it owns, and leaves the state root for inspection.
"""

from __future__ import annotations

import argparse
import signal
import sys
import tempfile
import threading
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app", help="the development build's executable (Contents/MacOS/<name>)")
    parser.add_argument("--codex-writable", action="store_true",
                        help="record the Codex never-rules test as passed, so writable Codex conversations are allowed")
    options = parser.parse_args()
    from tests.e2e.conftest import E2E

    # AF_UNIX paths on macOS are limited to 104 bytes: keep the root short.
    root = Path(tempfile.mkdtemp(prefix="sf-dev-", dir="/tmp")).resolve()
    harness = E2E(root)
    harness.env["SUBFLEET_FAKE_TURN_LOG"] = str(root / "turns.jsonl")
    env = {"SUBFLEET_DEV_APP_EXECUTABLE": str(Path(options.app).resolve())} if options.app else {}
    if options.codex_writable:
        flag = root / "conversations" / "codex-writable-verified.json"
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text('{"verified_at": "dev_daemon --codex-writable (fake providers only)"}\n')
    harness.start(env=env)
    print(f"SUBFLEET_HOME={root}", flush=True)
    print(f"workspace: {harness.workdir}", flush=True)
    print(f"daemon log: {root}/daemon-0.log; provider log: {root}/turns.jsonl", flush=True)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        while not stop.wait(1):
            if harness.process.poll() is not None:
                print(f"daemon exited {harness.process.returncode}; see {root}/daemon-0.log", file=sys.stderr)
                return 1
    finally:
        harness.close()
        print(f"stopped; state root kept at {root}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
