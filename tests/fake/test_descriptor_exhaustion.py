"""C-16.5, C-16.6: a daemon with few descriptors, and more clients than it has, stays up and answers.

The incident (2026-09-24 and 2026-09-25): launchd started the daemon with a soft
limit of 256 descriptors; clients piled up behind 32 busy readers, `accept` raised
EMFILE, and the uncaught error ended the process. Here the real daemon runs as a
child whose soft and hard RLIMIT_NOFILE are both pinned low, so it cannot raise
them, and hundreds of concurrent clients (more than the limit) connect at once.
"""

from __future__ import annotations

from contextlib import suppress
import json
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from subfleet import descriptors
from tests.fake.conftest import Harness


def open_fds(pid: int) -> int | None:
    """The child's open descriptors as lsof sees them from outside.

    Only rows whose FD column is a number count; lsof also lists the working
    directory, the executable and mapped files, which hold no descriptor.
    """
    try:
        out = subprocess.run(["/usr/sbin/lsof", "-n", "-P", "-p", str(pid)], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    rows = [line.split() for line in out.splitlines()[1:]]
    return sum(1 for row in rows if len(row) > 3 and row[3][:1].isdigit()) or None


def exchange(root: Path, message: dict | None, timeout: float) -> str:
    """One client: connect, maybe send, read one line. Says what happened, never raises."""
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(timeout)
    try:
        try:
            sock.connect(str(root / "daemon.sock"))
        except OSError:
            return "connect-refused"          # the listen backlog was full for a moment
        if message is not None:
            with suppress(BrokenPipeError, ConnectionResetError):
                sock.sendall((json.dumps(message) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                return "closed" if not data else "partial"
            data += chunk
        answer = json.loads(data)
        if answer.get("ok"):
            return "answered"
        return "busy" if "busy" in answer["error"]["message"] else "error:" + answer["error"]["message"]
    except TimeoutError:
        return "silent"
    except OSError as exc:
        return f"oserror:{exc.errno}"
    finally:
        sock.close()


def logs(h: Harness) -> str:
    """The daemon's own log and the child's stdout and stderr."""
    path = h.root / "daemon.log"
    return (path.read_text(errors="replace") if path.exists() else "") + "\n" + h.log_text()


@pytest.fixture
def harness(process_inspection_available):
    # This test process opens hundreds of sockets too; give it room first.
    descriptors.raise_open_file_limit(4096)
    with tempfile.TemporaryDirectory(prefix="sfx-", dir="/tmp") as directory:
        h = Harness(Path(directory))
        try:
            yield h
        finally:
            h.close()


def storm(root: Path, *, clients: int, kinds: dict, timeout: float) -> list[str]:
    """Start `clients` concurrent clients, round-robin over `kinds`, and collect every outcome."""
    outcomes: list[str] = []
    lock = threading.Lock()
    gate = threading.Barrier(clients)
    names = list(kinds)

    def run(index: int) -> None:
        name = names[index % len(names)]
        with suppress(threading.BrokenBarrierError):
            gate.wait(10)
        result = exchange(root, kinds[name], timeout)
        with lock:
            outcomes.append(f"{name}:{result}")
    threads = [threading.Thread(target=run, args=(i,)) for i in range(clients)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout + 30)
    return outcomes


def test_c16_6_more_clients_than_descriptors_get_answers_and_the_daemon_stays_up(harness):
    """C-16.5, C-16.6: with 128 descriptors and 300 concurrent clients, every client that
    connects is answered or refused at once, the job still runs, and the daemon recovers."""
    h = harness.start("--connection-idle-s", "2", open_files=128)
    pid = h.process.pid
    status = h.call("daemon.status")["descriptors"]
    assert status["soft_limit"] == 128 and status["hard_limit"] == 128     # pinned: no raise possible
    assert status["max_connections"] == descriptors.max_connections(128) == 32
    baseline = status["open"]
    job = h.submit(delay_s=4)
    peak = []
    sampling = threading.Event()

    def sample():
        while not sampling.is_set():
            if (count := open_fds(pid)) is not None:
                peak.append(count)
            sampling.wait(.25)
    sampler = threading.Thread(target=sample)
    sampler.start()
    try:
        outcomes = storm(h.root, clients=300, timeout=20, kinds={
            "wait": {"v": 1, "id": "w", "op": "wait", "args": {"job_ids": [job], "deadline_s": 30}},
            "ping": {"v": 1, "id": "p", "op": "ping", "args": {}},
            "idle": None,                                  # connects and never sends a byte
        })
    finally:
        sampling.set()
        sampler.join()
    assert h.process.poll() is None, logs(h)
    tally: dict[str, int] = {}
    for outcome in outcomes:
        tally[outcome] = tally.get(outcome, 0) + 1
    # Nobody was left hanging: every client that connected got a line, or (idle) was closed.
    unanswered = {k: v for k, v in tally.items()
                  if not k.endswith((":answered", ":busy", ":connect-refused")) and k != "idle:closed"}
    print("outcomes:", tally, "peak descriptors (lsof):", max(peak, default=None))
    assert not unanswered, tally
    assert tally.get("ping:busy", 0) + tally.get("wait:busy", 0) + tally.get("idle:busy", 0) > 0, tally
    assert tally.get("wait:answered", 0) > 0 and tally.get("ping:answered", 0) > 0, tally
    # Within its own budget, not merely under the kernel's limit (which cannot be passed):
    # the connections it held plus the reserve for its own work (C-16.6).
    budget = descriptors.max_connections(128) + descriptors.DESCRIPTOR_RESERVE
    assert max(peak, default=0) <= budget, (peak, budget)
    # The job ran to success beside the storm: the reserve kept descriptors for the daemon's own work.
    assert h.finished(job)["state"] == "succeeded"
    counts = h.call("daemon.status")["descriptors"]
    assert counts["refused"] > 0 and counts["idle_closed"] > 0 and counts["accept_failures"] == 0, counts
    assert h.call("ping")["pong"] is True
    # Back to where it started: no descriptor outlived the storm (C-16.6).
    h.until(lambda: h.call("daemon.status")["descriptors"]["open"] <= baseline + 4, timeout=10)
    assert "Traceback" not in logs(h)


def test_c16_5_emfile_from_accept_is_waited_out_under_a_real_kernel_limit(harness):
    """C-16.5: with the cap set above what 64 descriptors allow, `accept` really fails
    with EMFILE; the daemon waits it out, closes the idle connections, and answers again.

    On macOS the kernel drops the connection whose `accept` failed, so that client
    reads end of stream: surviving EMFILE protects the process and the next client,
    and C-16.6's cap is what keeps a client from meeting it at all."""
    h = harness.start("--connection-idle-s", "3", "--max-connections", "500", open_files=64)
    pid = h.process.pid
    held = []
    deadline = time.monotonic() + 15
    # Connect in small bursts, so the accept loop keeps up until descriptors run out;
    # on macOS a full listen backlog refuses a connect at once.
    while "accept failed: EMFILE" not in logs(h) and time.monotonic() < deadline:
        for _ in range(8):
            sock = socket.socket(socket.AF_UNIX)
            sock.settimeout(10)
            try:
                sock.connect(str(h.root / "daemon.sock"))
            except OSError:
                sock.close()
                continue
            held.append(sock)
        time.sleep(.05)
    assert "accept failed: EMFILE" in logs(h), (len(held), logs(h))
    assert len(held) > 40                               # the daemon held what 64 descriptors allow
    assert h.process.poll() is None, logs(h)
    # Idle connections are closed after 3 s, which frees descriptors for the backlog.
    closed = 0
    for sock in held:
        with suppress(OSError):
            if sock.recv(1) == b"":
                closed += 1
        sock.close()
    assert closed == len(held)                          # every one was closed or dropped, none hangs
    assert h.until(lambda: exchange(h.root, {"v": 1, "id": "p", "op": "ping", "args": {}}, 5) == "answered",
                   timeout=20)
    assert "accept recovered after" in logs(h)          # logged at the first accept that succeeds
    assert h.process.poll() is None
    counts = h.call("daemon.status")["descriptors"]
    assert counts["accept_failures"] > 0 and counts["idle_closed"] > 0, counts
    assert "Traceback" not in logs(h)
