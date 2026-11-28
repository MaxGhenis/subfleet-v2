"""C-8.5, review P3-4: verification never recurses through a tree without a cap.

A bundle of a few KB can hold one commit whose shared subtrees name some 10^12
paths. `.github` is compared as root entries; the one recursive walk (for
gitlinks and symlinks) is a streamed `ls-tree` refused past TREE_ENTRY_CAP;
every Git call of a verification shares one deadline. Real submit, acceptance
and local push.
"""
import resource
import subprocess
import sys
import time

import pytest

from subfleet import host_push
from test_host_push import accept, assert_failed, bundle_path, commit, git, submit, worlds  # noqa: F401

MEMORY_BOUND = 256 * 1024 * 1024    # bytes; diff-tree -r on this bomb reached 380 MiB in 4 s


def mktree(repo, lines):
    result = subprocess.run([host_push.GIT, "-C", str(repo), "mktree"], input="".join(lines).encode(),
                            capture_output=True, timeout=120,
                            env={"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    assert result.returncode == 0, result.stderr
    return result.stdout.decode().strip()


def tree_bomb(world, depth=10, width=16):
    """Commit a root holding `bomb/`: `depth` levels of `width` names, each
    level's names all one shared subtree, so width**depth paths in a few KB."""
    (world.repo / "leaf").write_text("x")
    oid = git(world.repo, "hash-object", "-w", "leaf")
    (world.repo / "leaf").unlink()
    kind, mode = "blob", "100644"
    for level in range(depth):
        oid = mktree(world.repo, [f"{mode} {kind} {oid}\t{'d' if kind == 'tree' else 'f'}{i}\n"
                                  for i in range(width)])
        kind, mode = "tree", "040000"
    root = git(world.repo, "ls-tree", "HEAD").splitlines(keepends=False)
    root = mktree(world.repo, [line + "\n" for line in root] + [f"040000 tree {oid}\tbomb\n"])
    head = git(world.repo, "rev-parse", "HEAD")
    tip = git(world.repo, "commit-tree", root, "-p", head, "-m", "bomb")
    git(world.repo, "update-ref", "HEAD", tip)
    return tip


def timed_verification(world):
    spent = []
    actual = host_push.verify_bundle

    def timed(*args, **kwargs):
        started = time.monotonic()
        try:
            return actual(*args, **kwargs)
        finally:
            spent.append(time.monotonic() - started)
    world.patch.setattr(host_push, "verify_bundle", timed)
    return spent


def children_peak():
    """The largest resident size of any child this process has reaped, in bytes."""
    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * (1 if sys.platform == "darwin" else 1024)


def test_a_tree_bomb_is_refused_at_the_entry_cap_with_git_memory_bounded(worlds):
    with worlds() as world:
        submit(world)
        tree_bomb(world)
        spent = timed_verification(world)
        before = children_peak()
        row = accept(world)
        assert bundle_path(world).stat().st_size < 16 * 1024
        assert_failed(world, row, f"more than {host_push.TREE_ENTRY_CAP} entries")
        assert spent and spent[0] < host_push.VERIFY_DEADLINE_S
        assert children_peak() <= max(before, MEMORY_BOUND)


def test_a_tree_bomb_past_any_cap_is_refused_by_the_one_deadline(worlds):
    """With no entry cap in the way, the shared deadline stops it: no Git call
    of the verification outlives it, and the refusal says so."""
    with worlds() as world:
        submit(world)
        tree_bomb(world)
        world.patch.setattr(host_push, "TREE_ENTRY_CAP", 10 ** 15)
        world.patch.setattr(host_push, "VERIFY_DEADLINE_S", 20)
        spent = timed_verification(world)
        before = children_peak()
        row = accept(world)
        assert_failed(world, row, "exceeded its 20 s deadline")
        assert spent[0] < 20 + 15, spent
        assert children_peak() <= max(before, MEMORY_BOUND)


@pytest.mark.parametrize("change", ["case-variant", "deleted", "deep-edit"])
def test_github_changes_are_found_from_the_root_entries_alone(worlds, change):
    with worlds() as world:
        commit(world.repo, ".github/workflows/ci.yml", "baseline workflow")
        git(world.repo, "push", "origin", "HEAD:refs/heads/trunk")
        world.base = git(world.repo, "rev-parse", "HEAD")
        submit(world)
        if change == "case-variant":
            commit(world.repo, ".GitHub/workflows/new.yml", "a second, differently cased folder")
        elif change == "deleted":
            git(world.repo, "rm", "-r", "-q", ".github")
            git(world.repo, "commit", "-m", "remove workflows")
        else:
            commit(world.repo, ".github/workflows/ci.yml", "edited deep below the root entry")
        calls = len(world.calls)
        assert_failed(world, accept(world), ".github/")
        assert not any(args[0] == "diff-tree" for _, args in world.calls[calls:])
        assert not any(args[:2] == ("ls-tree", "-r") for _, args in world.calls[calls:])
