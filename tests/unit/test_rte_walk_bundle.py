"""D-SURVEY-EACCES1 and D-RET-STALE-LOCK1 (C-8.4, C-13.4).

D-SURVEY-EACCES1: a directory at mode 0644 (owner read, no search) can be
opened and listed, but each entry's lstat needs search: `walk` raised a bare
PermissionError and `retention survey` died on `inner.bin`. Every permission
refusal in `walk` is `TreeError("unreadable")` now (invariant 1; its property
test over generated trees is in test_rte_tmpdir_acl.py). The survey finishes
and lists the job with the reason; retention defers the job with a reason
that names the directory; nothing changes the tree's modes.

D-RET-STALE-LOCK1: git leaves `commits.bundle.tmp.lock` when it is killed,
and refuses every later `bundle create` while it exists. The bundle step
removes it with `commits.bundle.tmp` (invariant 4: under the job's lease the
bundle step succeeds whichever of the two an earlier attempt left), once the
lock is `BUNDLE_LOCK_STALE_S` (10 minutes) old by its modification time; a
younger lock may be a running git's, and defers the job with nothing removed
(the hub's ruling). The wall clock is fixed in these tests.
"""
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet import retention_fs as rfs
from subfleet import retention_git as rgit
from subfleet import retention_survey
from tests.unit.retention_world import Clock, World, trust_temporary_directories


@pytest.fixture
def world(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    w = World(tmp_path)
    blocked: list[Path] = []
    w.blocked = blocked
    yield w
    for path in blocked:                     # restored even when the test failed
        if os.path.lexists(path):
            os.chmod(path, 0o700)
    w.close()


def run(w: World, **kwargs):
    kwargs.setdefault("max_jobs", 0)
    kwargs.setdefault("max_bytes", 0)
    kwargs.setdefault("holders", lambda watches, **_: {})
    kwargs.setdefault("measure_s", 0)        # a pool over its count needs no sizes
    return retention.maintenance(w.store, w.root, **kwargs)


def unsearchable(w: World, parent: Path) -> Path:
    """The live shape: a directory at 0644 (a file's mode) holding `inner.bin`."""
    blob = parent / "blob"
    blob.mkdir()
    (blob / "inner.bin").write_bytes(b"cloned in place of a file")
    w.blocked.append(blob)
    os.chmod(blob, 0o644)
    return blob


# --- walk ------------------------------------------------------------------------------------

def test_walk_maps_the_lstat_refused_in_a_listed_but_unsearchable_directory(tmp_path):
    """Mutant 1's witness: the per-entry lstat's PermissionError is
    `TreeError("unreadable")` with the entry's path, as `open_dir` and the
    listing map theirs."""
    root = tmp_path / "tree"
    root.mkdir()
    blob = root / "blob"
    blob.mkdir()
    (blob / "inner.bin").write_bytes(b"x")
    os.chmod(blob, 0o644)
    fd = os.open(root, rfs.O_DIR)
    try:
        with pytest.raises(rfs.TreeError) as refused:
            for _ in rfs.walk(fd):
                pass
    finally:
        os.close(fd)
        os.chmod(blob, 0o700)
    assert refused.value.reason == "unreadable"
    assert refused.value.detail == "blob/inner.bin: Permission denied"
    assert stat.S_IMODE(blob.lstat().st_mode) == 0o700       # restored by the test, never by walk


def test_walk_still_maps_an_unopenable_and_an_unlistable_directory(tmp_path):
    for mode in (0o000, 0o300, 0o100):
        root = tmp_path / f"tree-{mode:o}"
        (root / "d").mkdir(parents=True)
        (root / "d" / "f").write_text("x")
        os.chmod(root / "d", mode)
        fd = os.open(root, rfs.O_DIR)
        try:
            with pytest.raises(rfs.TreeError) as refused:
                list(rfs.walk(fd))
        finally:
            os.close(fd)
            os.chmod(root / "d", 0o700)
        assert refused.value.reason == "unreadable", mode


# --- the survey and sample -------------------------------------------------------------------

def test_the_survey_finishes_and_lists_the_job_with_the_reason(world):
    """The live crash: `retention survey` died with `[Errno 13] Permission
    denied: 'inner.bin'`. It finishes, lists the job, and keeps it with the
    reason; a job directory with the same shape is listed too."""
    w = world
    wt = w.job("job-wt")
    unsearchable(w, wt)
    w.job("job-dir")
    unsearchable(w, w.root / "jobs" / "job-dir")
    report = retention_survey.survey(w.root, holders=False, sample_throughput=False,
                                     budgets={"detached": (0, 0), "turn": (0, 0)})
    rows = {r["job_id"]: r for r in [*report.get("candidates", []), *report.get("kept_detail", [])]
            if isinstance(r, dict) and "job_id" in r}
    text = json.dumps(report, default=str)
    assert "job-wt" in text and "job-dir" in text
    for job_id in ("job-wt", "job-dir"):
        assert job_id in rows, (job_id, sorted(rows))
        issue = rows[job_id].get("issue") or ""
        assert "unreadable" in issue and "blob/inner.bin" in issue, rows[job_id]
        assert rows[job_id].get("kept") == "walk", rows[job_id]


def test_sample_keeps_a_job_whose_tree_it_cannot_walk(world):
    w = world
    wt = w.job("job-s")
    unsearchable(w, wt)
    result = retention_survey.sample(w.root, 5)
    assert "job-s" in result["kept_in_sample"], result["kept_in_sample"]
    assert "unreadable" in result["kept_in_sample"]["job-s"] and "blob" in result["kept_in_sample"]["job-s"]
    assert result["retirable_in_sample"] == 0


# --- retention defers, naming the directory, and edits nothing ---------------------------------

@pytest.mark.parametrize("where", ["worktree", "job"])
def test_retention_defers_the_job_naming_the_directory_and_changes_no_mode(world, where):
    w = world
    wt = w.job("job-u")
    blob = unsearchable(w, wt if where == "worktree" else w.root / "jobs" / "job-u")
    result = run(w)
    assert result["pruned"] == []
    reason = result["deferred"].get("job-u", "")
    assert reason.startswith("unreadable") and f"{where}: blob/inner.bin" in reason, result["deferred"]
    assert stat.S_IMODE(blob.lstat().st_mode) == 0o644, "retention never edits the tree's modes"
    assert wt.is_dir() and w.store.get_job("job-u") is not None


def test_an_admin_directory_walk_refusal_is_a_deferral_not_an_escape(world, monkeypatch):
    """`admin_objects` walks the admin directory; its TreeError (now also one
    for a directory listed but not searched) defers the job instead of
    escaping the pass, which catches Defer, not TreeError."""
    w = world
    w.job("job-a")

    def refused(*args, **kwargs):
        raise rfs.TreeError("unreadable", "logs/x: Permission denied")

    monkeypatch.setattr(rgit, "admin_objects", refused)
    result = run(w)
    assert result["deferred"].get("job-a", "").startswith("unreadable: admin: logs/x"), result["deferred"]


# --- the bundle lock ---------------------------------------------------------------------------

#: The fixed wall clock the bundle step reads (`rarch._wall_now`); lock times are set from it.
WALL = 2_000_000_000.0


def killed_bundle(left: str, lock_age: float):
    """`create_bundle` as an attempt killed mid-bundle leaves it: the temporary
    bundle, git's lock (`lock_age` seconds old by `WALL`), both or neither."""
    def killed(git_dir, path, refs, held, **kwargs):
        if left in ("tmp", "both"):
            Path(path).write_bytes(b"# v2 git bundle\npartial")
        if left in ("lock", "both"):
            lock = Path(str(path) + ".lock")
            lock.write_bytes(b"x" * 254)
            os.utime(lock, (WALL - lock_age, WALL - lock_age))
        raise rgit.GitError("git bundle create timed out after 3600 s")
    return killed


def leftovers(archive: Path) -> dict[str, tuple[bytes, int, int]]:
    """Bytes, mode and mtime (ns) of what the killed attempt left."""
    out = {}
    for name in ("commits.bundle.tmp", "commits.bundle.tmp.lock"):
        path = archive / name
        if os.path.lexists(path):
            st = path.lstat()
            out[name] = (path.read_bytes(), st.st_mode, st.st_mtime_ns)
    return out


def bundle_leftover_builder(tmp_path, monkeypatch):
    """Exercise the leftover decision without building a complete archive."""
    builder = rarch._Builder.__new__(rarch._Builder)
    builder.r = SimpleNamespace(job_id="job-lock")
    notes = []
    monkeypatch.setattr(builder, "_note", notes.append)
    monkeypatch.setattr(rarch, "_wall_now", lambda: WALL)
    temporary = tmp_path / "commits.bundle.tmp"
    temporary.write_bytes(b"partial bundle")
    return builder, temporary, notes


def test_a_bundle_lock_dated_seventy_years_ahead_is_young(tmp_path, monkeypatch):
    """R2's witness: clock skew cannot make an unfinished bundle stale."""
    builder, temporary, notes = bundle_leftover_builder(tmp_path, monkeypatch)
    lock = temporary.with_name(temporary.name + ".lock")
    lock.write_bytes(b"unfinished bundle lock")
    future = WALL + 70 * 365 * 86400
    os.utime(lock, (future, future))
    before = leftovers(tmp_path)
    with pytest.raises(rarch.Defer) as refused:
        builder._clear_bundle_leftovers(temporary)
    assert refused.value.reason == "bundle lock in use"
    assert lock.name in refused.value.detail
    assert leftovers(tmp_path) == before
    assert notes == []


def test_a_bundle_lock_just_below_the_stale_boundary_reports_a_young_age(tmp_path, monkeypatch, caplog):
    builder, temporary, notes = bundle_leftover_builder(tmp_path, monkeypatch)
    lock = temporary.with_name(temporary.name + ".lock")
    lock.write_bytes(b"unfinished bundle lock")
    os.utime(lock, (WALL - 599.999, WALL - 599.999))
    before = leftovers(tmp_path)
    with caplog.at_level(logging.WARNING, logger=rarch.__name__), pytest.raises(rarch.Defer) as refused:
        builder._clear_bundle_leftovers(temporary)
    assert "is 599 s old, under 600 s" in refused.value.detail
    assert "599 s old" in caplog.text
    assert leftovers(tmp_path) == before
    assert notes == []


def test_a_stale_folder_shaped_bundle_lock_defers_and_preserves_leftovers(tmp_path, monkeypatch, caplog):
    builder, temporary, notes = bundle_leftover_builder(tmp_path, monkeypatch)
    lock = temporary.with_name(temporary.name + ".lock")
    lock.mkdir()
    (lock / "unexpected").write_bytes(b"keep this")
    os.utime(lock, (WALL - 600, WALL - 600))
    before = lock.stat()
    with caplog.at_level(logging.WARNING, logger=rarch.__name__), pytest.raises(rarch.Defer) as refused:
        builder._clear_bundle_leftovers(temporary)
    assert refused.value.reason == "bundle lock in use"
    assert f"{lock.name} is a directory" in refused.value.detail
    assert "600 s old" in refused.value.detail
    assert f"({before.st_size} bytes)" in refused.value.detail
    assert temporary.read_bytes() == b"partial bundle"
    assert (lock / "unexpected").read_bytes() == b"keep this"
    after = lock.stat()
    assert (after.st_mode, after.st_mtime_ns) == (before.st_mode, before.st_mtime_ns)
    assert "removes" not in caplog.text
    assert notes == []


# One listing a pass (D-HOLDERS-LSOF1): a retirement finishes in the pass after its
# archive, as the daemon's catch-up runs it; see `catch_up` in tests/conftest.py.
@pytest.mark.retention_catch_up
@pytest.mark.parametrize("left", ["neither", "tmp", "lock", "both"])
def test_the_bundle_step_succeeds_whatever_an_earlier_attempt_left(world, monkeypatch, left):
    """Invariant 4, and mutant 2's witness: an attempt killed during `git bundle
    create` (as a timeout, a cancel or a reboot does) leaves the lock, the
    temporary bundle, both or neither. The next attempt under the job's lease
    removes them and retires the job; the lock's size and age are noted. The
    lock here is exactly `BUNDLE_LOCK_STALE_S` old by the fixed clock: the
    old side of the line includes it."""
    w = world
    w.job("job-b")
    real_create = rgit.create_bundle
    monkeypatch.setattr(rarch, "_wall_now", lambda: WALL)
    monkeypatch.setattr(rgit, "create_bundle", killed_bundle(left, rarch._Builder.BUNDLE_LOCK_STALE_S))
    state, clock = retention.RetentionState(), Clock()
    first = run(w, state=state, clock=clock)
    assert first["pruned"] == [] and first["deferred"]["job-b"].startswith("error")
    archive = w.root / "retention" / "job-b" / "archive"
    assert (archive / "commits.bundle.tmp.lock").exists() == (left in ("lock", "both"))
    notes = []
    real_note = rarch._Builder._note
    monkeypatch.setattr(rarch._Builder, "_note", lambda self, record: (notes.append(record), real_note(self, record)))
    monkeypatch.setattr(rgit, "create_bundle", real_create)
    clock.advance(2 * rarch.DEFER_ERROR_S)
    second = run(w, state=state, clock=clock)
    assert second["pruned"] == ["job-b"], second["deferred"]
    locks = [n for n in notes if n.get("k") == "bundle-lock"]
    if left in ("lock", "both"):
        assert len(locks) == 1 and locks[0]["size"] == 254, locks
        assert locks[0]["age_s"] == rarch._Builder.BUNDLE_LOCK_STALE_S and locks[0]["removed_at"], locks
    else:
        assert locks == []
    assert rarch.check_archive(w.root, "job-b")["ok"]


@pytest.mark.retention_catch_up
def test_a_bundle_lock_under_ten_minutes_old_defers_and_changes_nothing(world, monkeypatch, caplog):
    """The hub's ruling on D-RET-STALE-LOCK1, both sides of the line on a fixed
    clock: a lock 599 s old may still be a running git's (one a hard-killed
    daemon left keeps writing it, so its time stays fresh). The pass defers
    with a reason that names the lock and its age, and the lock and the
    temporary bundle keep their bytes, mode and time; size and age are
    logged. One second later the lock is 600 s old and goes; the job retires.
    The age mutant's witness."""
    w = world
    w.job("job-y")
    real_create = rgit.create_bundle
    wall = [WALL]
    monkeypatch.setattr(rarch, "_wall_now", lambda: wall[0])
    monkeypatch.setattr(rgit, "create_bundle", killed_bundle("both", 599))
    state, clock = retention.RetentionState(), Clock()
    first = run(w, state=state, clock=clock)
    assert first["deferred"]["job-y"].startswith("error"), first["deferred"]
    archive = w.root / "retention" / "job-y" / "archive"
    before = leftovers(archive)
    assert sorted(before) == ["commits.bundle.tmp", "commits.bundle.tmp.lock"]
    notes = []
    real_note = rarch._Builder._note
    monkeypatch.setattr(rarch._Builder, "_note", lambda self, record: (notes.append(record), real_note(self, record)))
    monkeypatch.setattr(rgit, "create_bundle", real_create)
    clock.advance(2 * rarch.DEFER_ERROR_S)
    with caplog.at_level(logging.WARNING, logger=rarch.__name__):
        young = run(w, state=state, clock=clock)
    assert young["pruned"] == [], young
    reason = young["deferred"]["job-y"]
    assert reason.startswith("bundle lock in use: commits.bundle.tmp.lock is 599 s old, under 600 s"), reason
    assert "254 bytes" in reason
    assert leftovers(archive) == before, "a young lock and the temporary bundle are left as they were"
    assert [n for n in notes if n.get("k") == "bundle-lock"] == []
    kept = [r.getMessage() for r in caplog.records if "bundle lock" in r.getMessage()]
    assert kept == ["Retention keeps the bundle lock of job-y: 254 bytes, 599 s old (removed at 600 s)"], kept
    caplog.clear()
    wall[0] = WALL + 1                       # now 600 s old
    clock.advance(2 * rarch.DEFER_BUSY_S)
    with caplog.at_level(logging.WARNING, logger=rarch.__name__):
        old = run(w, state=state, clock=clock)
    assert old["pruned"] == ["job-y"], old["deferred"]
    removed = [r.getMessage() for r in caplog.records if "bundle lock" in r.getMessage()]
    assert removed == ["Retention removes the bundle lock of job-y: 254 bytes, 600 s old (removed at 600 s)"], removed
    locks = [n for n in notes if n.get("k") == "bundle-lock"]
    assert len(locks) == 1 and locks[0]["size"] == 254 and locks[0]["age_s"] == 600.0, locks
    assert rarch.check_archive(w.root, "job-y")["ok"]


def test_a_real_git_refuses_a_bundle_while_its_lock_exists(world):
    """The live failure, from git itself: with the lock there, `git bundle
    create` exits 128 with "File exists"."""
    w = world
    target = w.root / "probe.bundle"
    Path(str(target) + ".lock").write_bytes(b"")
    with pytest.raises(rgit.GitError) as refused:
        rgit.create_bundle(w.repo / ".git", target, ["HEAD"], [])
    assert "128" in str(refused.value) and "File exists" in str(refused.value)
