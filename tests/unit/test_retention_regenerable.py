"""Retention by archive, disk relief (d635 round 2): regenerable output is
deleted with the tree, not archived, and only when its structure says what it
is, git tracks nothing in it and ignores all of it.

- a `.venv`/`venv` with a `pyvenv.cfg` naming `home`;
- a `node_modules` beside a `package.json`;
- a `__pycache__` holding only `.pyc`/`.pyo` files;
- a `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, `.tox` or `.uv-cache`
  carrying a `CACHEDIR.TAG` with the standard's signature.

Everything else is archived byte for byte, lookalikes included: `build/`,
`dist/`, `target/` and `.cache/` whatever they hold, a marker that is missing,
wrong or a link, a directory git tracks something in or does not ignore, and a
directory that holds a repository.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet import retention_fs as rfs
from subfleet import retention_git as rgit
from tests.unit.retention_world import World, git, snapshot, trust_temporary_directories

TAG = rfs.CACHEDIR_SIGNATURE + b"\n# This file is a cache directory tag.\n"


@pytest.fixture
def world(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    w = World(tmp_path)
    yield w
    w.close()


def run(w: World, **kwargs):
    kwargs.setdefault("max_jobs", 0)
    kwargs.setdefault("max_bytes", 0)
    kwargs.setdefault("holders", lambda watches, **_: {})
    return retention.maintenance(w.store, w.root, **kwargs)


def write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode()
    path.write_bytes(data)
    return path


def ignore(wt: Path, *patterns: str) -> None:
    """Ignore rules for the job's worktree only (its own info/exclude would be
    shared with the source clone)."""
    write(wt / ".gitignore", (wt / ".gitignore").read_text() + "".join(p + "\n" for p in patterns))


def regenerable_tree(wt: Path) -> dict[str, str]:
    """One of each kind, ignored the ways tools do it: by the project's
    .gitignore, or by a `*` .gitignore the tool writes inside."""
    ignore(wt, ".venv/", "node_modules/", "__pycache__/", ".uv-cache/")
    write(wt / ".venv" / "pyvenv.cfg", "home = /usr/local/bin\nversion_info = 3.14\n")
    write(wt / ".venv" / "lib" / "python3.14" / "site-packages" / "pkg" / "__init__.py", "x = 1\n" * 100)
    write(wt / ".venv" / "bin" / "python-data", os.urandom(20000))
    os.symlink("python-data", wt / ".venv" / "bin" / "python")
    write(wt / "web" / "package.json", '{"name": "web"}\n')
    write(wt / "web" / "node_modules" / "left-pad" / "index.js", "module.exports = 1;\n" * 50)
    write(wt / "src" / "__pycache__" / "main.cpython-314.pyc", os.urandom(3000))
    write(wt / "src" / "__pycache__" / "old.pyo", os.urandom(100))
    write(wt / ".pytest_cache" / "CACHEDIR.TAG", TAG)
    write(wt / ".pytest_cache" / ".gitignore", "*\n")               # pytest ignores its own cache
    write(wt / ".pytest_cache" / "v" / "cache" / "nodeids", "[]\n" * 10)
    write(wt / ".uv-cache" / "CACHEDIR.TAG", rfs.CACHEDIR_SIGNATURE)
    write(wt / ".uv-cache" / "archive-v0" / "wheel.bin", os.urandom(40000))
    return {".venv": "venv", "web/node_modules": "node_modules", "src/__pycache__": "pycache",
            ".pytest_cache": "cache", ".uv-cache": "cache"}


def lookalike_tree(w: World, wt: Path) -> set[str]:
    """Directories that look regenerable and are not; every one is archived."""
    ignore(wt, "build/", "dist/", "target/", ".cache/", "venv/", "fake-venv/", ".mypy_cache/", ".ruff_cache/",
           "node_modules/", "lib/__pycache__/", "keep/.venv/", "linked/.venv", "nested/.venv/")
    write(wt / "build" / "lib" / "data.h5", os.urandom(5000))               # real work found under build/
    write(wt / "dist" / "pkg-1.0.tar.gz", os.urandom(2000))
    write(wt / "target" / "CACHEDIR.TAG", TAG)                             # cargo tags it; still archived
    write(wt / "target" / "issue_repro", os.urandom(3000))
    write(wt / ".cache" / "results.parquet", os.urandom(1000))
    write(wt / "venv" / "notes.txt", "no pyvenv.cfg here\n")               # a name alone
    write(wt / "venv" / "pyvenv.cfg.bak", "home = /x\n")
    write(wt / ".mypy_cache" / "CACHEDIR.TAG", b"Signature: 0000\n")      # the wrong signature
    write(wt / ".mypy_cache" / "3.14" / "x.json", "{}")
    write(wt / "elsewhere-tag", TAG)
    (wt / ".ruff_cache").mkdir()
    os.symlink("../elsewhere-tag", wt / ".ruff_cache" / "CACHEDIR.TAG")   # a marker that is a link
    write(wt / ".ruff_cache" / "0.15" / "cache.bin", os.urandom(500))
    write(wt / "node_modules" / "orphan" / "index.js", "no package.json beside me\n")
    write(wt / "lib" / "__pycache__" / "mod.cpython-314.pyc", os.urandom(300))
    write(wt / "lib" / "__pycache__" / "notes.txt", "not bytecode\n")      # not only bytecode
    write(wt / "keep" / ".venv" / "pyvenv.cfg", "home = /x\n")             # a venv git tracks a file in
    write(wt / "keep" / ".venv" / "tracked.txt", "committed on purpose\n")
    git(wt, "add", "-f", "keep/.venv/tracked.txt")
    write(wt / "plain" / ".venv" / "pyvenv.cfg", "home = /x\n")            # not ignored at all
    write(wt / "plain" / ".venv" / "lib" / "site.py", "pass\n")
    write(wt / "outside-venv" / "pyvenv.cfg", "home = /x\n")
    (wt / "linked").mkdir()
    os.symlink("../outside-venv", wt / "linked" / ".venv")                  # a link is never a tree
    nested = wt / "nested" / ".venv"
    write(nested / "pyvenv.cfg", "home = /x\n")                            # pip -e git+...: a clone in src/
    write(nested / "src" / "pkg" / "setup.py", "pass\n")
    git(nested / "src" / "pkg", "init", "--quiet")
    git(nested / "src" / "pkg", "add", "setup.py")
    git(nested / "src" / "pkg", "commit", "--quiet", "-m", "work that exists only here")
    return {"build", "dist", "target", ".cache", "venv", ".mypy_cache", ".ruff_cache", "node_modules",
            "lib/__pycache__", "keep/.venv", "plain/.venv", "linked/.venv", "nested/.venv"}


def manifest_of(w: World, job_id: str) -> dict:
    return json.loads((w.root / "archive" / job_id / "manifest.json").read_text())


def entries(manifest: dict, label: str = "worktree") -> dict[str, dict]:
    return {e["p"]: e for e in manifest["trees"][label]["entries"]}


def test_regenerable_output_is_deleted_with_the_tree_not_archived(world):
    w = world
    wt = w.job("job-regen")
    kinds = regenerable_tree(wt)
    (wt / "untracked.txt").write_text("work\n")
    before = snapshot(wt)
    regen_bytes = sum(p.stat().st_size for root in kinds for p in (wt / root).rglob("*")
                      if p.is_file() and not p.is_symlink())
    result = run(w)
    assert result["pruned"] == ["job-regen"], result["deferred"]
    assert not wt.exists()
    manifest = manifest_of(w, "job-regen")
    assert {r["p"]: r["kind"] for r in manifest["regenerable"]} == kinds
    listed = entries(manifest)
    for root, kind in kinds.items():
        assert listed[root]["regen"] == kind
    inside = [e for p, e in listed.items() if any(p.startswith(r + "/") for r in kinds)]
    assert inside and all(e.get("regen") is True and not e.get("store") and not e.get("blob") for e in inside)
    # Every entry is listed with its signature: verified deletion covers them too.
    assert all("sig" in e for e in inside)
    totals = manifest["totals"]
    assert totals["regenerable_bytes"] == regen_bytes
    assert totals["freed_bytes"] == totals["omitted_bytes"] + totals["regenerable_bytes"]
    assert 0 <= totals["freed_disk_bytes"]
    assert result["freed_bytes"] == totals["freed_bytes"] and result["archived_bytes"] == totals["archived_bytes"]
    events = [e for e in w.store.list_events("job-regen") if e["kind"] == "retention.reclaimed"]
    data = json.loads(events[0]["data_json"])
    assert data["regenerable_bytes"] == regen_bytes and data["freed_bytes"] == totals["freed_bytes"]
    # Restore gives back everything else, byte for byte, and names what it did not.
    report = rarch.restore(w.root, "job-regen")
    assert {r["path"] for r in report["not_restored"]} == set(kinds)
    after = snapshot(wt)
    expected = {p: v for p, v in before.items() if not any(p == r or p.startswith(r + "/") for r in kinds)}
    assert after == expected


def test_lookalikes_are_archived_byte_for_byte(world):
    w = world
    wt = w.job("job-look")
    archived = lookalike_tree(w, wt)
    nested_commit = git(wt / "nested" / ".venv" / "src" / "pkg", "rev-parse", "HEAD")
    before = snapshot(wt)
    result = run(w)
    assert result["pruned"] == ["job-look"], result["deferred"]
    manifest = manifest_of(w, "job-look")
    assert manifest["regenerable"] == []
    assert manifest["totals"]["regenerable_bytes"] == 0
    listed = entries(manifest)
    assert not any(e.get("regen") for e in listed.values())
    for root in archived:
        assert root in listed, root
    rarch.restore(w.root, "job-look")
    assert snapshot(wt) == before
    assert git(wt / "nested" / ".venv" / "src" / "pkg", "rev-parse", "HEAD") == nested_commit


def test_without_git_nothing_is_regenerable(world):
    """No registration, so no word from git on what is ignored: when in doubt,
    the bytes are archived."""
    w = world
    wt = w.job("job-nogit")
    regenerable_tree(wt)
    before = snapshot(wt)
    import shutil
    shutil.rmtree(w.admin("job-nogit"))
    assert run(w)["pruned"] == ["job-nogit"]
    manifest = manifest_of(w, "job-nogit")
    assert manifest["regenerable"] == [] and manifest["totals"]["regenerable_bytes"] == 0
    rarch.restore(w.root, "job-nogit", to=w.base / "back")
    assert snapshot(w.base / "back" / "worktree") == before


def test_a_file_written_into_regenerable_output_after_the_check_is_kept(world, monkeypatch):
    """Regenerable output is deleted by the same verified deletion: an entry new
    or changed after the final check goes to conflicts, never unlinked."""
    w = world
    wt = w.job("job-late-venv")
    regenerable_tree(wt)
    original = rarch.Retirement.reclaim

    def write_then_reclaim(self):
        write(self.q_worktree / ".venv" / "lib" / "late.py", "written after the final check\n")
        return original(self)

    monkeypatch.setattr(rarch.Retirement, "reclaim", write_then_reclaim)
    result = run(w)
    assert result["pruned"] == ["job-late-venv"]
    kept = w.root / "retention-conflicts" / "job-late-venv" / "worktree" / ".venv" / "lib" / "late.py"
    assert kept.read_text() == "written after the final check\n"


def test_ignored_matches_git_status(world):
    """Differential: `Ignored.clear` agrees with `git status --ignored` on which
    directories hold only ignored, untracked files."""
    w = world
    wt = w.job("job-oracle")
    regenerable_tree(wt)
    lookalike_tree(w, wt)
    admin = w.admin("job-oracle")
    oracle = rgit.Ignored.of(admin, wt)
    out = subprocess.run(["git", "-C", str(wt), "status", "--porcelain=v1", "-z", "--ignored=matching",
                          "--untracked-files=all"], capture_output=True, check=True).stdout
    status = {}
    for record in out.split(b"\0"):
        if record:
            status[os.fsdecode(record[3:])] = record[:2].decode()
    tracked = set(os.fsdecode(p) for p in subprocess.run(["git", "-C", str(wt), "ls-files", "-z"],
                                                        capture_output=True, check=True).stdout.split(b"\0") if p)
    for directory in [".venv", "web/node_modules", "src/__pycache__", ".pytest_cache", ".uv-cache", "build",
                      "keep/.venv", "plain/.venv", "venv", "lib/__pycache__", "src", "web"]:
        files = [p for p in list(status) + list(tracked) if p.startswith(directory + "/")]
        only_ignored = bool(files) and all(status.get(p) == "!!" for p in files)
        assert oracle.clear(directory) == only_ignored, (directory, [(p, status.get(p)) for p in files][:5])


def _tree_state(path: Path) -> dict:
    out = {}
    for directory, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            p = Path(directory) / name
            if name.startswith("state.sqlite3"):
                continue
            st = p.lstat()
            out[str(p)] = (st.st_mode, st.st_size, st.st_mtime_ns, st.st_ino)
    return out


def test_sampled_survey_is_read_only_and_matches_the_retirement(world):
    """`retention survey --sample N` writes nothing, and its per-job figures are
    the ones the retirement then records (differential: the survey's walk and
    the archive's are separate code)."""
    from subfleet.retention_survey import sample
    w = world
    wt = w.job("s-regen")
    regenerable_tree(wt)
    (wt / "untracked.bin").write_bytes(os.urandom(7000))
    w.job("s-pinned")
    w.store.add_notice("s-pinned", "unread", "session-1")
    before = {**_tree_state(w.root), **_tree_state(w.repo)}
    report = sample(w.root, 5)
    assert {**_tree_state(w.root), **_tree_state(w.repo)} == before
    assert report["estimate"] is True and report["sampled"] == 1 and report["candidates_with_worktree"] == 1
    (job,) = report["jobs"]
    assert job["job_id"] == "s-regen"
    assert run(w)["pruned"] == ["s-regen"]
    totals = json.loads((w.root / "archive" / "s-regen" / "manifest.json").read_text())["totals"]
    walk = job["worktree_walk"]
    assert walk["regenerable_bytes"] == totals["regenerable_bytes"]
    assert walk["omitted_bytes"] == totals["omitted_bytes"]
    assert job["freed_bytes"] == totals["freed_bytes"]
    assert report["extrapolated"]["freed_bytes"] == totals["freed_bytes"]
