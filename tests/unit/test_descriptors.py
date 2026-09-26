"""C-16.6, C-16.7: the descriptor budget's pure parts, with their invariants as properties.

The properties run over seeded random inputs (the repository is standard library
only, so no Hypothesis): the same seeds give the same cases on every run, and a
failure prints the case that broke it.
"""

import io
import os
import plistlib
import random
import resource
import socket
import sys
import tempfile
from pathlib import Path

import pytest

from subfleet import descriptors
from subfleet.descriptors import OVERSIZED, LineFramer

INF = resource.RLIM_INFINITY


# --- raise_open_file_limit (C-16.6) -------------------------------------------

class FakeKernel:
    """getrlimit/setrlimit where values above `ceiling` are refused, as setrlimit's
    EINVAL and EPERM refuse them on some kernels (macOS 26 itself accepts any
    soft value within the hard limit and enforces kern.maxfilesperproc later)."""

    def __init__(self, soft, hard, ceiling):
        self.soft, self.hard, self.ceiling = soft, hard, ceiling
        self.calls = []

    def getrlimit(self, which):
        assert which == resource.RLIMIT_NOFILE
        return self.soft, self.hard

    def setrlimit(self, which, value):
        soft, hard = value
        self.calls.append(value)
        if hard != self.hard:
            raise AssertionError("the hard limit is never changed")
        if hard != INF and soft > hard:
            raise ValueError("not allowed to raise maximum limit")
        if soft > self.ceiling:
            raise ValueError("current limit exceeds maximum limit")
        self.soft = soft


def install(monkeypatch, kernel):
    monkeypatch.setattr(descriptors.resource, "getrlimit", kernel.getrlimit)
    monkeypatch.setattr(descriptors.resource, "setrlimit", kernel.setrlimit)


def expected_after(soft, hard, ceiling, wanted):
    """The reference answer: the first candidate the kernel takes, else what was there."""
    target = wanted if hard == INF else min(wanted, hard)
    if soft == INF or soft >= target:
        return soft
    for candidate in (target, *(v for v in descriptors.OPEN_FILES_FALLBACKS if v < target)):
        if candidate <= soft:
            break
        if candidate <= ceiling:
            return candidate
    return soft


def test_c16_6_the_launchd_default_is_raised_to_65536(monkeypatch):
    """C-16.6 the incident's numbers: launchd's 256 under an unlimited hard limit becomes 65536."""
    kernel = FakeKernel(256, INF, 245760)
    install(monkeypatch, kernel)
    assert descriptors.raise_open_file_limit() == (256, 65536, INF)
    assert kernel.calls == [(65536, INF)]


def test_c16_6_a_hard_limit_below_the_target_is_the_ceiling(monkeypatch):
    kernel = FakeKernel(256, 1000, 245760)
    install(monkeypatch, kernel)
    assert descriptors.raise_open_file_limit() == (256, 1000, 1000)
    assert kernel.calls == [(1000, 1000)]


def test_c16_6_a_refused_value_falls_back_to_the_next_lower(monkeypatch):
    """C-16.6 where setrlimit refuses the target, the next lower of the fallbacks is taken."""
    kernel = FakeKernel(256, INF, 10240)
    install(monkeypatch, kernel)
    assert descriptors.raise_open_file_limit() == (256, 8192, INF)
    assert [soft for soft, _ in kernel.calls] == [65536, 32768, 16384, 8192]


def test_c16_6_enough_already_is_left_alone(monkeypatch):
    for soft in (65536, 1048576, INF):
        kernel = FakeKernel(soft, INF, 245760)
        install(monkeypatch, kernel)
        assert descriptors.raise_open_file_limit() == (soft, soft, INF)
        assert kernel.calls == []


def test_c16_6_property_the_raise_never_lowers_never_exceeds_and_takes_the_best_allowed(monkeypatch):
    """C-16.6 invariants, for every (soft, hard, kernel ceiling, wanted) drawn:

    - the soft limit is never lowered, and setrlimit is never asked to lower it;
    - it never exceeds the hard limit, nor a refusal ceiling unless it already did;
    - the result is exactly the first candidate the kernel accepts (the reference model);
    - the returned `after` is what getrlimit reports afterwards, and `hard` is unchanged.
    """
    rng = random.Random(16_5)
    interesting = [0, 1, 64, 255, 256, 257, 1023, 1024, 4096, 8191, 8192, 10240, 24576,
                   32768, 65535, 65536, 65537, 245760, 1048576]
    for case in range(4000):
        soft = rng.choice(interesting + [rng.randrange(0, 2 ** 21)])
        hard = rng.choice([INF, INF, *interesting, rng.randrange(0, 2 ** 21)])
        if hard != INF and soft > hard:
            soft, hard = hard, soft
        ceiling = rng.choice(interesting + [rng.randrange(1, 2 ** 21)])
        wanted = rng.choice([descriptors.OPEN_FILES_WANTED, 8192, 1024, rng.randrange(1, 2 ** 20)])
        kernel = FakeKernel(soft, hard, ceiling)
        install(monkeypatch, kernel)
        before, after, reported_hard = descriptors.raise_open_file_limit(wanted)
        where = f"case {case}: soft={soft} hard={hard} ceiling={ceiling} wanted={wanted} -> {after}"
        assert before == soft and reported_hard == hard, where
        assert after == kernel.soft, where
        assert after >= soft, where
        assert hard == INF or after <= hard, where
        assert after <= max(soft, ceiling), where
        assert after == expected_after(soft, hard, ceiling, wanted), where
        assert all(value[0] > soft for value in kernel.calls), where


def test_c16_6_the_real_limit_can_be_raised_in_this_process():
    """C-16.6 on this machine the call succeeds and leaves at least what it found."""
    before, after, hard = descriptors.raise_open_file_limit()
    soft_now, hard_now = resource.getrlimit(resource.RLIMIT_NOFILE)
    assert after >= before and soft_now == after and hard_now == hard


# --- max_connections (C-16.7) -------------------------------------------------

def test_c16_7_property_the_connection_cap_leaves_the_reserve_and_is_monotone():
    """C-16.7 invariants over every soft limit from 0 to 70000 (exhaustive):

    - the cap is within [CONNECTIONS_FLOOR, CONNECTIONS_CEILING];
    - it never decreases as the limit grows;
    - once the limit leaves room for the floor, two descriptors per connection
      plus the reserve fit inside the limit.
    """
    previous = 0
    for soft in range(0, 70_001):
        cap = descriptors.max_connections(soft)
        assert descriptors.CONNECTIONS_FLOOR <= cap <= descriptors.CONNECTIONS_CEILING, soft
        assert cap >= previous, soft
        if soft >= descriptors.DESCRIPTOR_RESERVE + 2 * descriptors.CONNECTIONS_FLOOR:
            assert 2 * cap + descriptors.DESCRIPTOR_RESERVE <= soft, soft
        previous = cap
    assert descriptors.max_connections(INF) == descriptors.CONNECTIONS_CEILING
    assert descriptors.max_connections(256) == 96                   # launchd's default
    assert descriptors.max_connections(65536) == descriptors.CONNECTIONS_CEILING


# --- LineFramer (C-16.1, C-16.7) ----------------------------------------------

def reference(stream: bytes, limit: int) -> list:
    """What framing must produce: each line, or OVERSIZED for one longer than the limit."""
    out = []
    parts = stream.split(b"\n")
    for part in parts[:-1]:
        out.append(part + b"\n" if len(part) + 1 <= limit else OVERSIZED)
    if parts[-1]:
        out.append(parts[-1] if len(parts[-1]) <= limit else OVERSIZED)
    return out


def framed(stream: bytes, cuts: list[int], limit: int) -> list:
    framer, out, start = LineFramer(limit), [], 0
    for cut in sorted(cuts) + [len(stream)]:
        out += framer.feed(stream[start:cut])
        start = cut
    return out + framer.finish()


def random_stream(rng: random.Random, limit: int, oversized: bool) -> bytes:
    lines = []
    for _ in range(rng.randrange(0, 8)):
        top = limit * 3 if oversized else limit - 1
        length = rng.choice([0, 1, limit - 2, limit - 1, limit, limit + 1, rng.randrange(0, top + 1)])
        length = max(0, min(length, top))
        lines.append(bytes(rng.choice(b'{}"abc: ') for _ in range(length)))
    stream = b"\n".join(lines)
    if lines and rng.random() < .7:
        stream += b"\n"
    return stream


def test_c16_7_property_framing_does_not_depend_on_how_recv_chunks_the_stream():
    """C-16.7 invariant: for any stream and any chunking, the lines equal the reference.

    A line of at most `limit` bytes with its newline is returned whole; a longer
    one is one OVERSIZED and its tail is never mistaken for a request.
    """
    rng = random.Random(16_6)
    for case in range(3000):
        limit = rng.choice([1, 2, 3, 8, 17, 64])
        stream = random_stream(rng, limit, oversized=True)
        cuts = [rng.randrange(0, len(stream) + 1) for _ in range(rng.randrange(0, 12))]
        assert framed(stream, cuts, limit) == reference(stream, limit), (case, limit, stream, cuts)


def test_c16_7_differential_framing_matches_readline_for_every_line_within_the_limit():
    """C-16.7 the old reader was `makefile().readline(limit + 1)`; for requests within
    the limit the new framing returns exactly what it did, however the bytes arrive."""
    rng = random.Random(1616)
    for case in range(3000):
        limit = rng.choice([2, 5, 16, 64])
        stream = random_stream(rng, limit, oversized=False)
        old, reader = [], io.BytesIO(stream)
        while line := reader.readline(limit + 1):
            old.append(line)
        cuts = [rng.randrange(0, len(stream) + 1) for _ in range(rng.randrange(0, 12))]
        assert framed(stream, cuts, limit) == old, (case, limit, stream, cuts)


def test_c16_7_an_oversized_request_is_reported_once_and_the_next_one_still_parses():
    framer = LineFramer(limit=10)
    assert framer.feed(b"x" * 25) == [OVERSIZED]
    assert framer.feed(b"yyyy\n{}\n") == [b"{}\n"]
    assert framer.feed(b"123456789\n1234567890\n") == [b"123456789\n", OVERSIZED]
    assert framer.finish() == []


# --- read_only and client_gone (C-16.7) ---------------------------------------

def test_c16_7_only_reads_are_dropped_for_a_departed_client():
    for op in ("list", "show", "wait", "readings", "why", "pick", "daemon.status", "notice.pending"):
        assert descriptors.read_only(op, {}), op
    assert descriptors.read_only("ping", {}) and not descriptors.read_only("ping", {"text": "hello"})
    assert descriptors.read_only("lanes", {}) and descriptors.read_only("lanes", {"action": "list"})
    for action in ("transfer", "enroll", "hold", "release"):
        assert not descriptors.read_only("lanes", {"action": action})
    for op in ("submit", "kill", "notice.ack", "notice.mark", "sessions", "operations",
               "gate.start", "gate.poll", "gate.continue"):
        assert not descriptors.read_only(op, {}), op


@pytest.fixture
def listener():
    with tempfile.TemporaryDirectory(prefix="sfd-", dir="/tmp") as directory:
        path = os.path.join(directory, "s")
        server = socket.socket(socket.AF_UNIX)
        try:
            server.bind(path)
        except PermissionError:
            pytest.skip("sandbox denies unix socket binding")
        server.listen(8)
        yield server, path
        server.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="C-16.7 tells a closed peer apart with macOS getpeername")
def test_c16_7_a_client_is_gone_only_once_it_has_closed_its_whole_socket(listener):
    """C-16.7 an open or half-closed client is still there; a closed one is gone,
    even while the request it sent before closing is still unread."""
    server, path = listener
    verdicts = {}
    for mode in ("open", "half-closed", "closed", "closed-unread"):
        client = socket.socket(socket.AF_UNIX)
        client.connect(path)
        conn, _ = server.accept()
        client.sendall(b'{"v":1}\n')
        if mode != "closed-unread":
            assert conn.recv(100) == b'{"v":1}\n'
        if mode == "half-closed":
            client.shutdown(socket.SHUT_WR)
        if mode.startswith("closed"):
            client.close()
        verdicts[mode] = descriptors.client_gone(conn)
        if mode == "closed-unread":
            assert conn.recv(100) == b'{"v":1}\n'         # the request is still there to read
        conn.close()
        client.close()
    assert verdicts == {"open": False, "half-closed": False, "closed": True, "closed-unread": True}
    assert descriptors.client_gone(conn) is True          # a socket this side closed has no client


# --- launchd (C-16.6) ---------------------------------------------------------

def test_c16_6_the_plist_limit_stays_within_the_kernel_ceiling():
    assert descriptors.launchd_open_files(245760) == 65536
    assert descriptors.launchd_open_files(10240) == 10240
    assert descriptors.launchd_open_files(0) == 65536       # unreadable: the target


def test_c16_6_daemon_install_writes_the_open_file_limit_into_the_plist(tmp_path, monkeypatch):
    """C-16.6 the generated plist sets SoftResourceLimits.NumberOfFiles, and it reads back."""
    from subfleet import cli
    monkeypatch.setattr(descriptors, "kernel_open_files_ceiling", lambda: 245760)
    data = plistlib.loads(cli._plist(tmp_path))
    assert data["SoftResourceLimits"] == {"NumberOfFiles": 65536}
    assert "HardResourceLimits" not in data                 # the hard limit stays launchd's
    path = tmp_path / "com.subfleet.daemon.plist"
    path.write_bytes(cli._plist(tmp_path))
    assert descriptors.plist_open_files(path) == 65536
    path.write_bytes(plistlib.dumps({"Label": "x"}))
    assert descriptors.plist_open_files(path) is None
    assert descriptors.plist_open_files(tmp_path / "absent.plist") is None


@pytest.mark.skipif(sys.platform != "darwin", reason="kern.maxfilesperproc is macOS")
def test_c16_6_the_kernel_ceiling_is_read_on_macos():
    ceiling = descriptors.kernel_open_files_ceiling()
    assert isinstance(ceiling, int) and ceiling > 0


def test_c16_6_open_descriptors_counts_this_process():
    before = descriptors.open_descriptors()
    handle = open(os.devnull)
    try:
        assert descriptors.open_descriptors() == before + 1
    finally:
        handle.close()
    assert descriptors.limit_for_display(INF) is None and descriptors.limit_for_display(256) == 256
