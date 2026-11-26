"""C-6.8/F7: ownership pressure cannot masquerade as an absent source or copy."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from subfleet import daemon as daemon_module, uncommitted
from subfleet.daemon import Daemon, after
from subfleet.retention_git import registration_of
from subfleet.salvage import SalvageError, TRANSIENT_ERRNOS
from tests.fake.test_state_contract import state_daemon  # noqa: F401
from tests.fake.test_workspace_contract import repository


@pytest.mark.parametrize("code", sorted(TRANSIENT_ERRNOS))
def test_legacy_source_directory_read_preserves_transient_errno_and_cause(tmp_path, monkeypatch, code):
    """A legacy job without a repository record must retry a failed source stat."""
    daemon = object.__new__(Daemon)
    daemon._recorded_common = lambda job: None
    source = tmp_path / "source"
    source.mkdir()
    failure = OSError(code, os.strerror(code))
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if Path(path) == source:
            raise failure
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(daemon_module.os, "stat", stat)
    with pytest.raises(SalvageError) as caught:
        daemon._tree_registration({"workdir": str(source)}, str(tmp_path / "missing-tree"), 5)
    assert caught.value.transient
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize("code", sorted(TRANSIENT_ERRNOS))
def test_source_selection_preserves_transient_errno_and_never_starts_add(tmp_path, monkeypatch, code):
    """Pressure must not select a fallback that drops a caller's sparse settings."""
    daemon = object.__new__(Daemon)
    daemon.root = tmp_path
    daemon.policy = {"caps": {"worktree_add_timeout_s": 5}}
    daemon._require_add_empty = lambda job: None
    source = tmp_path / "source"
    source.mkdir()
    common = str(tmp_path / "common")
    job = {"job_id": "job", "workdir": str(source), "workdir_head": "commit"}
    failure = OSError(code, os.strerror(code))
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if Path(path) == source:
            raise failure
        return real_stat(path, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("source selection pressure must prevent starting an add")

    monkeypatch.setattr(daemon_module.os, "stat", stat)
    monkeypatch.setattr(daemon_module.subprocess, "Popen", forbidden)
    for operation in (lambda: daemon._tree_git(job, common, 5),
                      lambda: daemon._add_worktree(job, str(tmp_path / "tree"), common, 5)):
        with pytest.raises(SalvageError) as caught:
            operation()
        assert caught.value.transient
        assert caught.value.__cause__ is failure


@pytest.mark.parametrize("code", sorted(TRANSIENT_ERRNOS))
def test_transient_copy_marker_read_waits_without_add_or_removal(state_daemon, monkeypatch, code):  # noqa: F811
    """An unreadable applied marker keeps a finished copy tree intact until retry."""
    daemon, harness = state_daemon
    repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    job = daemon.store.get_job(job_id)
    jobdir = daemon.root / "jobs" / job_id
    kept = jobdir / uncommitted.DIRECTORY
    (kept / uncommitted.FILES).mkdir(parents=True)
    (kept / uncommitted.PATCH).write_bytes(b"")
    tree = Path(daemon._workspace(job)[0])
    admin = registration_of(daemon._recorded_common(job), tree)
    assert admin is not None
    marker = kept / uncommitted.APPLIED
    assert uncommitted.applied(jobdir, tree)
    before = (tree.stat().st_ino, (tree / ".git").read_bytes(), (admin / "index").read_bytes())
    failure = OSError(code, os.strerror(code))
    real_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == marker:
            raise failure
        return real_read_text(path, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("a transient ownership read must not add or remove a worktree")

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(daemon, "_add_worktree", forbidden)
    monkeypatch.setattr(daemon, "_discard_worktree", forbidden)
    assert not uncommitted.applied(jobdir, tree)  # Preserve existing boolean API.
    with pytest.raises(SalvageError) as caught:
        daemon._workspace(job)
    assert caught.value.transient
    assert caught.value.__cause__ is failure

    daemon._admit()
    waiting = daemon.store.get_job(job_id)
    assert (waiting["state"], waiting["wait_reason"], waiting["rc"]) == ("waiting", "workspace", None)
    assert not daemon.store.list_attempts(job_id)
    assert before == (tree.stat().st_ino, (tree / ".git").read_bytes(), (admin / "index").read_bytes())

    monkeypatch.setattr(Path, "read_text", real_read_text)
    daemon.store.update_job(job_id, next_check_at=after(-1))
    daemon._admit()
    assert daemon.store.get_job(job_id)["state"] == "running"
    assert len(daemon.store.list_attempts(job_id)) == 1
    assert tree.stat().st_ino == before[0]
