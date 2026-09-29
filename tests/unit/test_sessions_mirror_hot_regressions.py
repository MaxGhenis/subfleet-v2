"""C-23.28: bounded retries and exact refresh invalidation for hot flag sync."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

from subfleet.sessions import desktop, mirror
from tests import sessions_fixtures as fx

IDENTITIES = ("session-000", "session-001", "session-002")
ONE, TWO, THREE = IDENTITIES
ACCOUNTS = tuple((f"account-{letter}", f"org-{letter}") for letter in "abc")


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = fx.claude_home(tmp_path, monkeypatch)
    store = fx.desktop_store(tmp_path, monkeypatch)
    root = tmp_path / "state"
    root.mkdir()
    log = tmp_path / "main.log"
    log.write_text("")
    monkeypatch.setenv(desktop.LOG_ENV, str(log))
    # Scheduling in these focused regressions is explicit and independent of
    # host load; the configured production hot interval remains two seconds.
    monkeypatch.setattr(mirror.time, "monotonic", lambda: 1000.0)
    for identity in IDENTITIES:
        fx.transcript(home, identity, fx.completed())
        for account, org in ACCOUNTS:
            fx.index_entry(store, account, org, identity,
                           settings={"ultracode": True})
    running = mirror.Mirror(root, fx.policy(mirror_hot_interval_s=2), now=lambda: fx.NOW)
    assert running.run_once().state == "ok"
    assert running.run_hot().state == "ok"
    return home, store, running


def entry(world, account=0, identity=ONE):
    return world[1].joinpath(*ACCOUNTS[account], f"local_{identity}.json")


def read(path):
    return json.loads(path.read_text())


def rewrite(path, **changes):
    temporary = path.with_suffix(".app")
    temporary.write_text(json.dumps({**read(path), **changes}))
    temporary.replace(path)


def unreadable(monkeypatch, bad):
    original = mirror._read_entry

    def failing(path):
        if Path(path) == bad:
            raise OSError(errno.EIO, "injected unreadable copy")
        return original(path)

    monkeypatch.setattr(mirror, "_read_entry", failing)


def service(world):
    """Run the same synchronous worker used by a full pass's lock holder."""
    running = world[2]
    if running._hot_worker is None:
        running._hot_worker = running._fork_hot()
        running._hot_options = mirror.Options()
    before = running._hot_epoch
    running._service_hot()
    return running._hot_epoch - before


def test_hot_keeps_unread_copy_owner_and_propagates_unrelated_archive(world, monkeypatch):
    """A listing's recovered owner survives the hot flag-input reread (B1)."""
    running = world[2]
    bad = entry(world, 2)
    rewrite(bad, lastFocusedAt=1)
    unreadable(monkeypatch, bad)
    full = running.run_once()
    assert full.flags_held == 1
    rewrite(entry(world, identity=TWO), isArchived=True)

    hot = running.run_hot()

    assert hot.state == "ok", hot.error
    assert hot.flags_held == 1
    assert running._unread[str(bad)] == ONE
    assert running._flag_retry == {ONE}
    assert all(read(entry(world, account, TWO))["isArchived"] for account in range(3))
    assert read(running.flags_path)[TWO]["isArchived"]


@pytest.mark.parametrize("change", ["idle", "focus", "transcript-stamp", "held"])
def test_service_without_standing_write_or_base_decision_change_keeps_epoch(world, monkeypatch, change):
    """Candidates, nonflag bases, and held decisions do not invalidate a refresh (B2)."""
    running = world[2]
    before = read(running.flags_path)
    if change != "idle":
        rewrite(entry(world), lastFocusedAt=1)
    if change == "transcript-stamp":
        transcript = running._stems[ONE]
        info = transcript.stat()
        os.utime(transcript, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
    if change == "held":
        bad = entry(world, 2)
        rewrite(bad, lastFocusedAt=1)
        unreadable(monkeypatch, bad)

    assert service(world) == 0

    after = read(running.flags_path)
    if change == "transcript-stamp":
        assert after[ONE]["tmt"] != before[ONE]["tmt"]
    else:
        assert after == before
    if change == "held":
        assert running.sidecar()["hot"]["flags_held"] == 1
        assert service(world) == 0, "an unchanged held retry must not invalidate another refresh"


@pytest.mark.parametrize("field,value", [
    ("isArchived", True), ("isStarred", True), ("title", "A manual rename"),
])
def test_service_base_only_flag_advance_increments_epoch(world, monkeypatch, field, value):
    """Already agreeing copies can still advance the base and invalidate a refresh (B2)."""
    running = world[2]
    copy_writes = []
    original = mirror._prepare_json

    def observe(path, body):
        if Path(path).parent in {entry(world, account).parent for account in range(3)}:
            copy_writes.append(Path(path))
        return original(path, body)

    monkeypatch.setattr(mirror, "_prepare_json", observe)       # every flag copy write
    for account in range(3):
        rewrite(entry(world, account), **{field: value})

    assert service(world) == 1
    assert read(running.flags_path)[ONE][field] == value
    assert copy_writes == [], "this must exercise a base-only advance"
    assert service(world) == 0, "the next service has no newer decision"


def test_service_new_base_row_increments_epoch_without_copy_write(world, monkeypatch):
    """The first base for a newly observed identity invalidates a refresh (B2)."""
    identity = "session-new"
    for account, org in ACCOUNTS:
        fx.index_entry(world[1], account, org, identity, settings={"ultracode": True})
    writes = []
    original = mirror._prepare_json

    def observe(path, body):
        if Path(path).name == f"local_{identity}.json":
            writes.append(path)
        return original(path, body)

    monkeypatch.setattr(mirror, "_prepare_json", observe)       # every flag copy write
    assert service(world) == 1
    assert identity in read(world[2].flags_path)
    assert writes == []


def test_service_failed_base_publication_does_not_increment_epoch(world, monkeypatch):
    """A resolved base only counts after it was successfully published (B2)."""
    running = world[2]
    before = read(running.flags_path)
    for account in range(3):
        rewrite(entry(world, account), isArchived=True)
    original = mirror._write_json

    def fail_base(path, body, **kwargs):
        if Path(path) == running.flags_path:
            raise OSError(errno.ENOSPC, "injected base publication failure")
        return original(path, body, **kwargs)

    monkeypatch.setattr(mirror, "_write_json", fail_base)
    assert service(world) == 0
    assert read(running.flags_path) == before
    assert running.sidecar()["hot"]["state"] == "error"


@pytest.mark.parametrize("rollback_fails", [False, True], ids=["put-back", "standing-write"])
def test_service_epoch_tracks_writes_left_after_a_failed_batch(world, monkeypatch, rollback_fails):
    """A failed batch invalidates only when a copy write survives the put-back
    (B2). One that survives makes the decision stand for the copies it reached
    (review round 5, F2): the base advances, and the copy the batch did not
    reach keeps its own value as its reference."""
    running = world[2]
    rewrite(entry(world), isArchived=True)
    install, prepare = mirror._install, mirror._prepare_json
    attempted = []

    def fail_last_copy(temporary, destination, **kwargs):
        if Path(destination) == entry(world, 2):
            attempted.append("last-copy")
            raise OSError(errno.ENOSPC, "injected final copy failure")
        return install(temporary, destination, **kwargs)

    def fail_put_back(path, body):
        if Path(path) == entry(world, 1) and body.get("isArchived") is False:
            attempted.append("put-back")
            if rollback_fails:
                raise OSError(errno.ENOSPC, "injected put-back failure")
        return prepare(path, body)

    monkeypatch.setattr(mirror, "_install", fail_last_copy)
    monkeypatch.setattr(mirror, "_prepare_json", fail_put_back)
    assert service(world) == int(rollback_fails)
    assert attempted == ["last-copy", "put-back"]
    assert read(entry(world, 1))["isArchived"] is rollback_fails
    record = read(running.flags_path)[ONE]
    assert record["isArchived"] is rollback_fails
    if rollback_fails:
        c_key = "/".join((*ACCOUNTS[2], f"local_{ONE}.json"))
        assert record["refs"] == {c_key: {"isArchived": False}}
    else:
        assert "refs" not in record
    assert running.sidecar()["hot"]["flags_held"] == 1
    assert not running.publish_path.exists(), "resolved into the base"



def test_unobservable_copy_write_invalidates_refresh_and_preserves_unarchive(world, monkeypatch):
    """B2/P1: B survives a failed post-write stat and C write; refresh before unarchive."""
    running = world[2]
    original_checkpoint = running._checkpoint
    original_inventory = running._flag_inventory
    original_write, original_signature = mirror._install, mirror._signature_of
    inventories, partial, epochs = [], [], []
    started = False
    fail_b_stat = False
    failed_c = False

    def checkpoint(current, stage=None):
        nonlocal started
        if stage == "resolving flags" and not started:
            started = True
            running._service_hot()  # An idle service makes the full pass refresh.
        return original_checkpoint(current, stage)

    def write(temporary, destination, **kwargs):
        nonlocal fail_b_stat, failed_c
        if Path(destination) == entry(world, 2) and kwargs.get("keep") and not failed_c:
            failed_c = True
            raise OSError(errno.ENOSPC, "injected C write failure")
        placed = original_write(temporary, destination, **kwargs)
        if Path(destination) == entry(world, 1) and kwargs.get("keep") and not failed_c:
            assert placed
            fail_b_stat = True
        return placed

    def signature(path):
        nonlocal fail_b_stat
        if Path(path) == entry(world, 1) and fail_b_stat:
            fail_b_stat = False
            raise OSError(errno.EIO, "injected B post-write stat failure")
        return original_signature(path)

    def inventory(current, options):
        snapshot = original_inventory(current, options)
        inventories.append(snapshot)
        if len(inventories) == 1:
            # All three old projections are retained by the full refresh.
            rewrite(entry(world), isArchived=True)
            running._service_hot()
            partial.extend(read(entry(world, account))["isArchived"] for account in range(3))
            epochs.append(running._hot_epoch)
            assert partial == [True, True, False]
            # B's write stands, so the archive stands for the copies it
            # reached; C keeps its own value as its reference (F2).
            assert read(running.flags_path)[ONE]["isArchived"]
            assert running.sidecar()["hot"]["flags_held"] == 1
        return snapshot

    monkeypatch.setattr(running, "_checkpoint", checkpoint)
    monkeypatch.setattr(running, "_flag_inventory", inventory)
    monkeypatch.setattr(mirror, "_install", write)
    monkeypatch.setattr(mirror, "_signature_of", signature)
    full = running.run_once()
    assert full.state == "ok", full.error
    assert failed_c and not fail_b_stat

    rewrite(entry(world), isArchived=False)
    hot = running.run_hot()
    assert hot.state == "ok", hot.error
    assert all(not read(entry(world, account))["isArchived"] for account in range(3)), \
        "the stale full snapshot caused the user's unarchive to be lost"
    assert not read(running.flags_path)[ONE]["isArchived"]
    assert epochs == [1], "the unobservable successful write must advance the epoch"
    assert len(inventories) == 2, "the full pass must discard its stale first refresh"


def test_service_settings_only_copy_write_increments_epoch(world):
    """Any standing updated copy counts, even with unchanged decision fields (B2)."""
    running = world[2]
    before = read(running.flags_path)
    rewrite(entry(world), sessionSettings={})
    assert service(world) == 1
    assert read(entry(world))["sessionSettings"]["ultracode"]
    assert read(running.flags_path) == before


def test_failed_put_back_of_an_app_replacement_is_not_journaled_and_its_change_wins(
        world, monkeypatch):
    """A write replaced by the app before a failed put-back is no longer ours
    (B2): it is not journaled. The mirror's write did land, so the archive
    stands for the copies it reached (F2), and the app's later unarchive of
    that copy then reads as a change and wins."""
    running = world[2]
    rewrite(entry(world), isArchived=True)
    install, prepare = mirror._install, mirror._prepare_json
    replaced = []

    def fail_last_copy(temporary, destination, **kwargs):
        if Path(destination) == entry(world, 2):
            raise OSError(errno.ENOSPC, "injected final copy failure")
        return install(temporary, destination, **kwargs)

    def app_replaces_then_disk_fails(path, body):
        path = Path(path)
        if path == entry(world, 1) and body.get("isArchived") is False:
            rewrite(path, isArchived=False, lastFocusedAt=42)
            replaced.append(path)
            raise OSError(errno.ENOSPC, "injected put-back failure after app replacement")
        return prepare(path, body)

    monkeypatch.setattr(mirror, "_install", fail_last_copy)
    monkeypatch.setattr(mirror, "_prepare_json", app_replaces_then_disk_fails)
    assert service(world) == 1, "the base's decision moved"
    assert replaced == [entry(world, 1)]
    assert read(entry(world, 1))["lastFocusedAt"] == 42
    b_name = f"local_{ONE}.json"
    assert not [row for row in running.journal.rows()
                if row.name == b_name and row.folder == "/".join(ACCOUNTS[1])
                and row.ctime_ns == os.stat(entry(world, 1)).st_ctime_ns]
    assert read(running.flags_path)[ONE]["isArchived"]
    monkeypatch.setattr(mirror, "_install", install)
    monkeypatch.setattr(mirror, "_prepare_json", prepare)
    service(world)
    assert not any(read(entry(world, account))["isArchived"] for account in range(3))
    assert not read(running.flags_path)[ONE]["isArchived"]


def test_base_only_service_during_refresh_preserves_a_later_unarchive(world, monkeypatch):
    """A stale full snapshot must not restore the pre-service merge base (B2)."""
    running = world[2]
    original_checkpoint = running._checkpoint
    original_inventory = running._flag_inventory
    inventories = []
    first_service = []

    def checkpoint(current, stage=None):
        if stage == "resolving flags" and not first_service:
            # A preceding idle service makes this full pass refresh its flag
            # inventory, just as a due checkpoint during its first scan does.
            running._service_hot()
            first_service.append(True)
        return original_checkpoint(current, stage)

    def inventory(current, options):
        snapshot = original_inventory(current, options)
        inventories.append(snapshot)
        if len(inventories) == 1:
            # The refresh captured every old copy. The user archives all of
            # them and a checkpoint service advances only the base, with no
            # copy write. Returning the retained snapshot models that exact
            # interleaving at the final inventory checkpoint.
            for account in range(3):
                rewrite(entry(world, account), isArchived=True)
            running._service_hot()
            assert read(running.flags_path)[ONE]["isArchived"]
        return snapshot

    monkeypatch.setattr(running, "_checkpoint", checkpoint)
    monkeypatch.setattr(running, "_flag_inventory", inventory)
    full = running.run_once()
    assert full.state == "ok", full.error
    assert full.flags_held == 0
    assert len(inventories) == 2, "base-only advancement must invalidate the stale refresh"
    assert read(running.flags_path)[ONE]["isArchived"]

    rewrite(entry(world), isArchived=False)
    hot = running.run_hot()
    assert hot.state == "ok", hot.error
    assert all(not read(entry(world, account))["isArchived"] for account in range(3))
    assert not read(running.flags_path)[ONE]["isArchived"]


def test_nonflag_saves_during_slow_refresh_do_not_hold_every_session(world, monkeypatch):
    """Due services with candidates but no changed decisions cannot exhaust refreshes (B2)."""
    running = world[2]
    instant = [1000.0]
    monkeypatch.setattr(mirror.time, "monotonic", lambda: instant[0])
    original = running._entry
    reads = []

    def slow(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        reads.append(path)
        rewrite(entry(world, identity=THREE), lastFocusedAt=len(reads))
        instant[0] += 3
        return result

    monkeypatch.setattr(running, "_entry", slow)
    running._last_sweep = None
    full = running.run_once()
    assert full.state == "ok", full.error
    assert running._hot_services > 1
    assert running._hot_epoch == 0
    assert full.flags_held == 0
    assert running._flag_retry == set()


@pytest.mark.parametrize("hold", ["unreadable", "publication"])
def test_full_pass_retries_only_the_identity_sync_flags_held(world, monkeypatch, hold):
    """A single hold never recruits every idle identity into the hot retry set (B3)."""
    running = world[2]
    bad = entry(world, 2)
    if hold == "unreadable":
        rewrite(bad, lastFocusedAt=1)
        unreadable(monkeypatch, bad)
    else:
        rewrite(entry(world), isArchived=True)
        original = mirror._install

        def fail_copy(temporary, destination, **kwargs):
            if Path(destination) == bad:
                return False                    # the app saved it after the check
            return original(temporary, destination, **kwargs)

        monkeypatch.setattr(mirror, "_install", fail_copy)

    full = running.run_once()
    assert full.flags_held == 1
    assert running._flag_retry == {ONE}
    hot = running.run_hot()
    assert hot.sessions == 1
    assert hot.flags_held == 1
    assert running._flag_retry == {ONE}


@pytest.mark.parametrize("blind", ["unknown-copy", "changed-folder"])
def test_blind_holds_wait_for_full_inventory_without_populating_hot_retries(world, monkeypatch, blind):
    """Unbounded holds belong to the 60-second full inventory, not every hot tick (B3)."""
    running = world[2]
    rewrite(entry(world), isArchived=True)
    if blind == "unknown-copy":
        bad = fx.index_entry(world[1], *ACCOUNTS[2], "session-unknown",
                             settings={"ultracode": True})
        unreadable(monkeypatch, bad)
    else:
        bad = entry(world, 2)
        rewrite(bad, lastFocusedAt=1)
        original = mirror.os.scandir

        def unlisted(path):
            if Path(path) == bad.parent:
                raise OSError(errno.EIO, "injected changed-folder listing failure")
            return original(path)

        monkeypatch.setattr(mirror.os, "scandir", unlisted)

    full = running.run_once()
    assert full.flags_held == len(IDENTITIES)
    assert running._flag_retry == set()
    assert not read(entry(world, 1))["isArchived"]


def test_retried_identity_is_dropped_when_its_last_copy_disappears(world, monkeypatch):
    """An identity whose copies vanished cannot keep hot passes busy forever (B3)."""
    running = world[2]
    bad = entry(world, 2)
    rewrite(bad, lastFocusedAt=1)
    with monkeypatch.context() as blocked:
        unreadable(blocked, bad)
        assert running.run_once().flags_held == 1
    assert running._flag_retry == {ONE}
    for account in range(3):
        entry(world, account).unlink()

    for _ in range(2):
        result = running.run_hot()
        assert result.state == "ok", result.error
        assert result.sessions == 0
        assert running._flag_retry == set()
        assert ONE not in running._retry
