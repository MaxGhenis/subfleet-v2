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
- T6, rule-independence (r2): for every mode combination T3 draws, removal ends
  the same way under this host's directory rename rule and macOS 15's.

macOS 15 refuses to rename a directory that lacks owner write, even within one
parent; macOS 26, this host, allows it (#258's CI, r2). T1 to T5, and every
test that removes a folder whose mode it set, run under both rules
(`tests/rename_rules.py`), and each id names the rule: `host-rename` or
`macos15-rename`. A child process gets its parent's rule.

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
from tests import rename_rules
from tests.rename_rules import HOST, MACOS15, RULES

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


@pytest.mark.parametrize("rule", RULES)
def test_p3_2_owner_unreadable_top_folder_is_removed(tmp_path, monkeypatch, caplog, rule):
    """Review r2 P3-2: after `chmod 000` on an allocation, `remove(…, verified_empty=True)`
    returned None, logged a PermissionError, and the folder stayed. #258's first
    build fixed the mode after the rename, which macOS 15 refuses for this folder,
    so there it still did (r2)."""
    scratch = manager(tmp_path)
    record = allocation_with_payload(scratch, top_mode=0o000)
    rename_rules.install(rule, monkeypatch)
    caplog.set_level(logging.WARNING)
    try:
        assert scratch.remove(record, subject(), verified_empty=True) == "removed"
        assert leftovers(scratch) == []
        assert "PermissionError" not in caplog.text
    finally:
        unlock(scratch.root)


@pytest.mark.parametrize("rule", RULES)
def test_p3_2_a_locked_tombstone_a_crash_left_is_finished(tmp_path, monkeypatch, caplog, rule):
    """A tombstone whose mode was never fixed: what #258's first build left on macOS
    26 when it died between its rename and its mode change. No rename happens
    here, so only the mode change of a crash-left tombstone lets removal open it."""
    scratch = manager(tmp_path)
    record = allocation_with_payload(scratch)
    tomb = tombstone_of(scratch, record)
    rename_rules.move_dir(record["path"], tomb)
    os.chmod(tomb, 0o000)
    rename_rules.install(rule, monkeypatch)
    caplog.set_level(logging.WARNING)
    try:
        assert scratch.presence(record) == "tombstoned"
        assert scratch.remove(record, subject(), verified_empty=True) == "removed"
        assert leftovers(scratch) == []
        assert "TMPDIR removal deferred" not in caplog.text
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
from tests import rename_rules

spec = json.loads(sys.argv[1])
rename_rules.install(spec["rule"])             # the parent's directory rename rule, before anything renames
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
    # The allocation's mode fixed before its rename (r2), or a crash-left tombstone's:
    # through its descriptor when an open admits it, else by name (r3). A remove
    # child's first `fchmod` is the top folder's.
    "remove:top-chmodded": (("chmod", "fchmod"), "after",
                            lambda p, *a, **k: isinstance(p, int) or base(p).startswith(P)),
    "remove:emptying": ("unlink", "after", lambda p, *a, **k: True),
    "remove:emptied": ("rmdir", "before", lambda p, *a, **k: base(p).startswith(T)),
    "remove:removed": ("rmdir", "after", lambda p, *a, **k: base(p).startswith(T)),
    "remove:putting-back": ("rename", "before", lambda s, d, *a, **k: base(s).startswith(T)),
    "remove:put-back": ("rename", "after", lambda s, d, *a, **k: base(s).startswith(T)),
}


POINTS["remove:top-unreadable-chmodded"] = POINTS["remove:top-chmodded"]     # a 0000 folder: by name


def crash_at(point):
    attrs, when, hit = POINTS[point]
    for attr in (attrs,) if isinstance(attrs, str) else attrs:
        crash_on(attr, when, hit)


def crash_on(attr, when, hit):
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
#: The top folder at each point: 0500 (an open admits it) or 0000 (changed by name, r3).
TOP_MODES = {"remove:top-chmodded": 0o500, "remove:top-unreadable-chmodded": 0o000}
#: An unknown folder its owner set to 0000 has no readable marker, so no sweep sights it.
REMOVALS = [(kind, point) for point in [*REMOVE_POINTS, "remove:top-unreadable-chmodded"]
            for kind in ("recorded", "legacy", "unknown")
            if not (kind == "unknown" and point == "remove:top-unreadable-chmodded")]


def run_child(scratch, **spec):
    return run_python(CHILD, {"root": str(scratch.root), "store": scratch.store_root, **spec})


@pytest.mark.parametrize("rule", RULES)
@pytest.mark.parametrize("point", CREATE_POINTS)
def test_t1_a_crash_in_create_leaves_nothing_a_sweep_cannot_remove(tmp_path, monkeypatch, point, rule):
    """At `create:made`, `create:chmodded` and `create:marker-written` the folder has
    no marker and no record: only the store's journal says it is this store's."""
    scratch = manager(tmp_path)
    rename_rules.install(rule, monkeypatch)
    result = run_child(scratch, action="create", point=point, rule=rule)
    assert result.returncode == -signal.SIGKILL, (point, result.stdout, result.stderr)
    later_sweeps(scratch, Sightings(), {}, start=1_000_000)
    assert leftovers(scratch) == [], f"{point}: an orphan no sweep removes"
    assert journal(scratch) == []


@pytest.mark.parametrize("rule", RULES)
@pytest.mark.parametrize("kind,point", REMOVALS)
def test_t1_a_crash_in_remove_leaves_nothing_a_sweep_cannot_remove(tmp_path, monkeypatch, kind, point, rule):
    """`recorded`: a known allocation whose marker the provider removed. `legacy`:
    one recorded before D-TMP1-P3, with no tombstone of its own. `unknown`: the
    sweep's removal of a marked folder no record names. At `remove:top-chmodded`
    the folder lacks owner write, which macOS 15 will not rename, and its mode
    changes through its descriptor; at `remove:top-unreadable-chmodded` it is
    0000, so its mode changes by name (r3)."""
    scratch = manager(tmp_path)
    pinned = point in ("remove:putting-back", "remove:put-back")
    top_mode = TOP_MODES.get(point, 0o700)
    record = allocation_with_payload(scratch, top_mode=top_mode, unmark=kind != "unknown", pinned=pinned)
    if kind == "legacy":
        record.pop("tombstone", None)
    rename_rules.install(rule, monkeypatch)
    store, records = Sightings(), {}
    if kind == "unknown":
        store.sweep(scratch, now=0)
        assert [row["path"] for row in store.notes] == [record["path"]]
        result = run_child(scratch, action="sweep", point=point, sightings=store.rows, now=DAY + 1, pinned=pinned,
                           rule=rule)
    else:
        records["a1"] = record
        result = run_child(scratch, action="remove", point=point, record=record, subject=subject(), pinned=pinned,
                           rule=rule)
    assert result.returncode == -signal.SIGKILL, (point, result.stdout, result.stderr)
    receipts = later_sweeps(scratch, store, records, start=2 * DAY)
    assert leftovers(scratch) == [], f"{kind} {point}: an orphan no sweep removes"
    if records:
        assert receipts["a1"] in ("removed", "absent")
    elif point != "remove:removed":
        assert store.rows == {} and len(store.removed) == 1, "the unknown folder's removal is recorded"


# --- T2: release only on absence ---------------------------------------------------------------

OPERATIONS = ["finalize", "sweep", "vanish", "unmark", "lock", "crash", "swap", "advance"]


@pytest.mark.parametrize("rule", RULES)
@BOUNDED
@given(st.lists(st.tuples(st.sampled_from(OPERATIONS), st.integers(0, 3), st.booleans()), min_size=1, max_size=24))
def test_t2_a_record_is_released_absent_or_removed_only_when_nothing_of_it_is_left(rule, middle):
    """A model of the daemon's sweeps over known allocations that a provider may
    delete, unmark or lock, whose path may be swapped, and whose removal may
    crash after its rename. After each step, an `absent` or `removed` receipt
    has its folder at neither its path nor its tombstone (T2). At the end, a
    folder of this store's is a record's unless its path was swapped away (T1).

    Every example starts four allocations and ends with sweeps a day apart, so
    each one is released or held at the end: derandomized examples are small,
    and without that frame only one in thirty reached a release.

    A crash is the daemon's own (removal killed right after its rename), or with
    `flag` false a tombstone an earlier build left with the folder's mode as the
    provider set it. The model's own renames (that one, and a swap) work under
    either rename rule, as a provider's must (r2)."""
    steps = [("start", index, False) for index in range(4)] + middle + [
        ("sweep", 0, False), ("advance", 3, False), ("sweep", 0, False)]
    with tempfile.TemporaryDirectory(prefix="attempt-t2-") as directory, rename_rules.applied(rule):
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
                elif known and operation == "crash" and holds(path, key):
                    # It ended with a verified-empty census, its removal began, and
                    # the daemon died after the rename.
                    states[key], empties[key] = "succeeded", True
                    if flag:
                        with pytest.MonkeyPatch.context() as patch:
                            crash_after_tombstone_rename(patch)
                            try:
                                outcome = scratch.remove(records[key], subject(key), verified_empty=True)
                            except Crash:
                                outcome = None
                        if outcome:
                            receipts[key] = outcome
                    else:
                        rename_rules.move_dir(path, tombstone_of(scratch, records[key]))
                elif known and operation == "swap" and holds(path, key):
                    rename_rules.move_dir(path, scratch.root / f"aside-{index}-{len(steps)}-{clock}")
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


@pytest.mark.parametrize("rule", RULES)
@BOUNDED
@given(st.lists(TREES, max_size=4), MODES)
def test_t3_any_modes_the_owner_can_set_still_remove_the_tree(rule, children, top_mode):
    with tempfile.TemporaryDirectory(prefix="attempt-t3-") as directory, rename_rules.applied(rule):
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


@pytest.mark.parametrize("rule", RULES)
@pytest.mark.parametrize("mode", [0o000, 0o500, 0o077, 0o577], ids=lambda mode: f"{mode:04o}")
def test_t3_a_deletion_no_mode_change_fixes_defers_logs_and_keeps_the_record(tmp_path, monkeypatch, caplog, mode,
                                                                              rule):
    """What no mode change can fix (`chflags uchg`, here EPERM from unlink) defers
    removal, logs why, and leaves the folder at its recorded path at 0700
    (C-12.10): the next removal needs no rename of a folder macOS 15 would
    refuse. The folder and the directory holding the file both started at
    `mode`; removal set each to exactly 0700, never `mode | 0o700` (r3)."""
    scratch = manager(tmp_path)
    record = allocation_with_payload(scratch)
    os.chmod(Path(record["path"]) / "nested", mode)
    os.chmod(record["path"], mode)
    rename_rules.install(rule, monkeypatch)
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
    assert stat.S_IMODE(Path(record["path"]).lstat().st_mode) == 0o700, "at 0700"
    assert stat.S_IMODE((Path(record["path"]) / "nested").lstat().st_mode) == 0o700, "and so is the directory inside"
    assert scratch.remove(record, subject(), verified_empty=True) == "removed"


# --- T6: the same ending under either rename rule (r2) -------------------------------------------

class Lines(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def removal_under(rule, children, top_mode, pin):
    """Build the tree T3 draws, with the file `pin` picks (if any) undeletable as
    `chflags uchg` makes it, remove it under `rule`, and say how that ended: the
    outcome, the lines logged, where the record's folder is, the mode of each
    entry the tree had (or why it cannot be looked at), and the root's entries,
    with this run's random names as placeholders."""
    with tempfile.TemporaryDirectory(prefix="attempt-t6-") as directory:
        base = Path(directory)
        (base / "user-temp").mkdir()
        lines, log = Lines(), logging.getLogger("tests.attempt-t6")
        log.addHandler(lines)
        log.propagate = False
        scratch = attempt_tmp.AttemptTemp(base / "store", root=base / "user-temp", log=log)
        record = scratch.create("job/a1")
        folder, counter, modes = Path(record["path"]), [0], []
        for child in children:
            modes += build(folder, child, counter)
        files = [path.name for path, _ in modes if path.is_file()]
        pinned = files[pin % len(files)] if pin is not None and files else None
        for path, mode in modes:
            os.chmod(path, mode)
        os.chmod(folder, top_mode)
        real_unlink = os.unlink

        def unlink(path, *args, **kwargs):
            if os.path.basename(os.fspath(path)) == pinned:
                raise PermissionError(errno.EPERM, "dummy uchg")
            return real_unlink(path, *args, **kwargs)

        try:
            with rename_rules.applied(rule), pytest.MonkeyPatch.context() as patch:
                patch.setattr(os, "unlink", unlink)
                outcome = scratch.remove(record, subject(), verified_empty=True)
                where = scratch.presence(record)
            tree = []
            for path in [folder, *(path for path, _ in modes)]:
                try:
                    tree.append((path.relative_to(folder).as_posix(), oct(stat.S_IMODE(path.lstat().st_mode))))
                except OSError as exc:
                    tree.append((path.relative_to(folder).as_posix(), type(exc).__name__))
            names = {folder.name: "<allocation>", Path(tombstone_of(scratch, record)).name: "<tombstone>"}
            said = []
            for line in lines.lines:
                for name, placeholder in names.items():
                    line = line.replace(name, placeholder)
                said.append(line)
            left = sorted(names.get(entry.name, entry.name) for entry in scratch.root.iterdir())
            return outcome, said, where, tree, left, pinned is not None
        finally:
            log.removeHandler(lines)
            unlock(base)


@BOUNDED
@given(st.lists(TREES, max_size=4), MODES, st.none() | st.integers(0, 7))
def test_t6_removal_ends_the_same_way_under_either_rename_rule(children, top_mode, pin):
    """T6, rule-independence: for every mode combination T3 draws, with or without
    an undeletable file somewhere in the tree, removal ends the same way under
    this host's rename rule and macOS 15's. Removed under both; or deferred under
    both, with the same lines logged, the folder back at its recorded path and
    every entry with the same mode."""
    ended = {rule: removal_under(rule, children, top_mode, pin) for rule in RULES}
    assert ended[HOST] == ended[MACOS15], (oct(top_mode), pin, ended)
    outcome, said, where, _, _, pinned = ended[HOST]
    assert (outcome, where) == (("removed", "absent") if not pinned else (None, "present")), ended[HOST]
    if outcome is None:
        assert said == ["TMPDIR removal deferred for job/a1: PermissionError"], said


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


@pytest.mark.parametrize("rule", RULES)
def test_t4_a_chain_past_max_depth_is_refused_in_bounded_descriptors(tmp_path, monkeypatch, caplog, rule):
    scratch = manager(tmp_path)
    rename_rules.install(rule, monkeypatch)
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


@pytest.mark.parametrize("rule", RULES)
@BOUNDED
@given(st.integers(0, 24), st.integers(2, 12))
def test_t4_removal_is_bounded_by_max_depth_and_refuses_what_size_refuses(rule, depth, max_depth):
    with tempfile.TemporaryDirectory(prefix="attempt-t4-") as directory, pytest.MonkeyPatch.context() as patch:
        rename_rules.install(rule, patch)
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


@pytest.mark.parametrize("rule", RULES)
@BOUNDED
@given(PLACES, st.booleans())
def test_t5_removal_touches_nothing_outside_its_tree_wherever_links_point(rule, links, squat):
    """`squat`: before the removal, a symlink to a folder outside has the recorded
    tombstone's name. Removal refuses, and nothing moves."""
    with tempfile.TemporaryDirectory(prefix="attempt-t5-") as directory, rename_rules.applied(rule):
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


# --- r2: removal checks the folder before it changes its mode or renames it ----------------------

@pytest.mark.parametrize("rule", RULES)
@pytest.mark.parametrize("mode", [0o500, 0o000], ids=lambda mode: f"{mode:04o}")
def test_a_locked_directory_swapped_in_before_removal_is_neither_changed_nor_moved(tmp_path, monkeypatch, caplog,
                                                                                     mode, rule):
    """A directory of this user's, without owner write, renamed into the
    allocation's place after `presence()` looked. Removal refuses it before it
    changes any mode or renames anything: the check before the mode change (r2).
    For 0500, the descriptor's check refuses it too (r3); for 0000, which no
    open admits, the check by name is all that stands before a change by name."""
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    path = Path(record["path"])
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("dummy")
    rename_rules.install(rule, monkeypatch)
    checked = scratch.presence

    def swap_after_check(value):
        where = checked(value)
        if where == "present":
            os.rename(path, scratch.root / "moved-away")
            os.rename(victim, path)
            os.chmod(path, mode)
        return where

    real_rename, moved = os.rename, []

    def recording(src, dst, *args, **kwargs):
        real_rename(src, dst, *args, **kwargs)
        if os.path.basename(os.fspath(dst)).startswith(attempt_tmp.TOMBSTONE):
            moved.append(os.fspath(src))

    monkeypatch.setattr(scratch, "presence", swap_after_check)
    caplog.set_level(logging.WARNING)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "rename", recording)
            assert scratch.remove(record, subject(), verified_empty=True) is None
        assert moved == [], "refused before any rename"
        assert stat.S_IMODE(path.lstat().st_mode) == mode, "and before any mode change"
        os.chmod(path, 0o700)
        assert (path / "keep").read_text() == "dummy"
        assert "TMPDIR removal refused for job/a1: its folder was swapped" in caplog.messages
    finally:
        unlock(tmp_path)


@pytest.mark.parametrize("rule", RULES)
def test_a_directory_swapped_in_after_removals_last_look_is_refused_and_put_back(tmp_path, monkeypatch, caplog,
                                                                                   rule):
    """The swap comes right after removal last looks at the allocation by its name
    (or, in a build that never looks, at the rename itself), so only the check of
    what the rename moved can refuse it. The swapped-in directory goes back to
    its name untouched, and the allocation stays where the swap put it."""
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    path = Path(record["path"])
    (path / "payload").write_text("dummy")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("dummy")
    rename_rules.install(rule, monkeypatch)
    real_stat, real_rename, swapped = os.stat, os.rename, []

    def swap():
        if not swapped:
            swapped.append(True)
            real_rename(path, scratch.root / "moved-away")
            real_rename(victim, path)

    def stat_(target, *args, **kwargs):
        found = real_stat(target, *args, **kwargs)
        if kwargs.get("dir_fd") is not None and os.fspath(target) == path.name:
            swap()
        return found

    def rename_(src, dst, *args, **kwargs):
        if os.fspath(src) == path.name and os.path.basename(os.fspath(dst)).startswith(attempt_tmp.TOMBSTONE):
            swap()
        return real_rename(src, dst, *args, **kwargs)

    caplog.set_level(logging.WARNING)
    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", stat_)
        patch.setattr(os, "rename", rename_)
        assert scratch.remove(record, subject(), verified_empty=True) is None
    assert swapped, "the swap ran"
    assert (path / "keep").read_text() == "dummy", "the swapped-in directory is back at its name, untouched"
    assert (scratch.root / "moved-away" / "payload").read_text() == "dummy"
    assert not [name for name in leftovers(scratch) if name.startswith(attempt_tmp.TOMBSTONE)]
    assert "TMPDIR removal refused for job/a1: its folder was swapped" in caplog.messages


@pytest.mark.parametrize("code,logged", [
    ("too-deep", "TreeError(too-deep)"), ("mount-point", "TreeError(mount-point)"),
    ("swapped", "TreeError(swapped)"), ("SECRET7code9", "TreeError")])
def test_a_refused_trees_log_line_carries_a_code_of_the_closed_set_never_a_name(tmp_path, monkeypatch, caplog,
                                                                                   code, logged):
    """C-25.7: a refused tree is logged as its class and its code, a literal of the
    codes removal can raise, never the name its detail carries; any other code is
    logged as the class alone. The site passes the log-site lint as it stands (r2)."""
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    refusal = rfs.TreeError(code, "SECRET7name9/" + "chosen-by-the-provider")
    monkeypatch.setattr(attempt_tmp, "_empty", lambda *args, **kwargs: (_ for _ in ()).throw(refusal))
    caplog.set_level(logging.WARNING)
    assert scratch.remove(record, subject(), verified_empty=True) is None
    assert f"TMPDIR removal deferred for job/a1: {logged}" in caplog.messages
    assert "SECRET7" not in caplog.text
    assert scratch.presence(record) == "present"


def test_the_macos15_rule_refuses_to_rename_exactly_a_directory_without_owner_write(tmp_path, monkeypatch):
    """The emulation itself (`tests/rename_rules.py`): a directory without owner
    write is refused with EACCES, by path, relative to a descriptor, by
    `os.replace` and by `Path.rename`; a directory with owner rwx, a read-only
    file and a symlink to a locked directory are renamed. Only modes whose
    outcome CI's macOS 15 showed (0o500 and 0o000 refused) or all macOS allow
    (owner rwx) are asserted, so it holds on a real macOS 15 too."""
    rename_rules.install(MACOS15, monkeypatch)
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for n, mode in enumerate((0o500, 0o000, 0o555)):
            locked = tmp_path / f"locked-{n}"
            locked.mkdir()
            os.chmod(locked, mode)
            for move in (lambda: os.rename(locked, tmp_path / "x"), lambda: os.replace(locked, tmp_path / "x"),
                         lambda: locked.rename(tmp_path / "x"),
                         lambda: os.rename(locked.name, "x", src_dir_fd=parent, dst_dir_fd=parent)):
                with pytest.raises(PermissionError) as refused:
                    move()
                assert refused.value.errno == errno.EACCES, oct(mode)
            assert locked.is_dir() and not (tmp_path / "x").exists()
        for n, mode in enumerate((0o700, 0o755)):
            (tmp_path / f"open-{n}").mkdir(mode)
            os.chmod(tmp_path / f"open-{n}", mode)
            os.rename(f"open-{n}", f"moved-{n}", src_dir_fd=parent, dst_dir_fd=parent)
        (tmp_path / "file").write_text("dummy")
        os.chmod(tmp_path / "file", 0o400)
        os.rename(tmp_path / "file", tmp_path / "file-moved")
        (tmp_path / "link").symlink_to(tmp_path / "locked-0", target_is_directory=True)
        os.rename(tmp_path / "link", tmp_path / "link-moved")
        assert sorted(entry.name for entry in tmp_path.iterdir()) == [
            "file-moved", "link-moved", "locked-0", "locked-1", "locked-2", "moved-0", "moved-1"]
    finally:
        os.close(parent)
        unlock(tmp_path)


# --- r3: every mode change sets 0700, through the folder's descriptor where an open admits it ----

#: Where removal changes a directory's mode: the allocation before its rename, a
#: tombstone a crash left (which no rename precedes), and a directory inside the tree.
SITES = ["before-rename", "crash-left-tombstone", "nested"]
#: When the swap comes: right after removal's check of the entry by name, or right
#: before a mode change by name (where a build changes it by name at all).
WINDOWS = ["after-check", "at-mode-change"]
SWAPS = [(0o000, "hard-link"), (0o500, "hard-link"), (0o077, "hard-link"), (0o577, "hard-link"),
         (0o500, "symlink")]


def entry_at(scratch, site, mode):
    """An allocation whose directory at `site` has `mode`: the record, and the name
    removal looks that directory up by (relative to its parent's descriptor)."""
    record = scratch.create("job/a1")
    folder = Path(record["path"])
    (folder / "nested").mkdir()
    (folder / "nested" / "payload").write_text("dummy")
    if site == "nested":
        os.chmod(folder / "nested", mode)
        return record, "nested"
    os.chmod(folder, mode)
    if site == "crash-left-tombstone":
        tomb = tombstone_of(scratch, record)
        rename_rules.move_dir(folder, tomb)
        return record, os.path.basename(tomb)
    return record, folder.name


@pytest.mark.parametrize("rule", RULES)
@pytest.mark.parametrize("window", WINDOWS)
@pytest.mark.parametrize("folder_mode,swapped_in", SWAPS, ids=[f"{mode:04o}-{kind}" for mode, kind in SWAPS])
@pytest.mark.parametrize("site", SITES)
def test_an_entry_swapped_in_before_a_mode_change_never_gains_group_or_other_bits(
        tmp_path, monkeypatch, site, folder_mode, swapped_in, window, rule):
    """Review r1b2 P2-1, executed. Between removal's check of a directory and its
    mode change, a provider with write access to the user temp root moves the
    directory aside and puts in its place a hard link to a 0600 file of this
    user's outside the root, or a symlink to a 0500 directory outside it. #258's
    r2 build set `mode | 0o700` by name, so the file outside took the folder's
    group and other bits (0077 and 0577 made it 0777).

    The bound C-12.10 states: such an entry can be set to 0700, only if it is this
    user's (a symlink's own mode, never its target's); it never gains group or
    other bits. Under either rename rule, at each site, and whichever by-name
    call the swap precedes."""
    scratch = manager(tmp_path)
    record, entry = entry_at(scratch, site, folder_mode)
    victim = tmp_path / "outside-file"
    victim.write_text("dummy")
    os.chmod(victim, 0o600)
    target = tmp_path / "outside-dir"
    target.mkdir()
    (target / "kept").write_text("dummy")
    os.chmod(target, 0o500)
    real_rename, real_chmod, real_stat = os.rename, os.chmod, os.stat
    rename_rules.install(rule, monkeypatch)
    swapped = []

    def swap(dir_fd):
        # As a provider must on macOS 15: owner write, rename, its mode back.
        mode = stat.S_IMODE(real_stat(entry, dir_fd=dir_fd, follow_symlinks=False).st_mode)
        real_chmod(entry, mode | stat.S_IWUSR, dir_fd=dir_fd, follow_symlinks=False)
        real_rename(entry, entry + "-moved-away", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        real_chmod(entry + "-moved-away", mode, dir_fd=dir_fd, follow_symlinks=False)
        if swapped_in == "hard-link":
            os.link(victim, entry, dst_dir_fd=dir_fd)
        else:
            os.symlink(target, entry, dir_fd=dir_fd)
        swapped.append(True)

    def by_name(path, kwargs):
        return not swapped and os.fspath(path) == entry and kwargs.get("dir_fd") is not None

    def stat_(path, *args, **kwargs):
        found = real_stat(path, *args, **kwargs)
        if window == "after-check" and by_name(path, kwargs):
            swap(kwargs["dir_fd"])
        return found

    def chmod(path, mode, *args, **kwargs):
        if window == "at-mode-change" and by_name(path, kwargs):
            swap(kwargs["dir_fd"])
        return real_chmod(path, mode, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "stat", stat_)
            patch.setattr(os, "chmod", chmod)
            outcome = scratch.remove(record, subject(), verified_empty=True)
        if window == "after-check":
            assert swapped, "every build checks the entry by name first, so the swap ran"
        mode = stat.S_IMODE(victim.stat().st_mode)
        assert mode in (0o600, 0o700), f"{folder_mode:04o}: the file outside became {mode:04o}"
        assert victim.read_text() == "dummy"
        assert stat.S_IMODE(target.stat().st_mode) == 0o500, "a symlink's target is never changed"
        assert (target / "kept").read_text() == "dummy"
        assert outcome == (None if swapped else "removed"), (swapped, outcome)
    finally:
        unlock(tmp_path)


def mode_changes(monkeypatch):
    """Record, in order, each mode change (`by-name`, with the name; `descriptor`,
    with the directory's inode) and each open refused with a `PermissionError`."""
    calls = []
    real_chmod, real_fchmod, real_open = os.chmod, os.fchmod, os.open

    def chmod(path, mode, *args, **kwargs):
        calls.append(("by-name", os.fspath(path), mode))
        return real_chmod(path, mode, *args, **kwargs)

    def fchmod(fd, mode):
        calls.append(("descriptor", os.fstat(fd).st_ino, mode))
        return real_fchmod(fd, mode)

    def open_(path, *args, **kwargs):
        try:
            return real_open(path, *args, **kwargs)
        except PermissionError as exc:
            calls.append(("refused-open", os.fspath(path), exc.errno))
            raise

    monkeypatch.setattr(os, "chmod", chmod)
    monkeypatch.setattr(os, "fchmod", fchmod)
    monkeypatch.setattr(os, "open", open_)
    return calls


@pytest.mark.parametrize("rule", RULES)
@pytest.mark.parametrize("mode", [0o500, 0o400], ids=lambda mode: f"{mode:04o}")
@pytest.mark.parametrize("site", SITES)
def test_a_folder_the_owner_can_read_gets_0700_through_its_descriptor_never_by_name(tmp_path, monkeypatch, site,
                                                                                      mode, rule):
    """P2-1's fix, the path with no window: a directory an open admits (owner
    read) is opened without following a symlink, checked by its descriptor and
    set to 0700 through it. No mode change by name happens at all."""
    scratch = manager(tmp_path)
    record, entry = entry_at(scratch, site, mode)
    folder = Path(tombstone_of(scratch, record)) if site == "crash-left-tombstone" else Path(record["path"])
    inode = (folder / entry if site == "nested" else folder).lstat().st_ino
    rename_rules.install(rule, monkeypatch)
    try:
        with monkeypatch.context() as patch:
            calls = mode_changes(patch)
            assert scratch.remove(record, subject(), verified_empty=True) == "removed"
        assert calls == [("descriptor", inode, 0o700)], calls
        assert leftovers(scratch) == []
    finally:
        unlock(tmp_path)


@pytest.mark.parametrize("rule", RULES)
@pytest.mark.parametrize("mode", [0o000, 0o300], ids=lambda mode: f"{mode:04o}")
@pytest.mark.parametrize("site", SITES)
def test_a_folder_the_owner_cannot_read_gets_0700_by_name_once_its_open_is_refused(tmp_path, monkeypatch, site,
                                                                                    mode, rule):
    """The fallback: a directory without owner read (0000, 0300), which no open
    admits, is set to 0700 by name, never through a symlink, and only after its
    open was refused with EACCES."""
    scratch = manager(tmp_path)
    record, entry = entry_at(scratch, site, mode)
    rename_rules.install(rule, monkeypatch)
    try:
        with monkeypatch.context() as patch:
            calls = mode_changes(patch)
            assert scratch.remove(record, subject(), verified_empty=True) == "removed"
        assert calls == [("refused-open", entry, errno.EACCES), ("by-name", entry, 0o700)], calls
        assert leftovers(scratch) == []
    finally:
        unlock(tmp_path)


@pytest.mark.parametrize("value", ["../escape-r1b", "escape-r1b", attempt_tmp.TOMBSTONE + "x/../../escape-r1b",
                                   attempt_tmp.TOMBSTONE + "x" * 128, 7],
                         ids=["parent", "no-prefix", "slash", "too-long", "not-a-string"])
def test_a_tombstone_value_that_is_not_a_tombstone_name_refuses_removal(tmp_path, caplog, value):
    """Review r1b2 P3-1: a recorded tombstone that is not a tombstone name (one a
    corrupt or hand-edited record carries) refuses removal before anything is
    renamed. A build that took any string renamed the allocation to
    `../escape-r1b`, outside the temp root, and removed it there."""
    scratch = manager(tmp_path)
    record = {**scratch.create("job/a1"), "tombstone": value}
    folder = Path(record["path"])
    (folder / "payload").write_text("dummy")
    around = sorted(entry.name for entry in tmp_path.iterdir())
    caplog.set_level(logging.WARNING)
    assert scratch.remove(record, subject(), verified_empty=True) is None
    assert "TMPDIR removal refused for job/a1: its recorded tombstone is not one" in caplog.messages
    assert scratch.presence(record) == "present" and (folder / "payload").read_text() == "dummy"
    assert leftovers(scratch) == [folder.name], "nothing renamed inside the root"
    assert sorted(entry.name for entry in tmp_path.iterdir()) == around, "nor out of it"
    assert scratch.tombstone_path(record) is None


def test_a_successful_create_leaves_no_journal_entry(tmp_path, monkeypatch):
    """Review r1b2 P3-2: C-12.10 says each name is in the journal from before its
    mkdir until its marker is published. In-process, so a mutant module is
    reached too (T1's create tests run in a child)."""
    scratch = manager(tmp_path)
    made, real_mkdir = [], os.mkdir

    def mkdir(path, *args, **kwargs):
        name = os.path.basename(os.fspath(path))
        if name.startswith(attempt_tmp.PREFIX):
            made.append((name, journal(scratch)))
        return real_mkdir(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "mkdir", mkdir)
        records = [scratch.create(f"job/a{n}") for n in range(3)]
    names = [Path(record["path"]).name for record in records]
    assert [name for name, _ in made] == names
    assert all(name in listed for name, listed in made), ("named in the journal before it exists", made)
    assert journal(scratch) == [], "and no entry once its marker is published"
    assert all(scratch.owns(record) and (Path(record["path"]) / attempt_tmp.MARKER).is_file() for record in records)


def test_a_journal_entry_naming_a_symlink_or_a_file_is_never_recorded_or_removed(tmp_path):
    """Review r1b2 P3-2: a journal-named entry counts only if it is a real directory
    of this user's. A symlink to a directory outside, a file, and a hard link to
    a file outside, each at a name the journal holds (as a crash before its mkdir
    leaves one), are never sighted, aged or removed, and their entries go."""
    scratch = manager(tmp_path)
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "kept").write_text("dummy")
    os.chmod(outside, 0o500)
    victim = tmp_path / "outside-file"
    victim.write_text("dummy")
    os.chmod(victim, 0o600)
    names = {kind: attempt_tmp.PREFIX + f"job-a1-{kind}" for kind in ("symlink", "file", "hard-link")}
    (scratch.root / names["symlink"]).symlink_to(outside, target_is_directory=True)
    (scratch.root / names["file"]).write_text("dummy")
    os.link(victim, scratch.root / names["hard-link"])
    intents = Path(scratch.store_root) / attempt_tmp.INTENTS
    intents.mkdir(mode=0o700, parents=True)
    for name in names.values():
        (intents / name).write_text(json.dumps({"path": str(scratch.root / name)}) + "\n")
    store = Sightings()
    try:
        for day in range(3):
            store.sweep(scratch, now=1_000_000 + day * (DAY + 1))
        assert store.notes == [] and store.removed == [], (store.notes, store.removed)
        assert journal(scratch) == [], "the entries go: none names a directory"
        assert leftovers(scratch) == sorted(names.values())
        assert os.readlink(scratch.root / names["symlink"]) == str(outside)
        assert stat.S_IMODE(outside.stat().st_mode) == 0o500 and (outside / "kept").read_text() == "dummy"
        assert stat.S_IMODE(victim.stat().st_mode) == 0o600 and victim.read_text() == "dummy"
        assert (scratch.root / names["file"]).read_text() == "dummy"
    finally:
        unlock(tmp_path)


def test_the_journal_folders_own_entry_is_fsynced_once_when_it_is_made(tmp_path, monkeypatch):
    """Review r1b2 check 12, the power-loss gap: when `tmpdir-intents` is first made,
    its parent (the state root) is fsynced, so the folder's own entry is durable
    before any name in it is relied on. Exactly once: not again for later
    allocations, nor for another `AttemptTemp` on the same store."""
    (tmp_path / "store").mkdir()
    scratch = manager(tmp_path)
    store_root = Path(scratch.store_root)
    intents = store_root / attempt_tmp.INTENTS
    found = store_root.stat()
    events, real_fsync, real_mkdir, real_rename = [], os.fsync, os.mkdir, os.rename

    def fsync(fd):
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) == (found.st_dev, found.st_ino):
            events.append(("fsync-state-root", intents.is_dir(), journal(scratch)))
        return real_fsync(fd)

    def mkdir(path, *args, **kwargs):
        if os.path.basename(os.fspath(path)).startswith(attempt_tmp.PREFIX):
            events.append(("mkdir-allocation",))
        return real_mkdir(path, *args, **kwargs)

    def rename(src, dst, *args, **kwargs):
        if os.path.dirname(os.fspath(dst)) == str(intents):
            events.append(("journal-entry",))
        return real_rename(src, dst, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fsync)
        patch.setattr(os, "mkdir", mkdir)
        patch.setattr(os, "rename", rename)
        scratch.create("job/a1")
        scratch.create("job/a2")
        attempt_tmp.AttemptTemp(store_root, root=scratch.root).create("job/a3")
    assert events == [("fsync-state-root", True, []), *[("journal-entry",), ("mkdir-allocation",)] * 3], events
