"""Properties of disk relief (d635 round 2): what retention deletes without a
copy is exactly the regenerable output, and everything else comes back.

Hypothesis builds a worktree with directories named like regenerable output
(`.venv`, `venv`, `node_modules`, `__pycache__`, `.pytest_cache`, `.ruff_cache`,
`.mypy_cache`, `.tox`, `.uv-cache`) and like build output (`build`, `target`,
`.cache`). Each has a marker that is right, missing, wrong or a link; is ignored
by the project, by a `*` file inside, or not at all; may hold the tool's own
entries, a tracked file, something the tool did not write, and a repository
at its top level or inside one of the tool's entries. Retention retires the
job; then, checked against an oracle written here independently of
`retention_fs` and `retention_git.Ignored` (structure read with pathlib,
ignore status from `git status --ignored`):

- **R1, only regenerable output is dropped.** Every root the archive drops is
  one the oracle drops.
- **R2, all of it is dropped.** Every root the oracle drops, the archive drops
  (disk relief).
- **R3, everything else comes back.** Restoring gives back every entry outside
  the dropped roots: path, type, permission bits, bytes, link target, mtime.
- **R4, accounting.** `regenerable_bytes` is the sum of the regular files'
  sizes in the dropped roots, and `freed_bytes` is it plus `omitted_bytes`.

`RETENTION_PROP_EXAMPLES` raises the example count.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
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

# Weighted toward directories that are regenerable, so the cases that matter
# (a repository inside a tool's entry, uv's temporary directories) come up.
spec = st.fixed_dictionaries({
    "marker": st.sampled_from(["right", "right", "right", "missing", "wrong", "link"]),
    "ignored": st.sampled_from(["project", "project", "inside", "inside", "no"]),
    "own": st.sampled_from([True, True, True, False]),
    "tracked": st.sampled_from([False, False, False, True]),
    "repository": st.sampled_from(["none", "none", "top", "inside", "inside", "around"]),
    "extra": st.sampled_from(["none", "bytecode", "text", "subdir"]),
    "package_json": st.sampled_from([True, True, False]),
    "data": st.binary(min_size=0, max_size=2000),
})


def case(**changes) -> dict:
    return {"marker": "right", "ignored": "project", "own": True, "tracked": False, "repository": "none",
            "extra": "text", "package_json": True, "data": b"x" * 100, **changes}


#: Every kind once, regenerable, with work beside the tool's entries and a
#: repository inside one of them; and each kind's refusals.
EXPLICIT = [
    {".venv": case(repository="inside"), ".uv-cache": case(ignored="inside"), "src/__pycache__": case(extra="none")},
    {"web/node_modules": case(repository="top", extra="subdir"), ".tox": case(repository="inside", extra="subdir"),
     ".ruff_cache": case(), ".mypy_cache": case(ignored="inside")},
    {"web/node_modules": case(repository="around")},
    {".pytest_cache": case(repository="inside"), "venv": case(), "build": case(), "target": case(), ".cache": case()},
    {".venv": case(tracked=True), ".uv-cache": case(marker="link"), "node_modules": case(package_json=False),
     "__pycache__": case(extra="text"), ".tox": case(ignored="no")},
]


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def own_entries(name: str, data: bytes) -> tuple[dict[str, bytes], dict[str, str], str | None]:
    """What the tool itself would write: files, links, and one of its directories."""
    if name in (".venv", "venv"):
        return {"lib/python3.14/site.py": b"pass\n", "bin/python-data": data}, {"bin/python": "python-data"}, "lib"
    if name == "node_modules":
        return ({"left-pad/package.json": b"{}\n", "left-pad/index.js": data, "@types/node/package.json": b"{}\n",
                 ".bin/tool": b"#!/bin/sh\n"}, {"linked": "left-pad"}, "left-pad")
    if name == "__pycache__":
        return {"x.cpython-314.pyc": data}, {}, None
    if name == ".pytest_cache":
        return {"README.md": b"# pytest\n", "v/cache/nodeids": data}, {}, "v"
    if name == ".tox":
        return {"py314/pyvenv.cfg": b"home = /usr/bin\n", "py314/lib/site.py": data}, {}, "py314"
    if name == ".uv-cache":
        return {".lock": b"", "archive-v0/wheel": data, ".tmpAbC123/part": b"x"}, {}, "archive-v0"
    if name == ".ruff_cache":
        return {"0.16.9/cache": data}, {}, "0.16.9"
    if name == ".mypy_cache":
        return {"missing_stubs": b"\n", "3.14/x.json": data}, {}, "3.14"
    return {"out/data.bin": data}, {}, "out"                  # build, target, .cache


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
        elif name == "__pycache__":
            marker, good, bad = "m.cpython-314.pyc", b"\x00pyc", b"\x00pyc"
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
        inner = None
        if s["own"]:
            files, links, inner = own_entries(name, s["data"])
            for child, data in files.items():
                _write(d / child, data)
            for child, target in links.items():
                os.symlink(target, d / child)
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
        return "pycache" if all(_regular(p) and p.suffix in (".pyc", ".pyo") for p in d.iterdir()) else None
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


def oracle(wt: Path) -> set[str]:
    """The roots dropped, found without retention's code."""
    out = subprocess.run(["git", "-C", str(wt), "status", "--porcelain=v1", "-z", "--ignored=matching",
                          "--untracked-files=all"], capture_output=True, check=True).stdout
    status = {os.fsdecode(r[3:]): r[:2].decode() for r in out.split(b"\0") if r}
    tracked = {os.fsdecode(p) for p in subprocess.run(["git", "-C", str(wt), "ls-files", "-z"], capture_output=True,
                                                      check=True).stdout.split(b"\0") if p}
    found: set[str] = set()
    for directory in DIRS:
        d = wt / directory
        if not _real_dir(d):
            continue
        kind = identify(d)
        if kind is None:
            continue
        # Every file under it is tracked, untracked (`??`, listed one by one
        # with -uall; a repository as its directory) or ignored: all ignored
        # means none of the first two at, under or above it.
        prefix = directory + "/"
        if any(t.startswith(prefix) or prefix.startswith(t + "/") for t in tracked):
            continue
        if any(code == "??" and (k.startswith(prefix) or prefix.startswith(k)) for k, code in status.items()):
            continue
        parts = directory.split("/")
        if any(os.path.lexists(wt.joinpath(*parts[:n]) / ".git") for n in range(1, len(parts) + 1)):
            continue                                    # a nested repository answers for its own files
        roots = [directory] if kind == "pycache" else \
            [f"{directory}/{p.name}" for p in d.iterdir() if tools_own(kind, p)]
        for root in roots:
            r = wt / root
            if _real_dir(r) and any(q.name.lower() == ".git" for q in r.rglob("*")):
                continue                                # holds a repository: archived
            found.add(root)
    return found


def regular_bytes(wt: Path, roots) -> int:
    total = 0
    for root in roots:
        r = wt / root
        if r.is_symlink():
            continue
        if r.is_file():
            total += r.stat().st_size
        else:
            total += sum(q.stat().st_size for q in r.rglob("*") if q.is_file() and not q.is_symlink())
    return total


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
        dropped = {r for record in manifest["regenerable"] for r in record["roots"]}
        assert dropped <= expected, ("R1", dropped - expected)
        assert expected <= dropped, ("R2", expected - dropped)
        totals = manifest["totals"]
        assert totals["regenerable_bytes"] == size, "R4"
        assert totals["freed_bytes"] == totals["omitted_bytes"] + totals["regenerable_bytes"], "R4"
        rarch.restore(w.root, "job-prop")
        kept = {p: v for p, v in before.items() if not any(p == d or p.startswith(d + "/") for d in expected)}
        assert snapshot(wt) == kept, "R3"
    finally:
        w.close()
        shutil.rmtree(base, ignore_errors=True)
