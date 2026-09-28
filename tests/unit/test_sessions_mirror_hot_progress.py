"""C-23.28: flag sync progresses while the full inventory is slow.

All records, transcripts, state, and app logs belong to the test fixture.
The scheduling regressions advance a clock between reads, never sleep for a
production interval, and observe the store before the full pass completes.
"""

from __future__ import annotations

import errno
import json
import threading
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pytest

from subfleet.sessions import desktop, mirror
from tests import sessions_fixtures as fx

ONE, TWO = "session-000", "session-001"
ACCOUNTS = (("account-a", "org-a"), ("account-b", "org-b"))


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = fx.claude_home(tmp_path, monkeypatch)
    store = fx.desktop_store(tmp_path, monkeypatch)
    root = tmp_path / "state"
    root.mkdir()
    log = tmp_path / "main.log"
    log.write_text("")
    monkeypatch.setenv(desktop.LOG_ENV, str(log))
    for identity in (ONE, TWO):
        fx.transcript(home, identity, fx.completed())
        for account, org in ACCOUNTS:
            fx.index_entry(store, account, org, identity,
                           settings={"ultracode": True})
    return home, store, root


def engine(world):
    return mirror.Mirror(world[2], fx.policy(mirror_hot_interval_s=2),
                         now=lambda: fx.NOW)


def entry(world, account=0, identity=ONE):
    return world[1].joinpath(*ACCOUNTS[account], f"local_{identity}.json")


def read(path):
    return json.loads(path.read_text())


def rewrite(path, **changes):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({**read(path), **changes}))
    temporary.replace(path)


def warm(running):
    assert running.run_once().state == "ok"
    assert running.run_hot().state == "ok"


def test_cold_inventory_reads_each_copy_once_when_hot_service_would_be_due(world, monkeypatch):
    """C-23.28: a cold pass completes its only inventory before forking a hot
    worker, even when every entry read spans the configured service interval.
    """
    running = engine(world)
    clock = [0.0]
    monkeypatch.setattr(mirror.time, "monotonic", lambda: clock[0])
    original_read = mirror._read_entry
    reads = []

    def slow_read(path):
        reads.append(Path(path))
        clock[0] += 3
        return original_read(path)

    def premature_fork():
        pytest.fail("a cold process forked a second whole-store inventory")

    monkeypatch.setattr(mirror, "_read_entry", slow_read)
    monkeypatch.setattr(running, "_fork_hot", premature_fork)
    result = running.run_once()
    expected = {entry(world, account, identity)
                for account in range(2) for identity in (ONE, TWO)}
    assert result.state == "ok", result.error
    assert Counter(reads) == Counter({path: 1 for path in expected})
    assert result.entries_scanned == len(expected)
    assert running._inventoried and running._hot_services == 0


def test_slow_full_inventory_services_archive_before_its_next_entry(world, monkeypatch):
    """C-23.28: an elapsed hot interval is served at the next read checkpoint,
    once a prior full inventory exists, with both sidecars truthful.
    The full snapshot already read the old value and must not regress the base.
    """
    running = engine(world)
    warm(running)
    previous_ok = running.sidecar().get("last_ok_at")
    source, target = entry(world), entry(world, 1)
    offset = [0.0]
    clock = mirror.time.monotonic
    monkeypatch.setattr(mirror.time, "monotonic", lambda: clock() + offset[0])
    running.now = lambda: fx.NOW + timedelta(seconds=offset[0])
    running._last_sweep = None
    original_entry = running._entry
    changed = False
    observed = []

    def slow_entry(path, *args, **kwargs):
        nonlocal changed
        if changed and not observed:
            assert read(target)["isArchived"], "full inventory starved due flag sync"
            sidecar = running.sidecar()
            assert sidecar["pass"]["state"] == "running"
            assert sidecar["pass"]["finished_at"] is None
            assert sidecar["hot"]["kind"] == "hot"
            assert sidecar["hot"]["state"] == "ok"
            assert sidecar["last_ok_at"] == previous_ok
            observed.append(sidecar)
        result = original_entry(path, *args, **kwargs)
        if Path(path) == source and not changed:
            # The full pass has captured false. A user now archives the copy;
            # time advances as if the read took longer than the 2-second timer.
            rewrite(source, isArchived=True)
            changed = True
            offset[0] += 3
        return result

    monkeypatch.setattr(running, "_entry", slow_entry)
    result = running.run_once()
    assert result.state == "ok", result.error
    assert observed, "the assertion must run while the full pass is still scanning"
    assert all(read(entry(world, account))["isArchived"] for account in range(2))
    assert read(running.flags_path)[ONE]["isArchived"], "stale full snapshot regressed the base"
    assert running.sidecar()["pass"]["state"] == "ok"


@pytest.mark.parametrize("field,initial,changed", [
    ("isArchived", False, True),
    ("isArchived", True, False),
    ("isStarred", False, True),
    ("isStarred", True, False),
    ("title", "a session", "A manual rename"),
])
def test_hot_pass_syncs_existing_flags_in_both_directions(world, field, initial, changed):
    """C-23.28: a hot pass propagates archive, star, and title changes from
    every copy against the merge base, including an unarchive or unstar.
    """
    for account in range(2):
        rewrite(entry(world, account), **{field: initial})
    running = engine(world)
    warm(running)
    updates = {field: changed}
    if field == "title":
        updates["titleSource"] = "manual"
    rewrite(entry(world), **updates)
    result = running.run_hot()
    assert result.state == "ok", result.error
    assert all(read(entry(world, account))[field] == changed for account in range(2))
    assert read(running.flags_path)[ONE][field] == changed
    assert read(running.flags_path)[TWO]["isArchived"] is False, "unseen bases survive hot sync"


@pytest.mark.parametrize("failure", ["read", "listing"])
def test_hot_flag_holds_unknown_copies_and_retries_without_another_edit(world, monkeypatch, failure):
    """C-23.28: an unreadable copy or changed unlisted folder holds flags and
    records its cause. Known held sessions retry hot without another edit;
    blind listing holds wait for the next full inventory.
    """
    running = engine(world)
    warm(running)
    source, target = entry(world), entry(world, 1)
    rewrite(source, isArchived=True)
    rewrite(target)  # force discovery; the old directory signature is no proof
    original_read, original_scandir = mirror._read_entry, mirror.os.scandir

    def unreadable(path):
        if Path(path) == target:
            raise OSError(errno.EMFILE, "injected exhausted file descriptors")
        return original_read(path)

    def unlisted(path):
        if Path(path) == target.parent:
            raise OSError(errno.EMFILE, "injected exhausted file descriptors")
        return original_scandir(path)

    with monkeypatch.context() as blocked:
        if failure == "read":
            blocked.setattr(mirror, "_read_entry", unreadable)
        else:
            blocked.setattr(mirror.os, "scandir", unlisted)
        result = running.run_hot()
    assert result.flags_held >= 1
    assert result.held_by and any(str(target.parent) in cause["path"] for cause in result.held_by)
    assert not read(target)["isArchived"]
    assert not read(running.flags_path)[ONE]["isArchived"]
    sidecar = running.sidecar()["hot"]
    assert sidecar["flags_held"] == result.flags_held
    assert sidecar["held_by"] == result.held_by
    retried = running.run_hot() if failure == "read" else running.run_once()
    assert retried.state == "ok" and retried.flags_held == 0
    assert read(target)["isArchived"]
    assert read(running.flags_path)[ONE]["isArchived"]


def test_cooperative_hot_never_nests_a_flag_transaction(world, monkeypatch):
    """C-23.28: a due checkpoint inside flag resolution cannot recursively
    resolve against a merge base the outer transaction has not committed.
    """
    running = engine(world)
    warm(running)
    rewrite(entry(world), isArchived=True)
    offset = [0.0]
    clock = mirror.time.monotonic
    monkeypatch.setattr(mirror.time, "monotonic", lambda: clock() + offset[0])
    original_sync, original_entry = mirror.Mirror.sync_flags, running._entry
    active, kinds = [], []

    def sync(self, folder_files, stems, options, current, **kwargs):
        assert not active, "nested hot sync can overwrite the outer merge base"
        active.append(current.kind)
        kinds.append(current.kind)
        if current.kind == "full":
            offset[0] += 3  # its own next checkpoint finds hot service due
        try:
            return original_sync(self, folder_files, stems, options, current, **kwargs)
        finally:
            active.pop()

    def advance_once(path, *args, **kwargs):
        result = original_entry(path, *args, **kwargs)
        if not kinds:
            offset[0] += 3
        return result

    monkeypatch.setattr(mirror.Mirror, "sync_flags", sync)
    monkeypatch.setattr(running, "_entry", advance_once)
    running._last_sweep = None
    result = running.run_once()
    assert result.state == "ok", result.error
    assert "hot" in kinds and "full" in kinds
    assert read(running.flags_path)[ONE]["isArchived"]
    assert all(read(entry(world, account))["isArchived"] for account in range(2))


@pytest.mark.parametrize("winner", ["run_once", "run_hot"])
@pytest.mark.parametrize("competitor", ["run_once", "run_hot"])
def test_all_pass_pairs_exclude_concurrent_account_writers(world, monkeypatch, winner, competitor):
    """C-23.28: for every full/hot pairing, an account write held in progress
    excludes a second writer and the losing pass cannot overwrite its sidecar.
    """
    first, second = engine(world), engine(world)
    warm(first)
    warm(second)
    identity = "session-new"
    fx.transcript(world[0], identity, fx.completed())
    fx.index_entry(world[1], *ACCOUNTS[0], identity, settings={"ultracode": True})
    entered, release = threading.Event(), threading.Event()
    original_install = mirror._install
    writers, maximum, outcomes = [], [], []

    def install(temporary, destination, **kwargs):
        if Path(destination).parent == entry(world, 1).parent:
            writers.append(threading.get_ident())
            maximum.append(len(writers))
            entered.set()
            try:
                assert release.wait(5), "test did not release the writer gate"
                return original_install(temporary, destination, **kwargs)
            finally:
                writers.pop()
        return original_install(temporary, destination, **kwargs)

    def run():
        try:
            outcomes.append(getattr(first, winner)())
        except BaseException as exc:
            outcomes.append(exc)

    monkeypatch.setattr(mirror, "_install", install)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert entered.wait(5), "first pass did not start its account write"
        before = first.sidecar_path.read_bytes()
        result = getattr(second, competitor)()
        assert result.error and "holds the lock" in result.error
        assert first.sidecar_path.read_bytes() == before
        assert maximum == [1], "two passes wrote account copies concurrently"
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert len(outcomes) == 1 and isinstance(outcomes[0], mirror.Pass), outcomes
    assert outcomes[0].state == "ok"
    assert read(entry(world, 1, identity))["cliSessionId"] == identity


@pytest.mark.parametrize("continuous", [False, True], ids=["retry-refresh", "defer-churn"])
def test_slow_flag_refresh_services_edits_without_regressing_the_base(world, monkeypatch, continuous):
    """C-23.28: refreshing a stale full flag snapshot is cooperative too.
    A hot change invalidates that snapshot; two invalidated refreshes hold the
    full decision rather than loop forever or publish its obsolete base.
    """
    running = engine(world)
    warm(running)
    source, target = entry(world), entry(world, 1)
    offset = [0.0]
    clock = mirror.time.monotonic
    monkeypatch.setattr(mirror.time, "monotonic", lambda: clock() + offset[0])
    running._last_sweep = None
    original_entry = running._entry
    original_inventory = getattr(running, "_flag_inventory", None)
    refreshes, edited, observed = [], set(), []
    pending = []
    phase = [0]

    def inventory(current, options):
        refreshes.append(len(refreshes) + 1)
        phase[0] = len(refreshes)
        try:
            return original_inventory(current, options)
        finally:
            phase[0] = 0

    def slow_entry(path, *args, **kwargs):
        if pending:
            expected, during = pending.pop()
            assert read(target)["isArchived"] is expected, "flag refresh starved due hot sync"
            assert read(running.flags_path)[ONE]["isArchived"] is expected
            observed.append(during)
        data = original_entry(path, *args, **kwargs)
        during = phase[0]
        if (Path(path) == source and during not in edited
                and (during < 2 or continuous)):
            # First archive during the initial scan, then unarchive during its
            # refresh; continuous churn archives again during the second one.
            value = during % 2 == 0
            rewrite(source, isArchived=value)
            edited.add(during)
            pending.append((value, during))
            offset[0] += 3
        return data

    monkeypatch.setattr(running, "_flag_inventory", inventory, raising=False)
    monkeypatch.setattr(running, "_entry", slow_entry)
    result = running.run_once()
    assert result.state == "ok", result.error
    assert refreshes == [1, 2], "refresh retries are bounded, even during continuous edits"
    assert {0, 1} <= set(observed), "hot sync ran in initial and refresh inventories"
    assert all(read(entry(world, account))["isArchived"] is continuous for account in range(2))
    assert read(running.flags_path)[ONE]["isArchived"] is continuous
    if continuous:
        assert 2 in observed
        assert result.flags_held >= 1
        assert any("hot sync advanced" in cause["reason"] for cause in result.held_by)
        assert running.sidecar()["pass"]["flags_held"] == result.flags_held
    else:
        assert result.flags_held == 0
