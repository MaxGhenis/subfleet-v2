"""Optional Git probes must distinguish a missing object from host pressure."""

import os
import subprocess

import pytest

from subfleet import salvage


def nested_head(path):
    return salvage._head_status(os.fsencode(path / ".git"), {}, None)


@pytest.mark.parametrize("probe", [salvage.git_head, salvage.git_toplevel, nested_head],
                         ids=["head", "toplevel", "nested-head"])
@pytest.mark.parametrize("stderr", [
    b"fatal: cannot read object: No space left on device\n",
    b"fatal: Out of memory, malloc failed (tried to allocate 123 bytes)\n",
])
def test_optional_probe_raises_on_transient_git_failure(tmp_path, monkeypatch, probe, stderr):
    """A full disk or OOM has not established that the checkout or its HEAD is absent.
    Treating it as absent can admit a writable attempt with no baseline snapshot."""
    monkeypatch.setattr(salvage.subprocess, "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 128, b"", stderr))
    with pytest.raises(salvage.SalvageError) as caught:
        probe(tmp_path)
    assert caught.value.transient


@pytest.mark.parametrize("probe,missing", [(salvage.git_head, None), (salvage.git_toplevel, None),
                                          (nested_head, 128)], ids=["head", "toplevel", "nested-head"])
def test_optional_probe_still_reports_a_missing_repository(tmp_path, monkeypatch, probe, missing):
    monkeypatch.setattr(salvage.subprocess, "run", lambda command, **kwargs:
        subprocess.CompletedProcess(command, 128, b"", b"fatal: not a git repository\n"))
    assert probe(tmp_path) == missing


@pytest.fixture
def killed_git(tmp_path, monkeypatch):
    """A `git` first on PATH that kills itself with SIGKILL, as the kernel's memory-pressure
    kill or a signal to the daemon's process group would: it never finishes."""
    bindir = tmp_path / "killed-bin"
    bindir.mkdir()
    script = bindir / "git"
    script.write_text("#!/bin/sh\nkill -KILL $$\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def nested_head_as_salvage_asks(path):
    """`_head_status` with the environment salvage gives it (the daemon's, so PATH too)."""
    return salvage._head_status(os.fsencode(path / ".git"), dict(os.environ), None)


@pytest.mark.parametrize("probe", [salvage.git_head, salvage.git_toplevel, nested_head_as_salvage_asks,
                                   salvage.git_branch, salvage.validate_writable_workdir],
                         ids=["head", "toplevel", "nested-head", "branch", "main-refusal"])
def test_a_git_killed_by_a_signal_is_never_an_answer(tmp_path, killed_git, probe):
    """C-6.8 (adversarial review of the round-3 branch): git's return code is negative when a
    signal killed it, which read as "no HEAD" or "no branch", so a writable job on `main`
    passed the C-13.2 refusal. It did not finish: transient, as a timeout is."""
    with pytest.raises(salvage.SalvageError, match="was killed by SIGKILL$") as caught:
        probe(tmp_path)
    assert caught.value.transient


def test_an_add_killed_by_a_signal_is_transient_and_tried_no_further(tmp_path, monkeypatch):
    """A killed `add -A` raises at once, as a timed-out one does: no listing of nested
    repositories follows, since the add said nothing about the worktree."""
    calls = []

    def run(command, **kwargs):
        calls.append(command[3:5])
        return subprocess.CompletedProcess(command, -9, b"", b"")
    monkeypatch.setattr(salvage.subprocess, "run", run)
    with pytest.raises(salvage.SalvageError, match="git add was killed by SIGKILL") as caught:
        salvage._add_all(tmp_path, {})
    assert caught.value.transient and calls == [["add", "-A"]]
