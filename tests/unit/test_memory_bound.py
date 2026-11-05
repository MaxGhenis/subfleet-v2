"""D-CREDIT-MEM1: the per-test memory bound (`tests/memory_bound.py`) holds on macOS.

Each case runs pytest in a child process on a one-test project, with the bound loaded
(`-p tests.memory_bound`) at 256 MB, and samples the child's phys_footprint (what top's
MEM column shows, compressed pages included) from here while it runs:

- a test that grows its own process in Python fails at the bound, with its stacks, and
  the process never passes the hard bound;
- a test whose child process grows has that child killed and fails;
- a test inside one long C call, which no signal handler can interrupt, has its process
  killed at the hard bound, with every thread's stack left in a file;
- a test that starts above the bound, because an earlier test in the same process kept
  what it grew, still fails before the hard bound, at the midpoint between its starting
  footprint and the hard bound, so the process and the tests after it survive;
- the bound's own arithmetic: the hard bound stays under the host's 8 GB watchdog while
  the bound leaves room.

The growing data is one repeated byte, which compresses as well as the 2026-11-04
worker's did, so only a reading that counts compressed pages sees it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import zlib
from pathlib import Path

import pytest

from tests import memory_bound

REPO = Path(__file__).resolve().parents[2]
LIMIT_MB = 256
SOFT, HARD = memory_bound.bounds(LIMIT_MB)

pytestmark = pytest.mark.skipif(memory_bound.footprint(os.getpid()) is None,
                                reason="phys_footprint comes from macOS's proc_pid_rusage")

GROWS_IN_PYTHON = """
import time

def test_grows_in_python():
    hold = []
    while True:
        hold.append(b"x" * (8 << 20))
        time.sleep(0.05)
"""

GROWS_IN_A_CHILD = """
import subprocess, sys, time

def test_grows_in_a_child():
    subprocess.run([sys.executable, "-c",
                    "import time\\nhold = []\\nwhile True:\\n    hold.append(b'x' * (8 << 20))\\n    time.sleep(0.05)"],
                   timeout=120)
    time.sleep(5)      # the failure lands in this phase even if the child's end wins the race
"""

#: One C call that releases the GIL while it grows: zlib inflating 1.5 GB of zeros from a
#: stream of about 1.5 MB. The signal handler cannot run until it returns, and the test
#: then holds what it made, so only the hard bound can end it.
GROWS_IN_ONE_C_CALL = """
import pathlib, time, zlib

def test_grows_in_one_c_call():
    data = zlib.decompress((pathlib.Path(__file__).parent / "zeros.z").read_bytes())
    time.sleep(10)
    assert len(data) == 1536 << 20
"""


#: Two tests in one process: the first keeps what it grew past the bound (a module global),
#: so the second starts above the bound, and then grows again. Before r3, the bound fired
#: once and armed again only for a test that started under it, so the second test grew
#: unwatched until the hard bound killed the whole process (D-CREDIT-MEM1's forced faults).
GROWS_TWICE = """
import time

kept = []

def test_keeps_what_it_grew():
    while True:
        kept.append(b"x" * (8 << 20))
        time.sleep(0.05)

def test_grows_again_from_above_the_bound():
    hold = []
    while True:
        hold.append(b"y" * (8 << 20))
        time.sleep(0.05)
"""


def zeros_stream(size: int) -> bytes:
    """A zlib stream of `size` zero bytes, made a mebibyte at a time."""
    compressor, chunk = zlib.compressobj(9), bytes(1 << 20)
    return b"".join([*(compressor.compress(chunk) for _ in range(size >> 20)), compressor.flush()])


def run_bounded(tmp_path: Path, source: str, **files: bytes) -> tuple[subprocess.CompletedProcess, int, str]:
    """pytest on a one-test project under the bound; (result, peak footprint, output)."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "test_case.py").write_text(source)
    for name, data in files.items():
        (project / name.replace("_", ".")).write_bytes(data)
    logs = tmp_path / "logs"
    logs.mkdir()
    environment = {name: value for name, value in os.environ.items()
                   if name not in ("PYTEST_ADDOPTS", "PYTHONPATH")}
    environment.update({memory_bound.ENV: str(LIMIT_MB), memory_bound.LOG_ENV: str(logs)})
    output = tmp_path / "output.txt"
    peak = 0
    with output.open("wb") as sink:
        process = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "tests.memory_bound",
             "--rootdir", str(project), str(project / "test_case.py")],
            cwd=REPO, env=environment, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 180
        try:
            while process.poll() is None:
                assert time.monotonic() < deadline, "the bounded run did not end"
                found = memory_bound.footprint(process.pid)
                if found is not None:
                    peak = max(peak, found[0])
                time.sleep(0.02)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    text = output.read_bytes()[-20000:].decode("utf-8", "replace")
    return subprocess.CompletedProcess(process.args, process.returncode), peak, text


def test_a_test_that_grows_its_own_process_fails_at_the_bound(tmp_path):
    result, peak, text = run_bounded(tmp_path, GROWS_IN_PYTHON)
    assert result.returncode == 1, text
    assert "1 failed" in text and "memory bound (D-CREDIT-MEM1): this test's process reached" in text, text
    assert "Every thread's stack" in text and "test_grows_in_python" in text, text
    # The run never got near the hard bound: the failure came at the bound.
    assert SOFT < peak < HARD, (peak >> 20, SOFT >> 20, HARD >> 20)


def test_a_child_that_grows_past_the_bound_is_killed_and_its_test_fails(tmp_path):
    result, peak, text = run_bounded(tmp_path, GROWS_IN_A_CHILD)
    assert result.returncode == 1, text
    assert "1 failed" in text and "which this test started" in text and "was killed" in text, text
    assert peak < SOFT, (peak >> 20, SOFT >> 20)     # the child grew, not the test's process


def test_a_test_inside_one_long_c_call_is_killed_at_the_hard_bound(tmp_path):
    result, peak, text = run_bounded(tmp_path, GROWS_IN_ONE_C_CALL, zeros_z=zeros_stream(1536 << 20))
    assert result.returncode == -9, text
    [log] = (tmp_path / "logs").glob("memory-bound-*.txt")
    written = log.read_text()
    assert "over the hard bound" in written and "test_grows_in_one_c_call" in written, written
    assert "test_case.py" in written, written       # the stacks name the test's frame
    # Seen past the bound, and killed by the time its 1.5 GB was whole.
    assert SOFT < peak <= (1536 << 20) + (256 << 20), (peak >> 20, SOFT >> 20)


def test_a_test_that_starts_above_the_bound_still_fails_before_the_hard_bound(tmp_path):
    """C-20.7: memory one test keeps does not leave the next unwatched. The second test
    starts above the bound and fails at the midpoint between its starting footprint and
    the hard bound, with its stacks; the process survives to report both failures."""
    result, peak, text = run_bounded(tmp_path, GROWS_TWICE)
    assert result.returncode == 1, text
    assert "2 failed" in text, text
    assert "test_grows_again_from_above_the_bound" in text and "above the bound, so it failed at" in text, text
    assert peak < HARD, (peak >> 20, HARD >> 20)
    assert not list((tmp_path / "logs").glob("memory-bound-*.txt")), "the hard bound ended the process"


@pytest.mark.parametrize("started_mb,expected_mb", [(0, 256), (200, 256), (256, 256), (300, 534), (700, 734)])
def test_the_threshold_is_the_bound_or_halfway_to_the_hard_bound(started_mb, expected_mb):
    assert memory_bound.threshold(SOFT, HARD, started_mb << 20) == expected_mb << 20


@pytest.mark.parametrize("limit_mb", [0, 1, 256, 1024, 4096, 6144, 8192])
def test_the_hard_bound_stays_under_the_hosts_watchdog_while_the_bound_allows(limit_mb):
    soft, hard = memory_bound.bounds(limit_mb)
    assert soft == limit_mb << 20 and hard > soft
    if soft + (256 << 20) <= memory_bound.HARD_CEILING:
        assert hard <= memory_bound.HARD_CEILING < 8 << 30


def test_the_bound_reads_its_megabytes_from_the_environment():
    assert memory_bound.limit_mb({}) == memory_bound.DEFAULT_MB == 4096
    assert memory_bound.limit_mb({memory_bound.ENV: " 512 "}) == 512
    assert memory_bound.limit_mb({memory_bound.ENV: "0"}) == 0
    with pytest.raises(ValueError):
        memory_bound.limit_mb({memory_bound.ENV: "-1"})


def test_a_footprint_counts_pages_rss_leaves_out_once_they_compress():
    """phys_footprint and resident size both come back for this process, and the
    footprint is at least what the process touched."""
    before = memory_bound.footprint(os.getpid())
    hold = b"y" * (64 << 20)
    after = memory_bound.footprint(os.getpid())
    assert before is not None and after is not None
    assert after[0] - before[0] >= (48 << 20), (before, after)
    del hold


def test_this_session_runs_under_the_bound(pytestconfig):
    """tests/conftest.py installs the bound in the process that runs the tests: the
    session itself, or each xdist worker (the controller runs none and bounds none)."""
    if memory_bound.limit_mb() == 0:
        pytest.skip(f"{memory_bound.ENV}=0 turns the bound off")
    bound = pytestconfig.pluginmanager.get_plugin(memory_bound.NAME)
    assert isinstance(bound, memory_bound.MemoryBound)
    assert bound.pid == os.getpid() and bound.thread.is_alive()
    assert bound.soft == memory_bound.limit_mb() << 20
