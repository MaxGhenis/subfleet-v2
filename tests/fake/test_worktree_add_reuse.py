"""C-6.8: every condition of finished reuse gates the workspace handoff."""
from __future__ import annotations

import itertools
import json
import logging
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

from subfleet import procs, uncommitted, worktree_add
from subfleet.adapters.base import AdapterError
from subfleet.daemon import Daemon
from subfleet.salvage import SalvageError


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True,
        env=worktree_add._git_env(None), text=True,
    ).stdout.strip()


@pytest.fixture
def workspace_case(tmp_path):
    """Each example owns its repository, registration and state root."""
    sequence = itertools.count()

    def make():
        case = tmp_path / str(next(sequence))
        repository, root = case / "repository", case / "state"
        repository.mkdir(parents=True)
        git(repository, "init", "--quiet", "--template=", "-b", "task/reuse")
        git(repository, "config", "user.name", "Test")
        git(repository, "config", "user.email", "test@example.invalid")
        (repository / "tracked").write_text("baseline\n")
        git(repository, "add", "tracked")
        git(repository, "commit", "--quiet", "-m", "baseline")
        commit = git(repository, "rev-parse", "HEAD")
        git(repository, "commit", "--quiet", "--allow-empty", "-m", "other")
        other = git(repository, "rev-parse", "HEAD")
        tree, jobdir = root / "worktrees" / "job", root / "jobs" / "job"
        tree.parent.mkdir(parents=True)
        jobdir.mkdir(parents=True)
        git(repository, "worktree", "add", "--quiet", "--detach", str(tree), commit)
        common = repository / ".git"
        admin = common / "worktrees" / "job"
        (jobdir / "manifest.json").write_text(json.dumps({
            "workspace": {"common_dir": str(common)},
        }))
        daemon = object.__new__(Daemon)
        daemon.root = root
        daemon.policy = {"caps": {"workspace_git_timeout_s": 10}}
        daemon._worktree_adds = {"job": (4242, None)}
        job = {
            "job_id": "job", "kind": "dispatch", "workdir": str(repository),
            "workdir_head": commit, "worktree": None,
            "sandbox": "workspace-write", "in_place": False,
        }
        return daemon, job, repository, tree, jobdir, admin, other

    return make


VALID = {
    "path": "directory", "gitfile": "own", "lock": "none", "head": "expected",
    "index": "clean", "copy": "absent", "census": "empty",
}


CASES = st.fixed_dictionaries({
    "path": st.sampled_from(["directory", "link", "file", "missing"]),
    "gitfile": st.sampled_from(["own", "absent", "other-tree", "other-repo", "link"]),
    "lock": st.sampled_from(["none", "initializing", "person", "retention"]),
    "head": st.sampled_from(["expected", "other"]),
    "index": st.sampled_from(["clean", "staged"]),
    "copy": st.sampled_from(["absent", "applied", "pending", "wrong-tree"]),
    "census": st.sampled_from(["empty", "group", "marker", "both", "unverifiable", "error"]),
})


class RebuildRequired(Exception):
    """The old tree was rejected; stop before altering the example's evidence."""


@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(case=CASES)
@example(case=VALID)
@example(case={**VALID, "copy": "applied"})
@example(case={**VALID, "copy": "pending"})
@example(case={**VALID, "copy": "wrong-tree"})
@example(case={**VALID, "path": "link"})
@example(case={**VALID, "path": "file"})
@example(case={**VALID, "path": "missing"})
@example(case={**VALID, "gitfile": "absent"})
@example(case={**VALID, "gitfile": "other-tree"})
@example(case={**VALID, "gitfile": "other-repo"})
@example(case={**VALID, "gitfile": "link"})
@example(case={**VALID, "lock": "initializing"})
@example(case={**VALID, "lock": "person"})
@example(case={**VALID, "lock": "retention"})
@example(case={**VALID, "head": "other"})
@example(case={**VALID, "index": "staged"})
@example(case={**VALID, "census": "group"})
@example(case={**VALID, "census": "marker"})
@example(case={**VALID, "census": "both"})
@example(case={**VALID, "census": "unverifiable"})
@example(case={**VALID, "census": "error"})
def test_generated_reuse_handoff_matches_every_finished_condition(workspace_case, case):
    """Invariant 3's pure oracle includes the caller's copy marker and census.

    The returned workspace is the pre-attempt tree handed to admission. Rebuild
    is intercepted at its first removal so a rejected tree cannot later pass by
    being replaced. Every ownership and baseline git read is real, while the
    process table is a generated value and never reads a host process.
    """
    daemon, job, repository, tree, jobdir, admin, other = workspace_case()
    if case["index"] == "staged":
        (tree / "tracked").write_text("staged edit\n")
        git(tree, "add", "tracked")
    if case["head"] == "other":
        (admin / "HEAD").write_text(other + "\n")
    if case["lock"] != "none":
        reason = {"initializing": "initializing\n", "person": "person's claim\n",
                  "retention": "subfleet retention: job\n"}[case["lock"]]
        (admin / "locked").write_text(reason)
    if case["gitfile"] == "absent":
        (tree / ".git").unlink()
    elif case["gitfile"] in ("other-tree", "other-repo"):
        source = repository
        if case["gitfile"] == "other-repo":
            source = repository.parent / "foreign-repository"
            git(repository, "clone", "--quiet", str(repository), str(source))
        foreign = repository.parent / "foreign-tree"
        git(source, "worktree", "add", "--quiet", "--detach", str(foreign), job["workdir_head"])
        (tree / ".git").write_bytes((foreign / ".git").read_bytes())
    elif case["gitfile"] == "link":
        saved = repository.parent / "saved-gitfile"
        saved.write_bytes((tree / ".git").read_bytes())
        (tree / ".git").unlink()
        (tree / ".git").symlink_to(saved)
    if case["path"] == "link":
        original = repository.parent / "original-tree"
        tree.rename(original)
        tree.symlink_to(original, target_is_directory=True)
    elif case["path"] in ("file", "missing"):
        shutil.rmtree(tree)
        if case["path"] == "file":
            tree.write_text("a file is not a checkout\n")
    if case["copy"] != "absent":
        kept = jobdir / uncommitted.DIRECTORY
        (kept / uncommitted.FILES).mkdir(parents=True)
        (kept / uncommitted.PATCH).write_bytes(b"")
        if case["copy"] != "pending":
            (kept / uncommitted.APPLIED).write_text(
                str(tree) if case["copy"] == "applied" else str(tree.parent / "another-tree"))

    pids = frozenset({4243})
    observations = {
        "empty": procs.Containment(),
        "group": procs.Containment(group_pids=pids),
        "marker": procs.Containment(marker_pids=pids),
        "both": procs.Containment(group_pids=pids, marker_pids=pids),
        "unverifiable": procs.Containment(unverifiable=True),
        "error": procs.Containment(errors=("inspection failed",)),
    }
    censuses, rebuilds = [], []

    def census(group, *args, **kwargs):
        censuses.append(group)
        return observations[case["census"]]

    def reject_reuse(*args, **kwargs):
        rebuilds.append(args)
        raise RebuildRequired

    expected = (case["path"] == "directory" and case["gitfile"] == "own"
                and case["lock"] != "initializing" and case["head"] == "expected"
                and case["index"] == "clean" and case["copy"] in ("absent", "applied")
                and case["census"] == "empty")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(procs, "containment", census)
        patch.setattr(daemon, "_discard_worktree", reject_reuse)
        if expected:
            workspace, head, baseline, skipped = daemon._workspace(job)
            assert (workspace, head) == (str(tree), job["workdir_head"])
            assert baseline == git(repository, "rev-parse", job["workdir_head"] + "^{tree}")
            assert skipped == []
        elif case["census"] != "empty":
            with pytest.raises(SalvageError) as error:
                daemon._workspace(job)
            assert error.value.transient
        else:
            with pytest.raises(RebuildRequired):
                daemon._workspace(job)
    assert censuses == [4242]
    assert bool(rebuilds) is (not expected and case["census"] == "empty")


def test_file_at_allocation_gets_the_proof_refusal_and_preserves_its_index(workspace_case, monkeypatch):
    """A file's absent .git child is ENOTDIR, which must still reach ownership proof."""
    daemon, job, _, tree, _, admin, _ = workspace_case()
    original_index = (admin / "index").read_bytes()
    shutil.rmtree(tree)
    tree.write_text("preserve this person's bytes\n")
    daemon.store = SimpleNamespace(get_job=lambda job_id: job)
    daemon.log = logging.getLogger(__name__)
    monkeypatch.setattr(procs, "containment", lambda *args, **kwargs: procs.Containment())
    with pytest.raises(AdapterError) as error:
        daemon._workspace(job)
    assert error.value.reason == "worktree-unproven"
    assert tree.read_text() == "preserve this person's bytes\n"
    assert (admin / "index").read_bytes() == original_index
