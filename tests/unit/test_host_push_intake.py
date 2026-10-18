"""C-8.5, review P1-1: the job's bundle is read from its own workspace as data.

The job writes `.subfleet/push.bundle` in the directory it starts in, the one
place a Codex workspace-write sandbox lets it write. The daemon opens every
name below that directory without following a link and gives Git only its own
copy. Every case runs the real submit, acceptance and local `file://` push.
"""
import os
from pathlib import Path
import shutil

import pytest

from subfleet import host_push
from subfleet.adapters.base import AdapterError
from subfleet.contracts import attempt_dir
from test_host_push import (accept, assert_failed, bundle_path, commit, git, remote_heads,  # noqa: F401
                            submit, worlds, write_bundle)


def test_the_job_is_told_the_bundle_path_in_its_environment_and_prompt(worlds):
    with worlds() as world:
        job_id = submit(world)
        job = world.core._job(job_id)
        expected = world.core.root / "worktrees" / job_id / ".subfleet" / "push.bundle"
        assert bundle_path(world) == expected
        assert world.core._push_environment(job) == {"SUBFLEET_PUSH_BUNDLE": str(expected)}
        prepared = (world.core.root / "jobs" / job_id / "prompt.prepared.md").read_text()
        assert str(expected) in prepared and "$SUBFLEET_PUSH_BUNDLE" in prepared
        assert "mkdir -p .subfleet && git bundle create .subfleet/push.bundle HEAD" in prepared
        assert world.core._push_environment({**job, "push_branch": None}) == {}
    with worlds() as world:
        # In place, the job starts in its workdir, and writes there.
        job_id = submit(world, in_place=True)
        expected = world.repo / ".subfleet" / "push.bundle"
        assert world.core._push_environment(world.core._job(job_id)) == {"SUBFLEET_PUSH_BUNDLE": str(expected)}
        assert str(expected) in (world.core.root / "jobs" / job_id / "prompt.prepared.md").read_text()


def test_a_job_that_commits_and_bundles_in_its_own_checkout_is_published(worlds):
    """What a job does, end to end: commit in the checkout the daemon allocated,
    bundle with the command its prompt names, and the host publishes it."""
    with worlds() as world:
        job_id = submit(world)
        workspace, _, _, _ = world.core._workspace(world.core._job(job_id))
        checkout = Path(workspace)
        assert checkout == bundle_path(world).parents[1]
        (checkout / "work.txt").write_text("done")
        git(checkout, "add", "-A")
        git(checkout, "commit", "-m", "the job's work")
        sha = git(checkout, "rev-parse", "HEAD")
        (checkout / ".subfleet").mkdir()
        git(checkout, "bundle", "create", ".subfleet/push.bundle", "HEAD")
        # The daemon excludes the delivery folder, so `add -A` never commits it.
        assert git(checkout, "status", "--porcelain") == ""
        row = accept(world, bundle=False)
        assert row["push_sha"] == sha and not row["push_error"]
        assert remote_heads(world)["refs/heads/jobs/finished"] == sha
        # Git verified the daemon's own copy, kept with the attempt.
        copy = attempt_dir(world.core.root, job_id, 1) / "push.bundle"
        assert copy.read_bytes() == (checkout / ".subfleet/push.bundle").read_bytes()


def arrange(world, case, tmp_path):
    """Put the job's delivery in one of the shapes intake must refuse."""
    target = bundle_path(world)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    git(world.repo, "bundle", "create", str(elsewhere / "push.bundle"), "HEAD")
    target.parent.parent.mkdir(parents=True, exist_ok=True)
    if case == "absent":
        return
    if case == "symlinked-subfleet":
        target.parent.symlink_to(elsewhere)
        return
    target.parent.mkdir()
    if case == "symlinked-bundle":
        target.symlink_to(elsewhere / "push.bundle")
    elif case == "fifo":
        os.mkfifo(target)
    elif case == "directory":
        target.mkdir()
    elif case == "oversize":
        (world.repo / "large").write_bytes(os.urandom(1024 * 1024 + 16384))
        git(world.repo, "add", "large")
        git(world.repo, "commit", "-m", "too large")
        git(world.repo, "bundle", "create", str(target), "HEAD")
    elif case == "symlinked-workspace":
        shutil.rmtree(target.parent.parent)
        target.parent.parent.symlink_to(elsewhere)


#: (case, the refusal's words). Each would otherwise hand the daemon a bundle
#: from outside the workspace, hold its open, or read past the policy's cap.
REFUSALS = {
    "symlinked-subfleet": ".subfleet must be a plain directory",
    "symlinked-bundle": "must be a regular file, not a symlink",
    "fifo": "must be a regular file",
    "directory": "must be a regular file",
    "oversize": "max_bundle_mb",
    "absent": "no push bundle at .subfleet/push.bundle",
    "symlinked-workspace": "not a plain directory",
    "workspace-gone": "workspace",
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_intake_refuses_anything_but_a_regular_file_in_a_plain_directory(worlds, tmp_path, case):
    with worlds(**({"max_bundle_mb": 1} if case == "oversize" else {})) as world:
        submit(world)
        commit(world.repo)
        if case != "workspace-gone":
            arrange(world, case, tmp_path)
        row = accept(world, bundle=False)
        assert_failed(world, row, REFUSALS[case])
        assert not (attempt_dir(world.core.root, world.job_id, 1) / "push.bundle").exists()
        # Nothing behind a link was read: the elsewhere bundle never reached Git.
        assert not any(args[0] == "bundle" for _, args in world.calls)


def test_a_bundle_swapped_after_it_was_opened_is_never_read(worlds):
    """The one open file is what is copied and verified: renaming another
    bundle (one adding a workflow) over the name afterwards changes nothing."""
    with worlds() as world:
        submit(world)
        good = commit(world.repo, text="good")
        target = write_bundle(world)
        commit(world.repo, ".github/workflows/ci.yml", "uses secrets")
        evil = target.with_name("evil.bundle")
        git(world.repo, "bundle", "create", str(evil), "HEAD")
        actual = os.open
        swapped = []

        def open_then_swap(path, flags, mode=0o777, *, dir_fd=None):
            descriptor = actual(path, flags, mode, dir_fd=dir_fd)
            if path == host_push.BUNDLE_PATH[-1] and dir_fd is not None and not swapped:
                os.replace(evil, target)
                swapped.append(path)
            return descriptor
        world.patch.setattr(os, "open", open_then_swap)
        row = accept(world, bundle=False)
        assert swapped and row["push_sha"] == good and not row["push_error"]
        assert remote_heads(world)["refs/heads/jobs/finished"] == good
        assert git(world.repo, "bundle", "list-heads", str(target)).split()[0] != good


def test_a_bundle_left_before_an_attempt_starts_is_cleared_without_following_links(tmp_path):
    """Launch clears the delivery path, so a stale bundle (an earlier job's, in
    place) is never taken for this attempt's; it never follows a link to do so."""
    root = tmp_path / "workspace"
    (root / ".subfleet").mkdir(parents=True)
    (root / ".subfleet/push.bundle").write_bytes(b"stale")
    host_push.clear_bundle(root)
    assert not (root / ".subfleet/push.bundle").exists()
    other = tmp_path / "other"
    other.mkdir()
    (other / "push.bundle").write_bytes(b"not the job's")
    shutil.rmtree(root / ".subfleet")
    (root / ".subfleet").symlink_to(other)
    host_push.clear_bundle(root)
    assert (other / "push.bundle").read_bytes() == b"not the job's"
    host_push.clear_bundle(tmp_path / "absent")


def test_a_read_only_job_cannot_ask_for_a_push(worlds):
    with worlds() as world:
        with pytest.raises(AdapterError) as refused:
            submit(world, sandbox="read-only")
        assert refused.value.code == 7 and "writable" in str(refused.value) and refused.value.fix
        assert not world.core.store.query("SELECT * FROM jobs")
