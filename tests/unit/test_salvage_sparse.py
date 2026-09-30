"""C-13.1 under C-6.14: a sparse worktree's snapshot is a full checkout's with the same edits.

Every test cuts a sparse worktree and a full one from the same commit with
`checkout.create_worktree` (the daemon's own path), does the same things in both,
and compares what `salvage.working_tree` and `salvage.salvage` record. Files outside
the cone are never read as deleted, and the salvage ref of a sparse worktree records
only real changes.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import retention, salvage as salvage_module
from subfleet.checkout import create_worktree
from subfleet.conversations import diff as turn_diff
from subfleet.salvage import SalvageError, git_head, salvage, sparse_checkout, working_tree
from subfleet.store import Store
from tests.unit.test_checkout import git, make_repo

LAYOUT = {
    "README.md": b"top\n",
    "src/app.py": b"print('app')\n",
    "src/pkg/mod.py": b"VALUE = 1\n",
    "src/pkg/data.txt": b"payload\n",
    "docs/guide.md": b"guide\n",
    "data/index.json": b"{}\n",
    "data/big/one.bin": b"1" * 3000,
    "data/big/two.bin": b"2" * 3000,
    "data/big/sub/three.bin": b"3" * 1000,
    "data/other/notes.txt": b"notes\n",
}
CONE = ["src"]
IN_CONE = ["src/app.py", "src/pkg/mod.py", "src/pkg/data.txt"]


def cut(repo: Path, target: Path, *, sparse: bool, commit: str = "HEAD") -> Path:
    head = git(repo, "rev-parse", commit)
    create_worktree(str(repo), str(target), head,
                    {"mode": "sparse", "cone": CONE} if sparse else {"mode": "full"}, timeout_s=60)
    return target


@pytest.fixture
def pair(tmp_path):
    repo = make_repo(tmp_path / "repo", LAYOUT)
    return repo, cut(repo, tmp_path / "sparse", sparse=True), cut(repo, tmp_path / "full", sparse=False)


def tree_paths(repo: Path, tree: str) -> dict[str, str]:
    listed = git(repo, "ls-tree", "-r", "-z", tree)
    return {entry.split("\t", 1)[1]: entry.split()[2] for entry in listed.split("\0") if entry}


def test_c13_1_a_clean_sparse_worktree_snapshots_to_its_baseline(pair):
    """C-13.1, C-6.14 nothing changed: no deletions, no salvage ref."""
    repo, sparse, _ = pair
    assert sparse_checkout(sparse) is True
    assert not (sparse / "data").exists()
    head = git_head(sparse)
    assert working_tree(sparse, head) == git(sparse, "rev-parse", "HEAD^{tree}")
    assert salvage(sparse, head, 1) is None
    assert git(repo, "for-each-ref", "refs/subfleet-salvage/") == ""


def test_c13_1_plain_add_all_refuses_a_file_outside_the_cone_and_the_snapshot_does_not(pair):
    """C-13.1 the defect: `add -A` exits 1 on a file written outside the cone, so the old
    snapshot failed and held the worktree; the snapshot records it."""
    _, sparse, _ = pair
    (sparse / "reports").mkdir()
    (sparse / "reports" / "out.md").write_text("report\n")
    with tempfile.TemporaryDirectory() as temporary:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
        subprocess.run(["git", "-C", str(sparse), "read-tree", "HEAD"], env=env, check=True)
        plain = subprocess.run(["git", "-C", str(sparse), "add", "-A"], env=env, capture_output=True)
    assert plain.returncode == 1 and b"sparse-checkout" in plain.stderr
    tree = working_tree(sparse, git_head(sparse))
    assert "reports/out.md" in tree_paths(sparse, tree)


def edit(worktree: Path) -> None:
    (worktree / "src" / "app.py").write_text("print('changed')\n")
    (worktree / "src" / "pkg" / "data.txt").unlink()
    (worktree / "src" / "new").mkdir()
    (worktree / "src" / "new" / "file.py").write_text("NEW = True\n")
    (worktree / "reports").mkdir()
    (worktree / "reports" / "out.md").write_text("report\n")
    (worktree / "data" / "big").mkdir(parents=True, exist_ok=True)
    (worktree / "data" / "big" / "one.bin").write_bytes(b"agent wrote this\n")
    (worktree / "data" / "big" / "new.bin").write_bytes(b"new\n")


def test_c13_1_edits_in_and_outside_the_cone_equal_a_full_checkouts(pair):
    """C-13.1, C-6.14 the salvage of a sparse worktree equals a full checkout's with the same edits."""
    repo, sparse, full = pair
    head = git_head(sparse)
    for worktree in (sparse, full):
        edit(worktree)
    assert working_tree(sparse, head) == working_tree(full, head)
    ours = salvage(sparse, head, 1, timestamp="2026-09-29T10:00:00Z")
    theirs = salvage(full, head, 2, timestamp="2026-09-29T10:00:00Z")
    assert ours is not None and theirs is not None and ours.tree == theirs.tree
    kept = tree_paths(repo, ours.tree)
    # Out of the cone and untouched: kept, byte for byte.
    for name in ("data/big/two.bin", "data/big/sub/three.bin", "data/other/notes.txt", "docs/guide.md"):
        assert kept[name] == git(repo, "rev-parse", f"HEAD:{name}")
    assert "src/pkg/data.txt" not in kept
    assert git(repo, "show", f"{ours.ref}:data/big/one.bin") == "agent wrote this"
    changed = git(repo, "diff", "--name-status", head, ours.commit).splitlines()
    assert sorted(changed) == sorted(["M\tsrc/app.py", "D\tsrc/pkg/data.txt", "A\tsrc/new/file.py",
                                      "A\treports/out.md", "M\tdata/big/one.bin", "A\tdata/big/new.bin"])


def test_c13_1_the_snapshot_leaves_head_index_and_files_alone(pair):
    """C-13.1 in a sparse worktree too: HEAD, the real index and every file are untouched."""
    _, sparse, _ = pair
    edit(sparse)
    git(sparse, "add", "--", "src/app.py")
    index_path = Path(git(sparse, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    before = (git_head(sparse), index_path.read_bytes(), git(sparse, "status", "--porcelain=v1"),
              sorted(p.relative_to(sparse).as_posix() for p in sparse.rglob("*") if ".git" not in p.parts))
    salvage(sparse, before[0], 1)
    after = (git_head(sparse), index_path.read_bytes(), git(sparse, "status", "--porcelain=v1"),
             sorted(p.relative_to(sparse).as_posix() for p in sparse.rglob("*") if ".git" not in p.parts))
    assert before == after
    assert not list(index_path.parent.glob("subfleet-salvage-*"))


def test_c13_1_a_newer_commit_checked_out_in_the_sparse_worktree_is_snapshotted_whole(pair):
    """C-13.1 files outside the cone take the real index's content: an agent that checks
    out upstream work gets the snapshot a full checkout of it gives."""
    repo, sparse, full = pair
    baseline = git_head(sparse)
    git(repo, "checkout", "-q", "-b", "upstream")
    (repo / "data" / "big" / "two.bin").write_bytes(b"upstream\n")
    (repo / "data" / "other" / "notes.txt").unlink()
    git(repo, "commit", "-qam", "upstream")
    upstream = git_head(repo)
    git(repo, "checkout", "-q", "task/sparse")
    for worktree in (sparse, full):
        git(worktree, "checkout", "-q", "--detach", upstream)
    assert not (sparse / "data").exists()
    assert working_tree(sparse, baseline) == working_tree(full, baseline) == git(repo, "rev-parse", f"{upstream}^{{tree}}")


def test_c6_8_seeded_and_empty_index_reads_agree_in_a_sparse_worktree(pair, monkeypatch):
    """C-6.8, C-13.1 the empty-index read overlays the same entries, so both reads agree."""
    _, sparse, _ = pair
    edit(sparse)
    head = git_head(sparse)
    seeded = working_tree(sparse, head)
    monkeypatch.setattr(salvage_module, "_seed_index", lambda *args, **kwargs: False)
    assert working_tree(sparse, head) == seeded


def test_c6_8_an_unmerged_sparse_worktree_falls_back_and_still_agrees(pair):
    """C-6.8 an in-cone conflict (unmerged entries) takes the empty-index read, which
    still keeps the files outside the cone and equals the full checkout's."""
    repo, sparse, full = pair
    head = git_head(sparse)
    git(repo, "checkout", "-q", "-b", "theirs")
    (repo / "src" / "app.py").write_text("print('theirs')\n")
    (repo / "data" / "big" / "two.bin").write_bytes(b"theirs too\n")
    git(repo, "commit", "-qam", "theirs")
    git(repo, "checkout", "-q", "task/sparse")
    for worktree in (sparse, full):
        (worktree / "src" / "app.py").write_text("print('ours')\n")
        git(worktree, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "ours")
        merged = subprocess.run(["git", "-C", str(worktree), "-c", "user.name=t", "-c", "user.email=t@t",
                                 "merge", "theirs"], capture_output=True)
        assert merged.returncode == 1
        assert "UU src/app.py" in git(worktree, "status", "--porcelain=v1").splitlines()
    assert not (sparse / "data").exists()
    assert working_tree(sparse, head) == working_tree(full, head)


def test_c13_1_a_sparse_worktree_with_no_index_is_refused_not_read_as_deleted(pair):
    """C-13.1 without an index git reads every absent file as deleted; the snapshot refuses."""
    _, sparse, _ = pair
    Path(git(sparse, "rev-parse", "--path-format=absolute", "--git-path", "index")).unlink()
    with pytest.raises(SalvageError, match="no index"):
        working_tree(sparse, git_head(sparse))


def test_c13_1_a_users_sparse_index_checkout_snapshots_like_a_full_one(pair):
    """C-13.1 an in-place job in a user's own sparse checkout with a sparse index
    (directory entries in the index): the same snapshot as a full checkout, and
    the real index's bytes untouched."""
    _, sparse, full = pair
    git(sparse, "sparse-checkout", "set", "--cone", "--sparse-index", *CONE)
    assert "040000" in git(sparse, "ls-files", "--sparse", "-s")
    head = git_head(sparse)
    for worktree in (sparse, full):
        edit(worktree)
    index_path = Path(git(sparse, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    before = index_path.read_bytes()
    assert working_tree(sparse, head) == working_tree(full, head)
    assert index_path.read_bytes() == before


def test_c13_1_a_non_sparse_checkout_keeps_its_skip_worktree_semantics(pair):
    """C-13.1 unchanged for a checkout that is not sparse: a skip-worktree file is read from disk."""
    _, _, full = pair
    head = git_head(full)
    git(full, "update-index", "--skip-worktree", "--", "docs/guide.md")
    (full / "docs" / "guide.md").write_text("hidden edit\n")
    assert sparse_checkout(full) is False
    tree = working_tree(full, head)
    assert git(full, "cat-file", "-p", tree_paths(full, tree)["docs/guide.md"]) == "hidden edit"


# --- the property ------------------------------------------------------------------------

IN_DIRS = ["src", "src/pkg", "src/new", "src/new/deeper"]
OUT_DIRS = ["data/big", "data/new", "reports", "docs"]
content = st.binary(min_size=0, max_size=64)
name = st.sampled_from(["a.txt", "b.py", "c.bin", "d"])
in_cone_ops = st.one_of(
    st.tuples(st.just("modify"), st.integers(0, 2), content),
    st.tuples(st.just("delete"), st.integers(0, 2)),
    st.tuples(st.just("create"), st.sampled_from(IN_DIRS), name, content),
    st.tuples(st.just("chmod"), st.integers(0, 2)),
    st.tuples(st.just("symlink"), st.sampled_from(IN_DIRS), name),
    st.tuples(st.just("stage"), st.integers(0, 2)),
    st.tuples(st.just("commit"),),
)
outside_ops = st.one_of(
    st.tuples(st.just("create"), st.sampled_from(OUT_DIRS), name, content),
    st.tuples(st.just("overwrite"), st.sampled_from(["data/big/one.bin", "data/other/notes.txt"]), content),
)


def apply(worktree: Path, op: tuple) -> None:
    kind = op[0]
    if kind in ("modify", "delete", "chmod", "stage"):
        path = worktree / IN_CONE[op[1]]
        if kind == "modify":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(op[2])
        elif kind == "delete":
            path.unlink(missing_ok=True)
        elif kind == "chmod" and path.is_file():
            path.chmod(0o755)
        elif kind == "stage":
            subprocess.run(["git", "-C", str(worktree), "add", "-A", "--", IN_CONE[op[1]]], capture_output=True)
    elif kind == "create":
        directory = worktree / op[1]
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / op[2]
        if target.is_symlink() or target.exists():
            target.unlink()
        target.write_bytes(op[3])
    elif kind == "symlink":
        directory = worktree / op[1]
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / op[2]
        if target.is_symlink() or target.exists():
            target.unlink()
        target.symlink_to("../app.py")
    elif kind == "overwrite":
        path = worktree / op[1]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(op[2])
    elif kind == "commit":
        git(worktree, "add", "-A", "--", "src")
        git(worktree, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "step")


_SOURCE: dict[str, Path] = {}
_COUNTER = itertools.count()


def source_repo() -> Path:
    """One source repository for every example; each example cuts its own worktrees."""
    if "repo" not in _SOURCE:
        root = Path(tempfile.mkdtemp(prefix="sparse-property-"))
        _SOURCE["repo"] = make_repo(root / "repo", LAYOUT)
    return _SOURCE["repo"]


def check_equal_snapshots(ops: list[tuple]) -> None:
    repo = source_repo()
    n = next(_COUNTER)
    sparse = cut(repo, repo.parent / f"s{n}", sparse=True)
    full = cut(repo, repo.parent / f"f{n}", sparse=False)
    try:
        baseline = git_head(sparse)
        for op in ops:
            apply(sparse, op)
            apply(full, op)
        # `salvage` takes C-13.1's snapshot (`working_tree`) and writes a ref only
        # when it differs from the baseline, so equal results mean equal snapshots.
        snap_s = salvage(sparse, baseline, 1, timestamp="2026-09-29T00:00:00Z")
        snap_f = salvage(full, baseline, 1, timestamp="2026-09-29T00:00:01Z")
        assert (snap_s and snap_s.tree) == (snap_f and snap_f.tree)
        if not ops:
            assert snap_s is None
    finally:
        for worktree in (sparse, full):
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
                           capture_output=True)


@settings(max_examples=30, deadline=None)
@given(ops=st.lists(in_cone_ops, max_size=8))
def test_c13_1_property_in_cone_edits_salvage_to_a_full_checkouts_tree(ops):
    """C-13.1, C-6.14 for any in-cone edits (write, delete, create, chmod, symlink,
    stage, commit), the sparse worktree's snapshot and salvage tree equal a full
    checkout's with the same edits; with none, there is no salvage ref."""
    check_equal_snapshots(ops)


@settings(max_examples=20, deadline=None)
@given(ops=st.lists(st.one_of(in_cone_ops, outside_ops), max_size=8))
def test_c13_1_property_writes_outside_the_cone_are_kept_too(ops):
    """C-13.1, C-6.14 the same with files written outside the cone: new files, new
    directories, and tracked files the job wrote where the cone has none."""
    check_equal_snapshots(ops)


# --- retention and the turn diff read the same snapshot ----------------------------------

def test_c13_4_retention_removes_a_preserved_sparse_worktree_and_keeps_an_unpreserved_one(tmp_path):
    """C-13.4 the dirty check is C-13.1's snapshot: a sparse worktree whose salvage
    ref matches is removed; a later edit outside the cone keeps it."""
    repo = make_repo(tmp_path / "repo", LAYOUT)
    root = tmp_path / "state"
    worktree = cut(repo, root / "worktrees" / "job", sparse=True)
    edit(worktree)
    with Store(root / "state.sqlite3") as store:
        store.add_job(job_id="job", request_id="request", payload_digest="digest", kind="dispatch",
                      workdir=str(repo), worktree=str(worktree), prompt_path="/prompt",
                      sandbox="workspace-write", state="succeeded")
        result = salvage(worktree, git_head(worktree), 1, timestamp="2026-09-29T12:00:00Z")
        job = store.get_job("job")
    artifacts = [{"path": result.ref}]
    (worktree / "reports" / "late.md").write_text("after the salvage\n")
    with pytest.raises(ValueError, match="not preserved"):
        retention._remove_worktree(job, root.resolve(), artifacts, worktree.resolve())
    (worktree / "reports" / "late.md").unlink()
    retention._remove_worktree(job, root.resolve(), artifacts, worktree.resolve())
    assert not worktree.exists()
    assert git(repo, "rev-parse", f"{result.ref}:data/big/two.bin") == git(repo, "rev-parse", "HEAD:data/big/two.bin")


def test_c26_14_a_turn_diff_in_a_sparse_worktree_lists_only_real_changes(pair):
    """C-26.14 the start and end snapshots of a sparse workspace differ only where the turn wrote."""
    _, sparse, _ = pair
    head, start = turn_diff.snapshot(sparse)
    assert start == git(sparse, "rev-parse", "HEAD^{tree}")
    (sparse / "src" / "app.py").write_text("print('turn')\n")
    (sparse / "reports").mkdir()
    (sparse / "reports" / "r.md").write_text("r\n")
    end = turn_diff.end_snapshot(sparse, head_before=head, start_tree=start)
    result = turn_diff.build(sparse, start, end["end_tree"])
    assert sorted((item["status"], item["path"]) for item in result["files"]) == [
        ("added", "reports/r.md"), ("modified", "src/app.py")]
