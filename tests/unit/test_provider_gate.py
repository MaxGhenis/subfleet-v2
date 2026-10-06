"""The real waiting launcher must never exec before durable publication."""

import json
import os
import sys

import pytest

from subfleet import guardian
from tests.unit.test_guardian import guardian_identity  # noqa: F401


def run(tmp_path):
    return guardian.run_guardian(
        [sys.executable, "-c", "from pathlib import Path; Path('provider-ran').touch()"],
        attempt_dir=tmp_path, cwd=str(tmp_path), stdin_path=None,
        stdout_path=str(tmp_path / "stdout"), stderr_path=str(tmp_path / "stderr"))


def test_real_provider_exec_has_its_own_durable_launch_identity(tmp_path, guardian_identity):
    command = [sys.executable, "-c", "import json,os; from pathlib import Path; "
               "s=json.loads(Path('start.json').read_text()); "
               "assert s['child_pid']==s['child_identity']['pid']==os.getpid(); "
               "assert s['child_identity']['proc_start']=='test-start'"]
    assert guardian.run_guardian(command, attempt_dir=tmp_path, cwd=str(tmp_path),
        stdin_path=None, stdout_path=str(tmp_path / "stdout"),
        stderr_path=str(tmp_path / "stderr")) == 0


@pytest.mark.parametrize("boundary", ["identity", "publication", "directory-fsync"])
def test_publication_crash_reaps_launcher_without_executing_provider(
        tmp_path, monkeypatch, guardian_identity, boundary):
    original_start, receipt, sync = guardian.proc_start, guardian._receipt, guardian.os.fsync
    guardian_pid = os.getpid()
    def start(pid):
        if pid != guardian_pid and boundary == "identity":
            raise RuntimeError("publication crash")
        return original_start(pid)
    def publish(path, value):
        if "child_pid" in value:
            if boundary == "publication":
                raise RuntimeError("publication crash")
            if boundary == "directory-fsync":
                import stat
                def fsync(fd):
                    if stat.S_ISDIR(os.fstat(fd).st_mode):
                        raise RuntimeError("publication crash")
                    sync(fd)
                monkeypatch.setattr(guardian.os, "fsync", fsync)
        receipt(path, value)
    monkeypatch.setattr(guardian, "proc_start", start)
    monkeypatch.setattr(guardian, "_receipt", publish)
    with pytest.raises(RuntimeError, match="publication crash"):
        run(tmp_path)
    assert not (tmp_path / "provider-ran").exists()
    assert not (tmp_path / "exit.json").exists()


def test_uninspectable_launcher_never_executes_provider(tmp_path, monkeypatch, guardian_identity):
    guardian_pid = os.getpid()
    def start(pid):
        if pid != guardian_pid:
            raise guardian.InspectionError("unavailable")
        return "guardian-start"
    monkeypatch.setattr(guardian, "proc_start", start)
    assert run(tmp_path) == 127
    assert not (tmp_path / "provider-ran").exists()
    receipt = json.loads((tmp_path / "exit.json").read_text())
    assert receipt["child_pid"] is None and "execution gate refused" in receipt["spawn_error"]


def test_error_pipe_creation_failure_closes_the_launch_pipe(tmp_path, monkeypatch, guardian_identity):
    original = guardian.pipe_above_stdio
    descriptors = []
    def pipe():
        if descriptors:
            raise OSError("pipe creation failed")
        pair = original()
        descriptors.extend(pair)
        return pair
    monkeypatch.setattr(guardian, "pipe_above_stdio", pipe)
    assert run(tmp_path) == 127
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)
    assert not (tmp_path / "provider-ran").exists()
