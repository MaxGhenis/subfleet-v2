"""The four notes the parallel retention effort (#81) left on #76, each against
real git repositories (2026-10-01):

1. a file whose inode had other links when it was archived was matched without
   its ctime, so a same-size write whose mtime was then put back (`touch -r`,
   `rsync -t`, `cp -p`) passed as unchanged and was deleted;
2. `~/chief-of-staff/bin/worktree-archive-sweep` and `disk-guard` `git worktree
   move` a job's tree to `<parent>/.disk-guard-removing.<name>`, check it again
   there, and move it back when a check fails;
3. `_workspace` allocates `worktrees/<job>` before admission records
   `jobs.worktree`, so a job cancelled in between has a tree no row names;
4. a job whose tree and workdir are both gone, while its repository is not.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet import retention_fs as rfs
from subfleet import retention_git as rgit
from tests.unit.retention_world import Clock, World, git, snapshot, trust_temporary_directories


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


def _write_keeping_mtime(path: Path, data: bytes) -> None:
    """A write in place, then the mtime put back as `touch -r`, `rsync -t` or
    `cp -p` puts it: only the ctime says the file changed."""
    before = os.lstat(path)
    assert len(data) == before.st_size
    with open(path, "r+b") as f:
        f.write(data)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.lstat(path)
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


# --- note 1: a hard-linked file changed after its archive -------------------------------

def _hard_linked_job(w: World, job_id: str) -> tuple[Path, Path]:
    """A job whose tree holds a hard-link pair and a file a package store
    outside the tree links to (pnpm's layout)."""
    wt = w.job(job_id)
    (wt / "pair-a.bin").write_bytes(b"A" * 4096)
    os.link(wt / "pair-a.bin", wt / "pair-b.bin")
    store = w.base / "pnpm-store" / "index.js"
    store.parent.mkdir()
    store.write_bytes(b"S" * 4096)
    os.link(store, wt / "vendored.js")
    return wt, store


@pytest.mark.parametrize("when", ["before-the-final-check", "after-the-commit"])
def test_a_hard_linked_file_rewritten_with_its_mtime_put_back_is_not_deleted(world, monkeypatch, when):
    """Note 1. After the archive, the pair is rewritten through the tree (a
    late writer, as `test_file_written_after_the_final_check_is_kept_in_conflicts`
    has one) and the vendored file through the package store, which then drops
    its own link (`pnpm store prune`). Same size, mtime put back. Neither new
    version is in the archive, so neither may be deleted: before the final
    check the job is put back; after the commit both go to conflicts."""
    w = world
    wt, store = _hard_linked_job(w, "job-hl")
    pair, vendored = b"a" * 4096, b"v" * 4096

    def rewrite(tree: Path) -> None:
        _write_keeping_mtime(tree / "pair-a.bin", pair)
        _write_keeping_mtime(store, vendored)
        store.unlink()

    step = "final_check" if when == "before-the-final-check" else "publish"
    original = getattr(rarch.Retirement, step)

    def hooked(self):
        if step == "final_check":
            rewrite(self.q_worktree)
            return original(self)
        original(self)
        rewrite(self.q_worktree)

    monkeypatch.setattr(rarch.Retirement, step, hooked)
    result = run(w)
    if when == "before-the-final-check":
        assert result["pruned"] == [], result
        assert "changed after archive" in result["deferred"]["job-hl"]
        assert (wt / "pair-a.bin").read_bytes() == pair and (wt / "pair-b.bin").read_bytes() == pair
        assert (wt / "vendored.js").read_bytes() == vendored
        assert w.store.get_job("job-hl") is not None
        return
    assert result["pruned"] == ["job-hl"], result
    kept = w.root / "retention-conflicts" / "job-hl" / "worktree"
    assert (kept / "pair-a.bin").read_bytes() == pair
    assert (kept / "pair-b.bin").read_bytes() == pair
    assert (kept / "vendored.js").read_bytes() == vendored
    events = [json.loads(e["data_json"]) for e in w.store.list_events("job-hl") if e["kind"] == "retention.conflict"]
    assert {k["path"] for k in events[0]["kept"]} == {"pair-a.bin", "pair-b.bin", "vendored.js"}


def test_a_hard_link_whose_ctime_moved_only_because_its_sibling_went_is_deleted(world, monkeypatch):
    """The reason ctime was ignored still holds: unlinking one link of a pair
    moves the other's ctime, mid-deletion or in a deletion an interruption
    left half done. Those bytes are the archived ones, so the second link
    goes, and nothing lands in conflicts."""
    w = world
    wt, store = _hard_linked_job(w, "job-pair")
    cancel = threading.Event()
    original_unlink = os.unlink
    original_reclaim = rarch.Retirement.reclaim

    def unlink_then_cancel(name, *args, **kwargs):
        original_unlink(name, *args, **kwargs)
        if name == "pair-a.bin":
            cancel.set()                         # stop right after the first link of the pair

    def reclaim(self):
        monkeypatch.setattr(rfs.os, "unlink", unlink_then_cancel)
        try:
            return original_reclaim(self)
        finally:
            monkeypatch.setattr(rfs.os, "unlink", original_unlink)

    monkeypatch.setattr(rarch.Retirement, "reclaim", reclaim)
    first = run(w, cancel=cancel)
    assert first["interrupted"] == "cancelled"
    assert (w.root / "retention" / "job-pair" / "worktree" / "pair-b.bin").exists()
    monkeypatch.setattr(rarch.Retirement, "reclaim", original_reclaim)
    second = run(w)
    assert second["reclaimed"] == ["job-pair"], second
    assert not (w.root / "retention-conflicts" / "job-pair").exists()
    assert store.read_bytes() == b"S" * 4096 and os.lstat(store).st_nlink == 1


def _cache_linked_job(w: World, job_id: str) -> tuple[Path, Path, Path]:
    """A bytecode file linked into the tree's own data, which sorts and is
    deleted before `sub/`, and into another environment outside the tree."""
    wt = w.job(job_id)
    (wt / ".gitignore").write_text("__pycache__/\n")
    cache = wt / "sub" / "__pycache__"
    cache.mkdir(parents=True)
    (cache.parent / "mod.py").write_text("pass\n")
    cached = cache / "mod.cpython-312.pyc"
    cached.write_bytes(b"\xcb\x0d\r\n" + b"M" * 4092)
    os.link(cached, wt / "a-installed.py")
    outside = w.base / "other-venv" / "mod.py"
    outside.parent.mkdir()
    os.link(cached, outside)
    return wt, cached, outside


@pytest.mark.parametrize("when", ["only-the-sibling", "before-the-final-check", "after-the-commit"])
def test_a_regenerable_file_whose_ctime_another_link_moved_is_deleted(world, monkeypatch, when):
    """A regenerable file's digest distinguishes an unlinked sibling from a
    write even though its bytes are never copied into the archive."""
    w = world
    wt, cached, outside = _cache_linked_job(w, "job-uv")
    data = cached.read_bytes()
    step = "final_check" if when == "before-the-final-check" else "publish"
    original = getattr(rarch.Retirement, step)

    def link_again(self) -> None:
        os.link(outside, outside.parent / "mod-again.py")         # moves the shared inode's ctime

    def hooked(self):
        if step == "final_check":
            link_again(self)
            return original(self)
        original(self)
        link_again(self)

    if when != "only-the-sibling":
        monkeypatch.setattr(rarch.Retirement, step, hooked)
    result = run(w)
    assert result["pruned"] == ["job-uv"] and result["reclaimed"] == ["job-uv"], result
    manifest = json.loads((w.root / "archive" / "job-uv" / "manifest.json").read_text())
    listed = {e["p"]: e for e in manifest["trees"]["worktree"]["entries"]}
    bytecode = listed["sub/__pycache__/mod.cpython-312.pyc"]
    assert bytecode.get("regen") and "store" not in bytecode
    assert bytecode.get("sha256") == listed["a-installed.py"]["sha256"] == hashlib.sha256(data).hexdigest()
    assert not wt.exists()
    assert not (w.root / "retention-conflicts" / "job-uv").exists()
    assert outside.read_bytes() == data


@pytest.mark.parametrize("when", ["before-the-final-check", "after-the-commit"])
def test_a_regenerable_hardlink_rewritten_with_its_mtime_put_back_is_not_deleted(world, monkeypatch, when):
    """A late rewrite can invalidate a bytecode file's regenerability proof;
    neither a missing bytecopy nor an unchanged mtime authorizes its deletion."""
    w = world
    wt, cached, outside = _cache_linked_job(w, "job-bytecode")
    rel = cached.relative_to(wt)
    changed = b"a report, not Python bytecode\n".ljust(4096, b"R")
    step = "final_check" if when == "before-the-final-check" else "publish"
    original = getattr(rarch.Retirement, step)

    def hooked(self):
        if step == "final_check":
            _write_keeping_mtime(outside, changed)
            return original(self)
        original(self)
        _write_keeping_mtime(outside, changed)

    monkeypatch.setattr(rarch.Retirement, step, hooked)
    result = run(w)
    if when == "before-the-final-check":
        assert result["pruned"] == [] and "changed after archive" in result["deferred"]["job-bytecode"], result
        kept = wt
    else:
        assert result["pruned"] == ["job-bytecode"], result
        kept = w.root / "retention-conflicts" / "job-bytecode" / "worktree"
    assert (kept / rel).read_bytes() == (kept / "a-installed.py").read_bytes() == changed


def test_a_regenerable_file_changed_between_its_proof_and_digest_is_kept(world, monkeypatch):
    """A digest must describe the version whose bytecode proof was checked,
    rather than vouch for invalid replacement bytes read under that proof."""
    w = world
    wt, cached, outside = _cache_linked_job(w, "job-proof")
    (wt / "a-installed.py").unlink()      # only the omitted link and an external link remain
    rel = cached.relative_to(wt)
    changed = b"a report, not Python bytecode\n".ljust(4096, b"R")
    original = rarch._Builder._digest

    def rewrite_then_digest(self, entry, st_, parent, name):
        if entry["p"] == str(rel):
            _write_keeping_mtime(outside, changed)
        return original(self, entry, st_, parent, name)

    monkeypatch.setattr(rarch._Builder, "_digest", rewrite_then_digest)
    result = run(w)
    assert result["pruned"] == [] and "changed before it was read" in result["deferred"]["job-proof"], result
    assert cached.read_bytes() == changed and w.store.get_job("job-proof") is not None


class _Stop(Exception):
    pass


_TARGETS = ("a", "b", "sub/e", "c", "outside", "d")
_change = st.one_of(
    # Mostly the write that only the ctime shows: the same size, the mtime put back.
    st.tuples(st.just("write"), st.sampled_from(_TARGETS), st.binary(min_size=1, max_size=64),
              st.sampled_from((True, True, False)), st.sampled_from((True, True, False))),
    st.tuples(st.just("drop-outside")),
    st.tuples(st.just("link-outside"), st.sampled_from(("a", "d"))),
    st.tuples(st.just("chmod-and-back"), st.sampled_from(_TARGETS)),
    st.tuples(st.just("touch"), st.sampled_from(_TARGETS)),
)


@settings(max_examples=int(os.environ.get("RETENTION_PROP_EXAMPLES", "200")), deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(changes=st.lists(_change, max_size=6), stop_after=st.none() | st.integers(min_value=1, max_value=8))
def test_verified_deletion_unlinks_only_the_bytes_the_archive_holds(changes, stop_after):
    """Property (I2 by content, note 1). A tree holds a hard-link group inside
    it (`a`, `b`, `sub/e`), a file a store outside links to (`c`) and a single
    file (`d`); its manifest records each entry's signature and sha256, as
    the archive does. Then any changes: writes in place, of the same size or
    not, through the tree or the outside link, with the mtime put back or
    not; the outside link dropped, or another added; a chmod and back; a
    touch. Verified deletion runs, maybe interrupted after a few entries and
    resumed. Every entry it unlinked held exactly the bytes the archive
    recorded; everything else is in conflicts with the bytes it had. And an
    entry whose bytes, size, mtime and mode are the archived ones, and which
    had other links, is deleted even though its ctime moved (the reason
    ctime was ignored: unlinking a sibling moves it)."""
    base = Path(tempfile.mkdtemp(prefix="reclaim-prop-")).resolve()
    try:
        tree, store = base / "tree", base / "store"
        (tree / "sub").mkdir(parents=True)
        store.mkdir()
        (tree / "a").write_bytes(b"A" * 16)
        os.link(tree / "a", tree / "b")
        os.link(tree / "a", tree / "sub" / "e")
        (store / "c").write_bytes(b"C" * 16)
        os.link(store / "c", tree / "c")
        (tree / "d").write_bytes(b"D" * 16)
        entries: dict[str, dict] = {}
        fd = rfs.open_dir(tree)
        try:
            for rel, st_, parent, name in rfs.walk(fd):
                entry = {"p": rel, "sig": rfs.signature(st_)}
                if stat.S_ISREG(st_.st_mode):
                    entry["sha256"] = hashlib.sha256((tree / rel).read_bytes()).hexdigest()
                entries[rel] = entry
        finally:
            os.close(fd)

        def where(target: str) -> Path:
            return store / "c" if target == "outside" else tree / target

        for change in changes:
            what = change[0]
            if what == "write":
                _, target, data, same_size, keep_mtime = change
                path = where(target)
                if not path.exists():
                    continue
                before = os.lstat(path)
                if same_size:
                    data = (data * 16)[:before.st_size]
                with open(path, "r+b") as f:
                    f.truncate(0)
                    f.write(data)
                if keep_mtime:
                    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            elif what == "drop-outside":
                (store / "c").unlink(missing_ok=True)
                for extra in store.glob("extra-*"):
                    extra.unlink()
            elif what == "link-outside":
                n = len(list(store.glob("extra-*")))
                os.link(tree / change[1], store / f"extra-{n}")
            elif what == "chmod-and-back":
                path = where(change[1])
                if path.exists():
                    mode = stat.S_IMODE(os.lstat(path).st_mode)
                    os.chmod(path, 0o600 if mode != 0o600 else 0o644)
                    os.chmod(path, mode)
            elif what == "touch":
                path = where(change[1])
                if path.exists():
                    st_ = os.lstat(path)
                    os.utime(path, ns=(st_.st_atime_ns, st_.st_mtime_ns + 1_000_000))
        held = {rel: (tree / rel).read_bytes() for rel, e in entries.items() if e["sig"]["t"] == "f"}
        now = {rel: os.lstat(tree / rel) for rel in held}
        conflicts = base / "conflicts"
        calls = [0]

        def check() -> None:
            calls[0] += 1
            if stop_after is not None and calls[0] > stop_after:
                raise _Stop()

        try:
            rfs.Reclaim(entries, conflicts, "worktree", check=check).run(tree)
        except _Stop:
            rfs.Reclaim(entries, conflicts, "worktree").run(tree)        # resumed
        kept = conflicts / "worktree"
        for rel, data in held.items():
            if (kept / rel).exists():
                assert (kept / rel).read_bytes() == data, rel
                continue
            assert not (tree / rel).exists(), rel
            assert hashlib.sha256(data).hexdigest() == entries[rel]["sha256"], f"{rel} deleted with bytes not archived"
            recorded, st_ = entries[rel]["sig"], now[rel]
            assert (st_.st_size, st_.st_mtime_ns, stat.S_IMODE(st_.st_mode)) == \
                (recorded["size"], recorded["mtime"], recorded["mode"]), rel
        for rel, data in held.items():
            recorded, st_ = entries[rel]["sig"], now[rel]
            same = (hashlib.sha256(data).hexdigest() == entries[rel]["sha256"]
                    and (st_.st_size, st_.st_mtime_ns, stat.S_IMODE(st_.st_mode))
                    == (recorded["size"], recorded["mtime"], recorded["mode"]))
            if same and (recorded["nlink"] > 1 or st_.st_ctime_ns == recorded["ctime"]):
                assert not (kept / rel).exists(), f"{rel} holds the archived bytes but was kept"
            if recorded["nlink"] == 1 and st_.st_ctime_ns != recorded["ctime"]:
                # A file with one link is still matched on its ctime, as
                # before: a chmod and back, an xattr or a flag (none of which
                # the manifest records) keeps it.
                assert (kept / rel).exists(), f"{rel} changed (its ctime) but was deleted"
    finally:
        for directory, dirnames, _ in os.walk(base):
            for name in dirnames:
                os.chmod(Path(directory) / name, 0o700)
        shutil.rmtree(base)


# --- note 2: the sweep's quarantine --------------------------------------------------------

QUARANTINE_PREFIX = ".disk-guard-removing."


def _sweep_away(w: World, tree: Path) -> Path:
    """As `disk-guard`'s `move_worktree`: `git -C <repo> worktree move <tree> <quarantine>`."""
    quarantine = tree.parent / (QUARANTINE_PREFIX + tree.name)
    git(w.repo, "worktree", "move", str(tree), str(quarantine))
    return quarantine


def _sweep_back(w: World, quarantine: Path, tree: Path) -> bool:
    """As `disk-guard`'s `move_back`: refused when the original path exists, and
    git refuses a locked worktree (the sweep never forces)."""
    if os.path.lexists(tree):
        return False
    return git(w.repo, "worktree", "move", str(quarantine), str(tree), check=False) is not None and tree.exists()


def _job_with_private_commit(w: World, job_id: str) -> tuple[Path, str]:
    wt = w.job(job_id)
    (wt / "notes.txt").write_text("untracked work\n")
    (wt / "f.py").write_text("only on this tree's detached HEAD\n")
    git(wt, "add", "f.py")
    git(wt, "commit", "--quiet", "-m", "private")
    return wt, git(wt, "rev-parse", "HEAD")


def _registration_works(tree: Path) -> bool:
    return git(tree, "rev-parse", "--git-dir", check=False) != "" and \
        rgit.registration(tree)[0] is not None


def test_a_tree_the_sweep_holds_in_quarantine_keeps_its_job_until_it_is_back(world):
    """Note 2, the tree away for the whole pass. Retired then, the job's rows
    would go while its tree, moved back afterwards, stayed in `worktrees/` with
    no row naming it: never archived, never retired, never freed. The job waits,
    saying where its tree is; once the tree is back it retires with its
    registration, and its private commit is in its own bundle."""
    w = world
    wt, private = _job_with_private_commit(w, "job-q")
    state, clock = retention.RetentionState(), Clock()
    quarantine = _sweep_away(w, wt)
    first = run(w, state=state, clock=clock)
    assert first["pruned"] == [], first
    assert QUARANTINE_PREFIX in first["deferred"]["job-q"]
    assert w.store.get_job("job-q") is not None and (w.root / "jobs" / "job-q").is_dir()
    assert not (w.root / "retention" / "job-q").exists()
    assert _sweep_back(w, quarantine, wt) and _registration_works(wt)
    clock.advance(rarch.DEFER_CHANGED_S + 1)
    second = run(w, state=state, clock=clock)
    assert second["pruned"] == ["job-q"], second
    assert not wt.exists() and not w.admin("job-q").exists()
    bundle = w.root / "archive" / "job-q" / "commits.bundle"
    fresh = w.base / "fresh-q"
    git(w.base, "clone", "--quiet", str(w.remote), str(fresh))
    git(fresh, "fetch", "--quiet", str(bundle), "+refs/*:refs/r/*")
    assert git(fresh, "cat-file", "-t", private) == "commit"


def test_a_tree_the_sweep_moves_back_before_quarantine_is_not_archived_without_its_registration(world, monkeypatch):
    """Note 2, the tree back between `begin` (which found no registration, the
    tree being away) and `quarantine`. Archived then, the tree would go without
    its registration: the admin directory left behind, naming a tree that is
    gone, and the commit only its HEAD names in no bundle."""
    w = world
    wt, private = _job_with_private_commit(w, "job-qb")
    quarantine = _sweep_away(w, wt)
    original = rarch.Retirement.quarantine

    def back_then_quarantine(self):
        if os.path.lexists(quarantine):
            assert _sweep_back(w, quarantine, wt)
        return original(self)

    monkeypatch.setattr(rarch.Retirement, "quarantine", back_then_quarantine)
    # Bypass the initial discovery guards to exercise the quarantine presence
    # check independently; the persistent admin id normally defers even sooner.
    monkeypatch.setattr(rarch, "tree_away", lambda worktree: None, raising=False)
    monkeypatch.setattr(rgit, "moved_tree", lambda common, tree: None)
    monkeypatch.setattr(rgit, "named_admin", lambda common, tree: None)
    result = run(w)
    assert result["pruned"] == [], result
    assert wt.is_dir() and _registration_works(wt)
    assert w.store.get_job("job-qb") is not None
    assert git(wt, "rev-parse", "HEAD") == private


def test_a_tree_the_sweep_moves_away_after_begin_keeps_its_registration(world, monkeypatch):
    """Note 2, the tree moved away between `begin` (which found it and its
    registration) and `lock`. Retired then, the admin directory would be
    archived and deleted while the tree sat in the sweep's quarantine: a tree
    with a dangling `.git` that the sweep can no longer move back. The job is
    put back, retention's lock removed, and the sweep's move back works."""
    w = world
    wt, private = _job_with_private_commit(w, "job-qa")
    original = rarch.Retirement.lock
    moved = {}

    def away_then_lock(self):
        if not moved:
            moved["to"] = _sweep_away(w, wt)
        return original(self)

    monkeypatch.setattr(rarch.Retirement, "lock", away_then_lock)
    result = run(w)
    assert result["pruned"] == [], result
    assert w.store.get_job("job-qa") is not None
    assert w.admin("job-qa").is_dir() and not (w.admin("job-qa") / "locked").exists()
    assert _sweep_back(w, moved["to"], wt) and _registration_works(wt)
    assert git(wt, "rev-parse", "HEAD") == private


def test_a_tree_moved_back_while_its_job_retires_without_it_keeps_the_job(world, monkeypatch):
    """Note 2, a tree moved where no sibling name shows it (a person's `git
    worktree move` elsewhere) and moved back after `quarantine`, while the
    job archived without it: the final check sees it back and keeps the job,
    so the next pass retires it with its tree and registration."""
    w = world
    wt, private = _job_with_private_commit(w, "job-qm")
    elsewhere = w.base / "elsewhere"
    git(w.repo, "worktree", "move", str(wt), str(elsewhere))
    # Bypass initial discovery to exercise the final presence check independently
    # of the persistent admin id, which normally keeps this job at begin.
    monkeypatch.setattr(rgit, "moved_tree", lambda common, tree: None)
    monkeypatch.setattr(rgit, "named_admin", lambda common, tree: None)
    original = rarch.Retirement.final_check

    def back_then_check(self):
        if elsewhere.exists():
            git(w.repo, "worktree", "move", str(elsewhere), str(wt))
        return original(self)

    monkeypatch.setattr(rarch.Retirement, "final_check", back_then_check)
    state, clock = retention.RetentionState(), Clock()
    first = run(w, state=state, clock=clock)
    assert first["pruned"] == [], first
    assert "came back" in first["deferred"]["job-qm"]
    assert _registration_works(wt) and w.store.get_job("job-qm") is not None
    clock.advance(rarch.DEFER_CHANGED_S + 1)
    second = run(w, state=state, clock=clock)
    assert second["pruned"] == ["job-qm"], second
    assert not wt.exists() and not w.admin("job-qm").exists()


def test_the_survey_keeps_a_job_whose_tree_the_sweep_holds(world):
    """The survey runs the pass's checks (design section 13): a tree away in
    the sweep's quarantine keeps its job there too."""
    from subfleet.retention_survey import survey
    w = world
    wt, _ = _job_with_private_commit(w, "job-qs")
    _sweep_away(w, wt)
    report = survey(w.root, holders=False, sample_throughput=False, budgets={"detached": (0, 0), "turn": (0, 0)})
    assert report["would_retire"]["jobs"] == 0, report["kept"]
    assert any(reason.startswith("tree away") for reason in report["kept"]["jobs_by_reason"])


# --- note 3: an allocated tree no row names ----------------------------------------------

def _allocated_but_unrecorded(w: World, job_id: str, state: str = "cancelled") -> Path:
    """As `_workspace` leaves a job cancelled while it waited after admission
    cut its tree: `worktrees/<job>` exists, `jobs.worktree` is NULL, no attempt."""
    path = w.root / "worktrees" / job_id
    git(w.repo, "worktree", "add", "--quiet", "--detach", str(path), w.head())
    os.chmod(path, 0o700)
    w.store.add_job(job_id=job_id, request_id="req-" + job_id, payload_digest="d", kind="dispatch",
                    workdir=str(w.repo), workdir_head=w.head(), worktree=None, prompt_path="/p",
                    sandbox="workspace-write", state=state)
    (w.root / "jobs" / job_id).mkdir()
    (w.root / "jobs" / job_id / "stdout").write_bytes(b"")
    return path


def test_a_tree_admission_allocated_for_a_job_it_never_recorded_retires_with_it(world):
    """Note 3. Before, such a job retired without its tree, which stayed in
    `worktrees/` for ever with its registration (four live jobs, 2026-09-29).
    It is the job's own allocation, so it is archived and retired with it."""
    from subfleet.retention_survey import survey
    w = world
    path = _allocated_but_unrecorded(w, "job-cut")
    (path / "touched.txt").write_text("a person looked in\n")
    before = snapshot(path)
    report = survey(w.root, holders=False, sample_throughput=False, budgets={"detached": (0, 0), "turn": (0, 0)})
    assert report["worktree_dirs"]["orphans_never_touched"] == 0, report["worktree_dirs"]
    assert report["would_retire"] == {**report["would_retire"], "jobs": 1, "with_worktree": 1}
    result = run(w)
    assert result["pruned"] == ["job-cut"], result
    assert not path.exists() and not w.admin("job-cut").exists()
    manifest = json.loads((w.root / "archive" / "job-cut" / "manifest.json").read_text())
    assert set(manifest["trees"]) == {"worktree", "job", "admin"}
    rarch.restore(w.root, "job-cut")
    assert snapshot(path) == before


def test_a_job_still_to_run_inside_an_unrecorded_allocation_keeps_it(world):
    """The `worktree-in-use` pin covers such a tree as it covers a recorded one."""
    w = world
    path = _allocated_but_unrecorded(w, "job-cut2")
    w.store.add_job(job_id="guest", request_id="guest", payload_digest="d", kind="dispatch",
                    workdir=str(path), prompt_path="/p", sandbox="read-only", state="queued")
    result = run(w, referenced_job_ids=["guest"])
    assert "job-cut2" not in result["pruned"]
    assert result["pin_reasons"]["job-cut2"] == "worktree-in-use"
    assert path.is_dir()


# --- note 4: the workdir is gone ------------------------------------------------------------

SALVAGE_REF = "refs/subfleet-salvage/detached-20260929T120000Z-a1"


def _lane_job(w: World, job_id: str, *, salvage: bool = False, broken_lane: bool = False) -> tuple[Path, Path, str]:
    """A job submitted from a lane checkout (a linked worktree of the
    repository), with a commit only its own tree's HEAD names; then the lane
    checkout is removed and the job's tree deleted, its registration left in
    the repository, as `git worktree prune` has not run."""
    lane = w.base / "lanes" / "lane-1"
    lane.parent.mkdir(exist_ok=True)
    git(w.repo, "worktree", "add", "--quiet", "--detach", str(lane), w.head())
    tree = w.root / "worktrees" / job_id
    git(lane, "worktree", "add", "--quiet", "--detach", str(tree), w.head())
    w.store.add_job(job_id=job_id, request_id="req-" + job_id, payload_digest="d", kind="dispatch",
                    workdir=str(lane), workdir_head=w.head(), worktree=str(tree), prompt_path="/p",
                    sandbox="workspace-write", state="succeeded")
    (w.root / "jobs" / job_id).mkdir()
    (w.root / "jobs" / job_id / "stdout").write_bytes(b"done")
    (tree / "f.py").write_text("only on this tree's HEAD\n")
    git(tree, "add", "f.py")
    git(tree, "commit", "--quiet", "-m", "private")
    private = git(tree, "rev-parse", "HEAD")
    if salvage:
        # Named as the daemon names it (`salvage.py`): branch, reservation second,
        # attempt; recorded with the digest the daemon records (`Daemon._salvage`).
        w.attempt(job_id)
        git(w.repo, "update-ref", SALVAGE_REF, private)
        w.store.add_artifact(f"{job_id}/a1", "salvage", SALVAGE_REF, rarch.salvage_digest(private), 0)
    if broken_lane:
        lane_reg, why = rgit.registration(lane)
        assert lane_reg is not None and why is None
        shutil.rmtree(lane_reg.admin)       # directory and .git remain, but git cannot resolve the workdir
    else:
        git(w.repo, "worktree", "remove", "--force", str(lane))
    shutil.rmtree(tree)
    assert lane.exists() == broken_lane and (w.repo / ".git" / "worktrees" / job_id).is_dir()
    return tree, lane, private


def _another_job_from(w: World, job_id: str, workdir: Path) -> None:
    """A job of the same repository whose workdir is still there (most live
    repositories have one): pinned, so it stays."""
    w.store.add_job(job_id=job_id, request_id="req-" + job_id, payload_digest="d", kind="dispatch",
                    workdir=str(workdir), workdir_head=w.head(), prompt_path="/p", sandbox="read-only",
                    state="succeeded")
    (w.root / "jobs" / job_id).mkdir()


def _in_bundle(w: World, job_id: str, commit: str) -> bool:
    bundle = w.root / "archive" / job_id / "commits.bundle"
    if not bundle.exists():
        return False
    fresh = w.base / f"fresh-{job_id}"
    git(w.base, "clone", "--quiet", str(w.remote), str(fresh))
    git(fresh, "fetch", "--quiet", str(bundle), "+refs/*:refs/r/*")
    return git(fresh, "cat-file", "-t", commit, check=False) == "commit"


def test_a_gone_workdirs_job_finds_its_registration_through_the_repositories_retention_knows(world):
    """Note 4, no salvage. Before, the registration was not found, the archive
    had no anchor and no bundle, and the registration stayed behind, naming a
    tree that is gone, until a prune and a gc dropped the commit only its HEAD
    named. Found through a repository another job's workdir is in, it is
    anchored, bundled and removed, as for any job whose tree is gone."""
    from subfleet.retention_survey import survey
    w = world
    tree, lane, private = _lane_job(w, "job-lane")
    _another_job_from(w, "job-other", w.repo)
    report = survey(w.root, holders=False, sample_throughput=False, budgets={"detached": (0, 0), "turn": (0, 0)})
    assert report["would_retire"]["jobs"] == 2, report["kept"]          # job-other is pinned only in the pass
    result = run(w, referenced_job_ids=["job-other"])
    assert "job-lane" in result["pruned"], result
    assert not (w.repo / ".git" / "worktrees" / "job-lane").exists()
    assert _in_bundle(w, "job-lane", private)


def test_a_gone_workdirs_job_finds_its_repository_from_the_workdirs_nearest_ancestor(world):
    """Note 4, a workdir that was a folder of the checkout: its nearest
    existing ancestor is in the repository."""
    w = world
    sub = w.repo / "pkg" / "deep"
    sub.mkdir(parents=True)
    tree = w.root / "worktrees" / "job-sub"
    git(w.repo, "worktree", "add", "--quiet", "--detach", str(tree), w.head())
    w.store.add_job(job_id="job-sub", request_id="req-sub", payload_digest="d", kind="dispatch", workdir=str(sub),
                    workdir_head=w.head(), worktree=str(tree), prompt_path="/p", sandbox="workspace-write",
                    state="succeeded")
    (w.root / "jobs" / "job-sub").mkdir()
    (tree / "f.py").write_text("x\n")
    git(tree, "add", "f.py")
    git(tree, "commit", "--quiet", "-m", "private")
    private = git(tree, "rev-parse", "HEAD")
    shutil.rmtree(w.repo / "pkg")
    shutil.rmtree(tree)
    result = run(w)
    assert result["pruned"] == ["job-sub"], result
    assert not (w.repo / ".git" / "worktrees" / "job-sub").exists()
    assert _in_bundle(w, "job-sub", private)


@pytest.mark.parametrize("salvage", [False, True], ids=["private-head", "with-salvage"])
def test_a_broken_present_workdir_recovers_its_jobs_registration_and_bundle(world, salvage):
    """A linked workdir whose own admin was removed is still a directory.
    Its dangling gitfile identifies the source of the gone job's private HEAD,
    which must be anchored and bundled before its rows or registration retire.
    """
    from subfleet.retention_survey import survey
    w = world
    tree, lane, private = _lane_job(w, "job-broken-lane", salvage=salvage, broken_lane=True)
    assert lane.is_dir() and (lane / ".git").is_file() and rgit.common_dir(lane) is None
    report = survey(w.root, holders=False, sample_throughput=False, budgets={"detached": (0, 0), "turn": (0, 0)})
    assert report["would_retire"]["jobs"] == 1 and report["kept"]["jobs_by_reason"] == {}, report
    result = run(w)
    assert result["pruned"] == ["job-broken-lane"], result
    assert _in_bundle(w, "job-broken-lane", private)
    assert not w.admin("job-broken-lane").exists()


def test_a_broken_present_workdir_checks_the_salvage_commit_its_row_recorded(world):
    """A recovered registration does not make a replaced salvage ref valid."""
    from subfleet.retention_survey import survey
    w = world
    tree, lane, private = _lane_job(w, "job-broken-ref", salvage=True, broken_lane=True)
    git(w.repo, "update-ref", SALVAGE_REF, w.head())
    report = survey(w.root, holders=False, sample_throughput=False,
                    budgets={"detached": (0, 0), "turn": (0, 0)})
    assert report["would_retire"]["jobs"] == 0, report
    assert report["kept"]["jobs_by_reason"] == {"salvage not archivable": 1}, report
    result = run(w)
    assert result["pruned"] == [] and "not the commit its row recorded" in result["deferred"]["job-broken-ref"], result
    assert w.store.get_job("job-broken-ref") is not None and w.admin("job-broken-ref").is_dir()


def test_a_gone_workdirs_job_with_salvage_retires_with_its_salvage_bundled(world):
    """Note 4, with salvage. Before, the salvage ref could not be resolved
    without the repository, and the job was kept a day at a time for ever as
    `salvage not archivable` (55 live jobs, 2026-09-30)."""
    w = world
    tree, lane, private = _lane_job(w, "job-ls", salvage=True)
    _another_job_from(w, "job-other", w.repo)
    result = run(w, referenced_job_ids=["job-other"])
    assert "job-ls" in result["pruned"], result
    manifest = json.loads((w.root / "archive" / "job-ls" / "manifest.json").read_text())
    assert manifest["git"]["salvage_in_anchor"] == [private]
    assert _in_bundle(w, "job-ls", private)


def test_a_gone_workdirs_job_whose_registration_was_pruned_is_found_by_its_salvage_refs(world):
    """Note 4, the registration pruned too (`git worktree prune`): the
    repository that holds every salvage ref the job's rows name is its own."""
    w = world
    tree, lane, private = _lane_job(w, "job-pr", salvage=True)
    git(w.repo, "worktree", "prune")
    assert not (w.repo / ".git" / "worktrees" / "job-pr").exists()
    _another_job_from(w, "job-other", w.repo)
    result = run(w, referenced_job_ids=["job-other"])
    assert "job-pr" in result["pruned"], result
    manifest = json.loads((w.root / "archive" / "job-pr" / "manifest.json").read_text())
    assert manifest["git"]["salvage_in_anchor"] == [private]
    assert _in_bundle(w, "job-pr", private)


def test_a_repository_that_does_not_hold_the_jobs_salvage_is_not_taken_for_its_own(world, tmp_path):
    """Another repository retention knows, without the job's salvage refs, is
    not the job's: the job is kept, saying its repository was not found."""
    w = world
    _lane_job(w, "job-x", salvage=True)
    git(w.repo, "worktree", "prune")
    git(w.repo, "update-ref", "-d", SALVAGE_REF)
    other = tmp_path / "other-repo"
    git(tmp_path, "init", "--quiet", str(other))
    _another_job_from(w, "job-other", other)
    result = run(w, referenced_job_ids=["job-other"])
    assert result["pruned"] == [], result
    assert "repository not found" in result["deferred"]["job-x"]


def _repository_with_the_same_salvage_ref(tmp_path: Path, name: str) -> tuple[Path, str]:
    """Another project whose job was reserved in the same second: its salvage
    ref has the same name and names its own commit."""
    other = tmp_path / name
    git(tmp_path, "init", "--quiet", "-b", "main", str(other))
    (other / "b.txt").write_text("B\n")
    git(other, "add", "b.txt")
    git(other, "commit", "--quiet", "-m", "B")
    (other / "b.txt").write_text("B's salvage\n")
    git(other, "commit", "--quiet", "-am", "B's salvage")
    commit = git(other, "rev-parse", "HEAD")
    git(other, "update-ref", SALVAGE_REF, commit)
    return other, commit


@pytest.mark.parametrize("own_repository_known", [False, True], ids=["only-the-other", "both"])
def test_a_salvage_ref_of_the_same_name_in_another_repository_is_not_the_jobs(world, tmp_path, own_repository_known):
    """Review of the note-4 fix: salvage refs are named by branch, second and
    attempt, so two jobs of different repositories reserved in the same second
    share a name. A repository is the job's only when its ref names the commit
    the row recorded: the other one is never taken, and nothing is written
    into it; with the job's own repository known too, the job retires with its
    own salvage."""
    w = world
    _, _, private = _lane_job(w, "job-a", salvage=True)
    git(w.repo, "worktree", "prune")
    other, theirs = _repository_with_the_same_salvage_ref(tmp_path, "a-other-repo")     # listed first
    _another_job_from(w, "job-b", other)
    pinned = ["job-b"]
    if own_repository_known:
        _another_job_from(w, "job-own", w.repo)
        pinned.append("job-own")
    result = run(w, referenced_job_ids=pinned)
    assert not git(other, "for-each-ref", "refs/subfleet-archive/"), "an anchor was written into another repository"
    if not own_repository_known:
        assert result["pruned"] == [], result
        assert "repository not found" in result["deferred"]["job-a"]
        return
    assert result["pruned"] == ["job-a"], result
    manifest = json.loads((w.root / "archive" / "job-a" / "manifest.json").read_text())
    assert [s["commit"] for s in manifest["salvage"]] == [private] and private != theirs
    assert _in_bundle(w, "job-a", private)


def test_an_unreadable_registration_of_another_job_does_not_stop_the_search(world, tmp_path):
    """Review of the note-4 fix: listing the repositories the store's jobs are
    in reads every tree's registration; one another job's gitfile names under
    an unreadable directory is skipped, not an error for every job searched."""
    w = world
    tree, lane, private = _lane_job(w, "job-lane")
    _another_job_from(w, "job-other", w.repo)
    unreadable = w.job("job-y")
    locked = tmp_path / "locked"
    (locked / "admin").mkdir(parents=True)
    (unreadable / ".git").write_text(f"gitdir: {locked / 'admin'}\n")
    os.chmod(locked, 0)
    try:
        result = run(w, referenced_job_ids=["job-other", "job-y"])
    finally:
        os.chmod(locked, 0o700)
    assert "job-lane" in result["pruned"], result
    assert _in_bundle(w, "job-lane", private)


def test_a_gone_tree_that_comes_back_inside_quarantine_is_not_moved_in(world, monkeypatch):
    """Review of the note-2 fix: a tree gone at `begin` and moved back between
    `quarantine`'s presence check and its renames is never renamed into
    quarantine (it would be archived without its registration); the final
    check sees it back and keeps the job, and the next pass retires it with
    its registration."""
    w = world
    wt, private = _job_with_private_commit(w, "job-t")
    elsewhere = w.base / "elsewhere"
    git(w.repo, "worktree", "move", str(wt), str(elsewhere))
    # Exercise the return inside quarantine independently of the initial guards.
    monkeypatch.setattr(rgit, "moved_tree", lambda common, tree: None)
    monkeypatch.setattr(rgit, "named_admin", lambda common, tree: None)
    original_save = rarch.Retirement.save

    def save(self, **changes):
        original_save(self, **changes)
        if changes.get("state") == "quarantining" and elsewhere.exists():
            git(w.repo, "worktree", "move", str(elsewhere), str(wt))     # back, right after the check

    monkeypatch.setattr(rarch.Retirement, "save", save)
    state, clock = retention.RetentionState(), Clock()
    first = run(w, state=state, clock=clock)
    assert first["pruned"] == [], first
    assert "came back" in first["deferred"]["job-t"]
    assert _registration_works(wt) and git(wt, "rev-parse", "HEAD") == private
    clock.advance(rarch.DEFER_CHANGED_S + 1)
    second = run(w, state=state, clock=clock)
    assert second["pruned"] == ["job-t"], second
    assert not w.admin("job-t").exists()
    manifest = json.loads((w.root / "archive" / "job-t" / "manifest.json").read_text())
    assert manifest["git"]["admin"] and "worktree" in manifest["trees"]


def test_a_job_whose_repository_cannot_be_found_says_so(world):
    """Note 4, nothing names the repository any more: a job with salvage is
    kept (C-8.4), with a reason that says what is missing."""
    from subfleet.retention_survey import survey
    w = world
    _lane_job(w, "job-lost", salvage=True)
    report = survey(w.root, holders=False, sample_throughput=False, budgets={"detached": (0, 0), "turn": (0, 0)})
    assert report["kept"]["jobs_by_reason"] == {"salvage not archivable": 1}, report["kept"]
    result = run(w)
    assert result["pruned"] == [], result
    reason = result["deferred"]["job-lost"]
    assert "salvage not archivable" in reason and "repository not found" in reason


@pytest.mark.parametrize("workdir_gone", [False, True])
@pytest.mark.parametrize("recorded", [False, True])
def test_a_registered_tree_moved_elsewhere_keeps_its_job(world, workdir_gone, recorded):
    """A registration id survives a move outside the sweep's sibling path.
    Keep its rows while the checkout exists, including a missing workdir;
    after it is moved back, archive the private HEAD and untracked work.
    """
    from subfleet.retention_survey import survey
    w = world
    wt, private = _job_with_private_commit(w, "job-moved")
    elsewhere = w.base / "some-other-directory"
    git(w.repo, "worktree", "move", str(wt), str(elsewhere))
    if not recorded:
        w.store.connection.execute("UPDATE jobs SET worktree=NULL WHERE job_id=?", ("job-moved",))
    if workdir_gone:
        w.store.connection.execute("UPDATE jobs SET workdir=? WHERE job_id=?", (str(w.base / "gone-lane"), "job-moved"))
        _another_job_from(w, "job-other", w.repo)
    pins = ["job-other"] if workdir_gone else []
    report = survey(w.root, holders=False, sample_throughput=False, budgets={"detached": (0, 0), "turn": (0, 0)})
    assert any(reason.startswith("tree away") for reason in report["kept"]["jobs_by_reason"]), report
    state, clock = retention.RetentionState(), Clock()
    first = run(w, referenced_job_ids=pins, state=state, clock=clock)
    assert "job-moved" not in first["pruned"] and "registered at" in first["deferred"]["job-moved"], first
    assert w.store.get_job("job-moved") is not None and _registration_works(elsewhere)
    git(w.repo, "worktree", "move", str(elsewhere), str(wt))
    clock.advance(rarch.DEFER_CHANGED_S + 1)
    second = run(w, referenced_job_ids=pins, state=state, clock=clock)
    assert second["pruned"] == ["job-moved"], second
    assert _in_bundle(w, "job-moved", private)


def test_an_unrecorded_allocation_in_the_sweeps_quarantine_keeps_its_job(world):
    w = world
    wt = _allocated_but_unrecorded(w, "job-unrecorded-away")
    away = _sweep_away(w, wt)
    state, clock = retention.RetentionState(), Clock()
    first = run(w, state=state, clock=clock)
    assert first["pruned"] == [] and "tree away" in first["deferred"]["job-unrecorded-away"], first
    assert _sweep_back(w, away, wt) and _registration_works(wt)
    clock.advance(rarch.DEFER_CHANGED_S + 1)
    second = run(w, state=state, clock=clock)
    assert second["pruned"] == ["job-unrecorded-away"], second
    assert not wt.exists() and not w.admin("job-unrecorded-away").exists()


def test_an_undiscovered_repository_keeps_the_job_without_salvage_until_it_can_be_bundled(world):
    """An undiscovered registration can hold a private HEAD without salvage.
    Keeping the rows allows a later pass to discover and bundle it, rather
    than leaving that HEAD to git's eventual prune/gc without an archive.
    """
    from subfleet.retention_survey import survey
    w = world
    _, _, private = _lane_job(w, "job-undiscovered")
    report = survey(w.root, holders=False, sample_throughput=False, budgets={"detached": (0, 0), "turn": (0, 0)})
    assert report["would_retire"]["jobs"] == 0, report
    assert report["kept"]["jobs_by_reason"] == {"repository not found": 1}, report
    state, clock = retention.RetentionState(), Clock()
    first = run(w, state=state, clock=clock)
    assert first["pruned"] == [] and "repository not found" in first["deferred"]["job-undiscovered"], first
    assert w.store.get_job("job-undiscovered") is not None and w.admin("job-undiscovered").is_dir()
    _another_job_from(w, "job-other", w.repo)
    clock.advance(rarch.DEFER_PERMANENT_S + 1)
    second = run(w, referenced_job_ids=["job-other"], state=state, clock=clock)
    assert second["pruned"] == ["job-undiscovered"], second
    assert _in_bundle(w, "job-undiscovered", private)
    assert not w.admin("job-undiscovered").exists()


def test_an_unrecorded_allocation_whose_tree_is_gone_bundles_its_registration(world):
    w = world
    wt = _allocated_but_unrecorded(w, "job-unrecorded-gone")
    (wt / "f.py").write_text("private work\n")
    git(wt, "add", "f.py")
    git(wt, "commit", "--quiet", "-m", "private")
    private = git(wt, "rev-parse", "HEAD")
    shutil.rmtree(wt)
    result = run(w)
    assert result["pruned"] == ["job-unrecorded-gone"], result
    assert _in_bundle(w, "job-unrecorded-gone", private)
    assert not w.admin("job-unrecorded-gone").exists()


def test_an_unrecorded_allocation_returning_during_source_lookup_keeps_its_job(world, monkeypatch):
    w = world
    wt = _allocated_but_unrecorded(w, "job-unrecorded-return")
    elsewhere = w.base / "elsewhere"
    git(w.repo, "worktree", "move", str(wt), str(elsewhere))
    original = rarch.source_of_gone_tree

    def source(*args, **kwargs):
        if elsewhere.exists():
            git(w.repo, "worktree", "move", str(elsewhere), str(wt))
        return original(*args, **kwargs)

    monkeypatch.setattr(rarch, "source_of_gone_tree", source)
    state, clock = retention.RetentionState(), Clock()
    first = run(w, state=state, clock=clock)
    assert first["pruned"] == [] and "came back" in first["deferred"]["job-unrecorded-return"], first
    assert _registration_works(wt) and w.store.get_job("job-unrecorded-return") is not None
    clock.advance(rarch.DEFER_CHANGED_S + 1)
    second = run(w, state=state, clock=clock)
    assert second["pruned"] == ["job-unrecorded-return"], second
    assert not wt.exists() and not w.admin("job-unrecorded-return").exists()
