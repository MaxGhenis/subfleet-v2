"""Which session folders the app settles in, learned from its log: C-23.28a.

`subfleet/sessions/folder_kinds.py` reads the app's loads and logouts and
keeps, per `<account>/<org>` folder, whether the app ever settled there and
whether that account owns the org. A switch folder (never settled in, its org
owned by another account) is never seeded. These tests use the app's own log
lines with the ids replaced, and Hypothesis for the registry's invariants:

* **Idempotence.** Folding the same events again changes nothing, so a
  restart's replay, or a second process, never counts twice.
* **Chunking.** Folding events in any number of polls equals folding them at once.
* **Monotone.** Facts only accumulate: a folder once settled stays settled,
  an owner stays an owner.
* **Narrow exclusion.** A switch folder was never settled in and its org has
  another owner; a settled folder is never a switch folder.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet.sessions import desktop, folder_kinds
from subfleet.sessions.desktop import Event

UTC = timezone.utc
T0 = datetime(2026, 9, 14, 18, 0, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def load(folder: str, seconds: float, *, missing: bool = False) -> Event:
    return Event(at(seconds), folder, missing)


def logout(seconds: float) -> Event:
    return Event(at(seconds), None)


def classify(events, folders, *, now: float, path: Path | None = None) -> dict[str, str]:
    registry = folder_kinds.Registry(path or Path("/nonexistent/registry.json"))
    registry.observe(events)
    return registry.classify(folders, now=at(now).timestamp())


def test_a_switch_folder_is_one_never_settled_in_whose_org_another_account_owns():
    """C-23.28a, the 2026-09-24 switch in miniature: the old account with the
    new org for a second, then the new account, which stays."""
    events = [load("old/org-old", 0), load("new/org-new", 3600),
              load("old/org-new", 7200, missing=True), load("new/org-new", 7201)]
    verdicts = classify(events, ["old/org-old", "new/org-new", "old/org-new", "x/org-new",
                                 "never/seen"], now=7201 + 3600)
    assert verdicts == {"old/org-old": "settled", "new/org-new": "settled",
                        "old/org-new": "switch", "x/org-new": "switch",
                        "never/seen": "unknown"}


def test_a_real_login_a_logout_ends_is_settled_however_short():
    """C-23.28a: on 2026-09-22 two real logins lasted 27 s and 55 s before a
    logout; a mid-switch pair is always followed by the next load. A stay
    shorter than `SETTLE_S` settles its folder when a logout ends it, and not
    when the next load does."""
    short = folder_kinds.SETTLE_S / 2
    verdicts = classify([load("brief/org-brief", 0), logout(short), load("home/org-home", 60)],
                        ["brief/org-brief", "home/org-home"], now=60 + 30)
    assert verdicts["brief/org-brief"] == "settled"
    verdicts = classify([load("brief/org-brief", 0), load("home/org-home", short)],
                        ["brief/org-brief"], now=short + 30)
    assert verdicts["brief/org-brief"] == "unknown"


def test_an_org_nobody_owns_excludes_nothing():
    """C-23.28a: ownership needs a stay of `OWNER_S`; a folder of an org whose
    only stays were short is unknown, so it is seeded as before."""
    events = [load("a/org-shared", 0), load("a/org-own", 19), load("b/org-shared", 4000)]
    verdicts = classify(events, ["a/org-shared", "b/org-shared", "c/org-shared"], now=4010)
    assert set(verdicts.values()) == {"unknown"}


def test_the_open_loads_stay_counts_until_now_or_a_logout():
    """C-23.28a: the latest load's stay is still running; it settles the
    folder after `SETTLE_S`, makes an owner after `OWNER_S`, and a logout ends it."""
    events = [load("a/org-a", 0)]
    assert classify(events, ["a/org-a"], now=5)["a/org-a"] == "unknown"
    assert classify(events, ["a/org-a"], now=folder_kinds.SETTLE_S)["a/org-a"] == "settled"
    registry = folder_kinds.Registry(Path("/nonexistent/registry.json"))
    registry.observe(events)
    assert registry.classify(["b/org-a"], now=at(700).timestamp())["b/org-a"] == "switch"
    assert registry.classify(["b/org-a"], now=at(700).timestamp(),
                             logged_out=at(30))["b/org-a"] == "unknown"


def test_the_registry_outlives_the_log_and_its_process(tmp_path):
    """C-23.28a: the app keeps about ten days of log; what the registry
    learned survives its rotation, and a new process reads it back."""
    path = tmp_path / "sessions" / folder_kinds.REGISTRY_NAME
    first = folder_kinds.Registry(path)
    first.observe([load("a/org-a", 0), load("b/org-a", 900), load("a/org-a", 902)])
    first.replayed = True
    first.save()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    again = folder_kinds.Registry(path)
    assert again.replayed
    assert again.to_dict() == first.to_dict()
    assert again.classify(["b/org-a"], now=at(1000).timestamp())["b/org-a"] == "switch"
    assert not again.observe([load("b/org-a", 900), load("a/org-a", 902)]), \
        "events already folded add nothing"


def test_a_registry_that_cannot_be_read_starts_empty(tmp_path):
    """C-23.28a: an unreadable or foreign file is no knowledge, so every folder is seeded."""
    path = tmp_path / "registry.json"
    path.write_text("not json")
    registry = folder_kinds.Registry(path)
    assert registry.folders == {} and not registry.replayed
    path.write_text(json.dumps({"folders": {"a/b": {"settled": "yesterday"}}, "last": 3}))
    registry = folder_kinds.Registry(path)
    assert registry.classify(["a/b"], now=0)["a/b"] == "unknown"


# --- the log -------------------------------------------------------------------

def lines(store: Path, folder: str, when: datetime) -> list[str]:
    account, org = folder.split("/")
    stamp = when.strftime("%Y-%m-%d %H:%M:%S")
    return [f"{stamp} [info] [LocalSessionManager] Initialization succeeded — "
            f"accountId={account}, orgId={org}, existingSessions=0",
            f"{stamp} [info] Loaded 3 persisted sessions from {store / account / org}"]


def write_log(path: Path, rows: list[str]) -> None:
    path.write_text("".join(row + "\n" for row in rows), encoding="utf-8")
    stamp = datetime.strptime(rows[-1][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    os.utime(path, (stamp.timestamp(), stamp.timestamp()))


def test_replay_reads_every_rotation_oldest_first(tmp_path):
    """C-23.28a: `poll` reads only `main1.log` and `main.log`; learning which
    folders the app settles in needs every gap between loads, so the first
    learning reads `main4.log` down to `main.log`, in order, and a stay that
    spans a rotation is measured across it."""
    store = tmp_path / "store"
    log = tmp_path / "main.log"
    write_log(tmp_path / "main2.log", lines(store, "a/org-a", at(0)))
    write_log(tmp_path / "main1.log", lines(store, "b/org-a", at(3600)))
    write_log(log, ["2026-09-14 19:00:01 [info] unrelated", *lines(store, "a/org-a", at(3602))])
    reader = desktop.DesktopLog(log, store=store, tz=UTC)
    events = reader.replay().events
    assert [(event.folder, event.at) for event in events] == [
        ("a/org-a", at(0)), ("b/org-a", at(3600)), ("a/org-a", at(3602))]
    registry = folder_kinds.Registry(tmp_path / "registry.json")
    assert folder_kinds.learn(registry, reader, replay=True)
    assert registry.replayed
    assert registry.classify(["a/org-a", "b/org-a"], now=at(3700).timestamp()) == {
        "a/org-a": "settled", "b/org-a": "switch"}
    reader.poll()
    assert not folder_kinds.learn(registry, reader, replay=True), \
        "the live poll's events were already replayed"


def test_a_replay_that_finds_no_log_is_tried_again(tmp_path):
    """C-23.28a, review r1 P1-4: the first learning reads all the history the
    app kept; if there was no log to read, that history is not given up for
    good, and the next learning replays it."""
    store = tmp_path / "store"
    log = tmp_path / "main.log"
    registry = folder_kinds.Registry(tmp_path / "registry.json")
    folder_kinds.learn(registry, desktop.DesktopLog(log, store=store, tz=UTC), replay=True)
    assert not registry.replayed and registry.folders == {}
    write_log(tmp_path / "main1.log", lines(store, "a/org-a", at(0)))
    write_log(log, lines(store, "b/org-b", at(3600)))
    assert folder_kinds.learn(registry, desktop.DesktopLog(log, store=store, tz=UTC),
                              replay=True)
    assert registry.replayed and set(registry.folders) == {"a/org-a", "b/org-b"}
    assert registry.since == at(0).timestamp()


def test_a_rotation_during_the_replay_counts_as_unread(tmp_path, monkeypatch):
    """C-23.28a, review r3 R5: the app rotating its logs while they are read
    moves each up a name under the reader, which then reads one twice and
    skips another. That replay counts as unread; the next one learns all."""
    store = tmp_path / "store"
    log = tmp_path / "main.log"
    write_log(tmp_path / "main1.log", lines(store, "a/org-a", at(0)))
    write_log(log, lines(store, "b/org-b", at(3600)))
    reader = desktop.DesktopLog(log, store=store, tz=UTC)
    read, rotated = desktop.DesktopLog._read, []

    def rotating(self, path, offset):
        if not rotated:                                 # the app rotates, once
            rotated.append(path)
            os.rename(tmp_path / "main1.log", tmp_path / "main2.log")
            os.rename(log, tmp_path / "main1.log")
            write_log(log, lines(store, "c/org-c", at(7200)))
        return read(self, path, offset)

    monkeypatch.setattr(desktop.DesktopLog, "_read", rotating)
    found = reader.replay()
    assert any("rotated while they were read" in item for item in found.unread)
    assert {event.folder for event in found.events} == {"b/org-b", "c/org-c"}, "a/org-a skipped"
    registry = folder_kinds.Registry(tmp_path / "registry.json")
    assert folder_kinds.learn(registry, reader, replay=True)    # read again, whole
    assert set(registry.folders) == {"a/org-a", "b/org-b", "c/org-c"}
    assert registry.unread == ()


def test_a_save_keeps_the_home_records_another_process_wrote(tmp_path):
    """C-23.28a, review r5 P3-2: a one-off pass records its home while the
    daemon runs; the daemon's next save keeps that record, as older than its
    own, rather than writing its memory over it."""
    path = tmp_path / "registry.json"
    daemon = folder_kinds.Registry(path)
    daemon.record_home("org-home", "a/org-home")
    daemon.save()
    one_off = folder_kinds.Registry(path)
    one_off.record_home("b/org-x", "b/org-x")
    one_off.save()
    daemon.save()                                    # after learning, say
    assert list(folder_kinds.Registry(path).homes.items()) == [
        ("b/org-x", "b/org-x"), ("org-home", "a/org-home")]
    for number in range(folder_kinds.HOMES_KEPT):    # many one-off values
        one_off.record_home(f"org-{number}", f"c/org-{number}")
    one_off.save()
    daemon.save()
    homes = folder_kinds.Registry(path).homes
    assert len(homes) == folder_kinds.HOMES_KEPT and list(homes)[-1] == "org-home"


def test_a_log_created_during_the_replay_is_no_rotation(tmp_path, monkeypatch):
    """C-23.28a, review r4 P3-d: the app's first launch creates `main.log`; one
    created while the replay reads moved no file, so the replay counts."""
    store = tmp_path / "store"
    log = tmp_path / "main.log"
    write_log(tmp_path / "main1.log", lines(store, "a/org-a", at(0)))
    reader = desktop.DesktopLog(log, store=store, tz=UTC)
    read, created = desktop.DesktopLog._read, []

    def creating(self, path, offset):
        if not created:
            created.append(path)
            write_log(log, lines(store, "b/org-b", at(3600)))
        return read(self, path, offset)

    monkeypatch.setattr(desktop.DesktopLog, "_read", creating)
    found = reader.replay()
    assert found.unread == ()
    assert [event.folder for event in found.events] == ["a/org-a", "b/org-b"]


@pytest.mark.parametrize("before,after,rotated", [
    ([("main1.log", 1), ("main.log", 2)], [("main1.log", 1), ("main.log", 2)], False),
    ([("main1.log", 1), ("main.log", None)], [("main1.log", 1), ("main.log", 2)], False),
    ([("main1.log", 1), ("main.log", 2)], [("main1.log", 2), ("main.log", 3)], True),
    ([("main1.log", 1), ("main.log", 2)], [("main2.log", 1), ("main1.log", 2),
                                           ("main.log", 3)], True),
    ([("main1.log", 1), ("main.log", 2)], [("main1.log", 1), ("main.log", None)], True),
])
def test_what_counts_as_a_rotation(before, after, rotated):
    """C-23.28a, review r3 R5 and r4 P3-d: a log that moved or vanished, or a
    new name, is a rotation; growth or a live log's creation is not."""
    assert desktop._rotated(before, after) is rotated


def test_the_registry_keeps_a_home_per_value_the_most_recent_ones(tmp_path):
    """C-23.28a, review r3 R7: one record per `dead_home` value, the most
    recent `HOMES_KEPT`, and they survive a save."""
    registry = folder_kinds.Registry(tmp_path / "registry.json")
    for number in range(folder_kinds.HOMES_KEPT + 2):
        registry.record_home(f"org-{number}", f"a/org-{number}")
    registry.record_home("org-2", "b/org-2")              # resolved again: the newest
    registry.save()
    again = folder_kinds.Registry(tmp_path / "registry.json")
    assert len(again.homes) == folder_kinds.HOMES_KEPT
    assert list(again.homes)[-1] == "org-2" and again.homes["org-2"] == "b/org-2"
    assert "org-0" not in again.homes and "org-1" not in again.homes


def test_a_replay_that_cannot_read_a_rotation_folds_nothing_and_is_tried_again(tmp_path):
    """C-23.28a, review r2 N8: history with a gap is not the history. Folding
    the rest, or the live poll's events, would move the watermark past the
    unread file's loads for good; so nothing is folded until a replay reads
    every log, and the events passed over meanwhile are read then."""
    store = tmp_path / "store"
    log = tmp_path / "main.log"
    oldest = tmp_path / "main2.log"
    write_log(oldest, lines(store, "a/org-a", at(0)))
    write_log(tmp_path / "main1.log", lines(store, "b/org-a", at(3600)))
    write_log(log, lines(store, "a/org-a", at(3602)))
    reader = desktop.DesktopLog(log, store=store, tz=UTC)
    registry = folder_kinds.Registry(tmp_path / "registry.json")
    oldest.chmod(0)
    try:
        assert reader.replay().unread == (os.fspath(oldest),)
        reader.poll()                                   # live events, queued
        assert not folder_kinds.learn(registry, reader, replay=True)
        assert not registry.replayed and registry.folders == {} and registry.since is None
    finally:
        oldest.chmod(0o644)
    write_log(log, lines(store, "a/org-a", at(3602)) + lines(store, "c/org-c", at(3700)))
    reader.poll()
    assert not folder_kinds.learn(registry, reader, replay=False), \
        "between replays, nothing is folded"
    assert registry.folders == {}
    assert folder_kinds.learn(registry, reader, replay=True)
    assert registry.replayed and registry.since == at(0).timestamp()
    assert set(registry.folders) == {"a/org-a", "b/org-a", "c/org-c"}


def test_poll_queues_loads_and_logouts_for_the_registry(tmp_path):
    """C-23.28a: every load and logout the reader sees is kept, in order,
    until drained, including the missing-folder load that created a folder."""
    store = tmp_path / "store"
    log = tmp_path / "main.log"
    write_log(log, [*lines(store, "a/org-a", at(0)),
                    "2026-09-14 18:30:00 [info] [LocalSessionManager] Account logged out, "
                    "marking for re-init on next login",
                    "2026-09-14 18:30:05 [info] [LocalSessionManager] Session storage directory "
                    f"does not exist yet, skipping load: {store / 'b' / 'org-b'}"])
    reader = desktop.DesktopLog(log, store=store, tz=UTC)
    reader.poll()
    assert reader.drain() == [Event(at(0), "a/org-a"), Event(at(1800), None),
                              Event(at(1805), "b/org-b", True)]
    assert reader.drain() == []


# --- invariants ------------------------------------------------------------------

FOLDERS = ("a/o1", "b/o1", "a/o2", "b/o2", "c/o3")


@st.composite
def histories(draw):
    """A log's worth of loads and logouts, a second to an hour apart."""
    clock, events = 0.0, []
    for _ in range(draw(st.integers(1, 25))):
        clock += draw(st.sampled_from((0.0, 1.0, 2.0, 19.0, 21.0, 300.0, 700.0, 3600.0)))
        if draw(st.integers(0, 5)) == 0:
            events.append(logout(clock))
        else:
            events.append(load(draw(st.sampled_from(FOLDERS)), clock,
                               missing=draw(st.booleans())))
    return events


SETTINGS = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def folded(events, chunks=()) -> folder_kinds.Registry:
    registry = folder_kinds.Registry(Path("/nonexistent/registry.json"))
    start = 0
    for end in sorted(chunks):
        registry.observe(events[start:end])
        start = end
    registry.observe(events[start:])
    return registry


@SETTINGS
@given(histories(), st.lists(st.integers(0, 25), max_size=4))
def test_folding_is_idempotent_and_the_same_in_any_chunks(events, chunks):
    """C-23.28a: polls split the log anywhere, and a restart replays it."""
    whole = folded(events)
    assert folded(events, [min(item, len(events)) for item in chunks]).to_dict() == whole.to_dict()
    again = folded(events)
    assert not again.observe(events) and again.to_dict() == whole.to_dict()
    for cut in range(len(events) + 1):
        assert not again.observe(events[:cut])


@SETTINGS
@given(histories())
def test_facts_only_accumulate(events):
    """C-23.28a: settled stays settled, an owner stays an owner, as the log grows."""
    registry = folder_kinds.Registry(Path("/nonexistent/registry.json"))
    settled, owned = set(), set()
    for event in events:
        registry.observe([event])
        now_settled = {key for key, facts in registry.folders.items() if facts.settled is not None}
        now_owned = {key for key, facts in registry.folders.items() if facts.owned is not None}
        assert settled <= now_settled and owned <= now_owned
        settled, owned = now_settled, now_owned


@SETTINGS
@given(histories(), st.floats(0, 7200))
def test_a_switch_folder_is_never_one_the_app_settled_in(events, later):
    """C-23.28a: exclusion is narrow. A switch folder has no settled stay and
    its org has an owner other than its account."""
    registry = folded(events)
    now = (events[-1].at.timestamp() if events else T0.timestamp()) + later
    verdicts = registry.classify(FOLDERS, now=now)
    for folder, verdict in verdicts.items():
        facts = registry.folders.get(folder)
        if verdict == folder_kinds.SWITCH:
            assert facts is None or facts.settled is None
            account, _sep, org = folder.partition("/")
            assert any(other.partition("/")[2] == org and other.partition("/")[0] != account
                       and (item.owned is not None or other == registry.last_folder)
                       for other, item in registry.folders.items())
        if facts is not None and facts.settled is not None:
            assert verdict == folder_kinds.SETTLED
