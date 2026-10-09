"""C-8.4, C-8.5, review P3-8: retention retires an accepted push job.

A push job's checkout is standalone (`host_push.prepare_workspace`): its
`.git` is a directory the job controlled, not a gitfile naming an admin
directory in the caller's repository. Retention archives it as bytes and runs
no Git in it, so nothing the job planted there executes on the host.
"""
import json
from pathlib import Path

from subfleet import retention
from subfleet import retention_archive as rarch
from test_host_push import accept, git, remote_heads, submit, worlds  # noqa: F401


def test_retention_retires_an_accepted_push_job_whose_checkout_has_a_git_directory(worlds):
    with worlds() as world:
        job_id = submit(world)
        workspace, _, _, _ = world.core._workspace(world.core._job(job_id))
        with world.core.store.transaction("test.reserved") as tx:
            tx.execute("UPDATE jobs SET worktree=? WHERE job_id=?", (workspace, job_id))
        checkout = Path(workspace)
        assert (checkout / ".git").is_dir()
        # The job's work, then what it could plant for whoever runs Git here next.
        (checkout / "work.txt").write_text("done")
        git(checkout, "add", "-A")
        git(checkout, "commit", "-m", "the job's work")
        sha = git(checkout, "rev-parse", "HEAD")
        (checkout / ".subfleet").mkdir()
        git(checkout, "bundle", "create", ".subfleet/push.bundle", "HEAD")
        sentinel = world.repo.parent / "sentinel"
        script = world.repo.parent / "planted"
        script.write_text(f'#!/bin/sh\nprintf executed >> "{sentinel}"\ncat\n')
        script.chmod(0o755)
        planted = (f"[core]\n\tfsmonitor = {script}\n\tpager = {script}\n\thooksPath = {checkout}/.git/hooks\n"
                   f"[filter \"planted\"]\n\tclean = {script}\n\tsmudge = {script}\n\tprocess = {script}\n"
                   f"[diff]\n\texternal = {script}\n")
        with (checkout / ".git/config").open("a") as config:
            config.write(planted)
        (checkout / ".gitattributes").write_text("* filter=planted\n")
        (checkout / ".git/hooks").mkdir(exist_ok=True)
        for hook in ("post-checkout", "pre-commit", "post-index-change", "reference-transaction"):
            (checkout / ".git/hooks" / hook).write_text(script.read_text())
            (checkout / ".git/hooks" / hook).chmod(0o755)
        (checkout / "untracked.txt").write_text("the job never committed this")
        row = accept(world, bundle=False)
        assert row["push_sha"] == sha
        assert world.core._pending_exports() == []

        result = retention.maintenance(world.core.store, world.core.root, max_jobs=0, max_bytes=0,
                                       holders=lambda watches, cancel=None: {})
        assert result["pruned"] == [job_id], json.dumps(result, default=str, indent=1)[:4000]
        assert result["errors"] == []
        assert not checkout.exists()
        assert not sentinel.exists(), "retention ran Git with the job's configuration"
        check = rarch.check_archive(world.core.root, job_id)
        assert check["ok"], check
        # The archive holds the tree byte for byte, the job's own `.git` included.
        manifest = json.loads((world.core.root / "archive" / job_id / "manifest.json").read_text())
        archived = {entry["p"] for tree in manifest["trees"].values() for entry in tree["entries"]}
        assert {"work.txt", "untracked.txt", ".git/config", ".subfleet/push.bundle"} <= archived, sorted(archived)
        # The audit and ownership rows outlive the job's rows.
        assert world.core.store.query("SELECT * FROM jobs WHERE job_id=?", (job_id,)) == []
        assert world.core.store.one("SELECT result FROM job_pushes WHERE job_id=?", (job_id,))["result"] == "succeeded"
        assert world.core.store.one("SELECT sha FROM job_owned_branches")["sha"] == sha
        assert remote_heads(world)["refs/heads/jobs/finished"] == sha


def test_a_job_without_host_push_whose_git_is_a_directory_is_still_kept(worlds):
    """The standalone exception is the push job's alone: any other job's tree
    with a `.git` directory still has a registration nobody can read, and is
    kept as before."""
    with worlds() as world:
        job_id = submit(world)
        workspace, _, _, _ = world.core._workspace(world.core._job(job_id))
        with world.core.store.transaction("test.reserved") as tx:
            tx.execute("UPDATE jobs SET worktree=?, push_branch=NULL, state='succeeded' WHERE job_id=?",
                       (workspace, job_id))
        result = retention.maintenance(world.core.store, world.core.root, max_jobs=0, max_bytes=0,
                                       holders=lambda watches, cancel=None: {})
        assert result["pruned"] == [] and job_id in result["protected"]
        assert "registration: gitfile-unreadable" in result["deferred"][job_id]
        assert Path(workspace, ".git").is_dir()
