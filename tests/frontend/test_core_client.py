"""The app's socket client and endpoint (design D-21, §12; C-29.1, C-29.2, C-29.4).

A small AF_UNIX server stands in for the daemon where the test needs to see or
break the connection itself; `test_core_protocol.py` covers the answers the
daemon's own code gives.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time

import pytest

from subfleet import protocol
from tests.frontend.conftest import needs_swift, run_probe, write_json

pytestmark = needs_swift


class RawServer:
    """Accepts connections; per connection reads one line and then `answer`s,
    stays `silent`, or `close`s. Records every line and whether the client
    closed its side after the answer."""

    def __init__(self, path: Path, behaviour: str = "answer"):
        self.path = path
        self.behaviour = behaviour
        self.lines: list[dict] = []
        self.connections = 0
        self.client_closed_after_answer: list[bool] = []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.stop = threading.Event()
        self.held: list[socket.socket] = []
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            self.connections += 1
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn: socket.socket) -> None:
        reader = conn.makefile("rb")
        line = reader.readline()
        if not line:
            conn.close()
            return
        request = json.loads(line)
        self.lines.append(request)
        if self.behaviour == "silent":
            self.held.append(conn)
            return
        if self.behaviour == "close":
            conn.close()
            return
        conn.sendall(protocol.encode(protocol.ok(request["id"], {"requested": True, "running": False})))
        conn.settimeout(5)
        try:
            self.client_closed_after_answer.append(conn.recv(1) == b"")
        except OSError:
            self.client_closed_after_answer.append(False)
        conn.close()

    def close(self) -> None:
        self.stop.set()
        for conn in self.held:
            conn.close()
        self.sock.close()


@pytest.fixture
def short_dir():
    """AF_UNIX paths on macOS hold at most 103 bytes; pytest's tmp_path is too long."""
    directory = Path(tempfile.mkdtemp(prefix="sf-cl-", dir="/tmp"))
    yield directory
    for child in directory.iterdir():
        child.unlink()
    directory.rmdir()


def test_c16_one_request_per_connection_with_a_unique_id(core_probe, tmp_path, short_dir):
    server = RawServer(short_dir / "daemon.sock")
    try:
        args = write_json(tmp_path / "a.json", {})
        answers = [run_probe(core_probe, "call", server.path, "catalog.refresh", args) for _ in range(3)]
    finally:
        server.close()
    assert [a["ok"] for a in answers] == [{"requested": True, "running": False}] * 3
    assert server.connections == 3 and len(server.lines) == 3
    assert all(line["v"] == 1 and line["op"] == "catalog.refresh" and line["args"] == {} for line in server.lines)
    ids = [line["id"] for line in server.lines]
    assert len(set(ids)) == 3 and all(i.startswith("app-") for i in ids)
    assert server.client_closed_after_answer == [True, True, True]


def test_design_12_a_silent_daemon_times_out(core_probe, tmp_path, short_dir):
    """No answer within the op's deadline is `timedOut`, and the call returns (15 s default)."""
    server = RawServer(short_dir / "daemon.sock", behaviour="silent")
    try:
        started = time.monotonic()
        answer = run_probe(core_probe, "call", server.path, "capabilities", write_json(tmp_path / "a.json", {}),
                           timeout=60)
        elapsed = time.monotonic() - started
    finally:
        server.close()
    assert answer["error"]["kind"] == "timedOut" and answer["error"]["seconds"] == 15
    assert 14.5 <= answer["elapsed"] <= 25 and elapsed < 40


def test_c29_2_a_daemon_that_is_down_or_hangs_up_is_a_state(core_probe, tmp_path, short_dir):
    args = write_json(tmp_path / "a.json", {})
    missing = run_probe(core_probe, "call", short_dir / "daemon.sock", "capabilities", args)
    assert missing["error"]["kind"] == "unavailable" and "not running" in missing["error"]["message"]
    stale = short_dir / "stale.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(stale))
    listener.close()                     # a socket file nobody listens on: connection refused
    refused = run_probe(core_probe, "call", stale, "capabilities", args)
    assert refused["error"]["kind"] == "unavailable"
    long_path = "/tmp/" + "x" * 120 + "/daemon.sock"
    too_long = run_probe(core_probe, "call", long_path, "capabilities", args)
    assert too_long["error"]["kind"] == "unavailable" and "bytes" in too_long["error"]["message"]
    server = RawServer(short_dir / "daemon.sock", behaviour="close")
    try:
        closed = run_probe(core_probe, "call", server.path, "capabilities", args)
    finally:
        server.close()
    assert closed["error"]["kind"] == "transport"


def endpoint(core_probe, home: Path, flavor: str, override: str | None = None) -> dict:
    return run_probe(core_probe, "endpoint", home, flavor, *([override] if override is not None else []))


def test_c29_1_the_endpoint_is_subfleet_home_or_the_default(core_probe, tmp_path):
    home = tmp_path / "home"
    (home / ".subfleet").mkdir(parents=True)
    assert endpoint(core_probe, home, "release") == {
        "ready": str(home / ".subfleet"), "socket": str(home / ".subfleet/daemon.sock"),
        "status": str(home / ".subfleet/status.json")}
    assert endpoint(core_probe, home, "release", "~/elsewhere")["ready"] == str(home / "elsewhere")
    assert endpoint(core_probe, home, "release", str(tmp_path / "dev root"))["socket"] == str(tmp_path / "dev root/daemon.sock")


@pytest.mark.parametrize("override", [None, "", "~/.subfleet", "~/.subfleet/", "~/.Subfleet", "{home}/./.subfleet",
                                      "{link}", "~"])
def test_c29_4_the_development_build_refuses_the_installed_state_root(core_probe, tmp_path, override):
    """D-21: however SUBFLEET_HOME spells ~/.subfleet (or leaves it unset), the dev build refuses it."""
    home = tmp_path / "home"
    (home / ".subfleet").mkdir(parents=True)
    link = tmp_path / "link-to-installed"
    link.symlink_to(home / ".subfleet")
    if override == "~":
        # `~` alone is the home directory, not ~/.subfleet: allowed.
        assert "ready" in endpoint(core_probe, home, "development", override)
        return
    spelled = None if override is None else override.format(home=home, link=link)
    answer = endpoint(core_probe, home, "development", spelled)
    assert "refused" in answer, answer
    assert "~/.subfleet" in answer["refused"]
    assert "ready" in endpoint(core_probe, home, "release", spelled)      # the release build uses it
    assert "ready" in endpoint(core_probe, home, "development", str(tmp_path / "dev-root"))


def test_c29_4_the_refused_development_build_never_connects(core_probe, tmp_path):
    """The refusal comes before any socket: a listener at ~/.subfleet/daemon.sock sees nothing."""
    home = Path(tempfile.mkdtemp(prefix="sf-h-", dir="/tmp"))
    try:
        (home / ".subfleet").mkdir()
        server = RawServer(home / ".subfleet" / "daemon.sock")
        try:
            refused = run_probe(core_probe, "connect-current", home, "development")
            assert "refused" in refused and refused["banner"] == "This development build is not connected"
            assert server.connections == 0
            # The release build, same home, connects (and finds this is no conversation daemon).
            released = run_probe(core_probe, "connect-current", home, "release")
            assert server.connections == 1 and "incompatible" in released
        finally:
            server.close()
        down = run_probe(core_probe, "connect-current", home, "development", str(home / "dev"))
        assert "down" in down and down["banner"] == "The Subfleet daemon is not reachable"
    finally:
        for path in sorted(home.rglob("*"), reverse=True):
            path.unlink() if not path.is_dir() else path.rmdir()
        home.rmdir()
