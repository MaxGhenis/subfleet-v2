"""D-KILL3: in-process cwd ABI, resolved path boundaries, and read failures."""

import ctypes
import errno
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from subfleet import procs

pytestmark = pytest.mark.real_cwd


def reader(pidinfo):
    kernel = object.__new__(procs.Darwin)
    kernel._pidinfo = pidinfo
    return kernel


def test_d_kill3_cwd_uses_only_the_kernel_cwd_path_record():
    def pidinfo(pid, flavor, arg, buffer, size):
        assert (pid, flavor, arg, size) == (123, 9, 0, 2352)
        # The root-dir record is different; it must never be read as cwd.
        ctypes.memmove(ctypes.addressof(buffer) + 152, b"/tmp/owned/nested\0", 18)
        ctypes.memmove(ctypes.addressof(buffer) + 1328, b"/other/root\0", 12)
        return size
    assert reader(pidinfo).cwd(123) == "/tmp/owned/nested"


@pytest.mark.parametrize("failure", [errno.ESRCH, errno.EPERM])
def test_d_kill3_cwd_skips_gone_or_inaccessible_processes(failure):
    def pidinfo(*args):
        ctypes.set_errno(failure)
        return 0
    assert reader(pidinfo).cwd(123) is None


@pytest.mark.parametrize("shape", ["missing", "short", "unexpected-error", "unterminated"])
def test_d_kill3_cwd_does_not_turn_an_unreadable_record_into_empty(shape):
    def pidinfo(pid, flavor, arg, buffer, size):
        if shape == "short":
            return size - 1
        if shape == "unterminated":
            ctypes.memset(ctypes.addressof(buffer) + 152, ord("a"), 1024)
            return size
        ctypes.set_errno(errno.EIO)
        return 0
    with pytest.raises(procs.InspectionError, match="cwd inspection"):
        reader(None if shape == "missing" else pidinfo).cwd(123)


def test_d_kill3_cwd_comparison_resolves_both_paths_and_respects_components(tmp_path, monkeypatch):
    target = tmp_path / "owned"
    target.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(target, target_is_directory=True)
    cwds = {1: str(target), 2: str(link / "nested"), 3: str(target) + "-other", 4: None}
    monkeypatch.setattr(procs, "kernel", SimpleNamespace(pids=lambda: set(cwds), cwd=cwds.get))
    assert procs.worktree_cwd_pids(link) == {1, 2}


def test_d_kill3_a_cwd_listing_failure_remains_an_inspection_failure(monkeypatch):
    def failed():
        raise procs.InspectionError("process listing unavailable")
    monkeypatch.setattr(procs, "kernel", SimpleNamespace(pids=failed))
    with pytest.raises(procs.InspectionError):
        procs.worktree_cwd_pids("/scratch/owned")


def test_cwd_comparison_uses_disk_case_on_case_insensitive_apfs(tmp_path, monkeypatch):
    target = tmp_path / "wt"
    target.mkdir()
    alternate = tmp_path / "WT"
    if not alternate.is_dir():
        pytest.skip("requires a case-insensitive filesystem")
    monkeypatch.setattr(procs, "kernel", SimpleNamespace(pids=lambda: {123}, cwd=lambda pid: str(target)))
    assert procs.worktree_cwd_pids(alternate) == {123}


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS native zombie state")
def test_d_kill3_an_unreaped_zombie_does_not_hold_every_worktree():
    """Only our unreaped child is inspected, and its scratch group is reaped here."""
    child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while procs.kernel.process(child.pid) is not None:
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert procs.kernel.cwd(child.pid) is None
    finally:
        child.wait(timeout=10)
