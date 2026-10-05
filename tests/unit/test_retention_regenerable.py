"""Retention by archive, disk relief (d635 round 2): regenerable output is
deleted with the tree, not archived, and only what its structure says a tool
wrote, in a directory git tracks nothing in and ignores entirely:

- a `.venv`/`venv` with a `pyvenv.cfg` naming `home`: its creator's entries
  (`bin`, `lib`, `pyvenv.cfg`, ...);
- a `node_modules` beside a `package.json`: package directories (each with a
  `package.json`), `@scope` directories of them, links and the package
  managers' own files;
- a `__pycache__` holding only `.pyc`/`.pyo` files: all of it;
- a `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, `.uv-cache` or `.tox`
  carrying a `CACHEDIR.TAG` with the standard's signature: the entries that
  tool writes at its top level (fixed names, versions, cache buckets,
  temporary directories, environments).

Everything else is archived byte for byte: lookalikes (`build/`, `dist/`,
`target/`, `.cache/` whatever they hold; a marker that is missing, wrong or a
link; a directory git tracks something in or does not ignore; one inside a
nested repository), a root that holds a repository, and anything else in a
tool's directory. Live job trees held agents' logs, test reports, scripts, a
git bundle and a bare repository in tagged `.pytest_cache` and `.uv-cache`
directories, and a project folder in a `.venv`
(`test_work_left_inside_a_tool_directory_is_archived`).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet import retention_fs as rfs
from subfleet import retention_git as rgit
from tests.unit.retention_world import Clock, World, git, snapshot, trust_temporary_directories

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


def record_hash(data: bytes) -> str:
    """A RECORD row's hash, as the wheel format writes it."""
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def install(venv: Path, dist: str, files: dict[str, bytes], scripts: dict[str, bytes] | None = None,
            url: str | None = None, python: str = "python3.14", installer: str = "uv",
            local: bool = False) -> str:
    """What uv (or pip) writes into a virtualenv: the distribution's files, a
    dist-info (METADATA, INSTALLER, WHEEL, direct_url.json for an install from
    a URL, and uv_cache.json, which uv writes for a local source) and a RECORD
    listing them all with their sha256, console scripts in bin/ included.
    Returns the dist-info's venv-relative path."""
    sp = venv / "lib" / python / "site-packages"
    info = f"{dist}-1.0.dist-info"
    listed = {**files, f"{info}/METADATA": f"Name: {dist}\n".encode(),
              f"{info}/INSTALLER": f"{installer}\n".encode(), f"{info}/WHEEL": b"Wheel-Version: 1.0\n"}
    if url is not None:
        listed[f"{info}/direct_url.json"] = json.dumps({"url": url, "dir_info": {}}).encode()
    if local:
        listed[f"{info}/uv_cache.json"] = b'{"timestamp": {"secs_since_epoch": 1}, "commit": null}\n'
    rows = []
    for rel, data in listed.items():
        write(sp / rel, data)
        rows.append(f"{rel},{record_hash(data)},{len(data)}")
    for name, data in (scripts or {}).items():
        write(venv / "bin" / name, data)
        rows.append(f"../../../bin/{name},{record_hash(data)},{len(data)}")
    rows.append(f"{info}/RECORD,,")
    write(sp / info / "RECORD", "\n".join(rows) + "\n")
    return f"lib/{python}/site-packages/{info}"


def pyc(source: bytes = b"") -> bytes:
    """Bytecode as Python writes it: a magic number (two bytes, then CR LF), flags, then the code."""
    return b"\xcb\x0d\r\n" + b"\0" * 12 + hashlib.sha256(source).digest()


def regenerable_tree(wt: Path) -> dict[str, tuple[str, set[str]]]:
    """One of each kind, ignored the ways tools do it (by the project's
    .gitignore, or by a `*` .gitignore the tool writes inside), each holding
    what its tool makes again and what it does not. Returns directory ->
    (kind, the paths dropped whose parent was not): what a rule proves
    regenerable (final review of e50716e8, N3)."""
    ignore(wt, ".venv/", "node_modules/", "__pycache__/", ".uv-cache/", ".ruff_cache/", ".mypy_cache/", ".tox/")
    venv = wt / ".venv"
    write(venv / "pyvenv.cfg", "home = /usr/local/bin\nversion_info = 3.14\n")
    os.makedirs(venv / "bin")
    os.symlink("/usr/local/bin/python3.14", venv / "bin" / "python")
    write(venv / "bin" / "activate", "# made for this venv\n")
    write(venv / "lib" / "python3.14" / "site-packages" / "_virtualenv.py", "# the creator's\n")
    source = b"x = 1\n" * 100
    install(venv, "pkg", {"pkg/__init__.py": source, "pkg/data.bin": os.urandom(20000),
                          "pkg/__pycache__/__init__.cpython-314.pyc": pyc(source)},
            scripts={"pkg-tool": b"#!/bin/sh\nexec pkg\n"})
    write(wt / "src" / "helper.py", "def helper(): pass\n")
    write(wt / "src" / "__pycache__" / "main.cpython-314.pyc", pyc(b"main"))
    write(wt / "src" / "__pycache__" / "helper.cpython-314.opt-1.pyc", pyc(b"helper"))
    web = wt / "web"
    write(web / "package.json", '{"name": "web"}\n')
    write(web / "node_modules" / "left-pad" / "package.json", '{"name": "left-pad"}\n')
    write(web / "node_modules" / "left-pad" / "index.js", "module.exports = 1;\n" * 50)
    write(web / "node_modules" / "@types" / "node" / "package.json", "{}\n")
    write(web / "node_modules" / ".bin" / "tool", "#!/bin/sh\n")
    os.symlink("left-pad", web / "node_modules" / "linked-pad")
    write(web / "node_modules" / ".package-lock.json", '{"lockfileVersion": 3}\n')    # written last, by npm
    write(wt / ".pytest_cache" / "CACHEDIR.TAG", TAG)
    write(wt / ".pytest_cache" / ".gitignore", "*\n")               # pytest ignores its own cache
    write(wt / ".pytest_cache" / "README.md", "# pytest cache directory #\n")
    write(wt / ".pytest_cache" / "v" / "cache" / "nodeids", "[]\n" * 10)
    write(wt / ".pytest_cache" / "v" / "cache" / "lastfailed", "{}\n")
    write(wt / ".uv-cache" / "CACHEDIR.TAG", rfs.CACHEDIR_SIGNATURE)
    write(wt / ".uv-cache" / ".lock", "")
    write(wt / ".uv-cache" / "archive-v0" / "wheel.bin", os.urandom(40000))
    write(wt / ".ruff_cache" / "CACHEDIR.TAG", TAG)
    write(wt / ".ruff_cache" / "0.16.9" / "123", os.urandom(300))
    write(wt / ".mypy_cache" / "CACHEDIR.TAG", TAG)
    write(wt / ".mypy_cache" / "missing_stubs", "\n")
    write(wt / ".mypy_cache" / "3.14" / "x.data.json", "{}")
    write(wt / ".mypy_cache" / "3.14" / "x.meta.json", "{}")
    write(wt / ".tox" / "CACHEDIR.TAG", TAG)
    write(wt / ".tox" / "py314" / "pyvenv.cfg", "home = /usr/bin\n")
    install(wt / ".tox" / "py314", "toxpkg", {"toxpkg.py": b"pass\n"})
    sp = ".venv/lib/python3.14/site-packages"
    return {
        ".venv": ("venv", {".venv/bin/pkg-tool", f"{sp}/pkg", f"{sp}/pkg-1.0.dist-info"}),
        "src/__pycache__": ("pycache", {"src/__pycache__"}),
        ".pytest_cache": ("pytest_cache", {".pytest_cache/v"}),
        ".ruff_cache": ("ruff_cache", {".ruff_cache/0.16.9"}),
        ".mypy_cache": ("mypy_cache", {".mypy_cache/3.14"}),
        ".tox": ("tox", {".tox/py314/lib"}),                   # all of it installed, and verified
    }


def lookalike_tree(w: World, wt: Path) -> set[str]:
    """Directories that look regenerable and are not; every one is archived."""
    ignore(wt, "build/", "dist/", "target/", ".cache/", "venv/", "fake-venv/", ".mypy_cache/", ".ruff_cache/",
           "node_modules/", "lib/__pycache__/", "keep/.venv/", "linked/.venv", "vendored/")
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
    (wt / ".ruff_cache").mkdir(exist_ok=True)
    (wt / ".ruff_cache" / "CACHEDIR.TAG").unlink(missing_ok=True)
    os.symlink("../elsewhere-tag", wt / ".ruff_cache" / "CACHEDIR.TAG")   # a marker that is a link
    write(wt / ".ruff_cache" / "0.15" / "cache.bin", os.urandom(500))
    write(wt / "node_modules" / "orphan" / "package.json", "{}\n")        # no package.json beside node_modules
    write(wt / "lib" / "__pycache__" / "mod.cpython-314.pyc", os.urandom(300))
    write(wt / "lib" / "__pycache__" / "notes.txt", "not bytecode\n")      # not only bytecode
    write(wt / "keep" / ".venv" / "pyvenv.cfg", "home = /x\n")             # a venv git tracks a file in
    write(wt / "keep" / ".venv" / "lib" / "tracked.txt", "committed on purpose\n")
    git(wt, "add", "-f", "keep/.venv/lib/tracked.txt")
    write(wt / "plain" / ".venv" / "pyvenv.cfg", "home = /x\n")            # not ignored at all
    write(wt / "plain" / ".venv" / "lib" / "site.py", "pass\n")
    write(wt / "outside-venv" / "pyvenv.cfg", "home = /x\n")
    (wt / "linked").mkdir()
    os.symlink("../outside-venv", wt / "linked" / ".venv")                  # a link is never a tree
    # An ignored nested repository: its own git answers for its files.
    vendored = wt / "vendored" / "dep"
    write(vendored / "setup.py", "pass\n")
    git(vendored, "init", "--quiet")
    git(vendored, "add", "setup.py")
    git(vendored, "commit", "--quiet", "-m", "vendored")
    write(vendored / ".venv" / "pyvenv.cfg", "home = /x\n")
    write(vendored / ".venv" / "lib" / "work.py", "uncommitted, and the nested repository does not ignore it\n")
    return {"build", "dist", "target", ".cache", "venv", ".mypy_cache", ".ruff_cache", "node_modules",
            "lib/__pycache__", "keep/.venv", "plain/.venv", "linked/.venv", "vendored/dep/.venv"}


def manifest_of(w: World, job_id: str) -> dict:
    return json.loads((w.root / "archive" / job_id / "manifest.json").read_text())


def entries(manifest: dict, label: str = "worktree") -> dict[str, dict]:
    return {e["p"]: e for e in manifest["trees"][label]["entries"]}


def under(path: str, roots) -> bool:
    return any(path == r or path.startswith(r + "/") for r in roots)


def file_bytes(wt: Path, roots) -> int:
    total = 0
    for root in roots:
        p = wt / root
        if p.is_symlink():
            continue
        if p.is_file():
            total += p.stat().st_size
            continue
        total += sum(q.stat().st_size for q in p.rglob("*") if q.is_file() and not q.is_symlink())
    return total


def test_regenerable_output_is_deleted_with_the_tree_not_archived(world):
    w = world
    wt = w.job("job-regen")
    expected = regenerable_tree(wt)
    (wt / "untracked.txt").write_text("work\n")
    before = snapshot(wt)
    roots = set().union(*(r for _, r in expected.values()))
    regen_bytes = file_bytes(wt, roots)
    result = run(w)
    assert result["pruned"] == ["job-regen"], result["deferred"]
    assert not wt.exists()
    manifest = manifest_of(w, "job-regen")
    assert {r["p"]: (r["kind"], set(r["roots"])) for r in manifest["regenerable"]} == expected
    listed = entries(manifest)
    dropped = [e for p, e in listed.items() if under(p, roots)]
    assert dropped and all(e.get("regen") is True and not e.get("store") and not e.get("blob") for e in dropped)
    assert all("sig" in e for e in dropped)          # verified deletion covers them too
    # The tool directories themselves (all but bytecode) are archived, not dropped.
    for directory, (kind, _) in expected.items():
        assert bool(listed[directory].get("regen")) == (kind == "pycache"), directory
    totals = manifest["totals"]
    assert totals["regenerable_bytes"] == regen_bytes
    assert totals["freed_bytes"] == totals["omitted_bytes"] + totals["regenerable_bytes"]
    assert 0 <= totals["freed_disk_bytes"] <= totals["freed_bytes"] + (1 << 20)
    assert result["freed_bytes"] == totals["freed_bytes"] and result["archived_bytes"] == totals["archived_bytes"]
    pool = result["pools"]["detached"]
    assert pool["freed_bytes"] == totals["freed_bytes"] and pool["archived_bytes"] == totals["archived_bytes"]
    events = [e for e in w.store.list_events("job-regen") if e["kind"] == "retention.reclaimed"]
    data = json.loads(events[0]["data_json"])
    assert data["regenerable_bytes"] == regen_bytes and data["freed_bytes"] == totals["freed_bytes"]
    summary = json.loads((w.root / "archive" / "job-regen" / "summary.json").read_text())
    assert summary["regenerable_bytes"] == regen_bytes and summary["regenerable_dirs"] == len(expected)
    # Restore gives back everything else, byte for byte, and names what it did not.
    report = rarch.restore(w.root, "job-regen")
    assert {r["path"] for r in report["not_restored"]} == set(expected)
    after = snapshot(wt)
    assert after == {p: v for p, v in before.items() if not under(p, roots)}


def test_work_left_inside_a_tool_directory_is_archived(world):
    """Live job trees (2026-09-29) held agents' work inside tagged caches and a
    virtualenv: logs, test reports, scripts, a git bundle, a bare repository,
    a project folder. Only the tool's own entries are dropped."""
    w = world
    wt = w.job("job-extras")
    regenerable_tree(wt)
    extras = {
        ".pytest_cache/full-suite.log": b"598130 bytes of a test run\n",
        ".pytest_cache/r2/isolated-retry-00.log": b"retry\n",
        ".uv-cache/remaining-tests.py": b"print('a script an agent wrote')\n",
        ".uv-cache/pytest-baseline/junit.xml": b"<testsuite/>\n",
        ".uv-cache/containment-pid-reuse.bundle": b"# v2 git bundle\n" + os.urandom(200),
        ".uv-cache/serial-313-cache/x": b"not uv's name\n",
        ".venv/companion-engine-check/compiled.json": b'{"compiled": true}\n',
        ".ruff_cache/notes.md": b"# notes\n",
        "web/node_modules/scratch.txt": b"hand-placed\n",
        "web/node_modules/not-a-package/index.js": b"no package.json\n",
    }
    for rel, data in extras.items():
        write(wt / rel, data)
    bare = wt / ".pytest_cache" / "retention-commits.git"
    git(wt, "init", "--quiet", "--bare", str(bare))
    before = snapshot(wt)
    assert run(w)["pruned"] == ["job-extras"]
    manifest = manifest_of(w, "job-extras")
    listed = entries(manifest)
    for rel in list(extras) + [".pytest_cache/retention-commits.git/HEAD"]:
        assert listed[rel].get("store") and not listed[rel].get("regen"), rel
    kept = {r["p"]: set(r["kept"]) for r in manifest["regenerable"]}
    assert {"full-suite.log", "r2", "retention-commits.git"} <= kept[".pytest_cache"]
    assert ".uv-cache" not in kept                   # no rule proves anything of uv's cache: all archived
    assert "companion-engine-check" in kept[".venv"]
    rarch.restore(w.root, "job-extras")
    after = snapshot(wt)
    for rel, data in extras.items():
        assert (wt / rel).read_bytes() == data
        assert after[rel] == before[rel]
    assert git(bare, "rev-parse", "--is-bare-repository") == "true"


def _fates(manifest: dict, paths) -> dict[str, str]:
    listed = entries(manifest)
    return {p: ("dropped" if listed[p].get("regen") else "stored" if listed[p].get("store") else "other")
            for p in paths}


def test_work_inside_a_tool_entry_is_archived(world):
    """N3 (final review of e50716e8, `test_probe_work_inside_a_real_tool_entry`):
    inside a tool's own entries only what a rule proves regenerable is
    dropped. An agent's patch to an installed package, a file no RECORD
    lists, data under a venv's `share/`, a notebook in a package directory of
    a `node_modules` no install marker dates, and a `.pyc` that is not
    bytecode are each stored, and restored byte for byte."""
    w = world
    wt = w.job("job-inside")
    ignore(wt, ".venv/", "node_modules/", "__pycache__/")
    venv = wt / ".venv"
    write(venv / "pyvenv.cfg", "home = /usr/bin\n")
    sp = ".venv/lib/python3.12/site-packages"
    install(venv, "dep", {"dep/core.py": b"def core(): return 1\n", "dep/util.py": b"def util(): pass\n"},
            python="python3.12")
    write(wt / sp / "dep" / "core.py", "PATCHED BY THE AGENT\n")                   # listed, hash differs
    write(wt / sp / "dep" / "notes.py", "an agent's file no RECORD lists\n")
    write(venv / "share" / "results.h5", os.urandom(64))
    write(wt / "package.json", "{}\n")
    write(wt / "node_modules" / "my-work" / "package.json", "{}\n")
    write(wt / "node_modules" / "my-work" / "analysis.ipynb", "{}\n")
    write(wt / "src" / "__pycache__" / "results.pyc", os.urandom(64))
    work = [f"{sp}/dep/core.py", f"{sp}/dep/notes.py", ".venv/share/results.h5",
            "node_modules/my-work/analysis.ipynb", "src/__pycache__/results.pyc"]
    before = snapshot(wt)
    assert run(w)["pruned"] == ["job-inside"]
    manifest = manifest_of(w, "job-inside")
    fates = _fates(manifest, work + [f"{sp}/dep/util.py"])
    assert fates == {**dict.fromkeys(work, "stored"), f"{sp}/dep/util.py": "dropped"}, fates
    rarch.restore(w.root, "job-inside")
    after = snapshot(wt)
    for rel in work:
        assert after[rel] == before[rel], rel


def test_installed_files_are_dropped_only_as_their_record_says(world):
    """What pip and uv write is dropped when the RECORD vouches for it: listed
    with a sha256 the bytes match, from a distribution installed from an index
    or a network URL. One installed from a local path vouches for nothing (its
    source may be the only copy). A dist-info goes only whole, so a restore
    leaves no distribution half there."""
    w = world
    wt = w.job("job-record")
    ignore(wt, ".venv/")
    venv = wt / ".venv"
    write(venv / "pyvenv.cfg", "home = /usr/bin\n")
    sp = ".venv/lib/python3.14/site-packages"
    install(venv, "net", {"net/a.py": b"a\n"}, scripts={"net-cli": b"#!/bin/sh\n"},
            url="https://files.example.invalid/net-1.0.whl")
    install(venv, "local", {"local/b.py": b"b\n"}, url=f"file://{w.base}/local-src")
    # A wheel built from a patched source in /tmp and installed with
    # `--find-links`: no direct_url.json, but uv marks the local source
    # (final review of e50716e8, N3 follow-up). pip marks nothing, so its
    # installs vouch for nothing.
    install(venv, "findlinks", {"findlinks/d.py": b"patched\n"}, local=True)
    install(venv, "bypip", {"bypip/e.py": b"e\n"}, installer="pip")
    info = install(venv, "touched", {"touched/c.py": b"c\n"})
    write(venv / info / "METADATA", "Name: touched\nedited by hand\n")
    assert run(w)["pruned"] == ["job-record"]
    manifest = manifest_of(w, "job-record")
    listed = entries(manifest)
    assert listed[f"{sp}/net/a.py"].get("regen") and listed[f"{sp}/net-1.0.dist-info"].get("regen")
    assert listed[".venv/bin/net-cli"].get("regen") and listed[".venv/bin"].get("regen")
    assert listed[f"{sp}/net/a.py"]["sha256"] == hashlib.sha256(b"a\n").hexdigest()
    assert _fates(manifest, [f"{sp}/findlinks/d.py", f"{sp}/bypip/e.py"]) == \
        {f"{sp}/findlinks/d.py": "stored", f"{sp}/bypip/e.py": "stored"}
    manifest_fates = _fates(manifest, [f"{sp}/local/b.py", f"{sp}/local-1.0.dist-info/RECORD",
                                       f"{sp}/touched/c.py", f"{sp}/touched-1.0.dist-info/METADATA",
                                       f"{sp}/touched-1.0.dist-info/RECORD", f"{sp}/touched-1.0.dist-info/WHEEL"])
    assert manifest_fates == {f"{sp}/local/b.py": "stored", f"{sp}/local-1.0.dist-info/RECORD": "stored",
                              f"{sp}/touched/c.py": "dropped", f"{sp}/touched-1.0.dist-info/METADATA": "stored",
                              f"{sp}/touched-1.0.dist-info/RECORD": "stored",
                              f"{sp}/touched-1.0.dist-info/WHEEL": "stored"}, manifest_fates   # whole or not at all


def test_bytecode_needs_its_magic_number_and_its_source(tmp_path):
    src = tmp_path / "pkg"
    write(src / "mod.py", "x = 1\n")
    cache = src / "__pycache__"
    write(cache / "mod.cpython-314.pyc", pyc(b"x"))
    write(cache / "mod.cpython-314-pytest-9.1.1.pyc", pyc(b"x"))       # pytest's rewritten bytecode
    write(cache / "gone.cpython-314.pyc", pyc(b"y"))                   # its source is gone
    write(cache / "data.cpython-314.pyc", b"not bytecode at all")
    write(src / "data.py", "")
    fd = os.open(cache, rfs.O_DIR)
    try:
        verdicts = {name: rfs.bytecode(fd, name, os.stat(name, dir_fd=fd, follow_symlinks=False))
                    for name in os.listdir(fd)}
    finally:
        os.close(fd)
    assert verdicts == {"mod.cpython-314.pyc": True, "mod.cpython-314-pytest-9.1.1.pyc": True,
                        "gone.cpython-314.pyc": False, "data.cpython-314.pyc": False}


def test_nothing_under_node_modules_is_dropped(world):
    """`node_modules`: no package manager keeps a hash of each file it
    unpacks, and a rule by time (nothing written or changed after the install
    marker) drops an agent's edit to an installed package once a later install
    rewrites the marker and leaves the package in place (review of the
    revision-4 build). So nothing there is dropped, marker or not; no live job
    tree holds a `node_modules`."""
    w = world
    wt = w.job("job-node")
    ignore(wt, "node_modules/")
    write(wt / "package.json", "{}\n")
    write(wt / "node_modules" / "foo" / "package.json", "{}\n")
    write(wt / "node_modules" / "foo" / "index.js", "hand-patched before a later install\n")
    time.sleep(0.05)
    write(wt / "node_modules" / "left-pad" / "package.json", "{}\n")        # `npm install left-pad`
    write(wt / "node_modules" / ".package-lock.json", "{}\n")
    assert run(w)["pruned"] == ["job-node"]
    manifest = manifest_of(w, "job-node")
    assert not any(e.get("regen") for p, e in entries(manifest).items() if p.startswith("node_modules"))
    assert _fates(manifest, ["node_modules/foo/index.js"]) == {"node_modules/foo/index.js": "stored"}


def test_a_dist_info_goes_whole_or_not_at_all(world):
    """Review of the revision-4 build: a dist-info holding one file its RECORD
    does not list keeps all of its files, so a restore never yields a
    distribution half there; the distribution's own files still go."""
    w = world
    wt = w.job("job-distinfo")
    ignore(wt, ".venv/")
    write(wt / ".venv" / "pyvenv.cfg", "home = /usr/bin\n")
    info = install(wt / ".venv", "dep", {"dep/a.py": b"a\n"})
    write(wt / ".venv" / info / "extra.txt", "not in the RECORD\n")
    assert run(w)["pruned"] == ["job-distinfo"]
    listed = entries(manifest_of(w, "job-distinfo"))
    sp = ".venv/lib/python3.14/site-packages"
    assert listed[f"{sp}/dep/a.py"].get("regen")
    for name in ("METADATA", "WHEEL", "INSTALLER", "RECORD", "extra.txt"):
        assert listed[f".venv/{info}/{name}"].get("store"), name


def test_an_install_from_a_server_on_this_machine_vouches_for_nothing(world):
    """Review of the revision-4 build: a wheel served from /tmp by a server on
    this machine is no copy elsewhere."""
    w = world
    wt = w.job("job-loopback")
    ignore(wt, ".venv/")
    write(wt / ".venv" / "pyvenv.cfg", "home = /usr/bin\n")
    sp = ".venv/lib/python3.14/site-packages"
    for n, url in enumerate(["http://127.0.0.1:8000/foo-1.0-py3-none-any.whl", "http://localhost/x.whl",
                             "https://mac.local/y.whl"]):
        install(wt / ".venv", f"served{n}", {f"served{n}/m.py": b"patched\n"}, url=url)
    install(wt / ".venv", "pypi", {"pypi/m.py": b"x\n"}, url="https://files.pythonhosted.org/p/pypi-1.0.whl")
    assert run(w)["pruned"] == ["job-loopback"]
    fates = _fates(manifest_of(w, "job-loopback"), [f"{sp}/served{n}/m.py" for n in range(3)] + [f"{sp}/pypi/m.py"])
    assert fates == {**{f"{sp}/served{n}/m.py": "stored" for n in range(3)}, f"{sp}/pypi/m.py": "dropped"}, fates


def test_a_tool_caches_own_files_are_dropped_and_nothing_else(world):
    w = world
    wt = w.job("job-caches")
    regenerable_tree(wt)
    extras = [".pytest_cache/v/cache/results.h5", ".pytest_cache/v/plugin/notes", ".ruff_cache/0.16.9/notes.txt",
              ".mypy_cache/3.14/results.csv", ".uv-cache/archive-v0/wheel.bin"]
    for rel in extras[:-1]:
        write(wt / rel, "an agent's\n")
    assert run(w)["pruned"] == ["job-caches"]
    fates = _fates(manifest_of(w, "job-caches"), extras + [".pytest_cache/v/cache/nodeids",
                                                           ".ruff_cache/0.16.9/123", ".mypy_cache/3.14/x.data.json"])
    assert fates == {**dict.fromkeys(extras, "stored"), ".pytest_cache/v/cache/nodeids": "dropped",
                     ".ruff_cache/0.16.9/123": "dropped", ".mypy_cache/3.14/x.data.json": "dropped"}, fates


def test_lookalikes_are_archived_byte_for_byte(world):
    w = world
    wt = w.job("job-look")
    archived = lookalike_tree(w, wt)
    nested_commit = git(wt / "vendored" / "dep", "rev-parse", "HEAD")
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
    assert git(wt / "vendored" / "dep", "rev-parse", "HEAD") == nested_commit


def test_a_repository_inside_regenerable_output_is_archived(world, monkeypatch):
    """A `.git` under a root (a `pip install -e git+...` clone in site-packages)
    sends the walk round again with that root archived; the finding is kept
    with the cache, so the next attempt does not walk into it again. The
    tool's other entries are still dropped."""
    w = world
    wt = w.job("job-repo-in-venv")
    regenerable_tree(wt)
    clone = wt / ".venv" / "lib" / "python3.14" / "site-packages" / "editable"
    write(clone / "mod.py", "work that exists only here\n")
    git(clone, "init", "--quiet")
    git(clone, "add", "mod.py")
    git(clone, "commit", "--quiet", "-m", "only here")
    commit = git(clone, "rev-parse", "HEAD")
    write(clone / "mod.py", "and an uncommitted change\n")
    before = snapshot(wt)
    walks = []
    original = rfs.RegenerableWalk.__init__

    def counting(self, root_fd, clear, denied=frozenset()):
        walks.append(set(denied))
        original(self, root_fd, clear, denied)

    monkeypatch.setattr(rfs.RegenerableWalk, "__init__", counting)
    checks = []

    def busy_at_second_check(watches, **_):
        checks.append(1)
        return {"job-repo-in-venv": ["pid 1 (python): open for writing"]} if len(checks) == 2 else {}

    clock = Clock()
    state = retention.RetentionState()
    first = run(w, holders=busy_at_second_check, clock=clock, state=state)
    assert first["pruned"] == [] and "busy" in first["deferred"]["job-repo-in-venv"]
    assert walks == [set(), {".venv/lib"}]              # the walk went round once more
    progress = (w.root / "retention" / "job-repo-in-venv" / "archive" / "progress.jsonl").read_text()
    assert '"k":"nr:worktree:.venv/lib"' in progress
    walks.clear()
    clock.advance(rarch.DEFER_BUSY_S + 1)
    second = run(w, clock=clock, state=state)
    assert second["pruned"] == ["job-repo-in-venv"], second
    assert walks == [{".venv/lib"}]                     # remembered: no walk into it again
    manifest = manifest_of(w, "job-repo-in-venv")
    venv = next(r for r in manifest["regenerable"] if r["p"] == ".venv")
    assert set(venv["roots"]) == {".venv/bin/pkg-tool"} and "lib" in venv["kept"]      # lib: all archived
    # Only bytecode beside its source goes from the refused lib/ (a `__pycache__` proves itself).
    assert not any(e.get("regen") for p, e in entries(manifest).items()
                   if p.startswith(".venv/lib") and "/__pycache__" not in p)
    rarch.restore(w.root, "job-repo-in-venv")
    after = snapshot(wt)
    assert {p: v for p, v in after.items() if p.startswith(".venv/lib")} == \
        {p: v for p, v in before.items() if p.startswith(".venv/lib") and "/__pycache__" not in p}
    assert git(clone, "rev-parse", "HEAD") == commit
    assert (clone / "mod.py").read_text() == "and an uncommitted change\n"


def test_without_git_nothing_is_regenerable(world):
    """No registration, so no word from git on what is ignored: when in doubt,
    the bytes are archived."""
    w = world
    wt = w.job("job-nogit")
    regenerable_tree(wt)
    before = snapshot(wt)
    shutil.rmtree(w.admin("job-nogit"))
    assert run(w)["pruned"] == ["job-nogit"]
    manifest = manifest_of(w, "job-nogit")
    assert manifest["regenerable"] == [] and manifest["totals"]["regenerable_bytes"] == 0
    rarch.restore(w.root, "job-nogit", to=w.base / "back")
    assert snapshot(w.base / "back" / "worktree") == before


def test_ignore_rules_that_change_during_the_archive_defer_the_job(world, monkeypatch):
    """The ignore rules are read again after the walk: a directory no longer
    ignored (an index or .gitignore changed before the walk recorded it)
    defers the job, which retires on the next attempt."""
    w = world
    wt = w.job("job-flip")
    regenerable_tree(wt)
    calls = []
    real = rgit.Ignored.of

    def flipping(admin, tree, **kwargs):
        calls.append(tree)
        found = real(admin, tree, **kwargs)
        if len(calls) == 2:
            return rgit.Ignored(found.paths + [".venv/lib/now-tracked.py"])
        return found

    monkeypatch.setattr(rgit.Ignored, "of", staticmethod(flipping))
    clock = Clock()
    state = retention.RetentionState()
    result = run(w, clock=clock, state=state)
    assert result["pruned"] == [] and "changed" in result["deferred"]["job-flip"], result
    assert (wt / ".venv" / "lib").is_dir()
    clock.advance(2 * 3600)
    assert run(w, clock=clock, state=state)["pruned"] == ["job-flip"]


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


def test_a_replaced_tool_entry_is_archived(world, monkeypatch):
    """The layout is read when the directory is found; an entry replaced
    before the walk reaches it (another inode) is archived, not dropped."""
    w = world
    wt = w.job("job-swap")
    regenerable_tree(wt)
    real = rfs.regenerable

    def then_swap(parent_fd, name, st):
        found = real(parent_fd, name, st)
        if name == ".pytest_cache" and found is not None:
            os.rename(wt.parent.parent / "retention" / "job-swap" / "worktree" / ".pytest_cache" / "README.md",
                      wt.parent.parent / "retention" / "job-swap" / "worktree" / ".pytest_cache" / "old.md")
            write(wt.parent.parent / "retention" / "job-swap" / "worktree" / ".pytest_cache" / "README.md",
                  "an agent's report, written in its place\n")
        return found

    monkeypatch.setattr(rfs, "regenerable", then_swap)
    assert run(w)["pruned"] == ["job-swap"]
    listed = entries(manifest_of(w, "job-swap"))
    assert listed[".pytest_cache/README.md"].get("store") and not listed[".pytest_cache/README.md"].get("regen")
    rarch.restore(w.root, "job-swap")
    assert (wt / ".pytest_cache" / "README.md").read_text() == "an agent's report, written in its place\n"


LIVE_LAYOUTS = {
    # Top-level names seen in live job trees on 2026-09-29 (tool entries first).
    ".pytest_cache": (TAG, {"CACHEDIR.TAG": "f", "README.md": "f", ".gitignore": "f", "v": "d"},
                      {"retention-final.log": "f", "retention-commits.git": "d", "phone-root": "d"}),
    ".ruff_cache": (TAG, {"CACHEDIR.TAG": "f", ".gitignore": "f", "0.16.9": "d", "0.14.10": "d"},
                    {"0.16": "d", "notes": "f"}),
    ".mypy_cache": (TAG, {"CACHEDIR.TAG": "f", ".gitignore": "f", "missing_stubs": "f", "3.14": "d"},
                    {"3.14.1": "d"}),
    ".uv-cache": (TAG, {"CACHEDIR.TAG": "f", ".gitignore": "f", ".lock": "f", "archive-v0": "d", "sdists-v9": "d",
                        "simple-v25": "d", "interpreter-v4": "d", ".tmp0e4PjG": "d"},
                  {"venv313": "d", "serial-313-cache": "d", "baseline-full.xml": "f", "remaining-tests.py": "f",
                   "containment-commits.git": "d", "__pycache__": "d", ".tmp": "d"}),
}


@pytest.mark.parametrize("name", sorted(LIVE_LAYOUTS))
def test_tool_layouts_split_the_tools_entries_from_everything_else(tmp_path, name):
    tag, own, others = LIVE_LAYOUTS[name]
    d = tmp_path / name
    d.mkdir()
    for child, t in {**own, **others}.items():
        if child == "CACHEDIR.TAG":
            write(d / child, tag)
        elif t == "f":
            write(d / child, "x")
        else:
            (d / child).mkdir()
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        found = rfs.regenerable(fd, name, os.lstat(d))
    finally:
        os.close(fd)
    assert found is not None
    assert set(found.children) == set(own) and set(found.extra) == set(others)


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
    the archive's are separate code over one rule)."""
    from subfleet.retention_survey import sample
    w = world
    wt = w.job("s-regen")
    regenerable_tree(wt)
    write(wt / ".pytest_cache" / "notes-by-an-agent.log", "kept\n")
    (wt / "untracked.bin").write_bytes(os.urandom(7000))
    w.job("s-pinned")
    w.store.add_notice("s-pinned", "unread", "session-1")
    before = {**_tree_state(w.root), **_tree_state(w.repo)}
    report = sample(w.root, 5)
    assert {**_tree_state(w.root), **_tree_state(w.repo)} == before
    assert report["estimate"] is True and report["sampled"] == 1 and report["candidates_with_worktree"] == 1
    assert report["kept_inside_regenerable"]["pytest_cache: notes-by-an-agent.log"] == 1
    (job,) = report["jobs"]
    assert job["job_id"] == "s-regen"
    assert run(w)["pruned"] == ["s-regen"]
    totals = json.loads((w.root / "archive" / "s-regen" / "manifest.json").read_text())["totals"]
    walk = job["worktree_walk"]
    assert walk["regenerable_bytes"] == totals["regenerable_bytes"]
    assert walk["omitted_bytes"] == totals["omitted_bytes"]
    assert job["freed_bytes"] == totals["freed_bytes"]
    assert report["extrapolated"]["freed_bytes"] == totals["freed_bytes"]


def test_the_command_line_samples_and_lists_what_was_freed(world, monkeypatch, capsys):
    """`retention survey --sample N` (read-only) and `retention archives` name
    the bytes freed apart from the bytes kept in the archive."""
    from subfleet import cli
    w = world
    wt = w.job("cli-regen")
    expected = regenerable_tree(wt)
    roots = set().union(*(r for _, r in expected.values()))
    monkeypatch.setenv("SUBFLEET_HOME", str(w.root))
    assert cli.main(["retention", "survey", "--sample", "3", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["estimate"] is True and report["read_only"] is True and report["sampled"] == 1
    assert report["sample_totals"]["regenerable_bytes"] == file_bytes(wt, roots)
    assert cli.main(["retention", "survey", "--sample", "3"]) == 0
    assert "jobs" not in json.loads(capsys.readouterr().out)
    assert run(w)["pruned"] == ["cli-regen"]
    assert cli.main(["retention", "archives"]) == 0
    text = capsys.readouterr().out
    totals = manifest_of(w, "cli-regen")["totals"]
    assert f"{totals['freed_bytes']:,} bytes deleted without a copy" in text
    assert f"{totals['archived_bytes']:,} bytes kept in them" in text


# --- names as git prints them and as the disk spells them (final review of e50716e8, N2) ---------

def test_ignored_compares_names_folded_as_apfs_matches_them():
    """`git ls-files` prints the index's case and precomposed (NFC) names; the
    walk sees the disk's. APFS matches names regardless of case and
    normalization, so `clear` folds both sides: folding only merges names, so
    it can answer "not clear" more often, never less."""
    import unicodedata
    nfd = unicodedata.normalize("NFD", "projét")
    nfc = unicodedata.normalize("NFC", "projét")
    listed = rgit.Ignored([f"{nfc}/.venv/lib/work.py", "Pkg/node_modules/left/index.js", "Top.txt"])
    assert not listed.clear(f"{nfd}/.venv")                  # git lists work inside it
    assert not listed.clear(f"{nfd}/.venv/lib")
    assert not listed.clear("pkg/node_modules")               # tracked under another case
    assert not listed.clear("PKG/NODE_MODULES/left")
    assert not listed.clear("top.txt")
    assert listed.clear(f"{nfd}/other") and listed.clear("pkg/elsewhere")


def test_an_nfd_named_parent_does_not_make_a_visible_venv_ignored(world):
    import unicodedata
    w = world
    wt = w.job("job-nfd")
    parent = unicodedata.normalize("NFD", "projét")
    write(wt / parent / ".venv" / "pyvenv.cfg", "home = /usr/bin\n")
    write(wt / parent / ".venv" / "lib" / "work.py", "untracked and not ignored\n")
    assert "work.py" in git(wt, "status", "--porcelain", "--untracked-files=all")
    assert run(w)["pruned"] == ["job-nfd"]
    listed = entries(manifest_of(w, "job-nfd"))
    assert listed[f"{parent}/.venv/lib/work.py"].get("store"), "untracked, visible work was dropped"


def test_a_directory_whose_case_changed_on_disk_keeps_its_tracked_edits(world):
    w = world
    wt = w.job("job-case")
    write(wt / "Pkg" / "package.json", "{}\n")
    write(wt / "Pkg" / "node_modules" / "left" / "package.json", "{}\n")
    write(wt / "Pkg" / "node_modules" / "left" / "index.js", "committed\n")
    git(wt, "add", "-f", "Pkg")
    git(wt, "commit", "-q", "-m", "vendored node_modules")
    os.rename(wt / "Pkg", wt / "tmpname")
    os.rename(wt / "tmpname", wt / "pkg")                      # index says Pkg/, disk says pkg/
    write(wt / "pkg" / "node_modules" / "left" / "index.js", "uncommitted edit\n")
    assert run(w)["pruned"] == ["job-case"]
    listed = entries(manifest_of(w, "job-case"))
    assert listed["pkg/node_modules/left/index.js"].get("store"), "a tracked file's uncommitted edit was dropped"


def test_the_sampled_estimate_nets_what_the_archive_adds(world):
    """N1 (final review of e50716e8): `survey --sample` estimates what each
    archive adds (bundle, manifest, rows, summary) and reports the net on
    disk; its bundle figure is git's measure of the history the bundle then
    carries, and its manifest figure is close to the manifest then written."""
    from subfleet.retention_survey import sample
    w = world
    git(w.repo, "remote", "remove", "origin")
    (w.repo / "history.bin").write_bytes(os.urandom(80_000))
    git(w.repo, "add", ".")
    git(w.repo, "commit", "--quiet", "-m", "history")
    wt = w.job("s-net")
    regenerable_tree(wt)
    report = sample(w.root, 3)
    (job,) = report["jobs"]
    assert job["bundle_estimate"] > 80_000
    assert job["added_bytes"] == (job["bundle_estimate"] + job["manifest_bytes"] + job["rows_bytes"]
                                  + 1024)
    assert job["net_disk_bytes"] == job["freed_disk_bytes"] - job["added_bytes"]
    totals = report["sample_totals"]
    assert totals["net_disk_bytes"] == totals["freed_disk_bytes"] - totals["added_bytes"]
    assert report["extrapolated"]["added_bytes"] == totals["added_bytes"]
    assert run(w)["pruned"] == ["s-net"]
    archive = w.root / "archive" / "s-net"
    bundle = (archive / "commits.bundle").stat().st_size
    assert 0.5 * job["bundle_estimate"] <= bundle <= 1.5 * job["bundle_estimate"], (bundle, job["bundle_estimate"])
    manifest = (archive / "manifest.json").stat().st_size
    assert abs(job["manifest_bytes"] - manifest) <= 0.1 * manifest + 4096, (job["manifest_bytes"], manifest)
    assert job["rows_bytes"] == (archive / "rows.json").stat().st_size
