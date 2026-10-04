"""A pipe a child inherits by `pass_fds` stays above the standard streams (C-5.1, C-30.1)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]


# A process with some of fds 0, 1 and 2 closed (argv[2]) takes a pipe from
# `pipe_above_stdio`, starts a child with /dev/null standard streams that inherits the
# read end by `pass_fds`, and writes one byte. It reports the ends, their flags before
# and after the spawn, the descriptors the call opened and closed, and the child's exit
# status (0 when it read the byte) in argv[1], a file opened while all three were open.
PIPE_CHILD = r"""
import fcntl, json, os, subprocess, sys, traceback
report, result = open(sys.argv[1], "w"), {}
closed = [int(n) for n in sys.argv[2].split(",") if n]
cloexec = lambda fds: [bool(fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC) for fd in fds]
def opened():
    found = set()
    for fd in range(256):
        try:
            os.fstat(fd)
        except OSError:
            continue
        found.add(fd)
    return found
try:
    from subfleet.procs import pipe_above_stdio
    for fd in closed:
        os.close(fd)
    before = opened()
    ends = pipe_above_stdio()
    after = opened()
    result.update(ends=list(ends), cloexec=cloexec(ends), new=sorted(after - before), gone=sorted(before - after))
    child = subprocess.Popen([sys.executable, "-c", "import os, sys; sys.exit(0 if os.read(int(sys.argv[1]), 1) == b'1' else 3)",
                              str(ends[0])], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, pass_fds=(ends[0],))
    os.write(ends[1], b"1")
    result.update(child_rc=child.wait(30), cloexec_after=cloexec(ends))
    for fd in ends:
        os.close(fd)
except BaseException:
    result["error"] = traceback.format_exc()
report.write(json.dumps(result))
report.close()
"""


@pytest.mark.parametrize("closed", [(), (0,), (1,), (2,), (0, 1), (0, 2), (1, 2), (0, 1, 2)],
                         ids=lambda closed: "closed-" + ("".join(map(str, closed)) or "none"))
def test_a_pipe_a_child_inherits_stays_above_the_standard_streams(tmp_path, closed):
    """C-5.1, C-30.1: `os.pipe()` returns the lowest free descriptors, and a child's
    /dev/null standard streams replace a read end at 0, 1 or 2. For every set of them
    the process may have closed, both ends sit above 2 and stay close-on-exec in the
    process, the call leaves open exactly the two ends it returns (the closed ones stay
    closed), and the child reads the byte."""
    out = tmp_path / "pipe.json"
    done = subprocess.run([sys.executable, "-c", PIPE_CHILD, str(out), ",".join(map(str, closed))],
                          cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO)}, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=60)
    result = json.loads(out.read_text()) if out.exists() else {}
    assert done.returncode == 0 and result and "error" not in result, (done.returncode, done.stderr, result)
    assert min(result["ends"]) > 2, result
    assert result["cloexec"] == result["cloexec_after"] == [True, True], result
    assert (result["new"], result["gone"]) == (sorted(result["ends"]), []), result
    assert result["child_rc"] == 0, result
