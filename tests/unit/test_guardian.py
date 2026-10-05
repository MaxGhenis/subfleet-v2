"""Guardian receipt and publication tests with process identity explicitly mocked."""

import errno
import json
import os
import stat
import sys

import pytest

from subfleet import guardian


@pytest.fixture
def guardian_identity(monkeypatch):
    # Identity/setsid integration is exercised in tests/process when allowed.
    monkeypatch.setattr(guardian.os, "setsid", lambda: None)
    monkeypatch.setattr(guardian.signal, "signal", lambda *args: None)
    monkeypatch.setattr(guardian, "boot_id", lambda: "test-boot")
    monkeypatch.setattr(guardian, "proc_start", lambda pid: "test-start")
    previous = os.umask(0o077)
    yield
    os.umask(previous)


def test_guardian_receipts_stdout_stdin_environment(tmp_path, monkeypatch, guardian_identity):
    """C-5.1, C-5.2 and C-10.5 pass stdin and secrets only through environment."""
    prompt = tmp_path / "prompt.md"
    prompt.write_bytes(b"hello provider")
    monkeypatch.setenv("SUBFLEET_TEST_TOKEN", "secret-sentinel")
    command = [sys.executable, "-c", "import os,sys; assert os.getenv('SUBFLEET_TEST_TOKEN'); "
               "print(sys.stdin.read()); print('diagnostic',file=sys.stderr); raise SystemExit(4)"]
    assert guardian.run_guardian(command, attempt_dir=tmp_path, cwd=str(tmp_path),
        stdin_path=str(prompt), stdout_path=str(tmp_path / "stdout"), stderr_path=str(tmp_path / "stderr")) == 4
    assert (tmp_path / "stdout").read_bytes() == b"hello provider\n"
    assert (tmp_path / "stderr").read_bytes() == b"diagnostic\n"
    start = json.loads((tmp_path / "start.json").read_bytes())
    exit_info = json.loads((tmp_path / "exit.json").read_bytes())
    assert start["boot_id"] == "test-boot"
    assert exit_info["rc"] == 4 and exit_info["child_pid"] > 0
    assert exit_info["signal"] is None
    for path in (tmp_path / name for name in ("start.json", "exit.json", "stdout", "stderr")):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert b"secret-sentinel" not in path.read_bytes()


def test_guardian_spawn_failure_writes_exit_127(tmp_path, guardian_identity):
    """C-5.2 a missing provider command leaves a durable spawn_error and rc 127."""
    rc = guardian.run_guardian([str(tmp_path / "missing-executable")], attempt_dir=tmp_path,
        cwd=str(tmp_path), stdin_path=None, stdout_path=str(tmp_path / "stdout"), stderr_path=str(tmp_path / "stderr"))
    assert rc == 127
    receipt = json.loads((tmp_path / "exit.json").read_bytes())
    assert receipt["rc"] == 127 and receipt["spawn_error"]
    assert receipt["child_pid"] is None


def test_guardian_start_published_before_spawn(tmp_path, guardian_identity):
    """C-5.2 provider cannot execute until start.json has been atomically published."""
    rc = guardian.run_guardian([sys.executable, "-c", "from pathlib import Path; assert Path('start.json').exists()"],
        attempt_dir=tmp_path, cwd=str(tmp_path), stdin_path=None,
        stdout_path=str(tmp_path / "stdout"), stderr_path=str(tmp_path / "stderr"))
    assert rc == 0


@pytest.mark.parametrize('relay', [False, True])
def test_provider_identity_is_durable_before_wait_or_relay(tmp_path, monkeypatch, guardian_identity, relay):
    """C-5.5: an escaped provider and an unrelated reused PID can be distinguished."""
    provider = {'pid': 500, 'boot_id': 'test-boot', 'proc_start': 'provider-start'}
    monkeypatch.setattr(guardian, 'proc_start',
                        lambda pid: 'provider-start' if pid == 500 else 'guardian-start')
    def check():
        start = json.loads((tmp_path / 'start.json').read_text())
        assert start['child_pid'] == 500
        assert start['child_identity'] == provider
    class Child:
        pid = 500
        def poll(self):
            return None
        def wait(self):
            check()
            return 0
    monkeypatch.setattr(guardian.subprocess, 'Popen', lambda *args, **kwargs: Child())
    class Relay:
        def __init__(self, *args, **kwargs):
            pass
        def bind(self):
            pass
        def serve(self, fd, **kwargs):
            check()
            os.close(fd)
        def stop(self):
            pass
    from subfleet import relay as relay_module
    monkeypatch.setattr(relay_module, 'RelayServer', Relay)
    assert guardian.run_guardian(['provider'], attempt_dir=tmp_path, cwd=str(tmp_path),
        stdin_path=None, stdout_path=str(tmp_path / 'stdout'), stderr_path=str(tmp_path / 'stderr'),
        control_socket=str(tmp_path / 'relay.sock') if relay else None) == 0


def test_child_identity_inspection_failure_still_waits_and_writes_exit(tmp_path, monkeypatch, guardian_identity):
    from subfleet.procs import InspectionError
    def start(pid):
        if pid == 500:
            raise InspectionError('ps unavailable')
        return 'guardian-start'
    monkeypatch.setattr(guardian, 'proc_start', start)
    waited = []
    class Child:
        pid = 500
        def poll(self):
            return None
        def wait(self):
            waited.append(True)
            return 0
    monkeypatch.setattr(guardian.subprocess, 'Popen', lambda *args, **kwargs: Child())
    assert guardian.run_guardian(['provider'], attempt_dir=tmp_path, cwd=str(tmp_path),
        stdin_path=None, stdout_path=str(tmp_path / 'stdout'), stderr_path=str(tmp_path / 'stderr')) == 0
    assert waited == [True]
    assert json.loads((tmp_path / 'exit.json').read_text())['child_pid'] == 500


def test_guardian_launch_gate_eof_prevents_unrecorded_provider(tmp_path, guardian_identity):
    """C-4.2 a daemon crash before its starting commit cannot launch a provider."""
    reader, writer = os.pipe()
    os.close(writer)
    rc = guardian.run_guardian([sys.executable, "-c", "raise AssertionError('must not run')"],
        attempt_dir=tmp_path, cwd=str(tmp_path), stdin_path=None,
        stdout_path=str(tmp_path / "stdout"), stderr_path=str(tmp_path / "stderr"), launch_fd=reader)
    assert rc == 127
    assert not (tmp_path / "start.json").exists()
    assert not (tmp_path / "exit.json").exists()
    assert not (tmp_path / "stdout").exists()
    with pytest.raises(OSError):
        os.read(reader, 1)


def test_guardian_launch_gate_refuses_any_byte_but_the_release(tmp_path, guardian_identity):
    """C-5.1 a gate that reads anything but `1` (a byte the daemon did not write) starts
    nothing, writes neither start.json nor exit.json, and exits 127."""
    reader, writer = os.pipe()
    os.write(writer, b"0")
    os.close(writer)
    rc = guardian.run_guardian([sys.executable, "-c", "raise AssertionError('must not run')"],
        attempt_dir=tmp_path, cwd=str(tmp_path), stdin_path=None,
        stdout_path=str(tmp_path / "stdout"), stderr_path=str(tmp_path / "stderr"), launch_fd=reader)
    assert rc == 127
    assert not (tmp_path / "start.json").exists()
    assert not (tmp_path / "exit.json").exists()
    assert not (tmp_path / "stdout").exists()


def test_guardian_launch_gate_releases_after_starting_commit(tmp_path, guardian_identity):
    """C-4.2 an explicit daemon launch acknowledgement releases the guardian."""
    reader, writer = os.pipe()
    os.write(writer, b"1")
    os.close(writer)
    rc = guardian.run_guardian([sys.executable, "-c", "print('released')"],
        attempt_dir=tmp_path, cwd=str(tmp_path), stdin_path=None,
        stdout_path=str(tmp_path / "stdout"), stderr_path=str(tmp_path / "stderr"), launch_fd=reader)
    assert rc == 0
    assert (tmp_path / "stdout").read_text() == "released\n"


def test_atomic_publication_orders_fsync_rename_directory_fsync(tmp_path, monkeypatch):
    """C-8.1 publications sync their bytes before rename and directory afterward."""
    calls = []
    fsync = guardian.os.fsync
    rename = guardian.os.rename
    def sync(fd):
        calls.append("directory-fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file-fsync")
        fsync(fd)
    def move(source, destination):
        calls.append("rename")
        assert os.path.dirname(source) == str(tmp_path)
        rename(source, destination)
    monkeypatch.setattr(guardian.os, "fsync", sync)
    monkeypatch.setattr(guardian.os, "rename", move)
    path = tmp_path / "deliverable.md"
    guardian.atomic_publish(path, b"complete")
    assert calls == ["file-fsync", "rename", "directory-fsync"]
    assert path.read_bytes() == b"complete"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("boundary", ["file-fsync", "rename", "directory-fsync"])
def test_atomic_publication_enospc_never_leaves_partial_output(tmp_path, monkeypatch, boundary):
    """C-8.1 and C-20.3 ENOSPC preserves a complete old or new publication."""
    path = tmp_path / "deliverable.md"
    path.write_bytes(b"old")
    fsync = guardian.os.fsync
    rename = guardian.os.rename
    def sync(fd):
        phase = "directory-fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file-fsync"
        if boundary == phase:
            raise OSError(errno.ENOSPC, "disk full")
        fsync(fd)
    def move(source, destination):
        if boundary == "rename":
            raise OSError(errno.ENOSPC, "disk full")
        rename(source, destination)
    monkeypatch.setattr(guardian.os, "fsync", sync)
    monkeypatch.setattr(guardian.os, "rename", move)
    with pytest.raises(OSError) as error:
        guardian.atomic_publish(path, b"new complete bytes")
    assert error.value.errno == errno.ENOSPC
    assert path.read_bytes() == (b"new complete bytes" if boundary == "directory-fsync" else b"old")
    assert list(tmp_path.iterdir()) == [path]
