"""PR #130 review: A/B probes of the real retention pass.

Run unchanged against 08d6a09a (#76) and a5ea556a (#130). The injected clock is
the real monotonic clock plus skipped seconds, because 08d6a09a's entry
checkpoint reads `time.monotonic()` and ignores `clock`.
"""
import threading
import time

import pytest

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.store import Store


@pytest.fixture
def retained(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        store.put_lane(Lane("codex-1", "codex", "codex:one", Credential("codex", "/home/one", "home"),
                            "/home/one", LaneOwner.V2, False))
        yield store, tmp_path


def _aged(store, root, names, size=10):
    for index, name in enumerate(names):
        store.add_job(job_id=name, request_id=name, payload_digest="digest", kind="dispatch",
                      workdir=str(root), prompt_path="/prompt", sandbox="read-only", state="succeeded")
        directory = root / "jobs" / name
        directory.mkdir(parents=True)
        (directory / "stdout").write_bytes(b"x" * size)
        with store.transaction("fixture.age") as conn:
            conn.execute("UPDATE jobs SET created_at=? WHERE job_id=?", (f"2026-01-{index + 1:02d}T00:00:00Z", name))


class _Clock:
    def __init__(self):
        self.skipped = 0.0

    def __call__(self):
        return time.monotonic() + self.skipped


def _no_holders(*args, **kwargs):
    return {}


def _state(root, name):
    return (rarch.load_journal(root, name) or {}).get("state")


def _archived_bytes(root, name):
    """path -> bytes of every stored file in the published archive, read back."""
    checked = rarch.check_archive(root, name)
    assert checked["ok"], checked["problems"]
    files = root / "archive" / name / "files"
    return {f"{label}/{entry['p']}": (files / entry["store"]).read_bytes()
            for label, tree in checked["manifest"]["trees"].items()
            for entry in tree["entries"] if entry.get("store")}


def _overhead(monkeypatch, clock, seconds):
    """Every pass spends `seconds` reading its pins (recovery, listings, the pin queries)."""
    real_reasons = retention._Pass._reasons

    def reasons(run, *args, **kwargs):
        clock.skipped += seconds
        return real_reasons(run, *args, **kwargs)
    monkeypatch.setattr(retention._Pass, "_reasons", reasons)


def _slow_archive(monkeypatch, clock, seconds):
    real_archive = rarch.Retirement.archive

    def archive(retirement, slice_end):
        clock.skipped += seconds.get(retirement.job_id, 0)
        return real_archive(retirement, slice_end)
    monkeypatch.setattr(rarch.Retirement, "archive", archive)


# --- gaps the port names ------------------------------------------------------------

def test_gap3_a_cancel_mid_sizing_keeps_the_size_and_reports_progress(retained, monkeypatch):
    """retention.py:399 at 08d6a09a: the interrupted result has no `progressed`.
    The cached size itself survives on both commits."""
    store, root = retained
    _aged(store, root, ["a", "b", "c"])
    state, cancel, measured = retention.RetentionState(), threading.Event(), []
    original = retention._size

    def size(path, **kwargs):
        found = original(path, **kwargs)
        measured.append(path.name)
        cancel.set()                         # the next walk stops at its first checkpoint
        return found
    monkeypatch.setattr(retention, "_size", size)
    result = retention.maintenance(store, root, state=state, cancel=cancel, max_jobs=10, max_bytes=15)
    assert result["interrupted"] == "cancelled" and result["pruned"] == []
    assert measured == ["a"] and set(state.sizes) == {"a"}
    assert result.get("progressed") is True, result.get("progressed", "<absent>")


def test_reach_a_real_pass_reports_a_deadline_it_passes_mid_pass(retained, monkeypatch):
    """Whether the real pass can report `interrupted: deadline` at all, other
    than when called after its deadline. The clock jumps past the deadline once
    the pins have been read. 08d6a09a: never (so daemon.py:3403 was latent)."""
    store, root = retained
    _aged(store, root, ["a", "b"])
    clock = _Clock()
    _overhead(monkeypatch, clock, 10 ** 6)
    result = retention.maintenance(store, root, max_jobs=0, clock=clock, deadline=clock() + 180,
                                   holders=_no_holders)
    assert result.get("interrupted") == "deadline", (result.get("interrupted"), result["pruned"], result["more"])


# --- F1: the commit loop's deadline checkpoint (retention.py:585) -------------------

def test_f1_a_job_archived_before_the_deadline_commits_in_its_own_pass(retained, monkeypatch):
    """`maintenance`'s contract: the deadline bounds when new work may START; a
    started job finishes its slice (slices may start until deadline + SLICE_S).
    `a` is archived and verified 60 s into a 180 s pass; `b`'s slice starts at
    60 s and one file outlasts it (190 s). `a` must still commit."""
    store, root = retained
    _aged(store, root, ["a", "b"])
    clock = _Clock()
    _slow_archive(monkeypatch, clock, {"a": 60, "b": 190})
    result = retention.maintenance(store, root, max_jobs=0, clock=clock, deadline=clock() + 180,
                                   holders=_no_holders)
    states = {name: _state(root, name) for name in ("a", "b")}
    assert "a" in result["pruned"], (result["pruned"], result.get("interrupted"), states)


def test_f1_passes_to_drain_a_backlog_whose_batches_outlast_the_deadline(retained, monkeypatch):
    """Batches of 4 whose archiving takes 70 s a job (280 s a batch, past the
    180 s deadline but within deadline + SLICE_S): every pass should prune."""
    store, root = retained
    names = [f"j{n:02d}" for n in range(12)]
    _aged(store, root, names)
    clock, state = _Clock(), retention.RetentionState()
    _slow_archive(monkeypatch, clock, dict.fromkeys(names, 70))
    rows = []
    for _ in range(12):
        result = retention.maintenance(store, root, max_jobs=0, clock=clock, deadline=clock() + 180,
                                       holders=_no_holders, state=state, batch=4)
        rows.append((len(result["pruned"]), result.get("interrupted"), bool(result["more"])))
        if not result["more"]:
            break
    print("\nDRAIN", rows)
    assert not store.list_jobs()
    assert all(pruned for pruned, *_ in rows[:-1]), rows


def test_f1_a_parked_job_does_not_delay_verified_batch_commits(retained, monkeypatch):
    """Batch of 4: `big` (oldest) parks slice after slice; s1..s3 archive at once.
    Each pass spends 60 s before it archives (recovery, pins, listings), and
    `big`'s slice is the full 120 s. s1..s3 are archived and verified in pass 1."""
    store, root = retained
    _aged(store, root, ["big", "s1", "s2", "s3"])
    for n in range(4):
        (root / "jobs" / "big" / f"part{n}").write_bytes(b"y" * 10)
    clock, state = _Clock(), retention.RetentionState()
    _overhead(monkeypatch, clock, 60)
    _slow_archive(monkeypatch, clock, {"big": 120})
    rows = []
    for _ in range(20):
        result = retention.maintenance(store, root, max_jobs=0, clock=clock, deadline=clock() + 180,
                                       holders=_no_holders, state=state, batch=4)
        rows.append((sorted(result["pruned"]), result.get("interrupted"), result["progressed"],
                     {n: _state(root, n) for n in ("big", "s1")}))
        if all(store.get_job(n) is None for n in ("s1", "s2", "s3")):
            break
    print("\nPARKED", rows)
    assert len(rows) == 1, f"s1..s3 waited {len(rows)} passes for their commit: {rows}"


# --- F2: no job starts once the pre-selection work outlasts the deadline -----------

@pytest.mark.parametrize("overhead", [200, 400])
def test_f2_slow_pin_reads_still_start_a_job_each_pass(retained, monkeypatch, overhead):
    """08d6a09a starts the first chosen job even past the deadline (`and
    retirements`), and a slice may start until deadline + SLICE_S. Each pass
    here spends 200 s or 400 s before selection, including beyond the slice grace."""
    store, root = retained
    _aged(store, root, ["a", "b"])
    clock, state = _Clock(), retention.RetentionState()
    _overhead(monkeypatch, clock, overhead)
    rows = []
    for _ in range(3):
        result = retention.maintenance(store, root, max_jobs=0, clock=clock, deadline=clock() + 180,
                                       holders=_no_holders, state=state)
        rows.append((result["pruned"], result.get("interrupted"), result.get("progressed"), result["more"]))
    print("\nSLOW-PINS", rows)
    assert not store.list_jobs(), rows


# --- safety: deferred commits, restarts, leftover priority ------------------------

def _archive_then_stop(store, root, monkeypatch, state=None):
    """One pass that archives `a` and stops before its commit (cancel after the
    second holder check: the same journal state a deadline leaves on a5ea556a)."""
    cancel = threading.Event()
    real_check = retention._Pass._check_holders

    def check(run, retirements, *, second):
        found = real_check(run, retirements, second=second)
        if second:
            cancel.set()
        return found
    with monkeypatch.context() as patch:
        patch.setattr(retention._Pass, "_check_holders", check)
        result = retention.maintenance(store, root, max_jobs=0, cancel=cancel, holders=_no_holders,
                                       state=state)
    assert result["interrupted"] == "cancelled" and result["pruned"] == []
    assert _state(root, "a") == "archived"
    return result


def test_s1_a_tree_changed_between_archive_and_a_later_commit_is_put_back_whole(retained, monkeypatch):
    store, root = retained
    _aged(store, root, ["a"])
    (root / "jobs/a/notes").write_bytes(b"original")
    state = retention.RetentionState()
    _archive_then_stop(store, root, monkeypatch, state)
    quarantine = root / "retention/a/job"
    (quarantine / "notes").write_bytes(b"changed after the archive")
    (quarantine / "new").write_bytes(b"new bytes")
    second = retention.maintenance(store, root, max_jobs=0, holders=_no_holders, state=state)
    assert second["pruned"] == [] and "a" in second["deferred"], second["deferred"]
    assert store.get_job("a") is not None
    assert (root / "jobs/a/notes").read_bytes() == b"changed after the archive"
    assert (root / "jobs/a/new").read_bytes() == b"new bytes"
    assert (root / "jobs/a/stdout").read_bytes() == b"x" * 10


@pytest.mark.parametrize("change", [False, True])
def test_s2_a_restart_between_archive_and_delete_commits_only_what_is_verified(retained, monkeypatch, change):
    store, root = retained
    _aged(store, root, ["a"])
    (root / "jobs/a/notes").write_bytes(b"original")
    _archive_then_stop(store, root, monkeypatch)
    if change:
        (root / "retention/a/job/notes").write_bytes(b"changed while the daemon was down")
    # A new daemon: no RetentionState, only the journal.
    second = retention.maintenance(store, root, max_jobs=0, holders=_no_holders,
                                   state=retention.RetentionState())
    if change:
        assert second["pruned"] == [] and store.get_job("a") is not None
        assert (root / "jobs/a/notes").read_bytes() == b"changed while the daemon was down"
        return
    assert second["pruned"] == ["a"]
    assert _archived_bytes(root, "a") == {"job/stdout": b"x" * 10, "job/notes": b"original"}
    assert not (root / "jobs/a").exists() and not (root / "retention/a").exists()


def test_s3_a_restart_between_commit_and_delete_sets_changed_bytes_aside(retained, monkeypatch):
    store, root = retained
    _aged(store, root, ["a"])
    (root / "jobs/a/notes").write_bytes(b"original")
    publish, failed = rarch.Retirement.publish, []

    def publish_once(retirement):
        if not failed:
            failed.append(1)
            raise OSError("the daemon stops right after the commit")
        return publish(retirement)
    monkeypatch.setattr(rarch.Retirement, "publish", publish_once)
    first = retention.maintenance(store, root, max_jobs=0, holders=_no_holders)
    assert first["pruned"] == ["a"] and _state(root, "a") == "committed" and store.get_job("a") is None
    (root / "retention/a/job/notes").write_bytes(b"written after the commit")
    (root / "retention/a/job/late").write_bytes(b"late file")
    second = retention.maintenance(store, root, max_jobs=0, holders=_no_holders,
                                   state=retention.RetentionState())
    assert second["reclaimed"] == ["a"], second
    assert _archived_bytes(root, "a") == {"job/stdout": b"x" * 10, "job/notes": b"original"}
    kept = {path.name: path.read_bytes() for path in (root / "retention-conflicts" / "a").rglob("*") if path.is_file()}
    assert kept.get("notes") == b"written after the commit" and kept.get("late") == b"late file", kept


@pytest.mark.parametrize("taken", [("retire:z", "resume:req-1"), ("export:z", "z")])
def test_s4_leftover_priority_never_takes_a_job_someone_else_now_holds(retained, monkeypatch, taken):
    """`z` has a journal-less `retention:z` lease, so it goes first. After
    recovery releases that lease and the pins are read, a resume fences `z`
    (or its export takes a job lease). Selection must not retire it."""
    store, root = retained
    _aged(store, root, ["a", "z"])
    store.acquire_lease("retire:z", "retention:z")
    real_measure = retention._Pass._measure

    def measure(run, *args, **kwargs):
        store.acquire_lease(*taken)
        return real_measure(run, *args, **kwargs)
    monkeypatch.setattr(retention._Pass, "_measure", measure)
    result = retention.maintenance(store, root, max_jobs=0, batch=1, holders=_no_holders)
    assert "z" not in result["pruned"] and store.get_job("z") is not None
    assert (root / "jobs/z/stdout").read_bytes() == b"x" * 10
    holder = store.query("SELECT holder FROM leases WHERE lease_key=?", (taken[0],))
    assert holder and holder[0]["holder"] == taken[1]


def _blocked_cleanup(store, root, monkeypatch, blocked):
    from subfleet import retention_fs as rfs
    _aged(store, root, ["a"])
    for name in ("b", "c"):
        (root / "jobs/a" / name).write_bytes(name.encode())
    original = rfs.Reclaim._one

    def one(reclaim, fd, rel, name):
        if name not in blocked:
            original(reclaim, fd, rel, name)

    def no_conflicts(*args, **kwargs):
        raise PermissionError("fixture blocks setting aside the remnant")
    monkeypatch.setattr(rfs.Reclaim, "_one", one)
    monkeypatch.setattr(rfs.Reclaim, "_conflict_dir", no_conflicts)


def test_gap4a_an_incomplete_cleanup_reports_more_work(retained, monkeypatch):
    """retention.py:835 at 08d6a09a: a committed journal whose deletion could not
    finish leaves `more` False, so the daemon waits the hour."""
    store, root = retained
    _blocked_cleanup(store, root, monkeypatch, {"stdout", "b", "c"})
    first = retention.maintenance(store, root, max_jobs=0, holders=_no_holders)
    assert first["pruned"] == ["a"] and _state(root, "a") == "reclaiming"
    assert first["more"] is True


def test_gap4b_partial_verified_deletion_is_progress(retained, monkeypatch):
    store, root = retained
    blocked = {"stdout", "b", "c"}
    _blocked_cleanup(store, root, monkeypatch, blocked)
    retention.maintenance(store, root, max_jobs=0, holders=_no_holders)
    blocked.discard("b")
    second = retention.maintenance(store, root, max_jobs=0, holders=_no_holders)
    assert not (root / "retention/a/job/b").exists() and second["pruned"] == []
    assert second["progressed"] is True


@pytest.mark.parametrize("second", [False, True])
@pytest.mark.parametrize("error", [retention.ScanFailed, OSError, ValueError])
def test_a_failed_holder_scan_reports_a_pass_wide_blocker(retained, monkeypatch, second, error):
    """Starts and rollbacks must not schedule another batch against an unavailable scanner."""
    store, root = retained
    _aged(store, root, ["a", "b", "c"])
    calls = []

    def holders(*args, **kwargs):
        calls.append(1)
        if not second or len(calls) == 2:
            raise error("process listing unavailable")
        return {}

    result = retention.maintenance(store, root, max_jobs=0, batch=2, holders=holders)
    assert result["holder_scan_failed"] is True
    assert result["more"] is True and result["pruned"] == []
    assert len(store.list_jobs()) == 3
    assert all((root / "jobs" / name / "stdout").read_bytes() == b"x" * 10
               for name in ("a", "b", "c"))
