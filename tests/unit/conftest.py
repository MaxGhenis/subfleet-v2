"""Shared fixtures: a short-path state root and a fake daemon on a unix socket."""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

import pytest

from subfleet import protocol
from subfleet.contracts import Exit

# The socket path has a ~104 byte limit on macOS and pytest's tmp_path is long,
# so socket-bearing roots live directly under /tmp.
REFUSED_WORKDIR_PREFIXES = ("/" + "tmp/", "/private/" + "tmp/")  # C-2.4

SESSION_ENV = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID",
               "SUBFLEET_RUN_DETACH", "SUBFLEET_DAEMON_BIN")


@pytest.fixture
def root(monkeypatch) -> Path:
    """A `$SUBFLEET_HOME` short enough to hold a unix socket, outside any session."""
    path = Path(tempfile.mkdtemp(prefix="sf-", dir="/tmp"))
    monkeypatch.setenv("SUBFLEET_HOME", str(path))
    for name in SESSION_ENV:
        monkeypatch.delenv(name, raising=False)
    yield path
    shutil.rmtree(path, ignore_errors=True)


class FakeDaemon:
    """Serves the C-16 protocol from canned handlers and records every request."""

    def __init__(self, root: Path, handlers: dict[str, Callable[[protocol.Request], Any]]):
        self.root = root
        self.handlers = handlers
        self.requests: list[protocol.Request] = []
        self.path = root / "daemon.sock"
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self._server.listen(16)
        self._server.settimeout(0.1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def args(self, op: str) -> dict[str, Any]:
        """The arguments of the last request for `op`."""
        for request in reversed(self.requests):
            if request.op == op:
                return request.args
        raise AssertionError(f"no {op!r} request was made; saw "
                             f"{[r.op for r in self.requests]}")

    def ops(self) -> list[str]:
        return [request.op for request in self.requests]

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            with conn, conn.makefile("rb") as stream:
                line = stream.readline()
                if not line.strip():
                    return
                request = protocol.decode_request(line)
                self.requests.append(request)
                handler = self.handlers.get(request.op)
                if handler is None:
                    conn.sendall(protocol.encode(protocol.fail(
                        request.id, Exit.INVALID_INPUT, f"unknown op {request.op!r}")))
                    return
                value = handler(request)
                if isinstance(value, bytes):
                    conn.sendall(value)                 # a raw, possibly malformed line
                elif isinstance(value, protocol.Response):
                    conn.sendall(protocol.encode(value))
                else:
                    conn.sendall(protocol.encode(protocol.ok(request.id, value)))
        except OSError:
            pass

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        try:
            self._server.close()
        finally:
            self.path.unlink(missing_ok=True)


@pytest.fixture
def daemon(root):
    """Factory: `daemon({"op": handler})` starts a fake daemon in `root`."""
    servers: list[FakeDaemon] = []

    def start(handlers: dict[str, Callable[[protocol.Request], Any]]) -> FakeDaemon:
        server = FakeDaemon(root, handlers)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


@pytest.fixture
def workdir(tmp_path) -> Path:
    """A `-C` directory outside the temp roots C-2.4 refuses.

    The state root lives under the system temp dir because a unix socket path is
    capped near 104 bytes; a job workdir has no such limit, so it uses pytest's
    tmp_path, and falls back into the checkout if that ever lands under /tmp.
    """
    path = tmp_path / "repo"
    fallback = None
    if str(path.resolve()).startswith(REFUSED_WORKDIR_PREFIXES):
        fallback = Path(tempfile.mkdtemp(prefix="sfwork-",
                                         dir=str(Path(__file__).resolve().parents[2])))
        path = fallback
    path.mkdir(parents=True, exist_ok=True)
    yield path
    if fallback is not None:
        shutil.rmtree(fallback, ignore_errors=True)
