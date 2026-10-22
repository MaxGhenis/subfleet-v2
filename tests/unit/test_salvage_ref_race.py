"""C-13.1: concurrent finalizers keep every snapshot in the shared ref namespace."""

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import tempfile
from threading import Barrier, Event

from hypothesis import example, given, settings, strategies as st
import pytest

from subfleet import salvage as sm


BASE_REF = "refs/subfleet-salvage/detached-20261020T195655Z-a2"


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def repository(path):
    path.mkdir()
    git(path, "init", "--quiet", "-b", "task")
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "tracked").write_text("baseline\n")
    git(path, "add", ".")
    git(path, "commit", "--quiet", "-m", "baseline")
    return git(path, "rev-parse", "HEAD")


def race_salvagers(base, values, patch):
    """Force absent-ref reads before CAS, at both the base and sibling names.

    Writer zero wins the base name. Other writers then perform real Git CAS
    writes against it. Equal losing trees meet at a second barrier before
    creating their sibling, exercising the third-writer replay as well.
    """
    repo = base / "repo"
    parent = repository(repo)
    worktrees, trees = [], []
    for i, value in enumerate(values):
        worktree = base / f"writer-{i}"
        git(repo, "worktree", "add", "--quiet", "--detach", str(worktree), parent)
        (worktree / "tracked").write_text(f"snapshot {value}\n")
        git(worktree, "add", "tracked")
        trees.append(git(worktree, "write-tree"))  # Independent Git oracle.
        worktrees.append(worktree)

    base_barrier, base_created = Barrier(len(values), timeout=60), Event()
    side_barriers = {f"{BASE_REF}-{tree[:12]}": Barrier(n, timeout=60)
                     for tree, n in Counter(tree for tree in trees if tree != trees[0]).items()}
    absent = set()
    original = sm._git

    def synchronized(workdir, *args, **kwargs):
        if args[:2] == ("update-ref", BASE_REF):
            base_barrier.wait()
            if workdir == worktrees[0]:
                try:
                    return original(workdir, *args, **kwargs)
                finally:
                    base_created.set()
            assert base_created.wait(60), "base writer did not finish"
        elif args[0] == "update-ref" and args[1] in side_barriers:
            side_barriers[args[1]].wait()
        result = original(workdir, *args, **kwargs)
        if args == ("rev-parse", "--verify", BASE_REF) and result is None:
            absent.add(workdir)
        return result

    patch.setattr(sm, "_git", synchronized)

    def save(worktree):
        return sm.salvage(worktree, parent, 2, timestamp="2026-10-20T19:56:55Z")

    with ThreadPoolExecutor(max_workers=len(values)) as executor:
        results = list(executor.map(save, worktrees))
    assert absent == set(worktrees), "every writer must observe the base ref absent"

    held = dict(line.split() for line in git(
        repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/subfleet-salvage/"
    ).splitlines())
    refs_by_tree = defaultdict(set)
    for result, tree in zip(results, trees, strict=True):
        assert result is not None and result.tree == tree
        assert held[result.ref] == result.commit
        assert git(repo, "rev-parse", f"{result.ref}^{{tree}}") == tree
        assert git(repo, "rev-parse", f"{result.ref}^") == parent
        expected = BASE_REF if tree == trees[0] else f"{BASE_REF}-{tree[:12]}"
        assert result.ref == expected
        refs_by_tree[tree].add(result.ref)
    assert len(held) == len(set(trees))
    assert all(len(refs) == 1 for refs in refs_by_tree.values())
    return results, held


def test_concurrent_salvage_keeps_different_trees(tmp_path, monkeypatch):
    """D-SALV1 regression: two detached jobs with the same reservation second."""
    results, held = race_salvagers(tmp_path, [0, 1], monkeypatch)
    assert len(held) == 2 and results[0].ref != results[1].ref


def test_concurrent_salvage_reuses_same_tree(tmp_path, monkeypatch):
    results, held = race_salvagers(tmp_path, [0, 0], monkeypatch)
    assert len(held) == 1 and results[0] == results[1]


def test_concurrent_salvage_reuses_sibling_after_second_collision(tmp_path, monkeypatch):
    results, held = race_salvagers(tmp_path, [0, 1, 1], monkeypatch)
    assert len(held) == 2 and results[1] == results[2]


@settings(max_examples=12, derandomize=True, deadline=None)
@example(values=[0, 0, 1, 1, 2, 2])
@given(values=st.lists(st.integers(min_value=0, max_value=3), min_size=2, max_size=6))
def test_concurrent_salvage_holds_each_distinct_tree_once(values):
    """Bounded property: every tree is held once, duplicates share, no call fails."""
    with tempfile.TemporaryDirectory(prefix="salvage-ref-race-") as temporary:
        with pytest.MonkeyPatch.context() as patch:
            race_salvagers(Path(temporary), values, patch)


@pytest.mark.parametrize("failure", [
    sm.SalvageError("git update-ref failed: permission denied"),
    sm.SalvageError("git update-ref failed: full disk", transient=True),
    sm.SalvageError("git update-ref timed out after 1 s", timed_out=True),
])
def test_hold_does_not_mask_other_failures_even_when_ref_appears(tmp_path, monkeypatch, failure):
    repo = tmp_path / "repo"
    parent = repository(repo)
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    original = sm._git

    def fail(workdir, *args, **kwargs):
        if args[0] == "update-ref":
            original(workdir, *args, **kwargs)
            raise failure
        return original(workdir, *args, **kwargs)

    monkeypatch.setattr(sm, "_git", fail)
    with pytest.raises(sm.SalvageError) as error:
        sm._hold(repo, BASE_REF, tree, parent, "snapshot")
    assert error.value is failure


def test_hold_raises_original_collision_if_ref_disappears(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    parent = repository(repo)
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    original = sm._git
    failure = sm.SalvageError("git update-ref failed: reference already exists")

    def fail(workdir, *args, **kwargs):
        if args[0] == "update-ref":
            raise failure
        return original(workdir, *args, **kwargs)

    monkeypatch.setattr(sm, "_git", fail)
    with pytest.raises(sm.SalvageError) as error:
        sm._hold(repo, BASE_REF, tree, parent, "snapshot")
    assert error.value is failure


def test_hold_same_tree_with_different_parent_is_not_a_replay(tmp_path):
    repo = tmp_path / "repo"
    parent = repository(repo)
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    other_parent = git(repo, "commit-tree", tree, "-p", parent, "-m", "another parent")
    first = sm._hold(repo, BASE_REF, tree, parent, "first")
    second = sm._hold(repo, BASE_REF, tree, other_parent, "second")
    assert second[0] == f"{BASE_REF}-{tree[:12]}" and first[0] == BASE_REF
    third_parent = git(repo, "commit-tree", tree, "-p", other_parent, "-m", "third parent")
    with pytest.raises(sm.SalvageError, match="different snapshot"):
        sm._hold(repo, BASE_REF, tree, third_parent, "third")
    assert git(repo, "rev-parse", BASE_REF) == first[1]
    assert git(repo, "rev-parse", second[0]) == second[1]
