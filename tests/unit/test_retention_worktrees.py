"""Allocated worktree retention uses isolated repositories and no process inspection."""

import hashlib
from pathlib import Path
import subprocess

import pytest

from subfleet import retention
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.salvage import salvage
from subfleet.store import Store


def git(cwd, *args):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture
def owned(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "feature")
    (repository / "tracked").write_text("base" * 1024)
    git(repository, "add", "tracked")
    git(repository, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "baseline")
    root = tmp_path / "state"
    root.mkdir()
    worktree = root / "worktrees" / "job"
    git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    with Store(root / "state.sqlite3") as store:
        store.add_job(job_id="job", request_id="request", payload_digest="digest", kind="dispatch",
                      workdir=str(repository), worktree=str(worktree), prompt_path="/prompt",
                      sandbox="workspace-write", state="succeeded")
        artifact_dir = root / "jobs" / "job"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "stdout").write_bytes(b"output")
        yield store, root, repository, worktree


def record_salvage(store, worktree):
    store.put_lane(Lane("codex-1", "codex", "codex:one", Credential("codex", "/home/one", "home"), "/home/one", LaneOwner.V2, False))
    store.add_attempt(attempt_id="job/a1", job_id="job", seq=1, lane_id="codex-1", model_requested="gpt-6-astra", state="succeeded")
    result = salvage(worktree, git(worktree, "rev-parse", "HEAD"), 1, timestamp="2026-09-05T12:00:00Z")
    store.add_artifact("job/a1", "salvage", result.ref, hashlib.sha256(result.commit.encode()).hexdigest(), 0)
    return result


def test_c8_4_c13_4_retention_counts_and_removes_clean_allocated_worktree(owned, monkeypatch):
    """C-8.4, C-13.4, C-3.3: owned worktree bytes bind retention and Git removal runs outside tx."""
    store, root, repository, worktree = owned
    commands = []
    original = subprocess.run

    def inspect(argv, **kwargs):
        assert not store.connection.in_transaction
        commands.append(argv)
        if argv[3:5] == ["worktree", "remove"]:
            key = f"worktree:{worktree.resolve()}"
            assert store.one("SELECT holder FROM leases WHERE lease_key=?", (key,))["holder"] == "retention:job"
            assert not store.acquire_lease(key, "new-writer")
        return original(argv, **kwargs)

    monkeypatch.setattr(retention.subprocess, "run", inspect)
    result = retention.maintenance(store, root, max_jobs=1, max_bytes=100)
    assert result["pruned"] == ["job"]
    assert result["bytes_before"] >= 4096
    assert result["bytes_after"] == 0
    assert result["errors"] == []
    assert store.list_leases() == []
    assert not worktree.exists()
    assert str(worktree) not in git(repository, "worktree", "list", "--porcelain")
    removal = next(command for command in commands if command[3:5] == ["worktree", "remove"])
    assert "--force" not in removal


def test_c13_4_retention_never_removes_in_place_workdir(owned, monkeypatch):
    """C-13.4, C-8.4: pruning an in-place job leaves the caller's files and Git index intact."""
    store, root, repository, _ = owned
    store.update_job("job", in_place=1, worktree=str(repository))
    before = (repository / ".git" / "index").read_bytes()
    monkeypatch.setattr(retention.subprocess, "run", lambda *args, **kwargs: pytest.fail("in-place workdir must not be removed or inspected"))
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["job"]
    assert (repository / "tracked").read_text() == "base" * 1024
    assert (repository / ".git" / "index").read_bytes() == before


@pytest.mark.parametrize("path_kind", ["outside", "symlink", "container-symlink"])
def test_c13_4_retention_rejects_worktree_paths_outside_owned_root(owned, monkeypatch, path_kind):
    """C-13.4, C-2.1: resolved paths and symlinked containers cannot authorize external deletion."""
    store, root, repository, worktree = owned
    git(repository, "worktree", "remove", str(worktree))
    if path_kind == "outside":
        store.update_job("job", worktree=str(repository))
    elif path_kind == "symlink":
        worktree.symlink_to(repository, target_is_directory=True)
    else:
        (root / "worktrees").rmdir()
        (root / "worktrees").symlink_to(repository.parent, target_is_directory=True)
        store.update_job("job", worktree=str(root / "worktrees" / "repository"))
    monkeypatch.setattr(retention.subprocess, "run", lambda *args, **kwargs: pytest.fail("external paths cannot reach Git removal"))
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == []
    assert result["protected"] == ["job"]
    assert result["errors"]
    assert store.get_job("job") is not None
    assert (repository / "tracked").exists()


def test_c13_4_dirty_allocated_worktree_without_salvage_is_preserved(owned):
    """C-13.4, C-8.4: dirty owned work is retained when no salvage artifact records its snapshot."""
    store, root, _, worktree = owned
    (worktree / "tracked").write_text("unsalvaged changes")
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == []
    assert result["protected"] == ["job"]
    assert "no recorded salvage" in result["errors"][0]["error"]
    assert (worktree / "tracked").read_text() == "unsalvaged changes"
    assert store.get_job("job") is not None
    assert store.list_leases() == []


def test_c13_4_retention_resumes_after_worktree_removed_before_row_commit(owned):
    """C-13.4, C-8.4, C-3.2: a retained deletion lease is recoverable after Git removal."""
    store, root, repository, worktree = owned
    assert store.acquire_lease(f"worktree:{worktree.resolve()}", "retention:job")
    git(repository, "worktree", "remove", str(worktree))
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["job"]
    assert store.get_job("job") is None
    assert store.list_leases() == []


def test_c13_4_dirty_allocated_worktree_removed_only_after_salvage_is_retained(owned, monkeypatch):
    """C-13.4, C-8.4: force removal requires the current dirty tree in a retained salvage ref."""
    store, root, repository, worktree = owned
    (worktree / "tracked").write_text("salvaged changes")
    snapshot = record_salvage(store, worktree)
    assert retention.maintenance(store, root, max_jobs=0)["protected"] == ["job"]
    git(repository, "update-ref", "refs/heads/retained-checkpoint", snapshot.commit)
    commands = []
    original = subprocess.run

    def inspect(argv, **kwargs):
        assert not store.connection.in_transaction
        commands.append(argv)
        return original(argv, **kwargs)

    monkeypatch.setattr(retention.subprocess, "run", inspect)
    result = retention.maintenance(store, root, max_jobs=0, salvage_referenced_elsewhere=lambda artifact: True)
    assert result["pruned"] == ["job"]
    assert not worktree.exists()
    assert git(repository, "show", snapshot.ref + ":tracked") == "salvaged changes"
    removal = next(command for command in commands if command[3:5] == ["worktree", "remove"])
    assert "--force" in removal


@pytest.mark.parametrize("damage", ["new-edit", "missing-ref"])
def test_c13_4_dirty_worktree_needs_matching_existing_salvage(owned, damage):
    """C-13.4: an old or missing salvage ref cannot authorize discarding later dirty work."""
    store, root, repository, worktree = owned
    (worktree / "tracked").write_text("snapshot content")
    snapshot = record_salvage(store, worktree)
    if damage == "new-edit":
        (worktree / "tracked").write_text("later operator edits")
    else:
        git(repository, "update-ref", "-d", snapshot.ref)
    result = retention.maintenance(store, root, max_jobs=0, salvage_referenced_elsewhere=lambda artifact: True)
    assert result["pruned"] == []
    assert result["protected"] == ["job"]
    assert "not preserved" in result["errors"][0]["error"]
    assert worktree.exists()
    assert store.get_job("job") is not None


# C-13.4: the worktree admission cut for a job that ended before any attempt.
# `jobs.worktree` is written at reservation, so such a job owns its allocation
# by path alone: `worktrees/<job id>/`, with no attempt row.

HOUR_AGO = "2026-09-19T16:07:30Z"
COLLECTOR = "admission-collect:job"


@pytest.fixture
def unused(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "feature")
    (repository / "tracked").write_text("base" * 1024)
    (repository / ".gitignore").write_text("*.log\n")
    (repository / "nested").mkdir()
    (repository / "nested" / "file").write_text("nested\n")
    git(repository, "add", ".")
    git(repository, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "baseline")
    root = tmp_path / "state"
    root.mkdir()
    worktree = root / "worktrees" / "job"
    git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    with Store(root / "state.sqlite3") as store:
        store.add_job(job_id="job", request_id="request", payload_digest="digest", kind="dispatch",
                      workdir=str(repository), workdir_head=git(repository, "rev-parse", "HEAD"),
                      worktree=None, prompt_path="/prompt", sandbox="workspace-write",
                      state="cancelled", finished_at=HOUR_AGO)
        yield store, root, repository, worktree.resolve()


def registered(repository, worktree):
    return f"worktree {worktree.resolve()}" in git(repository, "worktree", "list", "--porcelain").splitlines()


def collect(store, root):
    return retention.collect_unused_worktree(store, root, "job", holder=COLLECTOR)


def recorder(store, monkeypatch):
    """Every git argv, asserting none runs inside a store transaction (C-3.3)."""
    commands, original = [], subprocess.run

    def inspect(argv, **kwargs):
        assert not store.connection.in_transaction
        commands.append(argv)
        return original(argv, **kwargs)
    monkeypatch.setattr(retention.subprocess, "run", inspect)
    return commands


def removal(commands):
    return next(command for command in commands if command[3:5] == ["worktree", "remove"])


def test_c13_4_retention_collects_an_unused_allocation_whatever_the_limits(unused, monkeypatch):
    """C-13.4, C-3.3: a terminal job with no attempt loses its clean worktree, outside tx and never forced; the row stays."""
    store, root, repository, worktree = unused
    commands, original = [], subprocess.run

    def inspect(argv, **kwargs):
        assert not store.connection.in_transaction
        commands.append(argv)
        if argv[3:5] == ["worktree", "remove"]:
            key = f"worktree:{worktree}"
            assert store.one("SELECT holder FROM leases WHERE lease_key=?", (key,))["holder"] == "retention:job"
        return original(argv, **kwargs)

    monkeypatch.setattr(retention.subprocess, "run", inspect)
    result = retention.maintenance(store, root)
    assert result["unused_worktrees"] == ["job"] and result["pruned"] == [] and result["errors"] == []
    assert not worktree.exists() and not registered(repository, worktree)
    assert store.get_job("job") is not None and store.list_leases() == []
    assert "--force" not in removal(commands)
    assert [row["kind"] for row in store.list_events("job") if row["kind"].startswith("retention.")] == [
        "retention.unused_worktree_selected", "retention.unused_worktree_removed"]


def test_c13_4_the_admission_fence_protects_a_job_that_ended_since(unused):
    """C-13.4: a job that ended after the admission pass in flight began may still be cut; neither path touches it."""
    store, root, repository, worktree = unused
    fence = "2026-09-19T16:07:30Z"            # the pass began the second the job ended
    result = retention.maintenance(store, root, max_jobs=0, unused_before=fence)
    assert result["unused_worktrees"] == [] and result["pruned"] == [] and "job" in result["protected"]
    assert worktree.exists() and store.get_job("job") is not None
    assert result["bytes_before"] >= 4096     # C-8.4: counted meanwhile
    result = retention.maintenance(store, root, max_jobs=0, unused_before="2026-09-19T16:07:31Z")
    assert result["unused_worktrees"] == ["job"] and result["pruned"] == ["job"]
    assert not worktree.exists() and not registered(repository, worktree)


def test_c13_4_c8_4_nothing_on_disk_is_pruned_without_git(unused, monkeypatch):
    """C-8.4: a job killed before its first pass (or whose add failed) and a deleted checkout still prune."""
    store, root, repository, worktree = unused
    git(repository, "worktree", "remove", str(worktree))
    import shutil
    shutil.rmtree(repository)
    monkeypatch.setattr(retention.subprocess, "run", lambda *args, **kwargs: pytest.fail("nothing on disk needs git"))
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["job"] and result["errors"] == [] and store.list_leases() == []


@pytest.mark.parametrize("state", ["queued", "waiting"])
def test_c13_4_an_unfinished_job_keeps_its_allocation(unused, state):
    """C-6.8: a waiting job reuses the worktree it was cut; neither path collects it."""
    store, root, _, worktree = unused
    store.update_job("job", state=state, finished_at=None)
    assert collect(store, root) is False
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == [] and worktree.exists()


def test_c13_4_a_job_with_an_attempt_row_never_owns_by_path(unused):
    """C-13.4: any attempt keeps the worktree for the ordinary rule, even with `jobs.worktree` empty."""
    store, root, repository, worktree = unused
    store.put_lane(Lane("codex-1", "codex", "codex:one", Credential("codex", "/home/one", "home"), "/home/one", LaneOwner.V2, False))
    store.add_attempt(attempt_id="job/a1", job_id="job", seq=1, lane_id="codex-1", model_requested="gpt-6-astra", state="interrupted")
    (worktree / "tracked").write_text("provider work")
    assert collect(store, root) is False
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["unused_worktrees"] == []
    assert (worktree / "tracked").read_text() == "provider work" and registered(repository, worktree)


@pytest.mark.parametrize("work", ["edit", "untracked", "ignored", "commit", "empty-commit", "staged", "symlink"])
def test_c13_4_an_unused_allocation_someone_worked_in_is_kept(unused, work):
    """C-13.4: no attempt means no salvage, so a tree holding anything its commit does not is never removed."""
    store, root, repository, worktree = unused
    if work == "edit":
        (worktree / "tracked").write_text("an edit")
    elif work == "untracked":
        (worktree / "notes.md").write_text("notes")
    elif work == "ignored":
        (worktree / "run.log").write_text("output someone kept")
    elif work == "commit":
        (worktree / "tracked").write_text("committed")
        git(worktree, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "work")
    elif work == "empty-commit":
        # the tree is still the cut commit's; only HEAD, and so a commit, would be lost
        git(worktree, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "note")
    elif work == "staged":
        (worktree / "new").write_text("new")
        git(worktree, "add", "new")
    else:
        (worktree / "tracked").unlink()
        git(worktree, "rm", "-q", "--cached", "tracked")
        (worktree / "tracked").symlink_to("nested/file")
    before = sorted(str(path.relative_to(worktree)) for path in worktree.rglob("*") if ".git" not in path.parts)
    with pytest.raises(ValueError):
        collect(store, root)
    assert registered(repository, worktree)
    assert sorted(str(path.relative_to(worktree)) for path in worktree.rglob("*") if ".git" not in path.parts) == before
    kept = [row for row in store.list_events("job") if row["kind"] == "retention.unused_worktree_kept"]
    assert len(kept) == 1 and store.list_leases() == []
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == [] and result["protected"] == ["job"] and result["errors"]


def test_c13_4_an_unused_allocation_another_job_works_in_is_kept(unused, tmp_path):
    """C-13.4: an in-place job run inside the directory references it, in any state; nothing is removed."""
    store, root, repository, worktree = unused
    store.add_job(job_id="inside", request_id="inside-request", payload_digest="digest", kind="dispatch",
                  workdir=str(worktree / "nested"), prompt_path="/prompt", sandbox="workspace-write",
                  in_place=1, state="succeeded", finished_at=HOUR_AGO)
    with pytest.raises(ValueError, match="job inside works in this worktree"):
        collect(store, root)
    result = retention.maintenance(store, root, max_jobs=1)
    assert "job" not in result["pruned"] and worktree.exists() and registered(repository, worktree)
    assert any(error["job_id"] == "job" and "inside" in error["error"] for error in result["errors"])


@pytest.mark.parametrize("path_kind", ["symlink", "container-symlink"])
def test_c13_4_an_unused_allocation_that_is_a_link_is_refused(unused, monkeypatch, path_kind):
    """C-13.4, C-2.1: the owned path is exactly `worktrees/<job id>`, never where a link points."""
    store, root, repository, worktree = unused
    git(repository, "worktree", "remove", str(worktree))
    if path_kind == "symlink":
        other = root / "worktrees" / "other"
        git(repository, "worktree", "add", "--detach", str(other), "HEAD")
        worktree.symlink_to(other, target_is_directory=True)
    else:
        (root / "worktrees").rmdir()
        (root / "worktrees").symlink_to(repository.parent, target_is_directory=True)
    monkeypatch.setattr(retention.subprocess, "run", lambda *args, **kwargs: pytest.fail("a link cannot reach Git removal"))
    with pytest.raises(ValueError):
        collect(store, root)
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == [] and result["protected"] == ["job"] and result["errors"]
    assert (repository / "tracked").exists()
    if path_kind == "symlink":
        assert (root / "worktrees" / "other" / "tracked").exists()


def test_c13_4_a_standalone_repository_at_the_path_is_refused(unused):
    """C-13.4: a `.git` directory is someone's repository, not a partial add; it is never removed."""
    store, root, repository, worktree = unused
    git(repository, "worktree", "remove", str(worktree))
    worktree.mkdir()
    git(worktree, "init", "-q")
    (worktree / "work").write_text("someone's")
    with pytest.raises(ValueError, match="not a linked worktree"):
        collect(store, root)
    assert (worktree / "work").read_text() == "someone's"


def test_c13_4_a_foreign_fence_refuses_and_a_collectors_fence_yields(unused):
    """C-13.4, C-6.5: an in-place writer's lease refuses removal; the other collector's makes this one stand aside."""
    store, root, repository, worktree = unused
    key = f"worktree:{worktree}"
    assert store.acquire_lease(key, "in-place-writer")
    with pytest.raises(ValueError, match="lease is held by in-place-writer"):
        collect(store, root)
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == [] and result["protected"] == ["job"]
    assert worktree.exists() and registered(repository, worktree)
    store.release_leases("in-place-writer")
    assert store.acquire_lease(key, "retention:job")
    assert collect(store, root) is False and worktree.exists()
    store.release_leases("retention:job")
    assert store.acquire_lease(key, COLLECTOR)
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["unused_worktrees"] == [] and result["pruned"] == [] and "job" in result["protected"]
    assert worktree.exists()


def test_c13_4_a_directory_without_a_git_link_goes_as_c6_8_removes_one(unused):
    """C-6.8, C-13.4: an add stopped before it linked, or a removal after it unlinked, leaves no `.git`."""
    store, root, repository, worktree = unused
    (worktree / ".git").unlink()                 # the registration stays, now stale
    assert collect(store, root) is True
    assert not worktree.exists() and not registered(repository, worktree)
    assert "prunable" not in git(repository, "worktree", "list", "--porcelain")


def test_c13_4_a_removal_stopped_partway_is_finished_by_the_next(unused, monkeypatch):
    """C-13.4: `git worktree remove` killed at its cap leaves tracked files missing; the next try forces it through."""
    store, root, repository, worktree = unused
    original = subprocess.run

    def killed(argv, **kwargs):
        if argv[3:5] == ["worktree", "remove"]:
            (worktree / "tracked").unlink()
            (worktree / "nested" / "file").unlink()
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        return original(argv, **kwargs)
    monkeypatch.setattr(retention.subprocess, "run", killed)
    with pytest.raises(subprocess.TimeoutExpired):
        collect(store, root)
    assert worktree.exists() and registered(repository, worktree)
    monkeypatch.undo()
    commands = recorder(store, monkeypatch)
    assert collect(store, root) is True
    assert removal(commands).count("--force") == 1
    assert not worktree.exists() and not registered(repository, worktree)


def test_c13_4_an_add_stopped_partway_is_removed_when_every_file_is_its_commits(unused, monkeypatch):
    """C-6.8, C-13.4: git's `initializing` lock and a partial index; every file present is the commit's, so -f -f."""
    store, root, repository, worktree = unused
    git(repository, "worktree", "lock", "--reason", "initializing", str(worktree))
    git(worktree, "rm", "-r", "-q", "--cached", ".")      # files written, not yet indexed
    (worktree / "nested" / "file").unlink()                # and some not yet written
    commands = recorder(store, monkeypatch)
    assert collect(store, root) is True
    assert removal(commands).count("--force") == 2
    assert not worktree.exists() and not registered(repository, worktree)


@pytest.mark.parametrize("damage", ["differs", "extra", "other-lock"])
def test_c13_4_a_partial_add_with_anything_else_is_kept(unused, damage):
    """C-13.4: a partial tree is removed only when it holds nothing its commit does not, under git's own lock."""
    store, root, repository, worktree = unused
    reason = "someone's" if damage == "other-lock" else "initializing"
    git(repository, "worktree", "lock", "--reason", reason, str(worktree))
    git(worktree, "rm", "-r", "-q", "--cached", ".")
    if damage == "differs":
        (worktree / "tracked").write_text("not the commit's")
    elif damage == "extra":
        (worktree / "extra").write_text("not in the commit")
    with pytest.raises(ValueError):
        collect(store, root)
    assert worktree.exists() and registered(repository, worktree)


def test_c13_4_an_interrupted_collection_keeps_its_fence_and_resumes(unused, monkeypatch):
    """C-13.4, C-16.4: a collection stopped after git removed the tree is finished, fence and all, by the next."""
    store, root, repository, worktree = unused
    original = retention._remove_unused

    def removed_then_interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise retention._Interrupted("deadline")
    monkeypatch.setattr(retention, "_remove_unused", removed_then_interrupted)
    with pytest.raises(InterruptedError):
        collect(store, root)
    key = f"worktree:{worktree}"
    assert store.one("SELECT holder FROM leases WHERE lease_key=?", (key,))["holder"] == COLLECTOR
    monkeypatch.undo()
    assert collect(store, root) is False
    assert store.list_leases() == [] and not registered(repository, worktree)


def test_c13_4_a_job_reserved_after_the_snapshot_is_not_pruned_as_unused(unused, monkeypatch):
    """C-13.4: ownership read before the pins is rechecked inside the selecting transaction."""
    store, root, _, worktree = unused
    store.put_lane(Lane("codex-1", "codex", "codex:one", Credential("codex", "/home/one", "home"), "/home/one", LaneOwner.V2, False))
    monkeypatch.setattr(retention, "_collect_unused", lambda *args, **kwargs: False)
    original = retention._pins
    calls = []

    def reserve_between(*args):
        calls.append(1)
        if len(calls) == 2:     # the recheck inside `retention.selected`
            store.add_attempt(attempt_id="job/a1", job_id="job", seq=1, lane_id="codex-1",
                              model_requested="gpt-6-astra", state="succeeded")
            store.update_job("job", worktree=str(worktree))
        return original(*args)
    monkeypatch.setattr(retention, "_pins", reserve_between)
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == [] and "job" in result["protected"]
    assert worktree.exists()


@pytest.mark.parametrize("identity", ["..", ".", "a/b"])
def test_c13_4_unused_ownership_rejects_path_like_job_ids(tmp_path, identity):
    root = tmp_path / "state"
    (root / "worktrees").mkdir(parents=True)
    job = {"job_id": identity, "sandbox": "workspace-write", "in_place": 0, "worktree": None}
    with pytest.raises(ValueError):
        retention._owned_worktree(job, root.resolve(), used=False)


@pytest.mark.parametrize("finished_at", [None, "not a time", "2026-09-19T16:07:30"])
def test_c13_4_an_unreadable_end_time_is_never_taken_for_an_early_one(unused, finished_at):
    """C-13.4: the fence needs an end time; without one, neither the sweep nor pruning touches the job."""
    store, root, repository, worktree = unused
    store.update_job("job", finished_at=finished_at)
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["unused_worktrees"] == [] and result["pruned"] == [] and "job" in result["protected"]
    assert worktree.exists() and registered(repository, worktree)


def test_c13_4_a_missing_link_drops_only_this_registration(unused, tmp_path):
    """C-13.4: no repository-wide `git worktree prune`; a sibling whose directory is away keeps its record."""
    store, root, repository, worktree = unused
    sibling = tmp_path / "sibling"
    git(repository, "worktree", "add", "--detach", str(sibling), "HEAD")
    (sibling / "tracked").write_text("sibling work")
    git(sibling, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "only on the sibling")
    away = tmp_path / "away"
    sibling.rename(away)                                 # an unmounted volume, a hand move
    (worktree / ".git").unlink()
    assert collect(store, root) is True
    assert not worktree.exists() and not registered(repository, worktree)
    listing = git(repository, "worktree", "list", "--porcelain")
    assert f"worktree {sibling.resolve()}" in listing and "prunable" in listing
    away.rename(sibling)
    assert git(sibling, "log", "-1", "--format=%s") == "only on the sibling"


@pytest.mark.parametrize("extra", ["file", "empty-dir-only"])
def test_c13_4_a_missing_link_is_checked_against_the_repository(unused, extra):
    """C-13.4, C-6.8: without the link the caller's repository answers for the commit; anything else keeps it."""
    store, root, repository, worktree = unused
    (worktree / ".git").unlink()
    (worktree / "tracked").unlink()                      # what a removal stopped partway leaves
    if extra == "file":
        (worktree / "someone's").write_text("not in the commit")
        with pytest.raises(ValueError, match="something its commit does not"):
            collect(store, root)
        assert (worktree / "someone's").exists()
    else:
        (worktree / "empty").mkdir()
        assert collect(store, root) is True
        assert not worktree.exists() and not registered(repository, worktree)


def test_c13_4_a_missing_link_with_the_repository_gone_is_kept(unused):
    store, root, repository, worktree = unused
    (worktree / ".git").unlink()
    import shutil
    shutil.rmtree(repository)
    with pytest.raises(ValueError, match="repository is gone"):
        collect(store, root)
    assert (worktree / "tracked").exists()


@pytest.mark.parametrize("hidden", ["nested-link", "nested-repo", "submodule", "skip-worktree", "staged-only"])
def test_c13_4_what_git_status_does_not_show_still_keeps_the_tree(unused, hidden):
    """C-13.4: the filesystem and the index, not `git status`, are the inventory."""
    store, root, repository, worktree = unused
    if hidden == "nested-link":
        (worktree / "nested" / ".git").write_text("gitdir: /elsewhere\n")
    elif hidden == "nested-repo":
        (worktree / "vendor" / ".git").mkdir(parents=True)     # git never lists a `.git` directory
        (worktree / "vendor" / ".git" / "precious").write_text("only here")
    elif hidden == "submodule":
        # a gitlink in the commit is an empty directory in a fresh worktree
        sub = worktree.parent.parent.parent / "sub"
        sub.mkdir()
        git(sub, "init", "-q")
        git(sub, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "s")
        git(repository, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "sub")
        git(repository, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "with submodule")
        git(worktree, "checkout", "-q", "--detach", git(repository, "rev-parse", "HEAD"))
        store.update_job("job", workdir_head=git(repository, "rev-parse", "HEAD"))
        (worktree / "sub" / "unpushed").write_text("work in a submodule directory")
    elif hidden == "skip-worktree":
        git(worktree, "update-index", "--skip-worktree", "tracked")
        (worktree / "tracked").write_text("hidden from status")
    else:
        (worktree / "tracked").write_text("staged content")
        git(worktree, "add", "tracked")
        (worktree / "tracked").write_text("base" * 1024)  # the file matches, the index does not
    assert git(worktree, "status", "--porcelain") == "" or hidden in ("submodule", "staged-only")
    with pytest.raises(ValueError) as refused:
        collect(store, root)
    if hidden == "nested-repo":
        assert "(vendor/.git)" in str(refused.value)      # refused at once, without walking it
    assert worktree.exists() and registered(repository, worktree)


@pytest.mark.parametrize("state", ["committed-link", "staged-deletion"])
def test_c13_4_nothing_beyond_the_commit_is_removed(unused, state):
    """C-13.4: a link the commit has, or a staged deletion, holds nothing the commit does not."""
    store, root, repository, worktree = unused
    if state == "committed-link":
        (repository / "alias").symlink_to("tracked")
        git(repository, "add", "alias")
        git(repository, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "link")
        git(worktree, "checkout", "-q", "--detach", git(repository, "rev-parse", "HEAD"))
        store.update_job("job", workdir_head=git(repository, "rev-parse", "HEAD"))
    else:
        git(worktree, "rm", "-q", "tracked")
    assert collect(store, root) is True
    assert not worktree.exists() and not registered(repository, worktree)


def test_c13_4_an_add_stopped_before_head_is_removed_through_the_repository(unused):
    """C-6.8, C-13.4: a link with no HEAD behind it; the repository answers, and only this entry goes."""
    store, root, repository, worktree = unused
    admin = Path(git(worktree, "rev-parse", "--absolute-git-dir"))
    (admin / "HEAD").unlink()
    assert collect(store, root) is True
    assert not worktree.exists() and not registered(repository, worktree)


def test_c13_4_a_stale_locked_registration_is_dropped_when_nothing_is_on_disk(unused, tmp_path):
    """C-13.4: git's `initializing` lock outlives its directory; pruning drops it, and nothing else."""
    store, root, repository, worktree = unused
    other = tmp_path / "other"
    git(repository, "worktree", "add", "--detach", str(other), "HEAD")
    git(repository, "worktree", "lock", "--reason", "mine", str(other))
    git(repository, "worktree", "lock", "--reason", "initializing", str(worktree))
    import shutil
    shutil.rmtree(worktree)
    shutil.rmtree(other)
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["job"] and not registered(repository, worktree)
    assert f"worktree {other.resolve()}" in git(repository, "worktree", "list", "--porcelain")


@pytest.mark.parametrize("partial", [False, True])
def test_c13_4_a_filtered_checkout_matches_through_gits_clean_filter(tmp_path, partial):
    """C-13.4: an `eol` checkout differs from its blobs byte for byte; git's own clean filter matches them."""
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "feature")
    (repository / ".gitattributes").write_text("*.txt eol=crlf\n")
    (repository / "a.txt").write_text("a\nb\n")
    git(repository, "add", ".")
    git(repository, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    root = tmp_path / "state"
    worktree = root / "worktrees" / "job"
    git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    assert (worktree / "a.txt").read_bytes() == b"a\r\nb\r\n"
    if partial:
        git(repository, "worktree", "lock", "--reason", "initializing", str(worktree))
        git(worktree, "rm", "-r", "-q", "--cached", ".")
    with Store(root / "state.sqlite3") as store:
        store.add_job(job_id="job", request_id="request", payload_digest="digest", kind="dispatch",
                      workdir=str(repository), workdir_head=git(repository, "rev-parse", "HEAD"),
                      prompt_path="/prompt", sandbox="workspace-write", state="cancelled", finished_at=HOUR_AGO)
        assert collect(store, root) is True
    assert not worktree.exists()


def test_c13_4_the_prune_fence_reads_the_row_not_the_snapshot(unused, monkeypatch):
    """C-13.4: a job listed as queued and killed during retention's size walk has ended after the fence."""
    store, root, repository, worktree = unused
    store.update_job("job", state="waiting", finished_at=None)
    original = retention._size

    def killed_meanwhile(path, **kwargs):
        if path == worktree:
            store.update_job("job", state="cancelled", finished_at="2026-09-19T16:10:00Z")
        return original(path, **kwargs)
    monkeypatch.setattr(retention, "_size", killed_meanwhile)
    result = retention.maintenance(store, root, max_jobs=0, unused_before="2026-09-19T16:09:00Z")
    assert result["pruned"] == [] and "job" in result["protected"]
    assert worktree.exists() and store.get_job("job") is not None


def test_c13_4_c8_4_the_sweep_stops_at_its_time_and_pruning_still_runs(unused, monkeypatch):
    """C-8.4, C-13.4: a slow unused tree takes at most half the pass; the rest of the pass still prunes."""
    store, root, repository, worktree = unused
    store.add_job(job_id="old", request_id="old-request", payload_digest="digest", kind="dispatch",
                  workdir=str(repository), prompt_path="/prompt", sandbox="read-only", state="succeeded",
                  finished_at=HOUR_AGO, created_at="2026-09-01T00:00:00Z")

    def slow(*args, **kwargs):
        raise retention._Interrupted("deadline")
    monkeypatch.setattr(retention, "_remove_unused", slow)
    import time
    result = retention.maintenance(store, root, max_jobs=1, deadline=time.monotonic() + 30)
    assert "interrupted" not in result and "old" in result["pruned"]
    assert any(error["job_id"] == "job" and "its time" in error["error"] for error in result["errors"])


def test_c13_4_a_refused_tree_is_not_read_again_every_hour(unused, monkeypatch):
    """C-13.4: a refusal stands until a start or a pruning pass tries again; a git failure is retried."""
    store, root, repository, worktree = unused
    (worktree / "notes").write_text("someone's")
    with pytest.raises(ValueError):
        collect(store, root)
    calls = []
    monkeypatch.setattr(retention, "_remove_unused", lambda *args, **kwargs: calls.append(1))
    assert retention.maintenance(store, root)["unused_worktrees"] == [] and calls == []
    assert store.one("SELECT 1 FROM events WHERE job_id='job' AND kind='retention.unused_worktree_failed'") is None
