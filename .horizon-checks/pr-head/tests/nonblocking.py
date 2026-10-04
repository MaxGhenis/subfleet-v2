"""Checks that a call never waits in open(), run in a child process of its own.

A reader that regresses to a plain open() waits there for a FIFO's other end.
On a helper thread of the test's own process that wait outlived the failed test:
the thread stayed blocked for the rest of the run unless something released its
FIFO, and a release sent before the thread reached open() released nothing
(review of aa41312). A child is killed with its process group and reaped when
its time runs out, so a regression fails its test and leaves nothing behind.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

#: Seconds a child has to finish. A call that does not block takes well under
#: one; the rest is headroom for starting Python on a loaded machine. A call
#: that blocks never finishes, so the bound only decides how long a failure takes.
CHILD_S = 60.0


def run_child(source: str, *, timeout: float = CHILD_S) -> str:
    """Run `source` (dedented) in `python -c` with this checkout first on sys.path,
    and return what it printed. A child still running after `timeout` s is killed
    and reaped and the test fails, naming the last step the child reported
    (`print(..., flush=True)`); one that exits nonzero fails it with its stderr."""
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    process = subprocess.Popen([sys.executable, "-c", textwrap.dedent(source)], cwd=REPO, env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = process.communicate()
        pytest.fail(f"still running after {timeout:g} s (killed and reaped); it had printed:\n{out}{err}")
    assert process.returncode == 0, f"exit {process.returncode}\n{out}{err}"
    return out
