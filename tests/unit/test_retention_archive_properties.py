"""Property: archive, then restore, gives back the tree's bytes and its commits (d635, I1, I3).

Hypothesis builds a job's worktree from random operations — files with random
bytes, modes and non-ASCII names in nested folders, empty folders (some
read-only), symbolic links inside, outside and dangling, hard links, FIFOs,
ignored output, and git history: detached commits, commits reset away, staged
content edited again, per-worktree refs and deleted tracked files. Retention
retires the job; then:

- restoring gives back every entry: path, type, permission bits, bytes, link
  target, hard-link grouping and mtime (I1);
- every commit and staged blob the worktree could reach before retirement is in
  a fresh clone of the remote plus the archive's bundle (I3): nothing depends on
  the source repository's local objects;
- nothing outside the job's paths changed (I4, a sentinel file).

`RETENTION_PROP_EXAMPLES` raises the example count for a long search.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, Phase, given, settings
from hypothesis import strategies as st

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet import retention_git as rgit
from tests.unit.retention_world import World, git, inode_groups, snapshot

EXAMPLES = int(os.environ.get("RETENTION_PROP_EXAMPLES", "10"))
#: Each example runs git and a retention pass; shrinking one can take many minutes.
PHASES = ((Phase.explicit, Phase.reuse, Phase.generate) if os.environ.get("RETENTION_PROP_NO_SHRINK")
          else tuple(Phase))
NAMES = ["a", "b.txt", "é", "日本", "sp ace", "x.log", "CRLF.txt", "nul"]
DIRS = ["", "d", "d/e", "ö", "out"]

content = st.binary(min_size=0, max_size=3000) | st.sampled_from([b"line\r\nline\r\n", b"\x00\x01\x02", b"$Id$\n"])
path = st.tuples(st.sampled_from(DIRS), st.sampled_from(NAMES)).map(lambda t: f"{t[0]}/{t[1]}" if t[0] else t[1])
op = st.one_of(
    st.tuples(st.just("write"), path, content, st.sampled_from([0o644, 0o600, 0o755, 0o444])),
    st.tuples(st.just("mkdir"), st.sampled_from(["empty", "d/empty", "ro"]), st.sampled_from([0o755, 0o700, 0o555])),
    st.tuples(st.just("symlink"), path, st.sampled_from(["b.txt", "../outside", "/etc/hosts", "missing"])),
    st.tuples(st.just("hardlink"), path, path),
    st.tuples(st.just("fifo"), path),
    st.tuples(st.just("commit"), path, content),
    st.tuples(st.just("reset")),
    st.tuples(st.just("stage-then-edit"), path, content, content),
    st.tuples(st.just("worktree-ref"), st.sampled_from(["keep", "other"])),
    st.tuples(st.just("delete-tracked")),
)


def _prepare(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.is_symlink() or p.exists():
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()


def apply(wt: Path, ops) -> dict:
    record = {"commits": set(), "blobs": set()}
    ro_dirs = []
    for o in ops:
        kind = o[0]
        try:
            if kind == "write":
                p = wt / o[1]
                _prepare(p)
                p.write_bytes(o[2])
                os.chmod(p, o[3])
            elif kind == "mkdir":
                p = wt / o[1]
                if not p.exists():
                    p.mkdir(parents=True)
                    if o[2] == 0o555:
                        ro_dirs.append(p)
                    else:
                        os.chmod(p, o[2])
            elif kind == "symlink":
                p = wt / o[1]
                _prepare(p)
                os.symlink(o[2], p)
            elif kind == "hardlink":
                src, dst = wt / o[1], wt / o[2]
                if src.is_file() and not src.is_symlink() and src != dst:
                    _prepare(dst)
                    os.link(src, dst)
            elif kind == "fifo":
                p = wt / o[1]
                _prepare(p)
                os.mkfifo(p)
            elif kind == "commit":
                p = wt / "committed" / o[1].replace("/", "_")
                _prepare(p)
                p.write_bytes(o[2])
                git(wt, "add", "-f", str(p.relative_to(wt)))
                git(wt, "commit", "--quiet", "-m", "c", "--allow-empty")
                record["commits"].add(git(wt, "rev-parse", "HEAD"))
            elif kind == "reset":
                if git(wt, "rev-parse", "--verify", "--quiet", "HEAD~1", check=False):
                    git(wt, "reset", "--quiet", "--hard", "HEAD~1")
            elif kind == "stage-then-edit":
                p = wt / "staged" / o[1].replace("/", "_")
                _prepare(p)
                p.write_bytes(o[2])
                git(wt, "add", "-f", str(p.relative_to(wt)))
                record["blobs"].add(git(wt, "rev-parse", ":" + str(p.relative_to(wt))))
                p.write_bytes(o[3])
            elif kind == "worktree-ref":
                commit = git(wt, "commit-tree", git(wt, "write-tree"), "-p", "HEAD", "-m", "ref " + o[1])
                git(wt, "update-ref", f"refs/worktree/{o[1]}", commit)
                record["commits"].add(commit)
            elif kind == "delete-tracked":
                if (wt / "README.md").exists():
                    (wt / "README.md").unlink()
        except (OSError, AssertionError):
            continue            # an operation that does not apply to this tree
    for p in ro_dirs:
        os.chmod(p, 0o555)
    return record


def _cleanup(base: Path) -> None:
    for directory, dirnames, _ in os.walk(base):
        for name in dirnames:
            p = Path(directory) / name
            if not p.is_symlink():
                os.chmod(p, 0o755)
    shutil.rmtree(base, ignore_errors=True)


def _retire_and_check(w: World, ops, *, delete_source: bool) -> None:
    wt = w.job("job-prop")
    sentinel = w.base / "outside"
    sentinel.write_text("outside the job\n")
    apply(wt, ops)
    expected = reachable(wt)
    before = snapshot(wt)
    groups = inode_groups(wt)
    result = retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0, holders=lambda watches, **_: {})
    assert result["pruned"] == ["job-prop"], result
    assert not wt.exists()
    assert sentinel.read_text() == "outside the job\n"
    if delete_source:
        shutil.rmtree(w.repo)
        rarch.restore(w.root, "job-prop", to=w.base / "restored")
        restored = w.base / "restored" / "worktree"
    else:
        git(w.repo, "worktree", "prune")
        git(w.repo, "reflog", "expire", "--expire=now", "--all")
        git(w.repo, "gc", "--quiet", "--prune=now")
        rarch.restore(w.root, "job-prop")
        restored = wt
    assert snapshot(restored) == before
    assert inode_groups(restored) == groups
    fresh = w.base / "fresh"
    git(w.base, "clone", "--quiet", str(w.remote), str(fresh))
    git(fresh, "fetch", "--quiet", str(w.root / "archive" / "job-prop" / "commits.bundle"), "+refs/*:refs/restored/*")
    for oid in expected:
        assert subprocess.run(["git", "-C", str(fresh), "cat-file", "-e", oid]).returncode == 0, oid


def reachable(wt: Path) -> set[str]:
    """What the worktree reaches at retirement: every commit reachable from HEAD,
    its reflog and its per-worktree refs, and every blob its index stages. (A
    commit a ref pointed at before it moved, with no reflog kept, is not.)"""
    tips = {git(wt, "rev-parse", "HEAD")}
    tips |= set(git(wt, "log", "-g", "--format=%H", "HEAD", check=False).split())
    tips |= set(git(wt, "for-each-ref", "--format=%(objectname)", "refs/worktree/", check=False).split())
    commits = set(git(wt, "rev-list", *sorted(tips)).split())
    blobs = {line.split()[1] for line in git(wt, "ls-files", "--stage").splitlines()
             if line and not line.startswith("160000")}
    return commits | blobs


@settings(max_examples=EXAMPLES, deadline=None, phases=PHASES, suppress_health_check=[HealthCheck.function_scoped_fixture,
                                                                        HealthCheck.too_slow])
@given(st.lists(op, min_size=1, max_size=14))
def test_archive_then_restore_round_trips_a_trusted_repository(monkeypatch, ops):
    monkeypatch.setattr(rgit, "temp_roots", lambda: {"/nonexistent-temporary-root"})
    base = Path(tempfile.mkdtemp(prefix="retention-prop-"))
    w = World(base)
    try:
        _retire_and_check(w, ops, delete_source=False)
    finally:
        w.close()
        _cleanup(base)


@settings(max_examples=EXAMPLES, deadline=None, phases=PHASES, suppress_health_check=[HealthCheck.function_scoped_fixture,
                                                                        HealthCheck.too_slow])
@given(st.lists(op, min_size=1, max_size=14))
def test_archive_then_restore_round_trips_after_the_scratch_source_is_deleted(ops):
    base = Path(tempfile.mkdtemp(prefix="retention-prop-scratch-"))
    w = World(base)          # a temporary directory: scratch, nothing omitted
    try:
        _retire_and_check(w, ops, delete_source=True)
    finally:
        w.close()
        _cleanup(base)
