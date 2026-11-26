"""D-TMP1: cleanup authority, orphan age and bounded lifecycle sequences."""

import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from hypothesis import example, given, settings, strategies as st
import pytest


def manager(tmp_path):
    from subfleet.attempt_tmp import AttemptTemp
    root = tmp_path / "user-temp"
    root.mkdir(exist_ok=True)
    return AttemptTemp(tmp_path / "store", root=root)


def subject(attempt_id="job/a1", state="succeeded", **evidence):
    return {"attempt_id": attempt_id, "state": state, "evidence_json": json.dumps(evidence)}


class Sightings:
    """The store's first sightings of unknown folders, as the daemon keeps them."""

    def __init__(self):
        self.rows, self.notes, self.removed = {}, [], []

    def seen(self, path):
        return self.rows.get(path)

    def note(self, record):
        self.notes.append(record)
        self.rows[record["path"]] = record

    def gone(self, record):
        self.removed.append(record)
        self.rows.pop(record["path"], None)

    def sweep(self, scratch, *, now, named=()):
        scratch.sweep_unknown(set(named).__contains__, self.seen, self.note, self.gone, now=now)


def sighting(path, *, at=0.0):
    found = Path(path).lstat()
    return {"path": str(path), "dev": found.st_dev, "ino": found.st_ino, "uid": found.st_uid, "first_seen_at": at}


def test_attempt_names_are_unique_even_after_path_sanitizing(tmp_path):
    scratch = manager(tmp_path)
    records = [scratch.create(name) for name in ("job/a1", "job-a1", "../a1", "job/a1")]
    assert len({record["path"] for record in records}) == len(records)
    assert all(Path(record["path"]).parent == scratch.root for record in records)


@pytest.mark.parametrize("state,empty", [
    ("finalizing", True), ("running", True), ("contained", True),
    ("quarantined", True), ("succeeded", False),
])
def test_cleanup_requires_terminal_empty_attempt(tmp_path, state, empty):
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    assert scratch.remove(record, subject(state=state), verified_empty=empty) is None
    assert Path(record["path"]).is_dir()


@pytest.mark.parametrize("hold", ["salvage_error", "salvage_skipped", "tmpdir_keep"])
def test_salvage_evidence_never_holds_scratch(tmp_path, hold):
    """Review r1 P2-2: salvage snapshots the worktree, never TMPDIR, so its
    evidence holds nothing; `tmpdir_keep` is no longer read either."""
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    (Path(record["path"]) / "venv").mkdir()
    attempt = subject(state="failed", **{hold: {"count": 1} if hold == "salvage_skipped" else "held"})
    assert scratch.remove(record, attempt, verified_empty=True) == "removed"
    assert not Path(record["path"]).exists()


@pytest.mark.parametrize("state,empty", [
    ("quarantined", False), ("running", False), ("succeeded", False), ("failed", True),
])
def test_absent_folder_is_released_whatever_holds_it(tmp_path, state, empty):
    """Review r1 P2-2: a folder that is not there pins nothing, held or not."""
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    shutil.rmtree(record["path"])
    assert scratch.remove(record, subject(state=state, salvage_skipped={"count": 1}),
                          verified_empty=empty) == "absent"


def test_clean_terminal_attempt_is_removed(tmp_path):
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    (Path(record["path"]) / "file").write_bytes(b"x" * 4096)
    assert scratch.remove(record, subject(), verified_empty=True) == "removed"
    assert not Path(record["path"]).exists()


def test_read_only_tree_is_removed_without_touching_anything_outside_it(tmp_path):
    """Review r1 P2-2: Go's module cache is 0555; the review's tree failed every retry."""
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    folder = Path(record["path"])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "kept").write_text("dummy")
    module = folder / "go" / "pkg" / "mod" / "example.com" / "dummy@v1.0.0"
    module.mkdir(parents=True)
    (module / "go.mod").write_text("module example.com/dummy\n")
    (module / "link").symlink_to(outside, target_is_directory=True)
    sealed = folder / "sealed"
    sealed.mkdir()
    (sealed / "inside").write_text("dummy")
    for directory in [module, *module.parents][:4]:
        os.chmod(directory, 0o555)
    os.chmod(sealed, 0o000)
    os.chmod(outside, 0o555)
    root_mode = stat.S_IMODE(scratch.root.stat().st_mode)
    try:
        assert scratch.remove(record, subject(), verified_empty=True) == "removed"
        assert not folder.exists()
        assert stat.S_IMODE(outside.stat().st_mode) == 0o555, "nothing outside the tree changes"
        assert (outside / "kept").read_text() == "dummy"
        assert stat.S_IMODE(scratch.root.stat().st_mode) == root_mode, "nor above it"
    finally:
        os.chmod(outside, 0o755)


def test_cleanup_does_not_follow_nested_symlinks_or_count_their_targets(tmp_path):
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    target = tmp_path / "other-job"
    target.mkdir()
    sentinel = target / "venv"
    sentinel.write_bytes(b"x" * 65536)
    (Path(record["path"]) / "link").symlink_to(target, target_is_directory=True)
    assert scratch.size(record) < 65536
    assert scratch.remove(record, subject(), verified_empty=True) == "removed"
    assert sentinel.read_bytes() == b"x" * 65536


def test_cleanup_releases_but_never_removes_a_folder_that_replaced_the_allocation(tmp_path):
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    path = Path(record["path"])
    path.rename(scratch.root / "held")
    path.mkdir()
    (path / "unrelated").write_text("keep")
    assert scratch.remove(record, subject(state="quarantined"), verified_empty=False) is None
    assert scratch.remove(record, subject(), verified_empty=True) == "replaced"
    assert (path / "unrelated").read_text() == "keep"


def test_directory_renamed_in_after_the_check_is_refused_and_put_back(tmp_path, monkeypatch):
    """Review r1 P3-8: a real directory renamed into place after `owns()` was deleted."""
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    path = Path(record["path"])
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("dummy")
    checked = scratch.presence

    def swap_after_check(value):
        where = checked(value)
        if where == "present":
            path.rename(scratch.root / "moved-away")
            victim.rename(path)
        return where

    monkeypatch.setattr(scratch, "presence", swap_after_check)
    assert scratch.remove(record, subject(), verified_empty=True) is None
    assert (path / "keep").is_file(), "the swapped-in directory is back at its name, untouched"
    assert (path / "keep").read_text() == "dummy"
    assert (scratch.root / "moved-away").is_dir()
    assert not [entry for entry in scratch.root.iterdir() if entry.name.startswith("subfleet-attempt-removing-")]


def test_cleanup_reconciles_scratch_removed_by_its_provider(tmp_path):
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    shutil.rmtree(record["path"])
    assert scratch.remove(record, subject(), verified_empty=True) == "absent"


def test_inaccessible_allocation_is_logged_and_kept_for_retry(tmp_path, monkeypatch, caplog):
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    folder = Path(record["path"])
    original = os.lstat

    def inaccessible(path, *args, **kwargs):
        if isinstance(path, (str, Path)) and Path(path) == folder:
            raise PermissionError("dummy allocation inspection failure")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "lstat", inaccessible)
        patch.setattr(Path, "lstat", lambda self: inaccessible(self))
        assert scratch.remove(record, subject(), verified_empty=True) is None
    assert folder.is_dir()
    assert "PermissionError" in caplog.text


def test_cleanup_never_removes_a_folder_owned_by_someone_else(tmp_path, monkeypatch):
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    uid = os.getuid()
    with monkeypatch.context() as patch:
        patch.setattr(os, "getuid", lambda: uid + 1)
        assert scratch.remove(record, subject(), verified_empty=True) == "replaced"
    assert Path(record["path"]).exists()


def test_sweep_requires_prefix_even_with_valid_creation_record(tmp_path):
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    outside = scratch.root / "unrelated"
    Path(record["path"]).rename(outside)
    store = Sightings()
    store.rows[str(outside)] = sighting(outside)
    store.sweep(scratch, now=10**12)
    assert outside.is_dir()
    assert store.notes == []


def test_sweep_never_follows_symlink(tmp_path):
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    target = scratch.root / "outside-prefix"
    Path(record["path"]).rename(target)
    (target / "payload").write_text("dummy")
    Path(record["path"]).symlink_to(target, target_is_directory=True)
    store = Sightings()
    store.rows[record["path"]] = {**sighting(target), "path": record["path"]}
    for now in (10**12, 10**12 + 1):
        store.sweep(scratch, now=now)
    assert (target / "payload").is_file() and (target / "payload").read_text() == "dummy"
    assert Path(record["path"]).is_symlink()
    assert store.removed == []


def test_unknown_folder_ages_from_the_store_never_its_marker_or_birth_time(tmp_path):
    """Review r1 P3-4: a provider set its marker's `created_at` to 0 and applied
    `utime(0, 0)`, and a sweep removed a folder created seconds earlier."""
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    folder = Path(record["path"])
    marker = folder / ".subfleet-attempt.json"
    forged = {**json.loads(marker.read_text()), "created_at": 0}
    marker.write_text(json.dumps(forged))
    os.utime(folder, (0, 0))
    if hasattr(folder.lstat(), "st_birthtime"):
        assert folder.lstat().st_birthtime == 0, "APFS lets the owner backdate birth time"
    store = Sightings()
    store.sweep(scratch, now=1_000_000)
    assert [note["first_seen_at"] for note in store.notes] == [1_000_000]
    store.sweep(scratch, now=1_000_060)
    assert folder.is_dir(), "seen a minute ago by the store: not old, whatever the folder says"
    store.sweep(scratch, now=1_000_000 + 86_401)
    assert not folder.exists()
    assert [Path(row["path"]) for row in store.removed] == [folder]


def test_unknown_folder_waits_24_hours_and_foreign_store_is_untouched(tmp_path):
    from subfleet.attempt_tmp import AttemptTemp
    scratch = manager(tmp_path)
    record = scratch.create("unknown")
    foreign = AttemptTemp(tmp_path / "other-store", root=scratch.root).create("other-live")
    store = Sightings()
    store.sweep(scratch, now=5_000)
    store.sweep(scratch, now=5_000 + 86_399)
    assert Path(record["path"]).exists()
    store.sweep(scratch, now=5_000 + 86_401)
    assert not Path(record["path"]).exists()
    assert Path(foreign["path"]).exists()
    assert [Path(note["path"]) for note in store.notes] == [Path(record["path"])], "nothing recorded for another store"


@pytest.mark.parametrize("defect", ["no-marker", "copied-marker", "foreign-marker", "file", "named"])
def test_folder_that_is_not_this_stores_unknown_is_never_recorded_or_removed(tmp_path, defect):
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    folder = Path(record["path"])
    marker = folder / ".subfleet-attempt.json"
    if defect == "no-marker":
        marker.unlink()
    elif defect == "copied-marker":
        other = scratch.create("job/a2")
        shutil.copy(Path(other["path"]) / ".subfleet-attempt.json", marker)
    elif defect == "foreign-marker":
        marker.write_text(json.dumps({**json.loads(marker.read_text()), "store_root": "/elsewhere"}))
    elif defect == "file":
        shutil.rmtree(folder)
        folder.write_text("dummy")
    store = Sightings()
    named = {str(folder)} if defect == "named" else ()
    for now in (0, 10**9):
        store.sweep(scratch, now=now, named=named)
    assert folder.exists()
    assert str(folder) not in {note["path"] for note in store.notes}


def test_unknown_folder_swapped_after_its_sighting_starts_a_new_one(tmp_path):
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    folder = Path(record["path"])
    store = Sightings()
    store.sweep(scratch, now=0)
    replacement = scratch.create("job/a2")
    folder.rename(scratch.root / "elsewhere")
    Path(replacement["path"]).rename(folder)
    store.sweep(scratch, now=10**9)
    assert folder.is_dir(), "the store saw a different folder at this path: a new sighting, not an old one"
    assert len(store.notes) == 2 and store.removed == []


def test_removal_error_is_logged_and_retried(tmp_path, monkeypatch, caplog):
    from subfleet import attempt_tmp
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    (Path(record["path"]) / "file").write_text("dummy")
    with monkeypatch.context() as patch:
        patch.setattr(attempt_tmp, "_empty", lambda *a, **kw: (_ for _ in ()).throw(PermissionError("dummy")))
        assert scratch.remove(record, subject(), verified_empty=True) is None
    assert "TMPDIR" in caplog.text
    assert (Path(record["path"]) / "file").read_text() == "dummy", "a failed removal is put back under its name"
    assert scratch.remove(record, subject(), verified_empty=True) == "removed"


def test_tmpdir_value_has_one_trailing_slash(tmp_path):
    """Review r1 P3-7: `"${TMPDIR}name"` must name a file inside the folder."""
    from subfleet.attempt_tmp import env_value
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    value = env_value(record["path"])
    assert value == record["path"] + "/" and env_value(value) == value
    Path(value + "concat").write_text("dummy")
    assert (Path(record["path"]) / "concat").read_text() == "dummy"


OPERATIONS = ["start", "finalize", "quarantine", "resolve", "sweep", "orphan", "swap", "vanish", "advance"]


@settings(max_examples=40, deadline=None)
@given(st.lists(st.tuples(st.sampled_from(OPERATIONS), st.integers(0, 4), st.booleans()),
                min_size=1, max_size=35))
@example([("start", 0, False), ("swap", 0, False), ("vanish", 0, False)])
def test_generated_attempt_lifetimes_preserve_isolation_and_cleanup_authority(steps):
    """Invariants 1, 3, 4, 6 and 8 over known and unknown allocations, holds,
    absent folders, swaps and the passage of time. The model releases known
    allocations as the daemon does: an absent folder at once, anything else
    only for a terminal attempt with a verified-empty census."""
    from subfleet.attempt_tmp import ORPHAN_AGE_S
    with tempfile.TemporaryDirectory(prefix="attempt-model-") as directory:
        scratch = manager(Path(directory))
        states, records, empties, receipts, all_paths = {}, {}, {}, {}, set()
        identities, victims, orphans, disturbed = {}, [], {}, set()
        store, clock = Sightings(), 1_000_000.0
        protected = scratch.root / "not-an-attempt"
        protected.mkdir()
        (protected / "keep").write_text("dummy")
        link = scratch.root / "subfleet-attempt-symlink"
        link.symlink_to(protected, target_is_directory=True)

        def release(name, empty):
            outcome = scratch.remove(records[name], states[name], verified_empty=False)
            if outcome is None and states[name]["state"] in {"succeeded", "lost"} and empty:
                outcome = scratch.remove(records[name], states[name], verified_empty=True)
            if outcome:
                receipts[name] = outcome

        for operation, index, flag in steps:
            name = f"job-{index}/a1"
            known = name in records and name not in receipts
            if operation == "start" and name not in states:
                states[name], empties[name] = subject(name, "running"), False
                records[name] = scratch.create(name)
                found = Path(records[name]["path"]).lstat()
                identities[name] = (found.st_dev, found.st_ino)
                assert records[name]["path"] not in all_paths
                all_paths.add(records[name]["path"])
            elif known and states[name]["state"] == "running" and operation in {"finalize", "quarantine"}:
                states[name]["state"] = "succeeded" if operation == "finalize" else "quarantined"
                empties[name] = flag
                release(name, flag)
            elif known and states[name]["state"] == "quarantined" and operation == "resolve":
                states[name]["state"], empties[name] = "lost", True
            elif operation == "orphan":
                path = scratch.create(f"orphan-{index}")["path"]
                assert path not in all_paths
                all_paths.add(path)
                orphans[path] = None
            elif known and operation == "swap" and Path(records[name]["path"]).is_dir():
                path = Path(records[name]["path"])
                path.rename(scratch.root / f"aside-{len(victims)}")
                path.mkdir()
                (path / "victim").write_text("dummy")
                victims.append(path)
                disturbed.add(name)
            elif known and operation == "vanish" and Path(records[name]["path"]).is_dir():
                # The recorded allocation vanishes; a swapped-in folder at its name is a victim, and stays.
                found = Path(records[name]["path"]).lstat()
                if (found.st_dev, found.st_ino) == identities[name]:
                    shutil.rmtree(records[name]["path"])
                    disturbed.add(name)
            elif operation == "advance":
                clock += (index + 1) * 6 * 3600
            elif operation == "sweep":
                for key in [key for key in records if key not in receipts]:
                    if states[key]["state"] != "running":
                        release(key, empties[key] or states[key]["state"] == "lost")
                before = {path: Path(path).exists() for path in orphans}
                store.sweep(scratch, now=clock, named={record["path"] for record in records.values()})
                for path, existed in before.items():
                    if existed and not Path(path).exists():
                        first = min(note["first_seen_at"] for note in store.notes if note["path"] == path)
                        assert clock - first >= ORPHAN_AGE_S, "an unknown folder goes only 24 h after the store saw it"
            for key, row in states.items():
                path = Path(records[key]["path"])
                if (row["state"] in {"running", "quarantined"} or not empties[key]) and key not in disturbed:
                    # Invariant 3: a held allocation is never removed.
                    found = path.lstat()
                    assert (found.st_dev, found.st_ino) == identities[key] and key not in receipts
            for victim in victims:
                assert (victim / "victim").read_text() == "dummy", "a swapped-in folder is never removed"
            assert protected.is_dir() and link.is_symlink() and (protected / "keep").read_text() == "dummy"
        # No permanent pin: once nothing holds them, every known allocation is released.
        for key in records:
            if key not in receipts and (not Path(records[key]["path"]).exists()
                                        or (states[key]["state"] in {"succeeded", "lost"} and empties[key])):
                release(key, True)
                assert key in receipts, (key, states[key])


def test_known_allocation_survives_marker_removal_then_cleans_up(tmp_path):
    scratch = manager(tmp_path)
    record = scratch.create("job/a1")
    folder = Path(record["path"])
    (folder / ".subfleet-attempt.json").unlink()
    (folder / "payload").write_bytes(b"x" * 65536)
    assert scratch.size(record) >= 65536
    assert scratch.remove(record, subject(state="quarantined"), verified_empty=True) is None
    assert scratch.remove(record, subject(), verified_empty=False) is None
    assert scratch.remove(record, subject(), verified_empty=True) == "removed"
    assert not folder.exists()
