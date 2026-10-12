"""C-25.6: recheck the image saved at accept before person-only requests.

Exec before the first token read remains a documented limitation: image
identity cannot prove which program wrote already queued request bytes.

Native probes use only disposable compiled development-app stand-ins. Only
ancestry is faked marker-free, because a Subfleet lane's real ancestors veto
it. Pids, audit tokens, executable paths, exec and filesystem moves are real.
PID reuse is represented by an incorrect pid version for a live native app;
we do not churn host processes to force the kernel to recycle a pid.
"""

from __future__ import annotations

import contextlib
import array
import ctypes
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet.conversations import peers
from subfleet.conversations import service as service_module
from subfleet.conversations.service import ConversationService
from subfleet.conversations.store import ConversationError
from tests.e2e import dev_app
from tests.unit.test_conversation_service import FakeDaemon

MACOS = pytest.mark.skipif(sys.platform != "darwin", reason="macOS audit tokens and libproc")


def token(pid=10, version=1):
    return peers.AuditToken(struct.pack("=8I", 0, 0, 0, 0, 0, pid, 0, version))


def clean(pid):
    return [peers.Proc(pid, 1, "??", "native client")]


def judge(peer, app, **kwargs):
    return peers.judge(peer, chain=clean, allowed_paths=peers.allowed_executable_paths((str(app),)), **kwargs)


@pytest.mark.parametrize("reply", [OSError("unavailable"), b"short"])
def test_missing_or_malformed_socket_token_is_refused(reply):
    def getsockopt(*args):
        assert args == (0, 6, 32)
        if isinstance(reply, OSError):
            raise reply
        return reply
    peer = peers.peer_token(SimpleNamespace(getsockopt=getsockopt))
    verdict = judge(peer, "/app", executable=lambda audit: "/app")
    assert not verdict.person and "audit token" in verdict.reason


def test_a_bare_pid_cannot_authorize_a_person_only_request():
    assert not judge(10, "/app", executable=lambda audit: "/app").person


def test_ancestry_uses_the_pid_in_the_token_and_lookup_receives_the_whole_token():
    peer = token(123, 456)
    seen = []
    verdict = peers.judge(peer, chain=lambda pid: seen.append(pid) or clean(pid),
                          allowed_paths=frozenset({"/app"}),
                          executable=lambda audit: seen.append(audit) or "/app")
    assert verdict.person and verdict.pid == 123
    assert seen == [123, peer] and peer.pidversion == 456


@pytest.mark.parametrize("failure", ["library", "symbol", "lookup"])
def test_failed_or_unavailable_token_lookup_is_a_refusal(monkeypatch, failure):
    def load(*args, **kwargs):
        if failure == "library":
            raise OSError("libproc unavailable")
        if failure == "symbol":
            return SimpleNamespace()
        def lookup(audit, buffer, size):
            assert ctypes.cast(audit, ctypes.POINTER(peers._AuditToken)).contents.val[5] == 10
            return 0
        return SimpleNamespace(proc_pidpath_audittoken=lookup)
    monkeypatch.setattr(peers.ctypes, "CDLL", load)
    # A current pid that looks like the app must not rescue a failed token lookup.
    monkeypatch.setattr(peers, "executable_path", lambda pid: "/app")
    verdict = judge(token(), "/app")
    assert not verdict.person and "lookup failed or unavailable" in verdict.reason
    assert "reconnect from the Subfleet app" in verdict.reason


def test_a_kernel_path_swapped_after_lookup_is_not_resolved_again(tmp_path):
    app, caller, moved = (tmp_path / name for name in ("app", "caller", "moved"))
    app.touch()
    caller.touch()
    allowed = peers.allowed_executable_paths((str(app),))
    def lookup_then_swap(audit):
        caller.rename(moved)
        caller.symlink_to(app)
        return str(caller)                       # kernel answer before the swap
    verdict = peers.judge(token(), chain=clean, allowed_paths=allowed, executable=lookup_then_swap)
    assert os.path.realpath(caller) == str(app)
    assert not verdict.person


def test_daemon_captures_the_token_before_reading_and_keeps_it_for_every_request(monkeypatch):
    from subfleet.daemon import Daemon
    original, later = token(10, 1), token(10, 2)
    current = [original]
    seen, socket_reads = [], []

    class Connection:
        chunks = iter((b"first\n", b"second\n", b""))
        def getsockopt(self, *args):
            socket_reads.append(args)
            return current[0].value
        def settimeout(self, timeout):
            pass
        def recv(self, size):
            current[0] = later                  # exec after connection identity capture
            return next(self.chunks)
        def close(self):
            pass

    conn = Connection()
    daemon = Daemon.__new__(Daemon)
    daemon.stopping = threading.Event()
    daemon.connection_idle_s = 5
    daemon._connection_lock = threading.Lock()
    daemon._connections = {conn}
    daemon._shut_down = set()
    daemon._decode = lambda *args: SimpleNamespace(op="message.resolve", args={})
    def start(conn, lock, request, arrived, peer):
        seen.append(peer)
        future = Future()
        future.set_result(None)
        return future
    daemon._start_request = start
    monkeypatch.setattr("subfleet.descriptors.client_gone", lambda conn: True)
    accepted = peers.peer_token(conn)
    daemon._connection(conn, accepted)
    assert seen == [original, original]
    assert socket_reads == [(0, 6, 32)]


def test_daemon_reads_the_token_immediately_after_accept(tmp_path, monkeypatch):
    from subfleet import daemon as daemon_module
    events = []
    conn = SimpleNamespace(getsockopt=lambda *args: events.append("token") or token().value)

    class Listener:
        def bind(self, path):
            Path(path).touch()
        def listen(self, backlog):
            pass
        def settimeout(self, timeout):
            pass
        def accept(self):
            events.append("accept")
            return conn, None

    daemon = daemon_module.Daemon.__new__(daemon_module.Daemon)
    daemon.root = tmp_path
    daemon.stopping = threading.Event()
    daemon._control = lambda: None
    daemon.lock_watch = SimpleNamespace(start=lambda: None)
    daemon.close = lambda: daemon._control_thread.join(5)
    def hold(connection, peer):
        events.append("hold")
        assert connection is conn and peer == token() and peer.conn is conn
        daemon.stopping.set()
    daemon._hold_connection = hold
    monkeypatch.setattr(daemon_module.socket, "socket", lambda *args: Listener())
    monkeypatch.setattr(daemon_module.os, "chmod", lambda *args: None)
    daemon.serve_forever()
    assert events == ["accept", "token", "hold"]


@pytest.mark.parametrize("current, accepted, reason", [
    (token(10, 1), True, "the Subfleet app"),
    (token(11, 1), False, "process/image changed"),
    (token(10, 2), False, "process/image changed"),
    (None, False, "could not be re-read"),
])
def test_person_check_rechecks_the_saved_pid_and_version(monkeypatch, current, accepted, reason):
    conn = object()
    peer = peers.AuditToken(token().value, conn)
    monkeypatch.setattr(peers, "peer_token", lambda connection: current if connection is conn else None)
    verdict = judge(peer, "/app", executable=lambda audit: "/app")
    assert verdict.person is accepted and reason in verdict.reason


def test_service_freezes_development_configuration_and_real_paths_at_startup(tmp_path, monkeypatch):
    first, second, alias = tmp_path / "first", tmp_path / "second", tmp_path / "alias"
    first.touch()
    second.touch()
    alias.symlink_to(first)
    monkeypatch.setenv(dev_app.ENV, str(alias))
    root = tmp_path / "state"
    root.mkdir()
    daemon = FakeDaemon(root)
    service = ConversationService(daemon)
    try:
        allowed = service._allowed_app_paths
        assert str(alias) in allowed and str(first) in allowed
        alias.unlink()
        alias.symlink_to(second)
        monkeypatch.setenv(dev_app.ENV, str(second))
        observed = []
        def inspect(peer, **kwargs):
            observed.append(kwargs["allowed_paths"])
            return peers.judge(peer, chain=clean, executable=lambda audit: str(second), **kwargs)
        monkeypatch.setattr("subfleet.conversations.service.judge", inspect)
        with pytest.raises(ConversationError, match="this needs the Subfleet app"):
            service._person(token(), "answering an approval")
        assert observed == [allowed] and str(second) not in allowed
        assert service._allowed_app_paths is allowed
    finally:
        service.close()
        daemon.store.close()


@MACOS
def test_local_peertoken_on_a_real_socket_pair_contains_this_process_pid():
    with contextlib.ExitStack() as stack:
        a, b = socket.socketpair()
        stack.enter_context(a)
        stack.enter_context(b)
        raw = a.getsockopt(0, 6, 32)
        assert len(raw) == 32 and struct.unpack("=8I", raw)[5] == os.getpid()
        peer = peers.peer_token(a)
        assert peer.value == raw and peer.pid == os.getpid()
        assert peer.pidversion > 0 and peers.peer_pid(a) == peer.pid
        assert peers.executable_path_for_token(peer) == peers.executable_path(os.getpid())


@pytest.fixture(scope="module")
def compiled_app(tmp_path_factory):
    if sys.platform != "darwin":
        pytest.skip("macOS native process probes")
    app = dev_app.build(tmp_path_factory.mktemp("native-app"))
    if app is None:
        pytest.skip(dev_app.NO_COMPILER)
    return app


@pytest.fixture
def app(compiled_app, tmp_path):
    bundle = tmp_path / "Subfleet Dev.app"
    shutil.copytree(compiled_app.parents[2], bundle)
    return bundle / "Contents" / "MacOS" / "Subfleet Dev"


@pytest.fixture
def server():
    # Keep the UNIX socket pathname below macOS's 104-byte bound.
    with tempfile.TemporaryDirectory(prefix="sf-r191-", dir="/private/tmp") as directory:
        path = Path(directory) / "s"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.settimeout(30)
            listener.bind(str(path))
            listener.listen(2)
            yield listener, path


@contextlib.contextmanager
def child_process(argv, **kwargs):
    child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True, **kwargs)
    try:
        yield child
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=30)


@contextlib.contextmanager
def native_client(server, image, *, reply=True):
    listener, path = server
    with child_process([str(image), str(path), "{}"], stdin=subprocess.DEVNULL) as child:
        conn, _ = listener.accept()
        with conn, conn.makefile("rb") as stream:
            conn.settimeout(30)
            peer = peers.peer_token(conn)          # before reading or queuing the request
            assert peer is not None and peer.pid == child.pid
            assert stream.readline() == b"{}\n"
            yield child, conn, peer
            if not reply:
                return
            conn.sendall(b'{"ok":true}\n')
            stdout, stderr = child.communicate(timeout=30)
            assert child.returncode == 0, (stdout, stderr)


@MACOS
@pytest.mark.parametrize("through_symlink", [False, True], ids=["direct", "symlink"])
def test_the_native_app_is_accepted_directly_and_through_a_symlink(app, server, through_symlink):
    image = app
    if through_symlink:
        image = app.parent / "alias"
        image.symlink_to(app)
    with native_client(server, image) as (_, _, peer):
        assert peers.executable_path_for_token(peer) == str(app)
        assert judge(peer, app).person


@MACOS
def test_a_client_that_execs_into_the_app_after_connecting_is_refused(app, server):
    listener, path = server
    source = ("import os,socket,sys; c=socket.socket(socket.AF_UNIX); "
              "c.connect(sys.argv[1]); os.set_inheritable(c.fileno(),True); c.sendall(b'{}\\n'); "
              "sys.stdin.readline(); os.execv(sys.argv[2],[sys.argv[2],sys.argv[1],'{}'])")
    with child_process([sys.executable, "-c", source, str(path), str(app)], stdin=subprocess.PIPE) as child:
        conn, _ = listener.accept()
        with conn, conn.makefile("rb") as stream:
            conn.settimeout(30)
            original = peers.peer_token(conn)
            assert stream.readline() == b"{}\n" and original.pid == child.pid
            assert not judge(original, app).person
            child.stdin.write(b"\n")
            child.stdin.flush()
            # The new image connects again: a readiness barrier without pid polling.
            fresh, _ = listener.accept()
            with fresh, fresh.makefile("rb") as ready:
                fresh.settimeout(30)
                after_exec = peers.peer_token(fresh)
                assert ready.readline() == b"{}\n"
                assert after_exec.pid == original.pid and after_exec.pidversion != original.pidversion
                assert peers.executable_path(child.pid) == str(app)
                assert judge(after_exec, app).person
                verdict = judge(original, app)
                assert not verdict.person and "audit token" in verdict.reason
                fresh.sendall(b'{"ok":true}\n')


@contextlib.contextmanager
def exec_socket_pair(app, server):
    """A child-created socket pair, so LOCAL_PEERTOKEN identifies the child.

    Pass the server end with SCM_RIGHTS; keep the client end across exec.
    The development stand-in's new connection is the exec readiness barrier.
    """
    _, path = server
    payload = {"v": 1, "id": "queued", "op": "message.resolve", "args": {}}
    source = (
        "import array,os,socket,sys; control=socket.socket(fileno=int(sys.argv[1])); "
        "client,remote=socket.socketpair(); os.set_inheritable(client.fileno(),True); "
        "control.sendmsg([b'c'],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[remote.fileno()]))]); "
        "remote.close(); control.close(); client.sendall(sys.argv[4].encode()+b'\\n'); "
        "sys.stdin.readline(); os.execv(sys.argv[3],[sys.argv[3],sys.argv[2],'{}'])"
    )
    with contextlib.ExitStack() as stack:
        control, inherited = socket.socketpair()
        stack.enter_context(control)
        stack.enter_context(inherited)
        control.settimeout(30)
        with child_process([sys.executable, "-c", source, str(inherited.fileno()), str(path),
                            str(app), json.dumps(payload)], stdin=subprocess.PIPE,
                           pass_fds=(inherited.fileno(),)) as child:
            inherited.close()
            message, ancillary, flags, _ = control.recvmsg(1, socket.CMSG_SPACE(array.array("i").itemsize))
            assert message == b"c" and not flags & socket.MSG_CTRUNC
            fds = array.array("i")
            for level, kind, data in ancillary:
                assert (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS)
                fds.frombytes(data)
            assert len(fds) == 1
            with socket.socket(fileno=fds[0]) as conn:
                conn.settimeout(30)
                yield child, conn, payload


def person_request_after_exec(app, server, monkeypatch, *, capture_before_exec):
    listener, _ = server
    acted = []
    service = ConversationService.__new__(ConversationService)
    service.root = app.parents[3] / "state"
    service._allowed_app_paths = peers.allowed_executable_paths((str(app),))
    service._lock = threading.Lock()
    service._resolve_message = lambda args, verdict: acted.append(verdict.pid) or {"accepted": True}
    monkeypatch.setattr(service_module, "judge", lambda peer, **kwargs: peers.judge(peer, chain=clean, **kwargs))
    with exec_socket_pair(app, server) as (child, conn, payload):
        saved = peers.peer_token(conn) if capture_before_exec else None
        child.stdin.write(b"\n")
        child.stdin.flush()
        # No pid polling: this connection can only be opened by the new image.
        fresh, _ = listener.accept()
        with fresh, fresh.makefile("rb") as ready, conn.makefile("rb") as queued:
            fresh.settimeout(30)
            after_exec = peers.peer_token(fresh)
            assert ready.readline() == b"{}\n"
            assert after_exec.pid == child.pid and peers.executable_path(child.pid) == str(app)
            if saved is None:
                saved = peers.peer_token(conn)       # deliberately delayed until after exec
                assert saved.pidversion == after_exec.pidversion
            else:
                assert saved.pid == after_exec.pid and saved.pidversion != after_exec.pidversion
            assert json.loads(queued.readline()) == payload
            if capture_before_exec:
                with pytest.raises(ConversationError, match="process/image changed") as failure:
                    service.handle(payload["op"], payload["args"], saved)
                assert failure.value.code == 7 and failure.value.reason == "person-only"
                assert not acted
            else:
                assert service.handle(payload["op"], payload["args"], saved) == {"accepted": True}
                assert acted == [child.pid]
            fresh.sendall(b'{"ok":true}\n')
            stdout, stderr = child.communicate(timeout=30)
            assert child.returncode == 0, (stdout, stderr)


@MACOS
def test_a_token_change_between_accept_and_request_is_refused(app, server, monkeypatch):
    person_request_after_exec(app, server, monkeypatch, capture_before_exec=True)


@MACOS
@pytest.mark.documented_limitation
def test_exec_before_the_first_token_read_is_accepted_documented_limitation(app, server, monkeypatch):
    """A credential proving each request must deliberately change this acceptance."""
    person_request_after_exec(app, server, monkeypatch, capture_before_exec=False)


@MACOS
def test_a_client_whose_process_is_gone_is_refused(app, server):
    with native_client(server, app, reply=False) as (child, _, peer):
        assert judge(peer, app).person
        child.kill()
        child.wait(timeout=30)
        assert peers.executable_path_for_token(peer) is None
        verdict = judge(peer, app)
        assert not verdict.person and "audit token" in verdict.reason


@MACOS
def test_a_reused_pid_version_is_refused_even_if_the_current_image_is_the_app(app, server):
    with native_client(server, app) as (_, _, peer):
        words = list(struct.unpack("=8I", peer.value))
        words[7] = (words[7] + 1) % (2 ** 32)
        stale = peers.AuditToken(struct.pack("=8I", *words))
        assert stale.pid == peer.pid and peers.executable_path(stale.pid) == str(app)
        assert judge(peer, app).person
        verdict = judge(stale, app)
        assert not verdict.person and "audit token" in verdict.reason


@MACOS
def test_swapping_a_path_to_a_symlink_after_lookup_cannot_change_the_verdict(app, server):
    copied, moved = app.parent / "copied-app", app.parent / "untrusted-running-image"
    shutil.copy2(app, copied)
    allowed = peers.allowed_executable_paths((str(app),))
    with native_client(server, copied) as (_, _, peer):
        def lookup_then_swap(audit):
            answer = peers.executable_path_for_token(audit)
            assert answer == str(copied)
            copied.rename(moved)
            copied.symlink_to(app)
            return answer
        verdict = peers.judge(peer, chain=clean, allowed_paths=allowed, executable=lookup_then_swap)
        assert peers.executable_path_for_token(peer) == str(moved)
        assert os.path.realpath(copied) == str(app)
        assert not verdict.person


@MACOS
def test_an_update_that_moves_the_running_bundle_requires_an_app_restart(app, server):
    allowed = peers.allowed_executable_paths((str(app),))
    with native_client(server, app) as (_, _, peer):
        assert peers.judge(peer, chain=clean, allowed_paths=allowed).person
        bundle = app.parents[2]
        moved = bundle.with_name("Subfleet Dev Old.app")
        bundle.rename(moved)
        assert peers.executable_path_for_token(peer) == str(moved / "Contents" / "MacOS" / app.name)
        verdict = peers.judge(peer, chain=clean, allowed_paths=allowed)
        assert not verdict.person and "restart the app" in verdict.reason
