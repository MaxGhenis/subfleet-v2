"""Helpers for the delete path's tests (C-8.7, d2660), on `retention_world.World`.

A delete path with no process scan, requests, runs stepped to their end, trees
holding every kind of git root whose commits the path keeps, and the oracles of
the invariants the build brief names:

1. an unlisted or gated job's tree is unchanged, entry by entry (`snapshot`);
2. after a deletion every commit any root of the tree reached (HEAD, refs,
   reflogs, per-worktree refs) is reachable from a ref of a surviving
   repository or from the job's verified bundle (`reachable`, `still_kept`);
3. nothing outside the tree changes, apart from the refs, the bundle and marker
   files, the job's journal folder and the admin directories it removes
   (`outside`, `changes`);
4. a dry run changes nothing anywhere (`everything`);
5. a kill at any step ends with the tree whole or the tree gone with its
   commits kept (`test_retention_delete_resume`).
"""
from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from subfleet import retention_delete as rd
from tests.unit.retention_world import World, git, snapshot

OLD = "2026-01-01T00:00:00Z"
#: The policy's `remote_less_history_bytes`, high enough that no test job is
#: `remote-less-history` unless a test lowers it.
STEP = {"remote_less_history_bytes": 10 ** 12}


def delete_path(w: World, **kwargs: Any) -> rd.DeletePath:
    kwargs.setdefault("holders", lambda watches, cancel=None: {})
    kwargs.setdefault("cwd_pids", lambda path: frozenset())
    return rd.DeletePath(w.store, w.root, **kwargs)


def request(*jobs: str, **fields: Any) -> rd.Request:
    data: dict[str, Any] = {"ruling": "d-test:2026-11-09:ruled", "jobs": list(jobs), "min_age_days": 0,
                            "min_free_gb": 0}
    data.update(fields)
    return rd.parse_request(data)


def finish(path: rd.DeletePath, *, mode: str = "apply", steps: int = 400, **step: Any) -> dict[str, Any]:
    """Step until the record of `mode` ends; its view."""
    kwargs = {**STEP, **step}
    key = "apply" if mode == "apply" else "dry_run"
    for _ in range(steps):
        path.step(**kwargs)
        record = path.status()[key]
        if record is not None and record["status"] not in ("running", "stopping"):
            return record
    raise AssertionError(f"the run did not end: {json.dumps(path.status(), default=str)[:2000]}")


def run(path: rd.DeletePath, req: rd.Request, *, mode: str = "apply", limit: int | None = None,
        **step: Any) -> dict[str, Any]:
    path.start(req, mode=mode, limit=limit)
    return finish(path, mode=mode, **step)


def results(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {line["job_id"]: line for line in record["results"]}


def commit(cwd: Path, name: str, text: str, message: str | None = None) -> str:
    path = Path(cwd) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(cwd, "add", name)
    git(cwd, "commit", "--quiet", "-m", message or f"add {name}")
    return git(cwd, "rev-parse", "HEAD")


def other_repo(w: World, name: str) -> Path:
    repo = w.base / "home" / name
    git(w.base, "init", "--quiet", "-b", "main", str(repo))
    commit(repo, "f", f"{name}\n", f"{name} base")
    return repo


def nested_job(w: World, job_id: str, *, repo: Path | None = None, rel: str = "nested", created: str = OLD,
               **job: Any) -> Path:
    """A job of class `nested-linked-worktree`: its tree holds a linked
    worktree of another repository, with a commit no other place has."""
    tree = w.job(job_id, created=created, **job)
    repo = repo or other_repo(w, f"other-{job_id}")
    (tree / rel).parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "--quiet", "--detach", str(tree / rel), "HEAD")
    commit(tree / rel, "nested.txt", f"{job_id}\n")
    return tree


def rich_job(w: World, job_id: str, *, created: str = OLD) -> dict[str, Any]:
    """A tree with every root the path keeps commits for, each holding a commit
    only an unusual record names (mutants 5 and 14):

    - `.`: the tree's own registration in the source repository, with a commit
      reset away (only the worktree's HEAD reflog names it), one only a
      per-worktree ref names and one only a bisect ref names;
    - `deep/nested`: a linked worktree of another repository, two levels down,
      with a commit reset away;
    - `inner`: a standalone repository inside the tree, with a branch, a stash,
      an annotated tag on a commit nothing else reaches, a commit reset away,
      and its own linked worktree `inner-wt`, also inside the tree;
    - `clone`: a clone of the server whose remote holds its base, so its
      bundle has prerequisites.
    """
    tree = w.job(job_id, created=created)
    a = commit(tree, "a.txt", "a\n")
    commit(tree, "b.txt", "b\n")
    git(tree, "reset", "--quiet", "--hard", a)
    c = git(tree, "commit-tree", git(tree, "write-tree"), "-p", a, "-m", "only refs/worktree")
    git(tree, "update-ref", "refs/worktree/kept", c)
    d = git(tree, "commit-tree", git(tree, "write-tree"), "-p", a, "-m", "only refs/bisect")
    git(tree, "update-ref", "refs/bisect/bad", d)
    other = other_repo(w, f"other-{job_id}")
    nested = tree / "deep" / "nested"
    nested.parent.mkdir()
    git(other, "worktree", "add", "--quiet", "--detach", str(nested), "HEAD")
    e = commit(nested, "e.txt", "e\n")
    commit(nested, "f.txt", "f\n")
    git(nested, "reset", "--quiet", "--hard", e)
    inner = tree / "inner"
    git(w.base, "init", "--quiet", "-b", "main", str(inner))
    g = commit(inner, "g.txt", "g\n")
    git(inner, "checkout", "--quiet", "-b", "side")
    commit(inner, "h.txt", "h\n")
    git(inner, "checkout", "--quiet", "main")
    (inner / "g.txt").write_text("stashed\n")
    git(inner, "stash", "--quiet")
    t = git(inner, "commit-tree", git(inner, "write-tree"), "-p", g, "-m", "only a tag")
    git(inner, "tag", "-a", "-m", "annotated", "v-only", t)
    commit(inner, "r.txt", "r\n")
    git(inner, "reset", "--quiet", "--hard", g)
    git(inner, "worktree", "add", "--quiet", "--detach", str(tree / "inner-wt"), "side")
    commit(tree / "inner-wt", "i.txt", "i\n")
    clone = tree / "clone"
    git(w.base, "clone", "--quiet", str(w.remote), str(clone))
    git(clone, "remote", "set-url", "origin", "https://git.example.invalid/project.git")
    commit(clone, "k.txt", "k\n")
    roots = {".": {"kind": "linked", "dir": tree, "common": w.repo / ".git"},
             "deep/nested": {"kind": "linked", "dir": nested, "common": other / ".git"},
             "inner": {"kind": "inside", "dir": inner, "common": inner / ".git", "held": False},
             "clone": {"kind": "inside", "dir": clone, "common": clone / ".git", "held": True}}
    return {"tree": tree, "roots": roots, "other": other, "admins": [w.admin(job_id), other / ".git" / "worktrees" / "nested"]}


def reachable(roots: dict[str, dict[str, Any]]) -> dict[str, set[str]]:
    """Every commit each root reaches, read with git itself: a linked
    worktree's HEAD, its HEAD reflog and its per-worktree refs; a repository's
    every ref, reflog and worktree HEAD."""
    out: dict[str, set[str]] = {}
    for rel, root in roots.items():
        d = root["dir"]
        if root["kind"] == "linked":
            tips = {git(d, "rev-parse", "HEAD")}
            tips |= set(git(d, "log", "-g", "--format=%H", "HEAD", check=False).split())
            tips |= set(git(d, "for-each-ref", "--format=%(objectname)", "refs/worktree/", "refs/bisect/",
                            "refs/rewritten/").split())
            out[rel] = set(git(d, "rev-list", *sorted(tips)).split())
        else:
            out[rel] = set(git(d, "rev-list", "--all", "--reflog", "HEAD").split())
    return out


def still_kept(w: World, job_id: str, roots: dict[str, dict[str, Any]], before: dict[str, set[str]]) -> None:
    """Invariant 2: each root's commits are reachable from refs of its
    surviving repository (no reflog, no admin directory) or from the job's
    bundle, fetched into an empty repository or, when it has prerequisites, a
    clone of the server."""
    marker = json.loads((w.root / "jobs" / job_id / "tree-deleted.json").read_text())
    for rel, root in roots.items():
        if root["kind"] == "linked":
            have = set(git(root["common"], "rev-list", "--all").split())
            assert before[rel] <= have, (rel, before[rel] - have)
            continue
        common = os.path.realpath(root["common"])
        bundle = [b for b in marker["bundles"] if os.path.normpath(b["repository"]) == common]
        assert len(bundle) == 1, (rel, marker["bundles"])
        path = Path(bundle[0]["path"])
        assert os.path.realpath(path.parent.parent) == os.path.realpath(w.root / "jobs" / job_id / "kept-commits")
        check = w.base / "checks" / f"{job_id}-{rel.replace('/', '-')}"
        check.parent.mkdir(exist_ok=True)
        if root.get("held"):
            git(w.base, "clone", "--quiet", "--bare", str(w.remote), str(check))
        else:
            git(w.base, "init", "--quiet", "--bare", str(check))
        git(check, "bundle", "verify", str(path))
        git(check, "fetch", "--quiet", str(path), "+refs/*:refs/restored/*")
        have = set(git(check, "rev-list", "--all").split())
        assert before[rel] <= have, (rel, before[rel] - have)


def _plain(view: dict[str, tuple]) -> dict[str, tuple]:
    """A snapshot without directory mtimes: what a directory holds is its
    entries, compared one by one."""
    return {rel: (kind, mode, None, None) if kind == "d" else (kind, mode, data, mtime)
            for rel, (kind, mode, data, mtime) in view.items()}


def everything(w: World) -> dict[str, tuple]:
    """Every entry under the world, the store's files apart (their rows are compared on their own)."""
    view = snapshot(w.base)
    return {rel: entry for rel, entry in view.items() if not rel.startswith("state/state.sqlite3")}


def rows(w: World) -> dict[str, list]:
    return {table: [tuple(r) for r in w.store.query(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("jobs", "attempts", "leases", "events", "artifacts", "notices")}


def outside(w: World, tree: Path) -> dict[str, tuple]:
    """Invariant 3's view: every entry under the world but the tree's own and the store's, directory mtimes aside."""
    rel = os.path.relpath(tree, w.base)
    return _plain({k: v for k, v in everything(w).items() if not (k == rel or k.startswith(rel + "/"))})


def changes(before: dict[str, tuple], after: dict[str, tuple]) -> dict[str, set[str]]:
    return {"added": set(after) - set(before), "removed": set(before) - set(after),
            "changed": {k for k in set(before) & set(after) if before[k] != after[k]}}


def allowed(w: World, job_id: str, commons: list[Path], admins: list[Path]) -> callable:
    """The places invariant 3 lets a deletion of `job_id` change."""
    def rel(path: Path) -> str:
        return os.path.relpath(path, w.base)
    prefixes = [rel(w.root / "retention-delete"), rel(w.root / "jobs" / job_id / "kept-commits"),
                rel(w.root / "jobs" / job_id / "tree-deleted.json"),
                *[rel(Path(c) / "refs" / "subfleet-kept") for c in commons], *[rel(a) for a in admins]]

    def ok(path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in prefixes)
    return ok


def restore_modes(base: Path) -> None:
    """Give every directory under `base` its owner's rwx back (tests make some
    unreadable), so pytest can remove it."""
    for directory, dirnames, _ in os.walk(base):
        for name in dirnames:
            path = Path(directory) / name
            if not path.is_symlink():
                try:
                    os.chmod(path, stat.S_IMODE(path.lstat().st_mode) | 0o700)
                except OSError:
                    pass


def remove(base: Path) -> None:
    restore_modes(base)
    assert "pytest" in str(base) or "sf-d2660" in str(base) or "retention-delete-" in str(base), base
    shutil.rmtree(base, ignore_errors=True)
