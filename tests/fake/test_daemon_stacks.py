"""C-3.6: `subfleet daemon stacks` dumps every thread of a live daemon process.

The daemon registers `faulthandler` on SIGUSR1 against daemon.log, so the dump
comes from the signal handler even when every Python thread is stuck; the CLI
signals only the identity daemon.lock records, then prints what was written.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.fake.conftest import REPO


def stacks(root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "subfleet.cli", "daemon", "stacks", *extra],
        cwd=REPO, env={**os.environ, "SUBFLEET_HOME": str(root), "PYTHONPATH": str(REPO)},
        capture_output=True, text=True, timeout=60)


def test_daemon_stacks_prints_every_thread_of_the_live_daemon(daemon):
    daemon.start()
    result = stacks(daemon.root)
    assert result.returncode == 0, result.stderr
    assert "Thread 0x" in result.stdout
    # The accept loop and the control loop are both in the dump.
    assert "serve_forever" in result.stdout and "_control" in result.stdout
    assert "stack dumps:" not in result.stdout           # only what the signal wrote
    assert daemon.call("ping")["pong"] is True             # the daemon lives on
    assert "Thread 0x" in (daemon.root / "daemon.log").read_text(errors="replace")


def test_daemon_stacks_without_a_daemon_says_so():
    with tempfile.TemporaryDirectory(prefix="sfs-", dir="/tmp") as directory:
        result = stacks(Path(directory))
    assert result.returncode == 69, (result.returncode, result.stderr)
    assert "not running" in result.stderr
