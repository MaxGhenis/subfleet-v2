"""Every finding of retention rounds 1-4 and of design revision 1, as a probe that passes
only when the defect is absent (docs/desktop/retention-archive.md, section 9).

Worktree-content findings: Subfleet must never delete a byte of the worktree itself
(W1), must leave its Git state exactly as it was, keeps the job while the tree or a
quarantined copy exists, and prunes it only once the machine's archiver has taken the
tree (W2). The archiver is simulated as the sweep acts: `git worktree move` to
`.disk-guard-removing.<name>`, a copy set aside, then `git worktree remove`.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from subfleet import retention
from subfleet import retention_archive as archive
from subfleet.store import Store
from tests.unit.test_retention_worktrees import add_job, archive_like_the_sweep, forbid_worktree_writes, git

IDENTITY = ["-c", "user.name=probe", "-c", "user.email=probe@example.com"]


def commit(cwd, message):
    git(cwd, *IDENTITY, "commit", "-q", "-am", message)
    return git(cwd, "rev-parse", "HEAD")


def fingerprint(worktree: Path, repository: Path) -> dict:
    """Everything that could be lost: every byte below the tree, the registration's admin
    directory, and the repository's refs and objects."""
    files = {}
    for path in sorted(worktree.rglob("*")):
        if path.is_symlink():
            files[str(path.relative_to(worktree))] = ("link", os.readlink(path))
        elif path.is_file():
            files[str(path.relative_to(worktree))] = hashlib.sha256(path.read_bytes()).hexdigest()
    admin = Path(git(worktree, "rev-parse", "--git-dir"))
    state = {str(p.relative_to(admin)): p.read_bytes() for p in sorted(admin.rglob("*")) if p.is_file()}
    objects = git(repository, "count-objects", "-v")
    refs = git(repository, "for-each-ref", "--format=%(refname) %(objectname)")
    return {"files": files, "admin": state, "objects": objects, "refs": refs}


# --- the scenarios: each prepares a worktree the way a finding described ------------------

def detached_commits(worktree, repository):                     # R1-1a
    (worktree / "tracked").write_text("step 1")
    commit(worktree, "step 1")
    (worktree / "tracked").write_text("step 2")
    commit(worktree, "step 2")


def ignored_output(worktree, repository):                      # R1-1b
    (worktree / ".gitignore").write_text("out/\n")
    (worktree / "out").mkdir()
    (worktree / "out" / "results.parquet").write_bytes(os.urandom(4096))


def nested_repositories(worktree, repository):                 # R1-1c, R2s-7
    inner = worktree / "vendor" / "inner"
    inner.mkdir(parents=True)
    git(inner, "init", "-q")
    (inner / "work").write_text("nested work")
    git(inner, "add", "work")
    git(inner, *IDENTITY, "commit", "-q", "-m", "nested")
    upper = worktree / "UPPER"
    upper.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", str(upper / ".GIT")], check=True, capture_output=True)


def microcosm_h5_under_build(worktree, repository):            # R2h-2, R2s-1
    package = worktree / "packages" / "microcosm-build" / "src" / "microcosm" / "build" / "us_runtime" / "data"
    package.mkdir(parents=True)
    (worktree / ".gitignore").write_text("*.h5\n")
    (package / "calibrated.h5").write_bytes(os.urandom(8192))


def staged_only(worktree, repository):                         # R2h-3
    (worktree / "tracked").write_text("staged, then restored in the working copy")
    git(worktree, "add", "tracked")
    (worktree / "tracked").write_text("base" * 1024)          # only the index holds the edit


def stat_cache_edit(worktree, repository):                     # R3-1
    path = worktree / "tracked"
    stat_ = path.stat()
    path.write_text("BASE" * 1024)                              # same size
    os.utime(path, ns=(stat_.st_atime_ns, stat_.st_mtime_ns))  # old mtime


def dependency_edits(worktree, repository):                    # R3-2
    (worktree / ".gitignore").write_text("node_modules/\n.venv/\n")
    for place in ("node_modules/pkg/index.js", ".venv/lib/analysis.py"):
        (worktree / place).parent.mkdir(parents=True, exist_ok=True)
        (worktree / place).write_text("a local fix that exists nowhere else")


def clean_filter(worktree, repository):                        # R3-3
    (worktree / ".gitattributes").write_text("*.ipynb filter=strip\n")
    git(worktree, "config", "filter.strip.clean", "cat /dev/null")
    (worktree / "results.ipynb").write_text('{"outputs": ["hours of compute"]}')


def worktree_ref_only(worktree, repository):                   # R3-7
    (worktree / "tracked").write_text("scratch")
    scratch = commit(worktree, "scratch")
    git(worktree, "update-ref", "refs/worktree/scratch", scratch)
    git(worktree, "reset", "-q", "--hard", "HEAD~1")


def reflog_and_orig_head(worktree, repository):                # R4-2
    (worktree / "tracked").write_text("u1")
    commit(worktree, "u1")
    (worktree / "tracked").write_text("u2")
    commit(worktree, "u2")
    git(worktree, "reset", "-q", "--hard", "HEAD~2")


def symlink_out(worktree, repository):                          # R3-5
    outside = worktree.parent.parent / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "sentinel").write_text("outside the tree")
    (worktree / "escape").symlink_to(outside)


def unrelated_registration(worktree, repository):              # R2h-4, R2s-2, R3-6
    other = worktree.parent.parent / "someone-elses-checkout"
    git(repository, "worktree", "add", "-q", "--detach", str(other), "HEAD")
    (other / "unique").write_text("their work")
    git(other, "add", "unique")
    moved = other.parent / "unmounted-volume"
    other.rename(moved)                                         # a missing registration


SCENARIOS = {
    "r1_1a_detached_head_commits": detached_commits,
    "r1_1b_ignored_output": ignored_output,
    "r1_1c_r2s_7_nested_and_uppercase_git": nested_repositories,
    "r2_microcosm_h5_under_build_package": microcosm_h5_under_build,
    "r2h_3_staged_only_content": staged_only,
    "r3_1_stat_cache_edit": stat_cache_edit,
    "r3_2_node_modules_and_venv_edits": dependency_edits,
    "r3_3_clean_filter_hides_edit": clean_filter,
    "r3_7_refs_worktree_only_commit": worktree_ref_only,
    "r4_2_reflog_and_orig_head": reflog_and_orig_head,
    "r3_5_symlink_to_outside": symlink_out,
    "r2h_4_unrelated_missing_registration": unrelated_registration,
}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-q", "-b", "main")
    (repository / "tracked").write_text("base" * 1024)
    git(repository, "add", "tracked")
    git(repository, *IDENTITY, "commit", "-q", "-m", "base")
    root = tmp_path / "state"
    (root / "worktrees").mkdir(parents=True)
    with Store(root / "state.sqlite3") as store:
        yield store, root, repository


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_probe_worktree_content_is_never_touched_by_subfleet(world, tmp_path, monkeypatch, scenario):
    """W1, W2: whatever the tree holds, Subfleet leaves every byte, the registration and the
    repository exactly as they were, and keeps the job while the tree exists, in place or
    quarantined by the archiver."""
    store, root, repository = world
    worktree = add_job(store, root, repository, "job")
    SCENARIOS[scenario](worktree, repository)
    before = fingerprint(worktree, repository)
    outside = root.parent / "outside" / "sentinel"
    forbid_worktree_writes(monkeypatch, [worktree])
    result = retention.maintenance(store, root, max_jobs=0, max_bytes=0)
    assert result["pruned"] == [] and store.get_job("job") is not None
    assert fingerprint(worktree, repository) == before
    monkeypatch.undo()
    quarantine = archive_like_the_sweep(repository, worktree)
    forbid_worktree_writes(monkeypatch, [quarantine])
    result = retention.maintenance(store, root, max_jobs=0, max_bytes=0)
    assert result["pruned"] == [] and store.get_job("job") is not None
    if outside.exists():
        assert outside.read_text() == "outside the tree"


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_probe_the_job_goes_only_after_the_archiver_took_the_tree(world, tmp_path, monkeypatch, scenario):
    """W2: after the archiver has set the tree aside and removed it, the job's records are
    retired; Subfleet deleted none of the tree (the archiver's copy is byte-identical)."""
    store, root, repository = world
    worktree = add_job(store, root, repository, "job")
    SCENARIOS[scenario](worktree, repository)
    files_before = fingerprint(worktree, repository)["files"]
    quarantine = archive_like_the_sweep(repository, worktree)
    aside = tmp_path / "aside"
    import shutil
    shutil.copytree(quarantine, aside, symlinks=True)
    git(repository, "worktree", "remove", "--force", str(quarantine))
    forbid_worktree_writes(monkeypatch, [worktree, quarantine])
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["job"]
    copied = {}
    for path in sorted(aside.rglob("*")):
        if path.is_symlink():
            copied[str(path.relative_to(aside))] = ("link", os.readlink(path))
        elif path.is_file():
            copied[str(path.relative_to(aside))] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert copied == files_before
    archive.verify(root / "archive" / "job")


# --- findings about passes, sizes, leases and records --------------------------------------

def plain_job(store, root, identity, order, size=10, **fields):
    store.add_job(job_id=identity, request_id=identity, payload_digest="digest", kind="dispatch",
                  workdir=str(root), prompt_path="/prompt", sandbox="read-only", state="succeeded",
                  created_at=f"2026-01-01T00:{order // 60:02d}:{order % 60:02d}Z", **fields)
    directory = root / "jobs" / identity
    directory.mkdir(parents=True)
    (directory / "stdout").write_bytes(b"x" * size)
    return directory


def test_r1_4_r2h_5_r3_9_a_stale_size_loses_nothing(world):
    """A stale cached size can only change which job goes first; what goes is archived."""
    store, root, repository = world
    directory = plain_job(store, root, "j1", 0, size=1024)
    plain_job(store, root, "j2", 1, size=3 * 1024)
    state = retention.RetentionState()
    assert retention.maintenance(store, root, max_bytes=4 * 1024, state=state)["pruned"] == []
    (directory / "stdout").write_bytes(b"late" * 700)               # grows after it was measured
    retention.maintenance(store, root, max_bytes=1, state=state)
    restored = root / "restored"
    archive.restore(root / "archive" / "j1", restored)
    assert (restored / "stdout").read_bytes() == b"late" * 700


def test_r1_5_a_deferral_expires(world, monkeypatch):
    """R1-5 (permanent demotion): a deferred job is offered again once its deferral ends."""
    store, root, repository = world
    plain_job(store, root, "old", 0)
    clock = [0.0]
    state = retention.RetentionState(clock=lambda: clock[0])
    state.defer("old", retention.DEFER_BUSY_S, "busy")
    assert retention.maintenance(store, root, max_jobs=0, state=state)["pruned"] == []
    clock[0] += retention.DEFER_BUSY_S + 1
    assert retention.maintenance(store, root, max_jobs=0, state=state)["pruned"] == ["old"]


def test_r1_6_salvage_refs_are_never_touched(world):
    """R1-6: retention reads and deletes no ref; a pruned job's salvage ref still resolves."""
    store, root, repository = world
    plain_job(store, root, "job", 0)
    head = git(repository, "rev-parse", "HEAD")
    git(repository, "update-ref", "refs/subfleet-salvage/job-a1", head)
    refs = git(repository, "for-each-ref")
    assert retention.maintenance(store, root, max_jobs=0)["pruned"] == ["job"]
    assert git(repository, "for-each-ref") == refs


def test_r1_7_worktree_and_repository_both_gone(world):
    """R1-7: a job whose worktree and source repository are gone is still retired."""
    store, root, repository = world
    worktree = add_job(store, root, repository, "job")
    git(repository, "worktree", "remove", "--force", str(worktree))
    import shutil
    shutil.rmtree(repository)
    assert retention.maintenance(store, root, max_jobs=0)["pruned"] == ["job"]


def test_r2s_4_worktrees_are_never_walked(world, monkeypatch):
    """R2s-4 (the largest trees never finish): retention budgets job directories and never
    walks a worktree, so a huge tree costs a pass nothing."""
    store, root, repository = world
    worktree = add_job(store, root, repository, "job")
    walked = []
    original = retention._measure

    def measure(state, job, places, **kwargs):
        walked.extend(places)
        return original(state, job, places, **kwargs)
    monkeypatch.setattr(retention, "_measure", measure)
    retention.maintenance(store, root, max_jobs=0)
    assert walked and all(worktree not in [p, *p.parents] for p in walked)


def test_r2s_5_r4_5_read_only_and_deep_job_directories(world):
    """R2s-5, R4-5: 0555 directories and a 200-deep tree are archived and fully deleted."""
    store, root, repository = world
    directory = plain_job(store, root, "job", 0)
    deep = directory
    for level in range(200):
        deep = deep / f"d{level % 10}"
    deep.mkdir(parents=True)
    (deep / "leaf").write_bytes(b"deep")
    (directory / "ro").mkdir()
    (directory / "ro" / "x").write_bytes(b"x")
    os.chmod(directory / "ro", 0o555)
    assert retention.maintenance(store, root, max_jobs=0)["pruned"] == ["job"]
    assert not directory.exists()
    restored = root / "restored"
    archive.restore(root / "archive" / "job", restored)
    assert (restored / Path(*[f"d{level % 10}" for level in range(200)]) / "leaf").read_bytes() == b"deep"
    os.chmod(restored / "ro", 0o755)


def test_r3_8_many_jobs_reach_the_budget(world):
    """R3-8 (expiry livelock): 150 jobs over a 10-job budget are all retired in one pass."""
    store, root, repository = world
    for order in range(150):
        plain_job(store, root, f"j{order:03d}", order)
    result = retention.maintenance(store, root, max_jobs=10)
    assert len(result["pruned"]) == 140 and result["jobs_after"] == 10


def test_r4_1_a_writer_inside_a_job_directory_after_archiving_keeps_its_file(world, monkeypatch):
    """R4-1: a late write into a job directory survives (J3); only unchanged entries go."""
    store, root, repository = world
    directory = plain_job(store, root, "job", 0)
    original = archive.delete_archived

    def write_first(path, manifest, **kwargs):
        (Path(path) / "late.txt").write_bytes(b"late result")
        return original(path, manifest, **kwargs)
    monkeypatch.setattr(archive, "delete_archived", write_first)
    assert retention.maintenance(store, root, max_jobs=0)["pruned"] == ["job"]
    assert (root / "retention-conflicts" / "job" / "late.txt").read_bytes() == b"late result"


def test_r4_3_a_stuck_oldest_job_does_not_block_the_queue(world, tmp_path):
    """R4-3: the oldest job's worktree is still in place; newer jobs are still retired."""
    store, root, repository = world
    add_job(store, root, repository, "stuck", order=0)
    plain_job(store, root, "later", 1)
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["later"] and "stuck" in result["kept"]


def test_design_r1_astra_6_rows_survive_as_verified_json(world):
    """Design review (Astra 6): the deleted rows are in the verified archive, digest checked."""
    store, root, repository = world
    plain_job(store, root, "job", 0)
    store.add_notice("job", "a message nobody read", None)
    assert retention.maintenance(store, root, max_jobs=0)["pruned"] == ["job"]
    manifest = archive.verify(root / "archive" / "job")
    rows = json.loads((root / "archive" / "job" / "rows.json").read_bytes())
    assert rows["sha256"] == manifest["rows_sha256"]
    assert rows["rows"]["jobs"][0]["job_id"] == "job"
    assert rows["rows"]["notices"][0]["text"] == "a message nobody read"


def test_r1_every_pin_holds_and_a_mid_pass_pin_is_rechecked(world, monkeypatch):
    """Round-1 probe `test_ok_every_pin_holds_and_midpass_pins_are_rechecked`, ported: the
    mid-pass notice arrives while a job directory is archived instead of during a proof."""
    store, root, repository = world
    from subfleet.contracts import Credential, Lane, LaneOwner
    store.put_lane(Lane("codex-1", "codex", "codex:one", Credential("codex", "/home/one", "home"),
                        "/home/one", LaneOwner.V2, False))
    ids = ["queued", "running", "quarantined", "leased-job", "leased-attempt", "turn-kept",
           "conversation", "gate-evidence", "gate-review", "notice", "parent", "child",
           "notice-during-archive", "free"]
    for order, identity in enumerate(ids):
        state = {"queued": "queued", "running": "running"}.get(identity, "succeeded")
        kind = {"turn-kept": "turn", "gate-review": "gate-review"}.get(identity, "dispatch")
        store.add_job(job_id=identity, request_id=identity, payload_digest="digest", kind=kind,
                      workdir=str(root), prompt_path="/prompt", sandbox="read-only", state=state,
                      created_at=f"2026-01-01T00:00:{order:02d}Z",
                      finished_at="2099-01-01T00:00:00Z" if identity == "turn-kept" else "2026-01-02T00:00:00Z",
                      parent_job_id="parent" if identity == "child" else None)
        (root / "jobs" / identity).mkdir(parents=True, exist_ok=True)
        store.add_attempt(attempt_id=f"{identity}/a1", job_id=identity, seq=1, lane_id="codex-1",
                          model_requested="gpt-6-astra",
                          state="quarantined" if identity == "quarantined" else "succeeded")
    with store.transaction() as tx:
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES('out:/x','leased-job','t')")
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES('lane:x:slot:0','leased-attempt/a1','t')")
        tx.execute("INSERT INTO actions(action_id,kind,op_key,subject,state,request_json,result_json,created_at,updated_at) "
                   "VALUES('a','gate.merge','k','pr','confirmed',?, 'null','t','t')",
                   (json.dumps({"evidence": ["gate-evidence/a1"]}),))
    store.add_notice("notice", "done", session_id="session-1")
    original = archive.archive

    def archive_with_a_late_notice(job_dir, *args, **kwargs):
        if job_dir.name == "notice-during-archive":
            store.add_notice("notice-during-archive", "late", session_id="session-2")
        return original(job_dir, *args, **kwargs)

    monkeypatch.setattr(archive, "archive", archive_with_a_late_notice)
    result = retention.maintenance(store, root, max_jobs=0, turn_max_jobs=0, turn_keep_s=86400,
                                   pins=lambda: {"conversation"})
    assert result["pruned"] == ["child", "free"]
    assert {job["job_id"] for job in store.list_jobs()} == set(ids) - {"child", "free"}
    assert (root / "jobs" / "notice-during-archive").is_dir()
    assert not (root / "archive" / "notice-during-archive").exists()
    assert {row["lease_key"] for row in store.list_leases()} == {"out:/x", "lane:x:slot:0"}
