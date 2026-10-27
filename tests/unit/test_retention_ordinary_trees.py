"""C-8.4/C-13.4: the "unarchived path" guard (cfcfdf45) never keeps an ordinary tree.

A worktree holding empty files, hard links (inside the tree, and to a file
outside it), symlinks (relative, absolute, dangling, to a directory), a nested
repository with and without a commit, `__pycache__` bytecode and a `.venv` uv
installed from its cache retires on the first pass, or on the pass after an
interrupted final check, and restores byte for byte except what the manifest
marks regenerable.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet.salvage import salvage
from tests.unit.retention_world import Clock, World, git, inode_groups, snapshot, trust_temporary_directories

PACKAGES = ("sortedcontainers==2.4.0",)


@pytest.fixture
def world(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    w = World(tmp_path)
    yield w
    w.close()


def _venv(path: Path, link_mode: str) -> None:
    """A virtualenv uv fills from its cache (offline: the cache is CI's or the caller's)."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    made = subprocess.run([uv, "venv", "-q", str(path), "--python", sys.executable],
                          capture_output=True, text=True, env=env)
    if made.returncode:
        pytest.skip(f"uv venv failed: {made.stderr.strip()[:200]}")
    installed = subprocess.run([uv, "pip", "install", "-q", "--offline", "--link-mode", link_mode,
                                "--python", str(path / "bin" / "python"), *PACKAGES],
                               capture_output=True, text=True, env=env)
    if installed.returncode:
        pytest.skip(f"{PACKAGES} not in uv's cache: {installed.stderr.strip()[:200]}")


def _ordinary_job(w: World, link_mode: str) -> tuple[Path, Path]:
    (w.repo / ".gitignore").write_text("out/\n*.log\n.venv/\n__pycache__/\n")
    (w.repo / "empty-tracked.txt").write_bytes(b"")
    git(w.repo, "add", ".")
    git(w.repo, "commit", "--quiet", "-m", "ignore a virtualenv; an empty tracked file")
    w.push()
    wt = w.job("job")
    # empty files: untracked, ignored, and an empty hard-link pair
    (wt / "empty-untracked.txt").write_bytes(b"")
    (wt / "out").mkdir()
    (wt / "out" / "empty.parquet").write_bytes(b"")
    (wt / "empty-a").write_bytes(b"")
    os.link(wt / "empty-a", wt / "empty-b")
    # hard links: an untracked pair, a tracked unchanged file, a link to a file outside the tree
    (wt / "notes.bin").write_bytes(os.urandom(5000))
    (wt / "sub").mkdir()
    os.link(wt / "notes.bin", wt / "sub" / "notes-link.bin")
    os.link(wt / "README.md", wt / "readme-copy.md")
    outside = w.base / "outside" / "shared.bin"
    outside.parent.mkdir()
    outside.write_bytes(b"shared with another tree\n" * 100)
    os.link(outside, wt / "shared-from-outside.bin")
    # symlinks
    os.symlink("src/main.py", wt / "link-to-main")
    os.symlink("/etc/hosts", wt / "absolute-link")
    os.symlink("no-such-file", wt / "dangling-link")
    os.symlink("src", wt / "link-to-dir")
    os.symlink("notes.bin", wt / "link-to-hardlinked")
    # nested repositories: one with no commit (salvage leaves it out), one with a commit
    bare = wt / "nested-no-commit"
    bare.mkdir()
    git(bare, "init", "--quiet")
    (bare / "inside.txt").write_text("never committed\n")
    (bare / "empty-inside").write_bytes(b"")
    committed = wt / "nested-committed"
    committed.mkdir()
    git(committed, "init", "--quiet")
    (committed / "kept.txt").write_text("committed in the nested repository\n")
    git(committed, "add", ".")
    git(committed, "commit", "--quiet", "-m", "nested")
    (committed / "dirty.txt").write_text("not committed\n")
    # bytecode and a virtualenv from uv's cache
    py_compile.compile(str(wt / "src" / "main.py"), cfile=importlib.util.cache_from_source(str(wt / "src" / "main.py")))
    _venv(wt / ".venv", link_mode)
    result = salvage(wt, w.head(), 1, timestamp="2026-10-05T12:00:00Z")
    assert result is not None and "nested-no-commit/" in result.skipped
    w.store.add_artifact(w.attempt("job"), "salvage", result.ref,
                         hashlib.sha256(result.commit.encode()).hexdigest(), 0)
    return wt, outside


def _run(w: World):
    return retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0, clock=Clock(),
                                 holders=lambda watches, **_: {},
                                 salvage_referenced_elsewhere=lambda artifact: True)


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("link_mode", ["clone", "hardlink"])
def test_an_ordinary_tree_retires_without_an_unarchived_path(world, monkeypatch, link_mode, resume):
    w = world
    wt, outside = _ordinary_job(w, link_mode)
    before = snapshot(wt)
    links_before = inode_groups(wt)
    shared = outside.read_bytes()
    if resume:
        def interrupt(self):
            raise rarch.Interrupted("restart before the final check")

        with monkeypatch.context() as patch:
            patch.setattr(rarch.Retirement, "final_check", interrupt)
            first = _run(w)
        assert first["pruned"] == [] and first.get("interrupted"), first
        assert rarch.Retirement(rarch.Context(w.root, w.store), "job").state == "archived"
    outcome = _run(w)
    assert outcome["pruned"] == ["job"] and outcome["protected"] == [], outcome
    assert not any("unarchived path" in str(v) for v in outcome.get("deferred", {}).values()), outcome
    assert not wt.exists() and outside.read_bytes() == shared
    assert rarch.check_archive(w.root, "job")["ok"]

    manifest = json.loads((w.root / "archive" / "job" / "manifest.json").read_text())
    entries = manifest["trees"]["worktree"]["entries"]
    files = [e for e in entries if e["sig"]["t"] == "f"]
    regen = {e["p"] for e in entries if e.get("regen")}
    # Every way a file is covered occurs, so the guard saw each of them.
    assert any(e.get("regen") for e in files)                        # the virtualenv and bytecode
    assert any(e.get("blob") for e in files)                         # omitted: the remote holds it
    assert any(e.get("hl") for e in files)                           # a second link
    assert any(e.get("store") and e["size"] == 0 for e in files)     # an empty file, stored
    assert all(e.get("store") or e.get("blob") or e.get("regen") for e in files)
    assert any(p.startswith(".venv/lib/") for p in regen)
    assert not any(p.startswith("nested-") for p in regen)

    report = rarch.restore(w.root, "job")
    assert "worktree" in report["restored"]
    kept = {p: e for p, e in before.items() if p not in regen}
    restored = {p: e for p, e in snapshot(wt).items() if p not in regen}
    # A regenerable directory comes back empty, so its own times differ.
    emptied = {p for p in kept if any(r.startswith(p + "/") for r in regen)}
    assert {p: e for p, e in restored.items() if p not in emptied} == \
        {p: e for p, e in kept.items() if p not in emptied}
    assert [g for g in inode_groups(wt) if not g & regen] == [g for g in links_before if not g & regen]
    assert (wt / "nested-no-commit" / "inside.txt").read_text() == "never committed\n"
