"""D-TMP1-P3: the leak paths #246's review r2 found in attempt TMPDIR removal.

Regressions for its P3-1, P3-2, P3-3 and P3-5, each in the form the review ran
it, and the invariants the fix keeps:
- T1, no orphan: after any single crash point in `create` or `_remove`, and a
  later sweep, every folder this store made is recorded or removed.
- T2, release only on absence: a record is released `absent` or `removed` only
  when neither its allocation nor its tombstone exists.
- T3, mode-proof: any modes the owner can set still let removal go, and what
  no mode change fixes defers with a logged reason and keeps the record.
- T4, bounded: removal holds at most `MAX_DEPTH` and a small constant of
  descriptors, and refuses (deferred, logged, kept) what `size()` refuses.
- T5, no widening: removal touches nothing outside the allocation and its
  tombstone, wherever symlinks point.

In the T1 tests a crash is a real one: the child process kills itself with
SIGKILL at the named point, so no `except` or `finally` of the code under test
runs. The in-process regressions fail `os.rename` with a BaseException right
after it renames, which `_remove` never catches and which its `finally` sees
before it records the move, so nothing puts the folder back either. They patch
only `os`, so the mutation witnesses (`test_attempt_tmpdir_mutations.py`) can
run them against a mutant module too.
"""

import errno
import inspect
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import attempt_tmp
from subfleet import retention_fs as rfs

REPO = Path(__file__).resolve().parents[2]
DAY = 86_400
BOUNDED = settings(max_examples=30, deadline=None, derandomize=True, database=None,
                   suppress_health_check=[HealthCheck.too_slow])


def manager(tmp_path):
    from subfleet.attempt_tmp import AttemptTemp
    root = tmp_path / "user-temp"
    root.mkdir(exist_ok=True)
    return AttemptTemp(tmp_path / "store", root=root)


def subject(attempt_id="job/a1", state="succeeded"):
    return {"attempt_id": attempt_id, "state": state, "evidence_json": "{}"}


def tombstone_of(scratch, record):
    """The tombstone path the store recorded for `record` (none before D-TMP1-P3)."""
    named = getattr(scratch, "tombstone_path", None)
    return named(record) if named else None


def leftovers(scratch):
    return sorted(entry.name for entry in scratch.root.iterdir() if entry.name.startswith(attempt_tmp.PREFIX))


def journal(scratch):
    folder = Path(scratch.store_root) / "tmpdir-intents"
    return sorted(os.listdir(folder)) if folder.is_dir() else []


def unlock(path: Path) -> None:
    """Give the owner back every directory under `path`, so cleanup can remove it."""
    try:
        found = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(found.st_mode):
        os.chmod(path, 0o700)
        for child in path.iterdir():
            unlock(child)


class Crash(BaseException):
    """The daemon dies here: nothing after this point of the code under test runs."""


def crash_after_tombstone_rename(monkeypatch):
    real = os.rename

    def rename(src, dst, *args, **kwargs):
        real(src, dst, *args, **kwargs)
        if os.path.basename(os.fspath(dst)).startswith(attempt_tmp.TOMBSTONE):
            raise Crash()

    monkeypatch.setattr(os, "rename", rename)


class Sightings:
    """The store's sightings of unknown folders, as the daemon keeps them,
    including the lookup of a sighting by its recorded tombstone."""

    def __init__(self, rows=None):
        self.rows, self.notes, self.removed = dict(rows or {}), [], []

    def seen(self, path):
        return self.rows.get(path)

    def note(self, record):
        self.notes.append(record)
        self.rows[record["path"]] = record

    def gone(self, record):
        self.removed.append(record)
        self.rows.pop(record["path"], None)

    def sweep(self, scratch, *, now, named=()):
        kwargs = {"now": now}
        if "tombstoned" in inspect.signature(scratch.sweep_unknown).parameters:
            tombs = {tombstone_of(scratch, row): row for row in self.rows.values()}
            kwargs["tombstoned"] = tombs.get
        scratch.sweep_unknown(set(named).__contains__, self.seen, self.note, self.gone, **kwargs)


def later_sweeps(scratch, store, records, *, start, rounds=3):
    """The daemon's sweep, `rounds` times a day apart: release each known record
    of a terminal, verified-empty holder, then age the unknown folders."""
    receipts = {}
    for n in range(rounds):
        clock = start + n * (DAY + 1)
        for key, record in records.items():
            if key in receipts:
                continue
            outcome = scratch.remove(record, subject(record["attempt_id"]), verified_empty=False)
            if outcome is None:
                outcome = scratch.remove(record, subject(record["attempt_id"]), verified_empty=True)
            if outcome:
                receipts[key] = outcome
        named = {record["path"] for record in records.values()}
        named |= {tombstone_of(scratch, record) for key, record in records.items() if key not in receipts} - {None}
        store.sweep(scratch, now=clock, named=named)
    return receipts


def allocation_with_payload(scratch, *, top_mode=0o700, unmark=True, pinned=False):
    record = scratch.create("job/a1")
    folder = Path(record["path"])
    (folder / "nested").mkdir()
    (folder / "nested" / "payload").write_bytes(b"x" * 65536)
    if pinned:
        (folder / "pinned").write_text("dummy")
    if unmark:
        (folder / attempt_tmp.MARKER).unlink()      # the provider may remove its marker
    os.chmod(folder, top_mode)
    return record


# --- regressions: review r2's executed inputs -------------------------------------------------

def test_p3_1_tombstone_a_crash_left_is_finished_never_released_as_absent(tmp_path, monkeypatch):
    """Review r2 P3-1: the provider unlinks its marker, removal renames the folder
    to a tombstone and the daemon dies there. Sweeps at 0 h, 24 h, 48 h and 30
    days kept the tombstone with no sighting, and `remove()` returned `absent`."""
    scratch = manager(tmp_path)
    record = allocation_with_payload(scratch)
    with monkeypatch.context() as patch:
        crash_after_tombstone_rename(patch)
        with pytest.raises(Crash):
            scratch.remove(record, subject(), verified_empty=True)
    tombs = [name for name in leftovers(scratch) if name.startswith(attempt_tmp.TOMBSTONE)]
    assert len(tombs) == 1 and not Path(record["path"]).exists(), "the crash left exactly a tombstone"
    store = Sightings()
    named = {record["path"], tombstone_of(scratch, record)} - {None}
    for clock in (0, DAY, 2 * DAY, 30 * DAY):
        store.sweep(scratch, now=clock, named=named)
        assert scratch.remove(record, subject(), verified_empty=False) is None, \
            "a tombstoned allocation is not absent: its holder's gate still applies"
    assert scratch.remove(record, subject(), verified_empty=True) == "removed"
    assert leftovers(scratch) == [], "the tombstone is finished, not left for ever"


def test_p3_2_owner_unreadable_top_folder_is_removed(tmp_path, caplog):
    """Review r2 P3-2: after `chmod 000` on an allocation, `remove(…, verified_empty=True)`
    returned None, logged a PermissionError, and the folder stayed."""
    scratch = manager(tmp_path)
    record = allocation_with_payload(scratch, top_mode=0o000)
    caplog.set_level(logging.WARNING)
    try:
        assert scratch.remove(record, subject(), verified_empty=True) == "removed"
        assert leftovers(scratch) == []
        assert "PermissionError" not in caplog.text
    finally:
        unlock(scratch.root)


def failing_marker_publish(monkeypatch, *, after_rename=False):
    """The marker's publish fails: ENOSPC at its rename, or EIO once it is written
    (its directory fsync, say). Patched in `os`, so a mutant module sees it too."""
    real = os.rename

    def rename(src, dst, *args, **kwargs):
        if os.path.basename(os.fspath(dst)) != attempt_tmp.MARKER:
            return real(src, dst, *args, **kwargs)
        if after_rename:
            real(src, dst, *args, **kwargs)
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(os, "rename", rename)


def test_p3_3_failed_marker_publish_leaves_no_folder(tmp_path, monkeypatch):
    """Review r2 P3-3: with the marker publish raising ENOSPC, `create()` raised and
    left `subfleet-attempt-job-a1-…`, which ten days of sweeps kept."""
    scratch = manager(tmp_path)
    with monkeypatch.context() as patch:
        failing_marker_publish(patch)
        with pytest.raises(OSError) as raised:
            scratch.create("job/a1")
    assert raised.value.errno == errno.ENOSPC, "it still raises as before"
    assert leftovers(scratch) == [], "the new folder was empty and made here: it is gone"
    store = Sightings()
    for day in range(10):
        store.sweep(scratch, now=day * DAY)
    assert leftovers(scratch) == [] and journal(scratch) == []


def test_p3_3_folder_a_failed_publish_could_not_remove_is_logged_and_swept(tmp_path, monkeypatch, caplog):
    """The publish wrote its marker, then failed: the folder is not empty, so its
    `rmdir` fails. That is logged, `create()` still raises, and the store's
    journal still names the folder, so the sweep ages and removes it."""
    scratch = manager(tmp_path)
    caplog.set_level(logging.WARNING)
    with monkeypatch.context() as patch:
        failing_marker_publish(patch, after_rename=True)
        with pytest.raises(OSError):
            scratch.create("job/a1")
    [left] = leftovers(scratch)
    assert f"TMPDIR {left} kept after its allocation failed: OSError" in caplog.text
    store = Sightings()
    store.sweep(scratch, now=0)
    assert leftovers(scratch) == [left], "an unknown folder waits 24 hours"
    store.sweep(scratch, now=DAY + 1)
    assert leftovers(scratch) == [] and journal(scratch) == []


DEEP = r'''
import json, logging, os, resource, sys
from pathlib import Path
from subfleet import attempt_tmp, retention_fs as rfs

spec = json.loads(sys.argv[1])
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (1024, hard))
lines = []


class Lines(logging.Handler):
    def emit(self, record):
        lines.append(record.getMessage())


log = logging.getLogger("deep")
log.addHandler(Lines())
scratch = attempt_tmp.AttemptTemp(spec["store"], root=Path(spec["root"]), log=log)
record = scratch.create("job/a1")
top = os.open(record["path"], os.O_RDONLY | os.O_DIRECTORY)
# Grown from the top, one shallow rename a level: a call on a deep folder costs
# more the deeper it is here (1,000 levels made one by one took 15 s), and the
# review's 2,000 took 61 s.
os.mkdir("d", dir_fd=top)
os.close(os.open("d/leaf", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=top))
for _ in range(spec["depth"] - 1):
    os.mkdir("n", dir_fd=top)
    os.rename("d", "n/d", src_dir_fd=top, dst_dir_fd=top)
    os.rename("n", "d", src_dir_fd=top, dst_dir_fd=top)
real_open, peak = os.open, [0]


def counting(*args, **kwargs):
    opened = real_open(*args, **kwargs)
    peak[0] = max(peak[0], opened)
    return opened


baseline = real_open("/dev/null", os.O_RDONLY)
os.close(baseline)
os.open = counting
subject = {"attempt_id": "job/a1", "state": "succeeded"}
outcome = scratch.remove(record, subject, verified_empty=True)
kept = os.path.isdir(record["path"])
os.open = real_open
# Taken apart from the top the same way, so the folder goes and the test's tmp_path with it.
while os.path.isdir(os.path.join(record["path"], "d", "d")):
    os.rename("d/d", "n", src_dir_fd=top, dst_dir_fd=top)
    os.rmdir("d", dir_fd=top)
    os.rename("n", "d", src_dir_fd=top, dst_dir_fd=top)
os.close(top)
cleanup = scratch.remove(record, subject, verified_empty=True)
print(json.dumps({"outcome": outcome, "kept": kept, "lines": lines, "peak": peak[0], "baseline": baseline,
                  "cleanup": cleanup}))
'''


def run_python(script, spec, *, timeout=120):
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["PYTHONPATH"] = str(REPO)
    return subprocess.run([sys.executable, "-c", script, json.dumps(spec)], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=timeout)


def run_deep(scratch, depth):
    result = run_python(DEEP, {"root": str(scratch.root), "store": scratch.store_root, "depth": depth}, timeout=300)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def test_p3_5_two_thousand_levels_are_refused_within_the_descriptor_bound(tmp_path):
    """Review r2 P3-5: a 2,000-deep chain, removed in a child with `RLIMIT_NOFILE`
    1024, hit EMFILE at descriptor 1023: removal held one descriptor per level."""
    scratch = manager(tmp_path)
    found = run_deep(scratch, 2000)
    assert found["outcome"] is None and found["kept"], "deferred, and its record keeps the folder"
    assert any("TreeError(too-deep)" in line for line in found["lines"]), found["lines"]
    assert not any("OSError" in line for line in found["lines"]), "refused for its depth, never by EMFILE"
    assert found["peak"] - found["baseline"] <= rfs.MAX_DEPTH + 4
    assert found["cleanup"] == "removed"


def test_p3_5_a_tree_as_deep_as_size_measures_is_removed(tmp_path):
    """The bound is `size()`'s: a chain of `MAX_DEPTH` - 1 levels is measured and removed."""
    scratch = manager(tmp_path)
    found = run_deep(scratch, rfs.MAX_DEPTH - 1)
    assert found["outcome"] == "removed" and not found["kept"], found["lines"]
    assert found["peak"] - found["baseline"] <= rfs.MAX_DEPTH + 4


# --- T1: no orphan after a real crash ------------------------------------------------------------

CHILD = r'''
import inspect, json, os, signal, sys
from pathlib import Path
from subfleet import attempt_tmp

spec = json.loads(sys.argv[1])
T, P, M = attempt_tmp.TOMBSTONE, attempt_tmp.PREFIX, attempt_tmp.MARKER


def base(value):
    return os.path.basename(os.fspath(value))


POINTS = {
    "create:intended": ("mkdir", "before", lambda p, *a, **k: base(p).startswith(P)),
    "create:made": ("mkdir", "after", lambda p, *a, **k: base(p).startswith(P)),
    "create:chmodded": ("chmod", "after", lambda p, *a, **k: base(p).startswith(P)),
    "create:marker-written": ("rename", "before", lambda s, d, *a, **k: base(d) == M),
    "create:marked": ("rename", "after", lambda s, d, *a, **k: base(d) == M),
    "remove:renaming": ("rename", "before", lambda s, d, *a, **k: base(d).startswith(T)),
    "remove:renamed": ("rename", "after", lambda s, d, *a, **k: base(d).startswith(T)),
    "remove:top-chmodded": ("chmod", "after", lambda p, *a, **k: base(p).startswith(T)),
    "remove:emptying": ("unlink", "after", lambda p, *a, **k: True),
    "remove:emptied": ("rmdir", "before", lambda p, *a, **k: base(p).startswith(T)),
    "remove:removed": ("rmdir", "after", lambda p, *a, **k: base(p).startswith(T)),
    "remove:putting-back": ("rename", "before", lambda s, d, *a, **k: base(s).startswith(T)),
    "remove:put-back": ("rename", "after", lambda s, d, *a, **k: base(s).startswith(T)),
}


def crash_at(point):
    attr, when, hit = POINTS[point]
    original = getattr(os, attr)

    def wrapper(*args, **kwargs):
        matched = hit(*args, **kwargs)
        if matched and when == "before":
            os.kill(os.getpid(), signal.SIGKILL)
        result = original(*args, **kwargs)
        if matched and when == "after":
            os.kill(os.getpid(), signal.SIGKILL)
        return result

    setattr(os, attr, wrapper)


if spec.get("pinned"):
    # A file nothing can delete (`chflags uchg`): the deletion fails and is put back.
    real_unlink = os.unlink

    def unlink(path, *args, **kwargs):
        if base(path) == "pinned":
            raise PermissionError(1, "dummy pinned file")
        return real_unlink(path, *args, **kwargs)

    os.unlink = unlink
if spec["point"] in POINTS:
    crash_at(spec["point"])
scratch = attempt_tmp.AttemptTemp(spec["store"], root=Path(spec["root"]))
if spec["action"] == "create":
    print(json.dumps(scratch.create("job/a1")), flush=True)
elif spec["action"] == "remove":
    print(scratch.remove(spec["record"], spec["subject"], verified_empty=True), flush=True)
else:
    rows = spec["sightings"]
    kwargs = {"now": spec["now"]}
    if "tombstoned" in inspect.signature(scratch.sweep_unknown).parameters:
        kwargs["tombstoned"] = {scratch.tombstone_path(row): row for row in rows.values()}.get
    scratch.sweep_unknown(lambda path: False, rows.get, lambda record: None, lambda record: None, **kwargs)
if spec["point"] == "create:returned":
    os.kill(os.getpid(), signal.SIGKILL)       # the daemon dies before it records the allocation
'''

CREATE_POINTS = ["create:intended", "create:made", "create:chmodded", "create:marker-written",
                 "create:marked", "create:returned"]
REMOVE_POINTS = ["remove:renaming", "remove:renamed", "remove:top-chmodded", "remove:emptying",
                 "remove:emptied", "remove:removed", "remove:putting-back", "remove:put-back"]


def run_child(scratch, **spec):
    return run_python(CHILD, {"root": str(scratch.root), "store": scratch.store_root, **spec})


@pytest.mark.parametrize("point", CREATE_POINTS)
def test_t1_a_crash_in_create_leaves_nothing_a_sweep_cannot_remove(tmp_path, point):
    """At `create:made`, `create:chmodded` and `create:marker-written` the folder has
    no marker and no record: only the store's journal says it is this store's."""
    scratch = manager(tmp_path)
    result = run_child(scratch, action="create", point=point)
    assert result.returncode == -signal.SIGKILL, (point, result.stdout, result.stderr)
    later_sweeps(scratch, Sightings(), {}, start=1_000_000)
    assert leftovers(scratch) == [], f"{point}: an orphan no sweep removes"
    assert journal(scratch) == []


@pytest.mark.parametrize("point", REMOVE_POINTS)
@pytest.mark.parametrize("kind", ["recorded", "legacy", "unknown"])
def test_t1_a_crash_in_remove_leaves_nothing_a_sweep_cannot_remove(tmp_path, kind, point):
    """`recorded`: a known allocation whose marker the provider removed. `legacy`:
    one recorded before D-TMP1-P3, with no tombstone of its own. `unknown`: the
    sweep's removal of a marked folder no record names."""
    scratch = manager(tmp_path)
    pinned = point in ("remove:putting-back", "remove:put-back")
    top_mode = 0o500 if point == "remove:top-chmodded" else 0o700
    record = allocation_with_payload(scratch, top_mode=top_mode, unmark=kind != "unknown", pinned=pinned)
    if kind == "legacy":
        record.pop("tombstone", None)
    store, records = Sightings(), {}
    if kind == "unknown":
        store.sweep(scratch, now=0)
        assert [row["path"] for row in store.notes] == [record["path"]]
        result = run_child(scratch, action="sweep", point=point, sightings=store.rows, now=DAY + 1, pinned=pinned)
    else:
        records["a1"] = record
        result = run_child(scratch, action="remove", point=point, record=record, subject=subject(), pinned=pinned)
    assert result.returncode == -signal.SIGKILL, (point, result.stdout, result.stderr)
    receipts = later_sweeps(scratch, store, records, start=2 * DAY)
    assert leftovers(scratch) == [], f"{kind} {point}: an orphan no sweep removes"
    if records:
        assert receipts["a1"] in ("removed", "absent")
    elif point != "remove:removed":
        assert store.rows == {} and len(store.removed) == 1, "the unknown folder's removal is recorded"


# --- T2: release only on absence ---------------------------------------------------------------

OPERATIONS = ["start", "finalize", "sweep", "vanish", "unmark", "lock", "crash", "swap", "advance"]


@BOUNDED
@given(st.lists(st.tuples(st.sampled_from(OPERATIONS), st.integers(0, 3), st.booleans()), min_size=1, max_size=30))
def test_t2_a_record_is_released_absent_or_removed_only_when_nothing_of_it_is_left(steps):
    """A model of the daemon's sweeps over known allocations that a provider may
    delete, unmark or lock, whose path may be swapped, and whose removal may
    crash after its rename. After each step, an `absent` or `removed` receipt
    has its folder at neither its path nor its tombstone (T2). At the end, a
    folder of this store's is a record's unless its path was swapped away (T1)."""
    with tempfile.TemporaryDirectory(prefix="attempt-t2-") as directory:
        scratch = manager(Path(directory))
        store, clock = Sightings(), 1_000_000.0
        records, states, empties, receipts, identity = {}, {}, {}, {}, {}

        def holds(path, key):
            try:
                found = Path(path).lstat()
            except FileNotFoundError:
                return False
            return (found.st_dev, found.st_ino) == identity[key]

        try:
            for operation, index, flag in steps:
                key = f"job-{index}/a1"
                known = key in records and key not in receipts
                path = Path(records[key]["path"]) if key in records else None
                if operation == "start" and key not in records:
                    records[key] = scratch.create(key)
                    found = Path(records[key]["path"]).lstat()
                    identity[key] = (found.st_dev, found.st_ino)
                    states[key], empties[key] = "running", False
                elif known and operation == "finalize" and states[key] == "running":
                    states[key], empties[key] = "succeeded", flag
                elif known and operation == "vanish" and holds(path, key):
                    unlock(path)
                    shutil.rmtree(path)
                elif known and operation == "unmark" and holds(path, key):
                    unlock(path)
                    (path / attempt_tmp.MARKER).unlink(missing_ok=True)
                elif known and operation == "lock" and holds(path, key):
                    os.chmod(path, 0o000 if flag else 0o500)
                elif (known and operation == "crash" and holds(path, key) and states[key] == "succeeded"
                      and empties[key]):
                    # A removal the gate allowed began, and the daemon died after its rename.
                    os.rename(path, tombstone_of(scratch, records[key]))
                elif known and operation == "swap" and holds(path, key):
                    path.rename(scratch.root / f"aside-{index}-{len(steps)}-{clock}")
                    path.mkdir()
                    (path / "unrelated").write_text("dummy")
                elif operation == "advance":
                    clock += (index + 1) * 6 * 3600
                elif operation == "sweep":
                    for name in [name for name in records if name not in receipts]:
                        outcome = scratch.remove(records[name], subject(name, states[name]), verified_empty=False)
                        if outcome is None and states[name] == "succeeded" and empties[name]:
                            outcome = scratch.remove(records[name], subject(name, states[name]), verified_empty=True)
                        if outcome:
                            receipts[name] = outcome
                    named = {record["path"] for record in records.values()}
                    named |= {tombstone_of(scratch, records[name]) for name in records if name not in receipts}
                    store.sweep(scratch, now=clock, named=named)
                for name, outcome in receipts.items():
                    if outcome in ("absent", "removed"):
                        assert not holds(records[name]["path"], name), (name, outcome)
                        assert not holds(tombstone_of(scratch, records[name]), name), (name, outcome)
            owners = {value: name for name, value in identity.items()}
            for entry in scratch.root.iterdir():
                found = entry.lstat()
                owner = owners.get((found.st_dev, found.st_ino))
                if owner is not None:
                    assert owner not in receipts or receipts[owner] in ("replaced", "outside"), \
                        (entry.name, receipts.get(owner))
        finally:
            unlock(scratch.root)


# --- T3: any modes the owner can set -------------------------------------------------------------

MODES = st.integers(0, 0o777)
TREES = st.recursive(st.tuples(st.just("file"), MODES),
                     lambda children: st.tuples(st.just("dir"), MODES, st.lists(children, max_size=3)),
                     max_leaves=8)


def build(parent: Path, node, counter):
    """Make `node` under `parent`; return the (path, mode) pairs to apply, deepest first."""
    counter[0] += 1
    path = parent / f"n{counter[0]}"
    if node[0] == "file":
        path.write_text("dummy")
        return [(path, node[1])]
    path.mkdir()
    modes = []
    for child in node[2]:
        modes += build(path, child, counter)
    return modes + [(path, node[1])]


@BOUNDED
@given(st.lists(TREES, max_size=4), MODES)
def test_t3_any_modes_the_owner_can_set_still_remove_the_tree(children, top_mode):
    with tempfile.TemporaryDirectory(prefix="attempt-t3-") as directory:
        scratch = manager(Path(directory))
        outside = Path(directory) / "outside"
        outside.mkdir()
        (outside / "kept").write_text("dummy")
        os.chmod(outside, 0o555)
        record = scratch.create("job/a1")
        folder, counter, modes = Path(record["path"]), [0], []
        for child in children:
            modes += build(folder, child, counter)
        for path, mode in modes:
            os.chmod(path, mode)
        os.chmod(folder, top_mode)
        try:
            assert scratch.remove(record, subject(), verified_empty=True) == "removed", (oct(top_mode), modes)
            assert leftovers(scratch) == []
            assert stat.S_IMODE(outside.stat().st_mode) == 0o555 and (outside / "kept").read_text() == "dummy"
        finally:
            unlock(Path(directory))


def test_t3_a_deletion_no_mode_change_fixes_defers_logs_and_keeps_the_record(tmp_path, monkeypatch, caplog):
    """What no mode change can fix (`chflags uchg`, here EPERM from unlink) defers
    removal, logs why, and leaves the folder at its recorded path."""
    scratch = manager(tmp_path)
    record = allocation_with_payload(scratch, top_mode=0o000)
    real = os.unlink

    def pinned(path, *args, **kwargs):
        if os.path.basename(os.fspath(path)) == "payload":
            raise PermissionError(errno.EPERM, "dummy uchg")
        return real(path, *args, **kwargs)

    caplog.set_level(logging.WARNING)
    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", pinned)
        assert scratch.remove(record, subject(), verified_empty=True) is None
    assert "TMPDIR removal deferred for job/a1: PermissionError" in caplog.text
    assert scratch.presence(record) == "present", "put back under its recorded path"
    assert scratch.remove(record, subject(), verified_empty=True) == "removed"


# --- T4: bounded descriptors, and the same depth bound as size() ----------------------------------

def chain(folder: Path, depth: int) -> None:
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    for _ in range(depth):
        os.mkdir("d", dir_fd=fd)
        child = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        os.close(fd)
        fd = child
    os.close(os.open("leaf", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd))
    os.close(fd)


def remove_counting_descriptors(scratch, record, monkeypatch):
    """`remove()`, with the most descriptors this thread held open at once
    through `os.open` (each closed with `os.close`)."""
    real_open, real_close = os.open, os.close
    me, live, peak = threading.get_ident(), set(), [0]

    def opening(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        if threading.get_ident() == me:
            live.add(fd)
            peak[0] = max(peak[0], len(live))
        return fd

    def closing(fd):
        live.discard(fd)
        return real_close(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", opening)
        patch.setattr(os, "close", closing)
        outcome = scratch.remove(record, subject(), verified_empty=True)
    return outcome, peak[0], live


def test_t4_a_chain_past_max_depth_is_refused_in_bounded_descriptors(tmp_path, monkeypatch, caplog):
    scratch = manager(tmp_path)
    monkeypatch.setattr(rfs, "MAX_DEPTH", 4)
    shallow, deep = scratch.create("job/a1"), scratch.create("job/a1")
    chain(Path(shallow["path"]), 3)
    chain(Path(deep["path"]), 6)
    caplog.set_level(logging.WARNING)
    assert remove_counting_descriptors(scratch, shallow, monkeypatch)[0] == "removed"
    outcome, peak, live = remove_counting_descriptors(scratch, deep, monkeypatch)
    assert outcome is None and scratch.owns(deep), "deferred, and the record keeps its folder"
    assert "TMPDIR removal deferred for job/a1: TreeError(too-deep)" in caplog.text
    assert peak <= 4 + 2 and not live, (peak, live)


@BOUNDED
@given(st.integers(0, 24), st.integers(2, 12))
def test_t4_removal_is_bounded_by_max_depth_and_refuses_what_size_refuses(depth, max_depth):
    with tempfile.TemporaryDirectory(prefix="attempt-t4-") as directory, pytest.MonkeyPatch.context() as patch:
        scratch = manager(Path(directory))
        record = scratch.create("job/a1")
        chain(Path(record["path"]), depth)
        patch.setattr(rfs, "MAX_DEPTH", max_depth)
        measured = scratch.size(record)
        outcome, peak, live = remove_counting_descriptors(scratch, record, patch)
        # The parent, the tree and one per level below it, never more.
        assert peak <= max_depth + 2 and not live, (depth, max_depth, peak)
        # Differential: `size()` (retention_fs.walk) and removal share one depth bound.
        assert (measured is None) == (outcome is None) == (depth >= max_depth), (depth, max_depth, measured, outcome)
        if outcome is None:
            assert scratch.owns(record)
            patch.setattr(rfs, "MAX_DEPTH", depth + 1)
            assert scratch.remove(record, subject(), verified_empty=True) == "removed"
        assert leftovers(scratch) == []


# --- T5: no widening, wherever symlinks point ----------------------------------------------------

PLACES = st.lists(st.tuples(st.sampled_from(["top", "nested", "deep"]),
                            st.sampled_from(["sibling", "outside-dir", "outside-file", "root", "dangling"])),
                  max_size=6)


@BOUNDED
@given(PLACES, st.booleans())
def test_t5_removal_touches_nothing_outside_its_tree_wherever_links_point(links, squat):
    """`squat`: before the removal, a symlink to a folder outside has the recorded
    tombstone's name. Removal refuses, and nothing moves."""
    with tempfile.TemporaryDirectory(prefix="attempt-t5-") as directory:
        base = Path(directory)
        scratch = manager(base)
        record = scratch.create("job/a1")
        folder = Path(record["path"])
        (folder / "nested" / "deep").mkdir(parents=True)
        sibling = scratch.create("job/a2")
        targets = {"sibling": Path(sibling["path"]), "outside-dir": base / "outside",
                   "outside-file": base / "outside-file", "root": scratch.root, "dangling": base / "nowhere"}
        targets["outside-dir"].mkdir()
        (targets["outside-dir"] / "kept").write_text("dummy")
        targets["outside-file"].write_text("dummy")
        (targets["sibling"] / "kept").write_text("dummy")
        os.chmod(targets["outside-dir"], 0o500)
        for n, (place, target) in enumerate(links):
            where = {"top": folder, "nested": folder / "nested", "deep": folder / "nested" / "deep"}[place]
            (where / f"link-{n}").symlink_to(targets[target])
        tomb = tombstone_of(scratch, record)
        squatted = squat and tomb is not None
        if squatted:
            os.symlink(targets["outside-dir"], tomb)

        def snapshot():
            seen = {}
            for top in (targets["sibling"], targets["outside-dir"], targets["outside-file"]):
                for path in [top, *(top.rglob("*") if top.is_dir() else ())]:
                    found = path.lstat()
                    seen[str(path)] = (stat.S_IMODE(found.st_mode), found.st_ino,
                                       path.read_bytes() if path.is_file() else None)
            return seen, sorted(entry.name for entry in scratch.root.iterdir())

        before, names = snapshot()
        try:
            outcome = scratch.remove(record, subject(), verified_empty=True)
            after, names_after = snapshot()
            assert after == before, "nothing outside the allocation changed"
            if squatted:
                assert outcome is None and scratch.owns(record), "a squatted tombstone name refuses removal"
                assert names_after == names
            else:
                assert outcome == "removed" and not folder.exists()
                assert names_after == sorted(set(names) - {folder.name})
        finally:
            unlock(base)


# --- the removal guards #246 had, each a mutant's oracle ---------------------------------------

def test_removal_never_enters_another_device(tmp_path, monkeypatch, caplog):
    """A directory inside the tree on another device (a mount point) is never
    entered or emptied: removal refuses and keeps the folder."""
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    mount = Path(record["path"]) / "mounted"
    mount.mkdir()
    (mount / "kept").write_text("dummy")
    real_stat, real_fstat, real_open, real_close = os.stat, os.fstat, os.open, os.close
    opened = set()

    def moved(found):
        values = list(found)
        values[stat.ST_DEV] += 1
        return os.stat_result(values)

    # Every look at it says the other device, so only the device check can refuse it.
    def stat_(path, *args, **kwargs):
        found = real_stat(path, *args, **kwargs)
        return moved(found) if os.fspath(path) == "mounted" and kwargs.get("dir_fd") is not None else found

    def open_(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if os.fspath(path) == "mounted":
            opened.add(fd)
        return fd

    def fstat_(fd):
        return moved(real_fstat(fd)) if fd in opened else real_fstat(fd)

    def close_(fd):
        opened.discard(fd)
        return real_close(fd)

    caplog.set_level(logging.WARNING)
    with monkeypatch.context() as patch:
        for name, value in (("stat", stat_), ("open", open_), ("fstat", fstat_), ("close", close_)):
            patch.setattr(os, name, value)
        assert scratch.remove(record, subject(), verified_empty=True) is None
    assert (mount / "kept").read_text() == "dummy"
    assert "TMPDIR removal deferred for job/a1: TreeError" in caplog.text


def test_put_back_never_replaces_a_name_that_reappeared(tmp_path, monkeypatch, caplog):
    """A removal that fails is renamed back, but never over a new entry at its name,
    even an empty folder a rename would replace: the tombstone stays instead, and
    the next removal finds it there and finishes it."""
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    path = Path(record["path"])
    (path / "payload").write_text("dummy")
    real = os.listdir

    def new_folder_then_failure(target=".", *args, **kwargs):
        if isinstance(target, int) and not path.exists():
            path.mkdir()                 # empty, at the name the tombstone came from
            raise PermissionError(errno.EPERM, "dummy")
        return real(target, *args, **kwargs)

    caplog.set_level(logging.WARNING)
    with monkeypatch.context() as patch:
        patch.setattr(os, "listdir", new_folder_then_failure)
        assert scratch.remove(record, subject(), verified_empty=True) is None
    assert path.is_dir() and list(path.iterdir()) == [], "the new folder is untouched"
    assert "reappeared" in caplog.text
    assert scratch.remove(record, subject(), verified_empty=True) == "removed"
    assert path.is_dir() and list(path.iterdir()) == [], "and still untouched once the tombstone is finished"
    assert leftovers(scratch) == [path.name]
