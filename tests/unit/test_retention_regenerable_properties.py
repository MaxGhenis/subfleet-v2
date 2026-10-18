"""Properties of disk relief (d635 round 2, and the final review of e50716e8,
N3): what retention deletes without a copy is exactly the regenerable output,
and everything else comes back.

Hypothesis builds a worktree with directories named like regenerable output
(`.venv`, `venv`, `node_modules`, `__pycache__`, `.pytest_cache`, `.ruff_cache`,
`.mypy_cache`, `.tox`, `.uv-cache`) and like build output (`build`, `target`,
`.cache`). Each has a marker that is right, missing, wrong or a link; is ignored
by the project, by a `*` file inside, or not at all; may hold the tool's own
entries (an installed distribution with its RECORD, bytecode, packages under an
install marker, a cache's own files), a tracked file, something the tool did
not write, work placed inside the tool's own entries (a patched or unlisted
file, a local install, bytecode without its source, an edit after the
install), and a repository at its top level or inside one of the tool's
entries. Retention retires the job; then, checked against an oracle written
here independently of `retention_fs` and `retention_git.Ignored` (structure
read with pathlib, RECORDs with `csv`, ignore status from `git status
--ignored`):

- **R1, only regenerable output is dropped.** Every entry the archive drops,
  the oracle drops.
- **R2, all of it is dropped.** Every entry the oracle drops, the archive drops
  (disk relief).
- **R3, everything else comes back.** Restoring gives back every entry the
  oracle keeps: path, type, permission bits, bytes, link target, mtime.
- **R4, accounting.** `regenerable_bytes` is the sum of the dropped regular
  files' sizes, and `freed_bytes` is it plus `omitted_bytes`.

`RETENTION_PROP_EXAMPLES` raises the example count.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from hypothesis import HealthCheck, Phase, example, given, settings
from hypothesis import strategies as st

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet import retention_git as rgit
from tests.unit.retention_world import World, git, snapshot

EXAMPLES = int(os.environ.get("RETENTION_PROP_EXAMPLES", "8"))
PHASES = ((Phase.explicit, Phase.reuse, Phase.generate) if os.environ.get("RETENTION_PROP_NO_SHRINK")
          else tuple(Phase))
SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"
DIRS = [".venv", "venv", "node_modules", "web/node_modules", "__pycache__", "src/__pycache__", ".pytest_cache",
        ".ruff_cache", ".mypy_cache", ".tox", ".uv-cache", "build", "target", ".cache"]
TAGGED = (".pytest_cache", ".ruff_cache", ".mypy_cache", ".tox", ".uv-cache", "target", ".cache", "build")
MAGIC = b"\xcb\x0d\r\n"

# Weighted toward directories that are regenerable, so the cases that matter
# (a repository inside a tool's entry, work inside a tool's entries) come up.
spec = st.fixed_dictionaries({
    "marker": st.sampled_from(["right", "right", "right", "missing", "wrong", "link"]),
    "ignored": st.sampled_from(["project", "project", "inside", "inside", "no"]),
    "own": st.sampled_from([True, True, True, False]),
    "tracked": st.sampled_from([False, False, False, True]),
    "repository": st.sampled_from(["none", "none", "none", "top", "inside", "around"]),
    "extra": st.sampled_from(["none", "bytecode", "text", "subdir"]),
    "package_json": st.sampled_from([True, True, False]),
    # work inside the tool's own entries (N3)
    "inside": st.sampled_from(["clean", "clean", "patched", "unlisted", "local", "findlinks", "pip", "orphan",
                               "late"]),
    "data": st.binary(min_size=0, max_size=2000),
})


def case(**changes) -> dict:
    return {"marker": "right", "ignored": "project", "own": True, "tracked": False, "repository": "none",
            "extra": "text", "package_json": True, "inside": "clean", "data": b"x" * 100, **changes}


#: Every kind once, regenerable, with work beside the tool's entries and a
#: repository inside one of them; work inside each kind's entries; and each
#: kind's refusals.
EXPLICIT = [
    {".venv": case(repository="inside"), ".uv-cache": case(ignored="inside"), "src/__pycache__": case(extra="none")},
    {"web/node_modules": case(repository="top", extra="subdir"), ".tox": case(repository="inside", extra="subdir"),
     ".ruff_cache": case(), ".mypy_cache": case(ignored="inside")},
    {"web/node_modules": case(repository="around")},
    {".pytest_cache": case(repository="inside"), "venv": case(), "build": case(), "target": case(), ".cache": case()},
    {".venv": case(inside="patched"), "node_modules": case(inside="late"), "src/__pycache__": case(inside="orphan"),
     ".pytest_cache": case(inside="unlisted"), ".tox": case(inside="local")},
    {"venv": case(inside="unlisted"), "web/node_modules": case(inside="local"), ".ruff_cache": case(inside="late"),
     ".mypy_cache": case(inside="patched"), "__pycache__": case(inside="patched")},
    {".venv": case(inside="findlinks"), "venv": case(inside="pip")},
    {".venv": case(tracked=True), ".uv-cache": case(marker="link"), "node_modules": case(package_json=False),
     "__pycache__": case(extra="text"), ".tox": case(ignored="no")},
]


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _record_row(rel: str, data: bytes) -> str:
    return f"{rel},sha256={base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()},{len(data)}"


def _install(venv: Path, data: bytes, inside: str) -> None:
    """An installed distribution, as pip or uv writes it, with bytecode, then
    the work an agent might leave inside it."""
    sp = venv / "lib" / "python3.14" / "site-packages"
    info = "pkg-1.0.dist-info"
    files = {"pkg/__init__.py": b"x = 1\n", "pkg/core.py": data, f"{info}/METADATA": b"Name: pkg\n",
             f"{info}/WHEEL": b"Wheel-Version: 1.0\n", f"{info}/INSTALLER": b"pip\n" if inside == "pip" else b"uv\n"}
    if inside == "local":
        files[f"{info}/direct_url.json"] = json.dumps({"url": "file:///somewhere/pkg", "dir_info": {}}).encode()
    elif inside == "findlinks":                      # uv's mark of a local source, without direct_url.json
        files[f"{info}/uv_cache.json"] = b'{"timestamp": 1}\n'
    rows = []
    for rel, content in files.items():
        _write(sp / rel, content)
        rows.append(_record_row(rel, content))
    _write(venv / "bin" / "pkg-cli", b"#!/bin/sh\n")
    rows.append(_record_row("../../../bin/pkg-cli", b"#!/bin/sh\n"))
    rows.append(f"{info}/RECORD,,")
    _write(sp / info / "RECORD", ("\n".join(rows) + "\n").encode())
    _write(sp / "pkg" / "__pycache__" / "core.cpython-314.pyc", MAGIC + b"\0" * 12 + data[:40])
    if inside == "patched":
        _write(sp / "pkg" / "core.py", b"patched: " + data)
    elif inside == "unlisted":
        _write(sp / "pkg" / "notes.txt", data)
    elif inside == "orphan":
        _write(sp / "pkg" / "__pycache__" / "gone.cpython-314.pyc", MAGIC + data)


def own_entries(root: Path, name: str, s: dict) -> str | None:
    """What the tool itself would write, and the work left inside it (`inside`).
    Returns one of its directories, where a repository may be put."""
    data, inside = s["data"], s["inside"]
    if name in (".venv", "venv"):
        os.makedirs(root / "bin", exist_ok=True)
        os.symlink("/usr/bin/python3", root / "bin" / "python")
        _install(root, data, inside)
        return "lib"
    if name == "node_modules":
        _write(root / "left-pad" / "package.json", b"{}\n")
        _write(root / "left-pad" / "index.js", data)
        _write(root / "@types" / "node" / "package.json", b"{}\n")
        _write(root / ".bin" / "tool", b"#!/bin/sh\n")
        os.symlink("left-pad", root / "linked")
        if inside != "local":                                  # "local": no install marker at all
            _write(root / ".package-lock.json", b"{}\n")
        if inside in ("late", "patched"):
            time.sleep(0.02)
            _write(root / "left-pad" / "index.js", b"edited after the install\n")
        elif inside == "unlisted":
            time.sleep(0.02)
            _write(root / "left-pad" / "notes.ipynb", data)
        return "left-pad"
    if name == "__pycache__":
        _write(root.parent / "mod.py", b"x = 1\n")
        _write(root / "mod.cpython-314.pyc", MAGIC + data)
        if inside == "orphan":
            _write(root / "gone.cpython-314.pyc", MAGIC + data)
        elif inside == "patched":
            _write(root / "junk.cpython-314.pyc", b"no magic" + data)
        return None
    if name == ".pytest_cache":
        _write(root / "README.md", b"# pytest\n")
        _write(root / "v" / "cache" / "nodeids", data)
        _write(root / "v" / "cache" / "lastfailed", b"{}")
        if inside in ("unlisted", "patched", "late"):
            _write(root / "v" / "cache" / "results.h5", data)
        return "v"
    if name == ".tox":
        _write(root / "py314" / "pyvenv.cfg", b"home = /usr/bin\n")
        _install(root / "py314", data, inside)
        return "py314"
    if name == ".uv-cache":
        _write(root / ".lock", b"")
        _write(root / "archive-v0" / "wheel", data)
        _write(root / ".tmpAbC123" / "part", b"x")
        return "archive-v0"
    if name == ".ruff_cache":
        _write(root / "0.16.9" / "12345", data)
        if inside in ("unlisted", "patched", "late"):
            _write(root / "0.16.9" / "notes.md", data)
        return "0.16.9"
    if name == ".mypy_cache":
        _write(root / "missing_stubs", b"\n")
        _write(root / "3.14" / "x.data.json", data)
        _write(root / "3.14" / "x.meta.json", b"{}")
        if inside in ("unlisted", "patched", "late"):
            _write(root / "3.14" / "results.csv", data)
        return "3.14"
    _write(root / "out" / "data.bin", data)                   # build, target, .cache
    return "out"


def build(wt: Path, dirs: dict[str, dict]) -> None:
    patterns = []
    for rel, s in sorted(dirs.items()):
        d = wt / rel
        d.mkdir(parents=True, exist_ok=True)
        name = d.name
        if name in (".venv", "venv"):
            marker, good, bad = "pyvenv.cfg", b"home = /usr/bin\n", b"version = 3\n"
        elif name in TAGGED:
            marker, good, bad = "CACHEDIR.TAG", SIGNATURE + b"\n", b"Signature: 0\n"
        else:
            marker, good, bad = None, b"", b""
        if marker is not None:
            if s["marker"] == "right":
                _write(d / marker, good)
            elif s["marker"] == "wrong":
                _write(d / marker, bad)
            elif s["marker"] == "link":
                _write(wt / f"marker-for-{name}", good)
                os.symlink(os.path.relpath(wt / f"marker-for-{name}", d), d / marker)
        if name == "node_modules" and s["package_json"]:
            _write(d.parent / "package.json", b"{}\n")
        inner = own_entries(d, name, s) if s["own"] else None
        extra = {"bytecode": "x.pyo", "text": "notes.txt", "subdir": "sub/inner.bin"}.get(s["extra"])
        if extra:
            _write(d / extra, s["data"])
        _write(d / ("payload.pyc" if name == "__pycache__" else "payload.bin"), s["data"])
        if s["ignored"] == "project":
            patterns.append("/" + rel + "/")
        elif s["ignored"] == "inside":
            _write(d / ".gitignore", b"*\n")
        if s["tracked"]:
            _write(d / "tracked.keep", b"kept on purpose\n")
            git(wt, "add", "-f", f"{rel}/tracked.keep")
        if s["repository"] == "around":
            if rel == "web/node_modules":                  # an ignored nested repository around it
                patterns.append("/web/")
                git(d.parent, "init", "--quiet")
        elif s["repository"] != "none":
            repo = (d / inner / "dep") if s["repository"] == "inside" and inner else d / "dep"
            _write(repo / "f.txt", b"only here\n")
            git(repo, "init", "--quiet")
            git(repo, "add", "f.txt")
            git(repo, "commit", "--quiet", "-m", "only here")
    with open(wt / ".gitignore", "a") as f:
        f.write("".join(p + "\n" for p in patterns))


# --- the oracle, independent of retention's code --------------------------------------------

def _regular(p: Path) -> bool:
    return p.is_file() and not p.is_symlink()


def _real_dir(p: Path) -> bool:
    return p.is_dir() and not p.is_symlink()


def _venv(d: Path) -> bool:
    cfg = d / "pyvenv.cfg"
    return _regular(cfg) and any(line.split("=")[0].strip() == "home" and "=" in line
                                 for line in cfg.read_text(errors="replace").splitlines())


def _tagged(d: Path) -> bool:
    tag = d / "CACHEDIR.TAG"
    return _regular(tag) and tag.read_bytes().startswith(SIGNATURE)


def _package(p: Path) -> bool:
    return _real_dir(p) and _regular(p / "package.json")


def identify(d: Path) -> str | None:
    name = d.name
    if name in (".venv", "venv"):
        return "venv" if _venv(d) else None
    if name == "node_modules":
        return "node" if _regular(d.parent / "package.json") else None
    if name == "__pycache__":
        return "pycache"
    if name in (".pytest_cache", ".ruff_cache", ".mypy_cache", ".tox", ".uv-cache"):
        return name if _tagged(d) else None
    return None


def tools_own(kind: str, p: Path) -> bool:
    """Whether a top-level entry of a tool's directory is one the tool writes."""
    n = p.name
    files = {"venv": {"pyvenv.cfg", "CACHEDIR.TAG", ".gitignore", ".lock"},
             "node": {".package-lock.json", ".modules.yaml", ".yarn-integrity", ".yarn-state.yml"},
             ".pytest_cache": {"CACHEDIR.TAG", "README.md", ".gitignore"},
             ".ruff_cache": {"CACHEDIR.TAG", ".gitignore"},
             ".mypy_cache": {"CACHEDIR.TAG", ".gitignore", "missing_stubs"},
             ".tox": {"CACHEDIR.TAG", ".gitignore"},
             ".uv-cache": {"CACHEDIR.TAG", ".gitignore", ".lock"}}[kind]
    dirs = {"venv": {"bin", "lib", "include", "share", "etc", "man", "Lib", "Scripts", "Include"},
            "node": {".bin", ".pnpm"}, ".pytest_cache": {"v"}}.get(kind, set())
    if n in files:
        return _regular(p)
    if n in dirs:
        return _real_dir(p)
    if kind == "venv" and n == "lib64":
        return _real_dir(p) or p.is_symlink()
    if kind == "node":
        if p.is_symlink():
            return not n.startswith(".")
        if n.startswith("@") and _real_dir(p):
            inside = list(p.iterdir())
            return bool(inside) and all(q.is_symlink() or _package(q) for q in inside)
        return _package(p)
    if kind == ".ruff_cache":
        return _real_dir(p) and re.fullmatch(r"\d+\.\d+\.\d+", n) is not None
    if kind == ".mypy_cache":
        return _real_dir(p) and re.fullmatch(r"\d+\.\d+", n) is not None
    if kind == ".tox":
        return _real_dir(p) and _venv(p)
    if kind == ".uv-cache":
        if re.fullmatch(r"\.tmp[0-9A-Za-z]{6}", n):
            return _real_dir(p) or _regular(p)
        return _real_dir(p) and re.fullmatch(r"[a-z]+(-[a-z]+)*-v\d+", n) is not None
    return False


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _vouched(venv: Path) -> dict[str, tuple[str, str]]:
    """venv-relative path -> (sha256 hex or "RECORD", size) from the RECORDs of
    distributions installed from an index or a network URL."""
    out: dict[str, tuple[str, str]] = {}
    for record in venv.glob("lib/*/site-packages/*.dist-info/RECORD"):
        if not _regular(record) or any(q.is_symlink() for q in record.parents if venv in q.parents):
            continue
        direct = record.parent / "direct_url.json"
        if direct.exists() and not json.loads(direct.read_text()).get("url", "").startswith(("https://", "http://")):
            continue
        installer = record.parent / "INSTALLER"
        if not _regular(installer) or installer.read_text().strip() != "uv" or \
                os.path.lexists(record.parent / "uv_cache.json"):
            continue                                # only uv's installs from an index vouch
        base = record.parent.parent.relative_to(venv)
        out[str(record.relative_to(venv))] = ("RECORD", "")
        for row in csv.reader(record.read_text().splitlines()):
            if len(row) >= 2 and row[1].startswith("sha256="):
                digest = base64.urlsafe_b64decode(row[1][7:] + "=").hex()
                out[os.path.normpath(str(base / row[0]))] = (digest, row[2] if len(row) > 2 else "")
    return out


def _venv_drops(venv: Path, f: Path, vouched: dict) -> bool:
    rel = str(f.relative_to(venv))
    hit = vouched.get(rel)
    if hit and hit[0] == "RECORD":
        others = [q for q in f.parent.rglob("*") if q != f and not _real_dir(q)]
        return all(_regular(q) and str(q.relative_to(venv)) in vouched
                   and vouched[str(q.relative_to(venv))][0] == _sha256(q) for q in others)
    dist = next((q for q in f.parents if q.name.endswith(".dist-info")), None)
    if dist is not None and str((dist / "RECORD").relative_to(venv)) in vouched:
        others = [q for q in dist.rglob("*") if q.name != "RECORD" and not _real_dir(q)]
        if not all(_regular(q) and str(q.relative_to(venv)) in vouched
                   and vouched[str(q.relative_to(venv))][0] == _sha256(q) for q in others):
            return False                               # a dist-info goes whole or not at all
    if hit:
        return hit[0] == _sha256(f) and hit[1] in ("", str(f.stat().st_size))
    return f.parent.name == "__pycache__" and _bytecode(f)


def _bytecode(f: Path) -> bool:
    return (f.suffix in (".pyc", ".pyo") and f.read_bytes()[2:4] == b"\r\n" and len(f.read_bytes()) >= 4
            and _regular(f.parent.parent / (f.name.split(".")[0] + ".py")))


def oracle(wt: Path) -> set[str]:
    """Every entry dropped (files a rule proves, and directories under a tool's
    entry whose every entry is dropped), found without retention's code."""
    out = subprocess.run(["git", "-C", str(wt), "status", "--porcelain=v1", "-z", "--ignored=matching",
                          "--untracked-files=all"], capture_output=True, check=True).stdout
    status = {os.fsdecode(r[3:]): r[:2].decode() for r in out.split(b"\0") if r}
    tracked = {os.fsdecode(p) for p in subprocess.run(["git", "-C", str(wt), "ls-files", "-z"], capture_output=True,
                                                      check=True).stdout.split(b"\0") if p}

    def usable(directory: str) -> bool:
        # Every file under it is tracked, untracked (`??`, listed one by one
        # with -uall; a repository as its directory) or ignored: all ignored
        # means none of the first two at, under or above it; and no nested
        # repository answers for it.
        prefix = directory + "/"
        if any(t.startswith(prefix) or prefix.startswith(t + "/") for t in tracked):
            return False
        if any(code == "??" and (k.startswith(prefix) or prefix.startswith(k)) for k, code in status.items()):
            return False
        parts = directory.split("/")
        return not any(os.path.lexists(wt.joinpath(*parts[:n]) / ".git") for n in range(1, len(parts) + 1))

    files: set[str] = set()
    walked: list[Path] = []                         # directories whose content may be dropped
    active: list[tuple[str, Path]] = []            # (kind, a tool entry of an identified directory)
    for directory in DIRS:
        d = wt / directory
        if not _real_dir(d) or d.name == "__pycache__":
            continue
        kind = identify(d)
        if kind is None or not usable(directory):
            continue
        for p in d.iterdir():
            if tools_own(kind, p) and not (_real_dir(p) and any(q.name.lower() == ".git" for q in p.rglob("*"))):
                active.append((kind, p))
    for kind, p in active:
        container = p.parent
        vouched = _vouched(container if kind == "venv" else p) if kind in ("venv", ".tox") else {}
        entries = [p] + (sorted(p.rglob("*")) if _real_dir(p) else [])
        walked.extend(q for q in entries if _real_dir(q))
        for f in entries:
            if not _regular(f):
                continue
            inner = f.relative_to(container).parts
            if kind in ("venv", ".tox"):
                env = container if kind == "venv" else p
                drop = _venv_drops(env, f, vouched)
            elif kind == ".pytest_cache":
                drop = "/".join(inner) in ("v/cache/nodeids", "v/cache/lastfailed", "v/cache/stepwise")
            elif kind == ".ruff_cache":
                drop = len(inner) == 2 and inner[1].isascii() and inner[1].isdigit()
            elif kind == ".mypy_cache":
                drop = len(inner) > 1 and (f.name.endswith((".data.json", ".meta.json", ".data.ff", ".meta.ff"))
                                           or f.name == "@plugins_snapshot.json")
            else:
                drop = False
            if drop:
                files.add(str(f.relative_to(wt)))
    # A `__pycache__` anywhere but under a tool's entry is judged by itself.
    covered = [p for _, p in active if _real_dir(p)]
    for cache in sorted(wt.rglob("__pycache__")):
        if not _real_dir(cache) or any(c == cache or c in cache.parents for c in covered):
            continue
        if not usable(str(cache.relative_to(wt))) or any(q.name.lower() == ".git" for q in cache.rglob("*")):
            continue                                    # holding a repository, it is archived whole
        walked.append(cache)
        files.update(str(f.relative_to(wt)) for f in cache.iterdir() if _regular(f) and _bytecode(f))
    dropped = set(files)
    for d in sorted(set(walked), key=lambda q: -len(q.parts)):          # deepest first
        inside = list(d.iterdir())
        if inside and all(str(q.relative_to(wt)) in dropped for q in inside):
            dropped.add(str(d.relative_to(wt)))
    return dropped


def regular_bytes(wt: Path, dropped) -> int:
    return sum((wt / p).stat().st_size for p in dropped if _regular(wt / p))


def _explicit(test):
    for dirs in reversed(EXPLICIT):
        test = example(dirs=dirs)(test)
    return test


@settings(max_examples=EXAMPLES, deadline=None, phases=PHASES,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(dirs=st.dictionaries(st.sampled_from(DIRS), spec, min_size=1, max_size=5))
@_explicit
def test_only_and_all_regenerable_output_is_dropped_and_the_rest_restored(monkeypatch, dirs):
    monkeypatch.setattr(rgit, "temp_roots", lambda: {"/nonexistent-temporary-root"})
    base = Path(tempfile.mkdtemp(prefix="retention-regen-prop-"))
    w = World(base)
    try:
        wt = w.job("job-prop")
        build(wt, dirs)
        expected = oracle(wt)
        before = snapshot(wt)
        size = regular_bytes(wt, expected)
        result = retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0, holders=lambda watches, **_: {})
        assert result["pruned"] == ["job-prop"], result
        manifest = json.loads((w.root / "archive" / "job-prop" / "manifest.json").read_text())
        dropped = {e["p"] for e in manifest["trees"]["worktree"]["entries"] if e.get("regen")}
        assert dropped <= expected, ("R1", sorted(dropped - expected))
        assert expected <= dropped, ("R2", sorted(expected - dropped))
        roots = {r for record in manifest["regenerable"] for r in record["roots"]}
        assert roots == {p for p in dropped if p.rpartition("/")[0] not in dropped}, "roots"
        totals = manifest["totals"]
        assert totals["regenerable_bytes"] == size, "R4"
        assert totals["freed_bytes"] == totals["omitted_bytes"] + totals["regenerable_bytes"], "R4"
        rarch.restore(w.root, "job-prop")
        kept = {p: v for p, v in before.items() if p not in expected}
        assert snapshot(wt) == kept, "R3"
    finally:
        w.close()
        shutil.rmtree(base, ignore_errors=True)
