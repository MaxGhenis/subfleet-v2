"""Properties of disk relief (d635 round 2): what retention deletes without a
copy is exactly the regenerable output, and everything else comes back.

Hypothesis builds a worktree with directories named like regenerable output
(`.venv`, `venv`, `node_modules`, `__pycache__`, `.pytest_cache`, `.tox`,
`.uv-cache`) and like build output (`build`, `target`, `.cache`), each with a
marker that is right, missing, wrong or a link, ignored by the project, by a
`*` file inside, or not at all, sometimes with a tracked file or a repository
inside. Retention retires the job; then, checked against an oracle written
here independently of `retention_fs.regenerable` and `retention_git.Ignored`
(structure read with pathlib, ignore status from `git status --ignored`):

- **R1, only regenerable output is dropped.** Every directory the archive
  marks regenerable is one the oracle calls regenerable.
- **R2, all of it is dropped.** Every directory the oracle calls regenerable
  (and that is not inside another) is marked regenerable (disk relief).
- **R3, everything else comes back.** Restoring gives back every entry outside
  the regenerable directories: path, type, permission bits, bytes, link
  target, mtime.
- **R4, accounting.** `regenerable_bytes` is the sum of the regular files'
  sizes under those directories, and `freed_bytes` is it plus `omitted_bytes`.

`RETENTION_PROP_EXAMPLES` raises the example count.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, Phase, given, settings
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
        ".tox", ".uv-cache", "build", "target", ".cache"]

spec = st.fixed_dictionaries({
    "marker": st.sampled_from(["right", "missing", "wrong", "link"]),
    "ignored": st.sampled_from(["project", "inside", "no"]),
    "tracked": st.booleans(),
    "repository": st.booleans(),
    "extra": st.sampled_from(["none", "bytecode", "text", "subdir"]),
    "package_json": st.booleans(),
    "data": st.binary(min_size=0, max_size=2000),
})


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def build(wt: Path, dirs: dict[str, dict]) -> None:
    patterns = []
    for rel, s in sorted(dirs.items()):
        d = wt / rel
        d.mkdir(parents=True, exist_ok=True)
        name = d.name
        if name in (".venv", "venv"):
            marker, good, bad = "pyvenv.cfg", b"home = /usr/bin\n", b"version = 3\n"
        elif name in (".pytest_cache", ".tox", ".uv-cache", "target", ".cache", "build"):
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
        if s["repository"]:
            repo = d / "dep"
            _write(repo / "f.txt", b"only here\n")
            git(repo, "init", "--quiet")
            git(repo, "add", "f.txt")
            git(repo, "commit", "--quiet", "-m", "only here")
    with open(wt / ".gitignore", "a") as f:
        f.write("".join(p + "\n" for p in patterns))


def oracle(wt: Path) -> set[str]:
    """The regenerable directories, found without retention's code."""
    out = subprocess.run(["git", "-C", str(wt), "status", "--porcelain=v1", "-z", "--ignored=matching",
                          "--untracked-files=all"], capture_output=True, check=True).stdout
    status = {os.fsdecode(r[3:]): r[:2].decode() for r in out.split(b"\0") if r}
    tracked = {os.fsdecode(p) for p in subprocess.run(["git", "-C", str(wt), "ls-files", "-z"], capture_output=True,
                                                      check=True).stdout.split(b"\0") if p}
    found = set()
    for directory in DIRS:
        d = wt / directory
        if d.is_symlink() or not d.is_dir():
            continue
        name = d.name
        if name in (".venv", "venv"):
            cfg = d / "pyvenv.cfg"
            ok = cfg.is_file() and not cfg.is_symlink() and any(
                line.split("=")[0].strip() == "home" and "=" in line for line in cfg.read_text(errors="replace").splitlines())
        elif name == "node_modules":
            pj = d.parent / "package.json"
            ok = pj.is_file() and not pj.is_symlink()
        elif name == "__pycache__":
            ok = all(p.is_file() and not p.is_symlink() and p.suffix in (".pyc", ".pyo") for p in d.iterdir())
        elif name in (".pytest_cache", ".tox", ".uv-cache"):
            tag = d / "CACHEDIR.TAG"
            ok = tag.is_file() and not tag.is_symlink() and tag.read_bytes().startswith(SIGNATURE)
        else:
            ok = False
        if not ok:
            continue
        if any(part == ".git" for p in d.rglob("*") for part in p.relative_to(d).parts):
            continue
        # Every file under it is tracked, untracked (`??`, listed one by one
        # with -uall; a repository as its directory) or ignored: all ignored
        # means none of the first two at, under or above it.
        prefix = directory + "/"
        if any(t.startswith(prefix) or prefix.startswith(t + "/") for t in tracked):
            continue
        if any(code == "??" and (k.startswith(prefix) or prefix.startswith(k)) for k, code in status.items()):
            continue
        found.add(directory)
    # Only the outermost: one inside another is dropped with it.
    return {d for d in found if not any(d.startswith(o + "/") for o in found)}


@settings(max_examples=EXAMPLES, deadline=None, phases=PHASES,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(st.dictionaries(st.sampled_from(DIRS), spec, min_size=1, max_size=5))
def test_only_and_all_regenerable_output_is_dropped_and_the_rest_restored(monkeypatch, dirs):
    monkeypatch.setattr(rgit, "temp_roots", lambda: {"/nonexistent-temporary-root"})
    base = Path(tempfile.mkdtemp(prefix="retention-regen-prop-"))
    w = World(base)
    try:
        wt = w.job("job-prop")
        build(wt, dirs)
        expected = oracle(wt)
        before = snapshot(wt)
        sizes = {d: sum(p.stat().st_size for p in (wt / d).rglob("*") if p.is_file() and not p.is_symlink())
                 for d in expected}
        result = retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0, holders=lambda watches, **_: {})
        assert result["pruned"] == ["job-prop"], result
        manifest = json.loads((w.root / "archive" / "job-prop" / "manifest.json").read_text())
        marked = {r["p"] for r in manifest["regenerable"]}
        assert marked <= expected, ("R1", marked - expected)
        assert expected <= marked, ("R2", expected - marked)
        totals = manifest["totals"]
        assert totals["regenerable_bytes"] == sum(sizes.values()), "R4"
        assert totals["freed_bytes"] == totals["omitted_bytes"] + totals["regenerable_bytes"], "R4"
        rarch.restore(w.root, "job-prop")
        kept = {p: v for p, v in before.items() if not any(p == d or p.startswith(d + "/") for d in expected)}
        assert snapshot(wt) == kept, "R3"
    finally:
        w.close()
        shutil.rmtree(base, ignore_errors=True)
