"""The sweep's move away and back across retention's steps (2026-10-03).

`~/chief-of-staff/bin/disk-guard` and `worktree-archive-sweep` `git worktree
move` a job's tree to `<parent>/.disk-guard-removing.<name>`, check it there,
and move it back when a check fails (`move_back`, `restore_leftover`). #81's
note 2 on #76 was closed for a tree away for a whole step, but the review of
43f09ea4 found it only partly closed: moved away while `begin` read the tree's
gitfile, the lookup found no gitfile and so no registration; moved back before
`quarantine`, the tree passed the presence check (there at `begin`, there now)
and retired without its registration. The commits only its HEAD and reflog
held went into no bundle, and its admin directory was left naming a tree that
was gone, for the next `git worktree prune` and gc to drop with them.

Everything here runs real git on real directories under a temporary root.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet import retention_fs as rfs
from subfleet import retention_git as rgit
from tests.unit.retention_world import Clock, World, git, trust_temporary_directories

SWEEP_PREFIX = ".disk-guard-removing."
#: Untracked and ignored work in the tree (`World`'s .gitignore ignores `out/`).
FILES = {"notes.txt": b"untracked work\n", "out/build.log": b"ignored work\n"}
#: Long enough for every deferral to expire (the longest is a day).
LATER = 2 * rarch.DEFER_PERMANENT_S


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


def _job_with_private_history(w: World, job_id: str) -> tuple[Path, dict[str, str]]:
    """A job tree holding each kind of private history: a commit only its
    detached HEAD reaches, one only its HEAD's reflog and ORIG_HEAD still
    name (committed, then reset away), a stash, and untracked and ignored
    files. `refs/stash` lives in the common directory, which every worktree
    shares, so no retirement can take the stash; it is checked all the same."""
    wt = w.job(job_id)
    (wt / "dropped.py").write_text("committed, then reset away\n")
    git(wt, "add", "dropped.py")
    git(wt, "commit", "--quiet", "-m", "dropped")
    dropped = git(wt, "rev-parse", "HEAD")
    git(wt, "reset", "--quiet", "--hard", "HEAD~1")
    (wt / "f.py").write_text("only on this tree's detached HEAD\n")
    git(wt, "add", "f.py")
    git(wt, "commit", "--quiet", "-m", "private")
    head = git(wt, "rev-parse", "HEAD")
    (wt / "stashed.txt").write_text("stashed work\n")
    git(wt, "add", "stashed.txt")
    git(wt, "stash", "push", "--quiet", "-m", "the job's stash")
    stash = git(wt, "rev-parse", "refs/stash")
    for rel, data in FILES.items():
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_bytes(data)
    return wt, {"head": head, "reflog": dropped, "stash": stash,
                "stash_list": git(wt, "stash", "list", "--format=%H %gs")}


class Sweep:
    """The sweep's two moves, as `disk-guard`'s `move_worktree` makes them:
    away to the quarantine name, `git -C <repo> worktree move <tree>
    <parent>/.disk-guard-removing.<name>`, and back. It never forces, so git
    refuses a locked tree and one that is not where its registration says,
    and `move_back` refuses when the original path is taken."""

    def __init__(self, w: World, tree: Path):
        self.w = w
        self.tree = tree
        self.aside = tree.parent / (SWEEP_PREFIX + tree.name)
        self.log: list[str] = []

    def away(self) -> bool:
        if not os.path.lexists(self.tree) or os.path.lexists(self.aside):
            return False
        git(self.w.repo, "worktree", "move", str(self.tree), str(self.aside), check=False)
        moved = os.path.lexists(self.aside)
        self.log.append(f"away {'moved' if moved else 'refused'}")
        return moved

    def back(self) -> bool:
        if not os.path.lexists(self.aside) or os.path.lexists(self.tree):
            return False
        git(self.w.repo, "worktree", "move", str(self.aside), str(self.tree), check=False)
        moved = os.path.lexists(self.tree)
        self.log.append(f"back {'moved' if moved else 'refused'}")
        return moved


def _bundled(w: World, job_id: str, commits: list[str]) -> dict[str, bool]:
    """Which commits the job's published bundle holds, read into a fresh clone
    of the server after `git bundle verify` (the source repository's own
    objects prove nothing: they are there until a prune and a gc)."""
    bundle = w.root / "archive" / job_id / "commits.bundle"
    if not bundle.exists():
        return dict.fromkeys(commits, False)
    fresh = Path(tempfile.mkdtemp(prefix="fresh-", dir=w.base))
    shutil.rmtree(fresh)
    git(w.base, "clone", "--quiet", str(w.remote), str(fresh))
    git(fresh, "bundle", "verify", "--quiet", str(bundle))
    git(fresh, "fetch", "--quiet", str(bundle), "+refs/*:refs/r/*")
    found = {c: git(fresh, "cat-file", "-t", c, check=False) == "commit" for c in commits}
    shutil.rmtree(fresh)
    return found


def _gitfile_target(tree: Path) -> Path | None:
    try:
        text = (tree / ".git").read_text().strip()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return None
    target = Path(text.removeprefix("gitdir:").strip())
    return target if target.is_absolute() else tree / target


def violations(w: World, job_id: str, tree: Path, history: dict[str, str]) -> list[str]:
    """What breaks retention's invariant for one job's tree: retention never
    deletes a byte its archive does not hold and never orphans a tree. The
    tree is kept intact (once, where the sweep can find it, its registration
    working and not left locked, its work in it), or it is gone with its
    private history in a verified bundle and its files in the archive. No
    `.git` names a missing admin directory, and no admin directory names a
    tree that is gone without a bundle of what it held."""
    out: list[str] = []
    aside = tree.parent / (SWEEP_PREFIX + tree.name)
    admin = w.admin(job_id)
    held = w.root / "retention" / job_id / "worktree"
    conflicts = w.root / "retention-conflicts" / job_id
    places = [p for p in (tree, aside, held) if os.path.lexists(p)]
    if conflicts.is_dir():
        places += sorted(p for p in conflicts.iterdir() if p.name.startswith("worktree"))
    private = [history["head"], history["reflog"]]
    bundled = _bundled(w, job_id, private)
    for place in places:
        named = _gitfile_target(place)
        if named is not None and not named.is_dir():
            out.append(f"orphan: {place}/.git names {named}, which is gone")
    if admin.is_dir():
        backlink = Path((admin / "gitdir").read_text().strip())
        if not backlink.exists() and not all(bundled.values()):
            out.append(f"orphan: {admin} names {backlink}, which is gone, and no bundle holds {bundled}")
    if git(w.repo, "rev-parse", "--verify", "--quiet", "refs/stash", check=False) != history["stash"]:
        out.append("the stash is gone")
    if git(w.repo, "stash", "list", "--format=%H %gs", check=False) != history["stash_list"]:
        out.append("the stash list changed")
    if w.store.get_job(job_id) is not None:
        if [p for p in places if p not in (tree, aside)] or len(places) != 1:
            out.append(f"kept, but its tree is at {places}, not once where the sweep can find it")
            return out
        place = places[0]
        if git(place, "rev-parse", "HEAD", check=False) != history["head"]:
            out.append(f"kept, but git in {place} does not find its HEAD")
        reflog = git(place, "reflog", "show", "--format=%H", "HEAD", check=False).splitlines()
        if history["reflog"] not in reflog:
            out.append(f"kept, but its HEAD reflog no longer names {history['reflog']}")
        if (admin / "locked").exists():
            out.append(f"kept, but its registration is still locked: {(admin / 'locked').read_text()!r}")
        for rel, data in FILES.items():
            if not (place / rel).is_file() or (place / rel).read_bytes() != data:
                out.append(f"kept, but {rel} is not as it was")
        return out
    if places:
        out.append(f"retired, but its tree is still at {places}")
    if admin.exists():
        out.append(f"retired, but its admin directory {admin} is left")
    missing = [c for c, ok in bundled.items() if not ok]
    if missing:
        out.append(f"retired without {[k for k, c in history.items() if c in missing]} in its verified bundle")
    archive = w.root / "archive" / job_id
    try:
        manifest = json.loads((archive / "manifest.json").read_bytes())
    except FileNotFoundError:
        return out + ["retired without an archive"]
    entries = {e["p"]: e for e in manifest["trees"].get("worktree", {}).get("entries", [])}
    for rel, data in FILES.items():
        entry = entries.get(rel)
        stored = archive / "files" / entry["store"] if entry and entry.get("store") else None
        if stored is None or hashlib.sha256(stored.read_bytes()).hexdigest() != hashlib.sha256(data).hexdigest():
            out.append(f"retired without {rel} in its archive")
    if manifest["git"].get("admin") != str(admin.resolve()) or manifest["git"].get("head") != history["head"]:
        out.append(f"retired with another registration archived: {manifest['git'].get('admin')}")
    return out


# --- the reproduction ---------------------------------------------------------------------

@pytest.mark.parametrize("back", ["after-the-lookup", "before-quarantine"])
def test_a_tree_moved_away_during_its_registration_lookup_and_back_keeps_its_job(world, monkeypatch, back):
    """The review of 43f09ea4. The sweep moves the tree away exactly while
    `begin` reads its gitfile, and moves it back right after that read, or
    when retention next acts on the tree (its lock or its quarantine). On
    43f09ea4 the lookup found no gitfile, so the journal named no
    registration; the tree, there at `begin` and there again at `quarantine`,
    passed the presence check and was archived and deleted without its
    registration: the commits only its HEAD and reflog held were in no bundle,
    and its admin directory was left naming a tree that was gone.

    Now the job is kept, nothing is archived under a registration the tree
    does not have, and nothing is left locked or moved: renamed into
    quarantine, the tree has a gitfile, and `begin` read none (its identity
    in the journal), so `quarantine` puts it back before the archive. Once
    the sweep is done, the next pass retires the job with its registration
    and its private history in its bundle."""
    w = world
    wt, history = _job_with_private_history(w, "job-race")
    sweep = Sweep(w, wt)
    in_begin, archived = [], []
    real_begin, real_lookup = rarch.Retirement.begin, rgit.gitfile_admin
    real_lock, real_quarantine = rarch.Retirement.lock, rarch.Retirement.quarantine
    real_archive = rarch.Retirement.archive

    def begin(self, job, pool):
        in_begin.append(True)
        try:
            return real_begin(self, job, pool)
        finally:
            in_begin.clear()

    def lookup(tree):
        if not in_begin or Path(tree) != wt or sweep.log:
            return real_lookup(tree)
        assert sweep.away(), "the sweep could not move the tree away"
        try:
            return real_lookup(tree)
        finally:
            if back == "after-the-lookup":
                assert sweep.back()

    def lock(self):
        if back == "before-quarantine":
            sweep.back()
        return real_lock(self)

    def quarantine(self):
        if back == "before-quarantine":
            sweep.back()
        return real_quarantine(self)

    def archive(self, slice_end):
        archived.append(self.job_id)
        return real_archive(self, slice_end)

    monkeypatch.setattr(rarch.Retirement, "begin", begin)
    monkeypatch.setattr(rgit, "gitfile_admin", lookup)
    monkeypatch.setattr(rarch.Retirement, "lock", lock)
    monkeypatch.setattr(rarch.Retirement, "quarantine", quarantine)
    monkeypatch.setattr(rarch.Retirement, "archive", archive)
    state, clock = retention.RetentionState(), Clock()
    first = run(w, state=state, clock=clock)
    assert sweep.log[0] == "away moved", sweep.log
    sweep.back()                     # whatever its check found, the sweep puts the tree back
    assert violations(w, "job-race", wt, history) == [], (first, sweep.log)
    assert first["pruned"] == [], first
    assert "changed" in first["deferred"]["job-race"], first
    assert archived == [], "archived under a registration the tree did not have"
    assert not (w.root / "retention" / "job-race" / "worktree").exists()
    for name, real in (("begin", real_begin), ("lock", real_lock), ("quarantine", real_quarantine),
                       ("archive", real_archive)):
        monkeypatch.setattr(rarch.Retirement, name, real)
    monkeypatch.setattr(rgit, "gitfile_admin", real_lookup)
    clock.advance(LATER)
    second = run(w, state=state, clock=clock)
    assert second["pruned"] == ["job-race"], second
    assert violations(w, "job-race", wt, history) == []


def test_a_tree_moved_away_as_it_is_moved_in_keeps_its_job(world, monkeypatch):
    """The quarantine's own window: the tree there at its presence check, then
    moved away (it has no lock when `begin` found no registration) before the
    rename. On 43f09ea4 the rename was skipped without a word, the job retired
    without its tree, and the sweep moved back a tree no row named, its
    untracked and ignored work never archived and never to be retired."""
    w = world
    wt, history = _job_with_private_history(w, "job-in")
    sweep = Sweep(w, wt)
    in_begin = []
    real_begin, real_lookup, real_save = rarch.Retirement.begin, rgit.gitfile_admin, rarch.Retirement.save

    def begin(self, job, pool):
        in_begin.append(True)
        try:
            return real_begin(self, job, pool)
        finally:
            in_begin.clear()

    def lookup(tree):
        if not in_begin or Path(tree) != wt or sweep.log:
            return real_lookup(tree)
        sweep.away()
        try:
            return real_lookup(tree)
        finally:
            sweep.back()

    def save(self, **changes):
        real_save(self, **changes)
        if changes.get("state") == "quarantining":
            sweep.away()

    monkeypatch.setattr(rarch.Retirement, "begin", begin)
    monkeypatch.setattr(rgit, "gitfile_admin", lookup)
    monkeypatch.setattr(rarch.Retirement, "save", save)
    state, clock = retention.RetentionState(), Clock()
    first = run(w, state=state, clock=clock)
    assert sweep.log[:3] == ["away moved", "back moved", "away moved"], sweep.log
    sweep.back()
    assert violations(w, "job-in", wt, history) == [], (first, sweep.log)
    assert first["pruned"] == [] and "changed" in first["deferred"]["job-in"], first
    monkeypatch.setattr(rarch.Retirement, "begin", real_begin)
    monkeypatch.setattr(rgit, "gitfile_admin", real_lookup)
    monkeypatch.setattr(rarch.Retirement, "save", real_save)
    clock.advance(LATER)
    assert run(w, state=state, clock=clock)["pruned"] == ["job-in"]
    assert violations(w, "job-in", wt, history) == []


def test_a_gitfile_rewritten_before_the_archive_reads_it_keeps_the_job(world, monkeypatch):
    """The registration is checked again in the final check, the last moment
    a change can keep the job. A tree's `.git` rewritten inside quarantine
    before the archive read it (a process with its cwd inside, say) would be
    archived as rewritten, so the final check's comparison with the archive
    passes; the tree would then go with the registration `begin` read and
    archived, while its gitfile named another."""
    w = world
    wt, history = _job_with_private_history(w, "job-gf")
    other = w.base / "elsewhere" / "worktrees" / "other"
    real_archive = rarch.Retirement.archive

    def archive(self, slice_end):
        if self.job_id == "job-gf" and not other.exists():
            other.mkdir(parents=True)
            (self.q_worktree / ".git").write_text(f"gitdir: {other}\n")
        return real_archive(self, slice_end)

    monkeypatch.setattr(rarch.Retirement, "archive", archive)
    first = run(w)
    assert first["pruned"] == [], first
    assert "changed" in first["deferred"]["job-gf"], first
    assert w.store.get_job("job-gf") is not None and wt.is_dir()
    assert (wt / ".git").read_text().strip() == f"gitdir: {other}"


def test_a_gone_tree_found_registered_then_moved_away_before_the_lock_keeps_its_registration(world, monkeypatch):
    """The window of a tree that is gone at `begin`. The sweep has it away
    when `begin` looks (so it counts as gone), moves it back before the
    sibling check (so no `tree away`), and the registration is found by its
    backlink; then it moves the tree away again before the lock. On 43f09ea4
    `quarantine` saw the tree gone, as at `begin`, so did the final check, and
    retention archived and deleted the registration while the tree sat in
    the sweep's quarantine: its `.git` named nothing, so the sweep could not
    move it back, and its untracked and ignored work was in no archive. Now
    the tree beside it, in another tool's quarantine, keeps the job, and the
    lock retention wrote meanwhile is removed so the sweep can move it back."""
    w = world
    wt, history = _job_with_private_history(w, "job-gone")
    sweep = Sweep(w, wt)
    assert sweep.away()
    real_away, real_lock = rarch.tree_away, rarch.Retirement.lock

    def tree_away(worktree):
        if Path(worktree) == wt and sweep.log == ["away moved"]:
            assert sweep.back()
        return real_away(worktree)

    def lock(self):
        if sweep.log == ["away moved", "back moved"]:
            assert sweep.away()
        return real_lock(self)

    monkeypatch.setattr(rarch, "tree_away", tree_away)
    monkeypatch.setattr(rarch.Retirement, "lock", lock)
    state, clock = retention.RetentionState(), Clock()
    first = run(w, state=state, clock=clock)
    assert sweep.log == ["away moved", "back moved", "away moved"], sweep.log
    assert first["pruned"] == [], first
    assert "another tool's quarantine" in first["deferred"]["job-gone"], first
    assert violations(w, "job-gone", wt, history) == [], first
    assert sweep.back(), sweep.log          # not locked: the sweep's move back works
    assert violations(w, "job-gone", wt, history) == []
    monkeypatch.setattr(rarch, "tree_away", real_away)
    monkeypatch.setattr(rarch.Retirement, "lock", real_lock)
    clock.advance(LATER)
    assert run(w, state=state, clock=clock)["pruned"] == ["job-gone"]
    assert violations(w, "job-gone", wt, history) == []


def test_a_gone_trees_registration_repaired_to_another_place_keeps_its_job(world, monkeypatch):
    """The registration's backlink is held to the tree's original path, not
    only the sweep's sibling name. A tree moved by hand (`mv`, so its
    registration still names the old path) is gone at `begin`, which finds
    the registration by that backlink; before the lock, `git worktree repair`
    in the moved tree points the backlink at it. On 43f09ea4 the registration
    was archived and deleted, and the moved tree's `.git` named nothing. Now
    the job is kept, and the next pass finds the tree registered elsewhere
    (`tree away`) and keeps it until the tree is back or gone."""
    w = world
    wt, history = _job_with_private_history(w, "job-rep")
    elsewhere = w.base / "elsewhere" / "checkout"
    elsewhere.parent.mkdir()
    os.rename(wt, elsewhere)
    real_lock = rarch.Retirement.lock
    repaired = []

    def lock(self):
        if not repaired:
            repaired.append(git(elsewhere, "worktree", "repair"))
        return real_lock(self)

    monkeypatch.setattr(rarch.Retirement, "lock", lock)
    state, clock = retention.RetentionState(), Clock()
    first = run(w, state=state, clock=clock)
    assert repaired, "the lock was never reached"
    assert first["pruned"] == [], first
    assert f"names {elsewhere.resolve()}/.git" in first["deferred"]["job-rep"], first
    assert w.store.get_job("job-rep") is not None
    assert w.admin("job-rep").is_dir() and not (w.admin("job-rep") / "locked").exists()
    assert git(elsewhere, "rev-parse", "HEAD") == history["head"]
    monkeypatch.setattr(rarch.Retirement, "lock", real_lock)
    clock.advance(LATER)
    second = run(w, state=state, clock=clock)
    assert second["pruned"] == [] and second["deferred"]["job-rep"].startswith("tree away"), second
    assert git(elsewhere, "rev-parse", "HEAD") == history["head"]


def test_a_tree_replaced_by_a_copy_before_quarantine_keeps_its_job(world, monkeypatch):
    """The identity is the directory, not only its gitfile: a tree replaced
    after `begin` by a copy (the same gitfile, the same registration, another
    inode: a restore over it, a `cp -R` then `mv`) is not the tree whose
    registration was read, and the job is read again next time."""
    w = world
    wt, history = _job_with_private_history(w, "job-copy")
    real_lock = rarch.Retirement.lock

    def lock(self):
        if not (w.base / "old").exists():
            shutil.copytree(wt, w.base / "copy", symlinks=True)
            os.rename(wt, w.base / "old")
            os.rename(w.base / "copy", wt)
        return real_lock(self)

    monkeypatch.setattr(rarch.Retirement, "lock", lock)
    first = run(w)
    assert first["pruned"] == [], first
    assert "not the directory whose registration was read" in first["deferred"]["job-copy"], first
    assert w.store.get_job("job-copy") is not None and git(wt, "rev-parse", "HEAD") == history["head"]
    assert not (w.admin("job-copy") / "locked").exists()


def test_a_tree_put_back_by_hand_after_the_final_check_keeps_its_registration(world, monkeypatch):
    """Immediately before the registration is deleted, retention looks again
    for a tree outside its quarantine that names it (at the original path,
    beside it in another tool's quarantine, or where its backlink says). The
    sweep cannot move a tree retention locked, but a person can put one back
    by hand after the final check, as here. Deleted, the registration would
    leave that tree a `.git` naming nothing; it stays, unlocked, and the
    retirement reports it."""
    w = world
    wt, history = _job_with_private_history(w, "job-hand")
    admin = w.admin("job-hand")
    real_publish = rarch.Retirement.publish

    def publish(self):
        if not wt.exists():
            wt.mkdir()
            (wt / ".git").write_text(f"gitdir: {admin}\n")
        return real_publish(self)

    monkeypatch.setattr(rarch.Retirement, "publish", publish)
    result = run(w)
    assert result["pruned"] == ["job-hand"], result
    assert admin.is_dir() and not (admin / "locked").exists()
    assert git(wt, "rev-parse", "HEAD") == history["head"]
    events = [json.loads(e["data_json"]) for e in w.store.list_events("job-hand") if e["kind"] == "retention.conflict"]
    assert [k["reason"] for k in events[0]["kept"]] == ["a tree names it"], events
    assert events[0]["kept"][0]["files"] == [str(wt)], events
    assert all(_bundled(w, "job-hand", [history["head"], history["reflog"]]).values())


def test_a_journal_written_before_the_identity_check_is_read_again(world, monkeypatch):
    """A retirement in flight from before this check (its journal has no
    identity) is not finished on trust: the final check puts it back, and the
    next pass reads the tree and its registration again and retires it."""
    w = world
    wt, history = _job_with_private_history(w, "job-old")
    real_archive = rarch.Retirement.archive

    class Crash(BaseException):
        pass

    def crash(self, slice_end):
        raise Crash()

    monkeypatch.setattr(rarch.Retirement, "archive", crash)
    with pytest.raises(Crash):
        run(w)
    monkeypatch.setattr(rarch.Retirement, "archive", real_archive)
    path = rarch.journal_path(w.root, "job-old")
    journal = json.loads(path.read_bytes())
    assert journal["state"] == "quarantined" and journal["identity"]["gitfile"], journal
    del journal["identity"]
    path.write_bytes(rarch._canonical(journal))
    state, clock = retention.RetentionState(), Clock()
    second = run(w, state=state, clock=clock)
    assert second["pruned"] == [], second
    assert "no identity" in second["deferred"]["job-old"], second
    assert git(wt, "rev-parse", "HEAD") == history["head"]
    clock.advance(LATER)
    assert run(w, state=state, clock=clock)["pruned"] == ["job-old"]
    assert violations(w, "job-old", wt, history) == []


# --- the property: generated schedules of the sweep's moves -----------------------------------

#: Where the sweep may act, in the order retention reaches them: before
#: `begin`; inside it, at the gone-tree check, as the tree's gitfile is read,
#: as its registration's backlink is read, and as a gone tree's registration
#: is looked for; before the lock and the quarantine; inside the quarantine,
#: after its presence check and before the renames; before the archive, the
#: final check, the commit (the point of no return), the publish and the
#: verified deletion.
POINTS = ("begin", "gone-check", "lookup", "backlink", "gone-lookup", "lock", "quarantine", "moving-in",
          "archive", "final-check", "commit", "publish", "reclaim")
ACTIONS = ("away", "back")


class Schedule:
    """Hooks retention's steps and makes the sweep's move due at each one.
    A point inside `begin` fires only there, for this job's tree."""

    def __init__(self, w: World, tree: Path, moves: dict[str, str]):
        self.sweep = Sweep(w, tree)
        self.moves = moves
        self.tree = tree
        self.admin = w.admin(tree.name)
        self.fired: list[str] = []
        self.in_begin = False

    def at(self, point: str) -> None:
        if point in self.fired:
            return
        self.fired.append(point)
        action = self.moves.get(point)
        if action == "away":
            self.sweep.away()
        elif action == "back":
            self.sweep.back()

    def install(self, mp: pytest.MonkeyPatch) -> None:
        schedule = self
        real = {name: getattr(rarch.Retirement, name) for name in
                ("begin", "lock", "quarantine", "save", "archive", "final_check", "commit", "publish", "reclaim")}
        real_away, real_lookup = rarch.tree_away, rgit.gitfile_admin
        real_read, real_in = rfs.read_regular, rgit.registration_in

        def begin(self, job, pool):
            schedule.at("begin")
            schedule.in_begin = True
            try:
                return real["begin"](self, job, pool)
            finally:
                schedule.in_begin = False

        def tree_away(worktree):
            if schedule.in_begin:
                schedule.at("gone-check")
            return real_away(worktree)

        def gitfile_admin(tree):
            if schedule.in_begin and Path(tree) == schedule.tree:
                schedule.at("lookup")
            return real_lookup(tree)

        def read_regular(path, *args, **kwargs):
            if schedule.in_begin and Path(path) == schedule.admin / "gitdir":
                schedule.at("backlink")
            return real_read(path, *args, **kwargs)

        def registration_in(common, tree, **kwargs):
            if schedule.in_begin:
                schedule.at("gone-lookup")
            return real_in(common, tree, **kwargs)

        def save(self, **changes):
            real["save"](self, **changes)
            if changes.get("state") == "quarantining":
                schedule.at("moving-in")

        def before(name, point):
            def hooked(self, *args, **kwargs):
                schedule.at(point)
                return real[name](self, *args, **kwargs)
            return hooked

        mp.setattr(rarch.Retirement, "begin", begin)
        mp.setattr(rarch.Retirement, "save", save)
        for name, point in (("lock", "lock"), ("quarantine", "quarantine"), ("archive", "archive"),
                            ("final_check", "final-check"), ("commit", "commit"), ("publish", "publish"),
                            ("reclaim", "reclaim")):
            mp.setattr(rarch.Retirement, name, before(name, point))
        mp.setattr(rarch, "tree_away", tree_away)
        mp.setattr(rgit, "gitfile_admin", gitfile_admin)
        mp.setattr(rfs, "read_regular", read_regular)
        mp.setattr(rgit, "registration_in", registration_in)


schedules = st.dictionaries(st.sampled_from(POINTS), st.sampled_from(ACTIONS), max_size=6)


@settings(max_examples=int(os.environ.get("RETENTION_SWEEP_EXAMPLES", "40")), deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(moves=schedules)
@example(moves={"lookup": "away", "quarantine": "back"})
@example(moves={"lookup": "away", "lock": "back"})
@example(moves={"lookup": "away", "backlink": "back"})
@example(moves={"lookup": "away", "quarantine": "back", "moving-in": "away"})
@example(moves={"backlink": "away"})
@example(moves={"backlink": "away", "lock": "back"})
@example(moves={"begin": "away", "gone-check": "back"})
@example(moves={"begin": "away", "gone-check": "back", "gone-lookup": "away"})
@example(moves={"begin": "away", "gone-check": "back", "lock": "away"})
@example(moves={"begin": "away", "gone-check": "back", "quarantine": "away"})
@example(moves={"lock": "away"})
@example(moves={"lock": "away", "quarantine": "back"})
@example(moves={"quarantine": "away"})
@example(moves={"archive": "away", "final-check": "back", "reclaim": "away"})
def test_no_schedule_of_sweep_moves_loses_or_orphans_a_tree(moves):
    """Over generated schedules of the sweep's moves (away, back, away to stay)
    interleaved with retention's steps: after the pass, and once the sweep
    has put its tree back, the tree is kept intact or removed only with its
    private history in a verified bundle and its files in the archive, and
    nothing is orphaned (`violations`). Then a pass with the sweep quiet
    retires the job, so no schedule wedges it."""
    base = Path(tempfile.mkdtemp(prefix="sweep-race-")).resolve()     # retention works in resolved paths
    try:
        with pytest.MonkeyPatch.context() as mp:
            trust_temporary_directories(mp)
            w = World(base)
            try:
                wt, history = _job_with_private_history(w, "job-s")
                schedule = Schedule(w, wt, moves)
                state, clock = retention.RetentionState(), Clock()
                with pytest.MonkeyPatch.context() as hooks:
                    schedule.install(hooks)
                    first = run(w, state=state, clock=clock)
                trace = (moves, schedule.fired, schedule.sweep.log, first["pruned"], first["deferred"])
                assert violations(w, "job-s", wt, history) == [], trace
                schedule.sweep.back()       # the sweep's next pass puts back a tree it left aside
                assert violations(w, "job-s", wt, history) == [], trace
                if first["pruned"]:
                    return
                clock.advance(LATER)
                second = run(w, state=state, clock=clock)
                assert second["pruned"] == ["job-s"], (trace, second)
                assert violations(w, "job-s", wt, history) == [], trace
            finally:
                w.close()
    finally:
        shutil.rmtree(base, ignore_errors=True)
