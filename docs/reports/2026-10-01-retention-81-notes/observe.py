"""Observe, at a given commit, what each of #81's four notes leaves behind.

Run from the checkout under test: `python observe.py` with the repo on sys.path.
Every scenario uses its own temporary directory and state root.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.getcwd())

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet import retention_git as rgit
from tests.unit import retention_world as rw
from tests.unit.retention_world import World, git

rgit.temp_roots = lambda: {"/nonexistent-temporary-root"}


def run(w, **kw):
    kw.setdefault("max_jobs", 0)
    kw.setdefault("max_bytes", 0)
    kw.setdefault("holders", lambda watches, **_: {})
    return retention.maintenance(w.store, w.root, **kw)


def fresh_world():
    return World(Path(tempfile.mkdtemp(prefix="obs-")).resolve())


def bundle_has(w, job, commit):
    b = w.root / "archive" / job / "commits.bundle"
    if not b.exists():
        return "no bundle"
    f = w.base / f"fresh-{job}"
    git(w.base, "clone", "--quiet", str(w.remote), str(f))
    git(f, "fetch", "--quiet", str(b), "+refs/*:refs/r/*")
    return git(f, "cat-file", "-t", commit, check=False) == "commit"


def keep_mtime_write(path, data):
    st = os.lstat(path)
    with open(path, "r+b") as fh:
        fh.write(data)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def note1():
    w = fresh_world()
    wt = w.job("job-hl")
    (wt / "pair-a.bin").write_bytes(b"A" * 4096)
    os.link(wt / "pair-a.bin", wt / "pair-b.bin")
    store = w.base / "pnpm-store" / "index.js"
    store.parent.mkdir()
    store.write_bytes(b"S" * 4096)
    os.link(store, wt / "vendored.js")
    orig = rarch.Retirement.publish

    def hooked(self):
        orig(self)
        keep_mtime_write(self.q_worktree / "pair-a.bin", b"a" * 4096)
        keep_mtime_write(store, b"v" * 4096)
        store.unlink()

    rarch.Retirement.publish = hooked
    try:
        r = run(w)
    finally:
        rarch.Retirement.publish = orig
    conflicts = w.root / "retention-conflicts" / "job-hl"
    found = []
    for base in (w.base,):
        for d, _, files in os.walk(base):
            for n in files:
                p = Path(d) / n
                try:
                    data = p.read_bytes()
                except OSError:
                    continue
                if data in (b"a" * 4096, b"v" * 4096):
                    found.append(str(p.relative_to(w.base)))
    manifest = json.loads((w.root / "archive" / "job-hl" / "manifest.json").read_text())
    stored = {e["p"]: (w.root / "archive" / "job-hl" / "files" / e["store"]).read_bytes()[:1]
              for e in manifest["trees"]["worktree"]["entries"] if e["p"] in ("pair-a.bin", "vendored.js")}
    print("NOTE 1 (write after commit, same size, mtime restored, store link dropped):")
    print("  pruned:", r["pruned"], "| conflicts folder exists:", conflicts.exists())
    print("  files anywhere holding the new bytes:", found or "NONE (deleted)")
    print("  archive holds (first byte):", stored)


def archive_trees(w, job):
    m = w.root / "archive" / job / "manifest.json"
    if not m.exists():
        return "no archive"
    return sorted(json.loads(m.read_text())["trees"])


def archived(w, job, rel):
    m = w.root / "archive" / job / "manifest.json"
    if not m.exists():
        return "no archive (job kept)"
    return any(e["p"] == rel for t in json.loads(m.read_text())["trees"].values() for e in t["entries"])


def note2a():
    w = fresh_world()
    wt = w.job("job-q")
    (wt / "notes.txt").write_text("untracked work\n")
    q = wt.parent / (".disk-guard-removing." + wt.name)
    git(w.repo, "worktree", "move", str(wt), str(q))
    r = run(w)
    back = git(w.repo, "worktree", "move", str(q), str(wt), check=False)
    print("NOTE 2a (tree in the sweep's quarantine for the whole pass, moved back after):")
    print("  pruned:", r["pruned"], "| rows left:", w.store.get_job("job-q") is not None)
    print("  deferred:", r["deferred"])
    print("  after move back: tree exists:", wt.exists(), "| registration:", rgit.registration(wt)[1] or "ok",
          "| archive trees:", archive_trees(w, "job-q"))
    print("  untracked work archived:", archived(w, "job-q", "notes.txt"))


def note2b():
    w = fresh_world()
    wt = w.job("job-qb")
    (wt / "f.py").write_text("x\n")
    git(wt, "add", "f.py")
    git(wt, "commit", "--quiet", "-m", "private")
    private = git(wt, "rev-parse", "HEAD")
    q = wt.parent / (".disk-guard-removing." + wt.name)
    git(w.repo, "worktree", "move", str(wt), str(q))
    orig = rarch.Retirement.quarantine

    def hooked(self):
        if q.exists():
            git(w.repo, "worktree", "move", str(q), str(wt))
        return orig(self)

    rarch.Retirement.quarantine = hooked
    try:
        r = run(w)
    finally:
        rarch.Retirement.quarantine = orig
    admin = w.admin("job-qb")
    print("NOTE 2b (tree back between begin and quarantine):")
    print("  pruned:", r["pruned"], "| deferred:", r["deferred"])
    print("  tree exists:", wt.exists(), "| admin dir left:", admin.exists(),
          "| its backlink names:", (admin / "gitdir").read_text().strip() if admin.exists() else None)
    print("  `git worktree list` says:", [l for l in git(w.repo, "worktree", "list", "--porcelain").splitlines()
                                         if "prunable" in l])
    print("  private commit in the bundle:", bundle_has(w, "job-qb", private))
    git(w.repo, "worktree", "prune")
    git(w.repo, "reflog", "expire", "--expire=now", "--all")
    git(w.repo, "gc", "--quiet", "--prune=now")
    print("  after `git worktree prune` and gc, commit present:",
          git(w.repo, "cat-file", "-t", private, check=False) == "commit")


def note2c():
    w = fresh_world()
    wt = w.job("job-qa")
    (wt / "notes.txt").write_text("untracked work\n")
    q = wt.parent / (".disk-guard-removing." + wt.name)
    orig = rarch.Retirement.lock

    def hooked(self):
        if wt.exists():
            git(w.repo, "worktree", "move", str(wt), str(q))
        return orig(self)

    rarch.Retirement.lock = hooked
    try:
        r = run(w)
    finally:
        rarch.Retirement.lock = orig
    out = git(w.repo, "worktree", "move", str(q), str(wt), check=False)
    print("NOTE 2c (tree moved away between begin and lock):")
    print("  pruned:", r["pruned"], "| admin dir exists:", w.admin("job-qa").exists())
    print("  sweep's move back worked:", wt.exists())
    where = wt if wt.exists() else q
    print("  tree at:", where.name, "| its .git:", (where / ".git").read_text().strip())
    print("  git works in it:", git(where, "rev-parse", "HEAD", check=False) != "")
    print("  untracked work archived:", archived(w, "job-qa", "notes.txt"), "| deferred:", r["deferred"])


def note3():
    w = fresh_world()
    path = w.root / "worktrees" / "job-cut"
    git(w.repo, "worktree", "add", "--quiet", "--detach", str(path), w.head())
    w.store.add_job(job_id="job-cut", request_id="r", payload_digest="d", kind="dispatch", workdir=str(w.repo),
                    workdir_head=w.head(), worktree=None, prompt_path="/p", sandbox="workspace-write",
                    state="cancelled")
    (w.root / "jobs" / "job-cut").mkdir()
    r = run(w)
    print("NOTE 3 (allocated tree, jobs.worktree NULL, cancelled):")
    print("  pruned:", r["pruned"], "| tree left:", path.exists(), "| registration left:", w.admin("job-cut").exists())
    r2 = run(w)
    print("  next pass sees it:", r2["pruned"], r2["deferred"], "| tree still left:", path.exists())


def note4(salvage, other=False):
    w = fresh_world()
    lane = w.base / "lanes" / "lane-1"
    lane.parent.mkdir()
    git(w.repo, "worktree", "add", "--quiet", "--detach", str(lane), w.head())
    tree = w.root / "worktrees" / "job-lane"
    git(lane, "worktree", "add", "--quiet", "--detach", str(tree), w.head())
    w.store.add_job(job_id="job-lane", request_id="r", payload_digest="d", kind="dispatch", workdir=str(lane),
                    workdir_head=w.head(), worktree=str(tree), prompt_path="/p", sandbox="workspace-write",
                    state="succeeded")
    (w.root / "jobs" / "job-lane").mkdir()
    (tree / "f.py").write_text("x\n")
    git(tree, "add", "f.py")
    git(tree, "commit", "--quiet", "-m", "private")
    private = git(tree, "rev-parse", "HEAD")
    if salvage:
        w.attempt("job-lane")
        git(w.repo, "update-ref", "refs/subfleet-salvage/detached-x-a1", private)
        w.store.add_artifact("job-lane/a1", "salvage", "refs/subfleet-salvage/detached-x-a1", "d", 0)
    git(w.repo, "worktree", "remove", "--force", str(lane))
    shutil.rmtree(tree)
    kw = {}
    if other:
        w.store.add_job(job_id="job-other", request_id="o", payload_digest="d", kind="dispatch", workdir=str(w.repo),
                        workdir_head=w.head(), prompt_path="/p", sandbox="read-only", state="succeeded")
        (w.root / "jobs" / "job-other").mkdir()
        kw["referenced_job_ids"] = ["job-other"]
    state, clock = retention.RetentionState(), rw.Clock()
    r = run(w, state=state, clock=clock, **kw)
    print(f"NOTE 4 ({'with' if salvage else 'no'} salvage; tree and workdir gone, repository there"
          f"{', another job of it in the store' if other else ', no other job of it'}):")
    print("  pruned:", r["pruned"], "| deferred:", r["deferred"])
    if not salvage and (w.root / "archive" / "job-lane" / "manifest.json").exists():
        m = json.loads((w.root / "archive" / "job-lane" / "manifest.json").read_text())
        print("  archive git:", {k: m["git"].get(k) for k in ("common", "anchor")},
              "| registration left:", (w.repo / ".git" / "worktrees" / "job-lane").exists(),
              "| private commit in bundle:", bundle_has(w, "job-lane", private))
    else:
        if r["pruned"]:
            m = json.loads((w.root / "archive" / "job-lane" / "manifest.json").read_text())
            print("  salvage in anchor:", m["git"].get("salvage_in_anchor") == [private],
                  "| private commit in bundle:", bundle_has(w, "job-lane", private))
            return
        for _ in range(3):
            clock.advance(rarch.DEFER_PERMANENT_S + 1)
            r = run(w, state=state, clock=clock, **kw)
        print("  three days later:", r["pruned"], r["deferred"])


if __name__ == "__main__":
    for f in (note1, note2a, note2b, note2c, note3, lambda: note4(False), lambda: note4(True),
              lambda: note4(False, True), lambda: note4(True, True)):
        f()
