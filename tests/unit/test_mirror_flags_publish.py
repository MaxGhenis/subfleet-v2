"""Flag publishes that fail or crash: C-23.28, review round 5 finding F2.

Three failures used to leave copies ahead of the merge base: a put-back write
that raises, a crash between a session's first copy write and the base write,
and a base write that raises. The next pass then read the mirror's own writes
as a change and undid a user's revert, in every account. Now:

* each copy has its own reference in the merge base, the value the last
  decision gave it or read there, and votes only when it differs from it, so
  the mirror's own writes never vote;
* a publish is recorded (`mirror-publish.jsonl`, fsynced) before its first
  copy write, naming a prepared temporary per copy, and its put-backs are
  recorded before they run; a temporary still standing proves its rename
  never ran, so nothing removes one before the publish is resolved;
* a publish that reached no copy changes nothing; otherwise its decision
  stands, and a copy it did not reach keeps the value read there as its
  reference;
* the next pass resolves a record a failed or crashed one left, before it
  decides anything, and journals the mirror's writes that still stand.

`test_mirror_flags_model.py` checks the protocol over every reachable state,
and `test_mirror_flags_stateful.py` holds this code to it on random
interleavings with every fault; these are the cases one at a time.
"""

from __future__ import annotations

import itertools
import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from subfleet.sessions import mirror
from tests import mirror_flags_model as model
from tests import sessions_fixtures as fx

SESSION = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
FOLDERS = (("acct-a", "org-a"), ("acct-b", "org-b"), ("acct-c", "org-c"))


class Crash(BaseException):
    """The process dies: nothing in it runs again, not even `except OSError`."""


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = fx.claude_home(tmp_path, monkeypatch)
    store = fx.desktop_store(tmp_path, monkeypatch)
    fx.transcript(home, SESSION, fx.completed())
    root = tmp_path / "state"
    root.mkdir()
    ticks = itertools.count()

    def process() -> mirror.Mirror:
        return mirror.Mirror(root, fx.policy(),
                             now=lambda: fx.NOW + timedelta(seconds=next(ticks)))

    return process, store


def seed(store, *values: bool) -> None:
    for (account, org), value in zip(FOLDERS, values):
        fx.index_entry(store, account, org, SESSION, archived=value, settings={"ultracode": True})


def path(store, index: int) -> Path:
    account, org = FOLDERS[index]
    return store / account / org / f"local_{SESSION}.json"


def key(index: int) -> str:
    account, org = FOLDERS[index]
    return f"{account}/{org}/local_{SESSION}.json"


def flags(store) -> tuple[bool, ...]:
    return tuple(json.loads(path(store, i).read_text())["isArchived"] for i in range(3))


def rewrite(store, index: int, **fields) -> None:
    """The app's own save: beside the file, then renamed over it."""
    target = path(store, index)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps({**json.loads(target.read_text()), **fields}))
    temporary.replace(target)


def record(running) -> dict:
    return mirror._load(running.flags_path).get(SESSION) or {}


def publish_temporaries(store) -> list[Path]:
    return [item for account, org in FOLDERS for item in (store / account / org).iterdir()
            if item.name.endswith(mirror.PUBLISH_SUFFIX)]


def synced(process, store, value: bool):
    """A settled session: every copy and the base hold `value`."""
    seed(store, value, value, value)
    running = process()
    assert running.run_once().state == "ok" and record(running)["isArchived"] is value
    return running


def at_install(patch, store, index: int, action, *, occurrence: int = 0, after=False) -> None:
    """Run `action` at a flag publish's rename of copy `index` (its
    `occurrence`-th: 0 the write, 1 its put-back), before or `after` it."""
    install = mirror._install
    seen = itertools.count()

    def hooked(temporary, destination, **kwargs):
        if destination == path(store, index) and kwargs.get("keep") and next(seen) == occurrence:
            if not after:
                action()
                return install(temporary, destination, **kwargs)
            placed = install(temporary, destination, **kwargs)
            action()
            return placed
        return install(temporary, destination, **kwargs)

    patch.setattr(mirror, "_install", hooked)


def crash() -> None:
    raise Crash()


def run_crashing(running) -> None:
    with pytest.raises(Crash):
        running.run_once()


# --- F2: the three failures ------------------------------------------------------

def test_a_base_that_cannot_be_written_leaves_the_record_and_a_revert_stands(world, monkeypatch):
    """The base write fails after every copy is written: the pass fails, the
    record stays, and the user's unarchive in the same account stands."""
    process, store = world
    running = synced(process, store, False)
    rewrite(store, 0, isArchived=True)                   # the user archives in A
    write = mirror._write_json

    def disk_full(target, value, **kwargs):
        if target == running.flags_path:
            raise OSError(28, "No space left on device")
        return write(target, value, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(mirror, "_write_json", disk_full)
        assert running.run_once().state == "error"
    assert flags(store) == (True,) * 3 and record(running)["isArchived"] is False
    assert running.publish_path.exists(), "the record outlives the failed base write"
    rewrite(store, 0, isArchived=False)                  # and unarchives in A
    assert running.run_once().state == "ok"
    assert flags(store) == (False,) * 3 and record(running)["isArchived"] is False
    assert not running.publish_path.exists() and not publish_temporaries(store)


def test_a_put_back_that_fails_leaves_a_write_that_does_not_vote(world, monkeypatch):
    """The reviewer's case: the user unarchives while the publish runs, the
    app saves C so its write fails, and B's put-back cannot be written. B
    keeps the mirror's archive, which must not outvote the unarchive."""
    process, store = world
    running = synced(process, store, False)
    rewrite(store, 0, isArchived=True)                   # the user archives in A

    def the_user_unarchives_and_the_app_saves_c():
        rewrite(store, 0, isArchived=False)
        rewrite(store, 2, lastFocusedAt=9)

    prepare = mirror._prepare_json

    def disk_full_on_put_back(target, value):
        if target == path(store, 1) and value.get("isArchived") is False:
            raise OSError(28, "No space left on device")
        return prepare(target, value)

    with monkeypatch.context() as patch:
        at_install(patch, store, 2, the_user_unarchives_and_the_app_saves_c)
        patch.setattr(mirror, "_prepare_json", disk_full_on_put_back)
        result = running.run_once()
    assert result.state == "ok" and result.flags_held == 1
    assert flags(store) == (False, True, False)
    assert running.run_once().state == "ok"
    assert flags(store) == (False,) * 3, "the user's unarchive stands"


def test_a_crash_mid_publish_then_a_revert_in_a_new_process(world, monkeypatch):
    """The process dies before C's rename; the user unarchives in A; a new
    process resolves the record and the unarchive stands."""
    process, store = world
    running = synced(process, store, False)
    rewrite(store, 0, isArchived=True)
    with monkeypatch.context() as patch:
        at_install(patch, store, 2, crash)
        run_crashing(running)
    assert flags(store) == (True, True, False) and running.publish_path.exists()
    rewrite(store, 0, isArchived=False)
    fresh = process()
    assert fresh.run_once().state == "ok"
    assert flags(store) == (False,) * 3 and record(fresh)["isArchived"] is False
    assert not fresh.publish_path.exists() and not publish_temporaries(store)


def test_a_revert_where_the_user_saw_the_mirrors_archive_stands(world, monkeypatch):
    """Every copy is written and the process dies at the base write. The user
    unarchives in B, where they saw the archive the mirror wrote. The design
    first proposed for F2 (the mirror's own writes do not vote) loses this
    one: B is then the user's, and it and A's archive disagree."""
    process, store = world
    running = synced(process, store, False)
    rewrite(store, 0, isArchived=True)
    write = mirror._write_json

    def dies_at_the_base(target, value, **kwargs):
        if target == running.flags_path:
            raise Crash()
        return write(target, value, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(mirror, "_write_json", dies_at_the_base)
        run_crashing(running)
    assert flags(store) == (True,) * 3 and record(running)["isArchived"] is False
    rewrite(store, 1, isArchived=False)                  # unarchived where it was seen
    fresh = process()
    assert fresh.run_once().state == "ok"
    assert flags(store) == (False,) * 3


def test_a_revert_after_a_skipped_put_back_stands_without_any_fault(world, monkeypatch):
    """No I/O failure at all: the app saves B right after the mirror's write
    and C before it, so C's write fails and B's put-back is skipped. The user
    unarchives in B. Before per-copy references A's archive outvoted it."""
    process, store = world
    running = synced(process, store, False)
    rewrite(store, 0, isArchived=True)
    with monkeypatch.context() as patch:
        at_install(patch, store, 1, lambda: rewrite(store, 1, lastFocusedAt=5), after=True)
        at_install(patch, store, 2, lambda: rewrite(store, 2, lastFocusedAt=6))
        result = running.run_once()
    assert result.flags_held == 1 and flags(store) == (True, True, False)
    assert record(running)["isArchived"] is True, "B's write stands, so the decision does"
    assert record(running)["refs"] == {key(2): {"isArchived": False}}
    rewrite(store, 1, isArchived=False)                  # the user unarchives in B
    assert running.run_once().state == "ok"
    assert flags(store) == (False,) * 3


def test_an_incomplete_bootstrap_publish_stands_and_a_revert_stands(world, monkeypatch):
    """A session never synced (no base): archived-anywhere wins, the publish
    reaches B and the process dies. The user unarchives in A. The decision
    stands for the copy it reached, so B's archive (the mirror's) does not
    count as an archive anywhere."""
    process, store = world
    seed(store, True, False, False)
    running = process()
    with monkeypatch.context() as patch:
        at_install(patch, store, 2, crash)
        run_crashing(running)
    assert flags(store) == (True, True, False) and record(running) == {}
    rewrite(store, 0, isArchived=False)
    fresh = process()
    assert fresh.run_once().state == "ok"
    assert flags(store) == (False,) * 3


# --- the record, its temporaries and their order -----------------------------------

def test_the_record_is_durable_before_the_first_rename(world, monkeypatch):
    """The publish record names every prepared temporary before any is renamed."""
    process, store = world
    running = synced(process, store, False)
    rewrite(store, 0, isArchived=True)
    seen = []

    def check():
        rows = running._publish_rows()
        assert rows and rows[-1]["session"] == SESSION
        named = {copy[0]: copy[1] for copy in rows[-1]["copies"]}
        assert set(named) == {key(1), key(2)}
        assert all(os.path.exists(name) for name in named.values())
        seen.append(named)

    with monkeypatch.context() as patch:
        at_install(patch, store, 1, check)
        assert running.run_once().state == "ok"
    assert seen and flags(store) == (True,) * 3
    assert not running.publish_path.exists() and not publish_temporaries(store)


def test_a_rename_that_finds_its_copy_rewritten_keeps_its_temporary(world, monkeypatch):
    """C's rename finds the app's save, and the process dies right there. The
    temporary left standing is what tells the next process C was not reached:
    C keeps its own value as its reference, and the archive reaches it."""
    process, store = world
    running = synced(process, store, False)
    rewrite(store, 0, isArchived=True)
    install = mirror._install

    def the_app_saves_c_then_the_process_dies(temporary, destination, **kwargs):
        if destination == path(store, 2) and kwargs.get("keep"):
            rewrite(store, 2, lastFocusedAt=7)
            assert install(temporary, destination, **kwargs) is False
            assert temporary.exists(), "kept as the proof its rename never ran"
            raise Crash()
        return install(temporary, destination, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(mirror, "_install", the_app_saves_c_then_the_process_dies)
        run_crashing(running)
    fresh = process()
    assert fresh.run_once().state == "ok"
    assert flags(store) == (True,) * 3, "the user's archive stands"


def test_put_backs_are_recorded_before_they_run(world, monkeypatch):
    """B is written, C's rename finds the app's save, B is put back, and the
    process dies right after that rename. The next process must read B as
    put back (nothing reached, nothing changes), so A's archive is still a
    change and spreads."""
    process, store = world
    running = synced(process, store, False)
    rewrite(store, 0, isArchived=True)
    with monkeypatch.context() as patch:
        at_install(patch, store, 2, lambda: rewrite(store, 2, lastFocusedAt=7))
        at_install(patch, store, 1, crash, occurrence=1, after=True)     # B's put-back
        run_crashing(running)
    assert flags(store) == (True, False, False), "B was put back"
    rows = process()._publish_rows()
    assert [bool(row.get("back")) for row in rows] == [False, True]
    fresh = process()
    assert fresh.run_once().state == "ok"
    assert flags(store) == (True,) * 3, "the user's archive stands"
    assert record(fresh)["isArchived"] is True and "refs" not in record(fresh)


def test_temporaries_outlive_a_crash_at_the_base_write(world, monkeypatch):
    """B's write stands (the app saved over it, so its put-back is skipped),
    C is not reached, and the process dies at the base write: C's temporary
    must still be there for the next process."""
    process, store = world
    running = synced(process, store, False)
    rewrite(store, 0, isArchived=True)
    write = mirror._write_json

    def dies_at_the_base(target, value, **kwargs):
        if target == running.flags_path:
            raise Crash()
        return write(target, value, **kwargs)

    with monkeypatch.context() as patch:
        at_install(patch, store, 1, lambda: rewrite(store, 1, lastFocusedAt=5), after=True)
        at_install(patch, store, 2, lambda: rewrite(store, 2, lastFocusedAt=6))
        patch.setattr(mirror, "_write_json", dies_at_the_base)
        run_crashing(running)
    assert flags(store) == (True, True, False)
    assert len(publish_temporaries(store)) == 1, "C's, unrenamed"
    fresh = process()
    assert fresh.run_once().state == "ok"
    assert flags(store) == (True,) * 3


def test_a_sweep_leaves_the_temporaries_of_a_pending_record(world, monkeypatch):
    """A sweep removes stale temporaries, but never one a pending record names,
    however old: it is the proof its rename never ran. Once nothing is
    pending, an old publish temporary is swept like any other leftover."""
    process, store = world
    running = synced(process, store, False)
    rewrite(store, 0, isArchived=True)
    install = mirror._install

    def the_app_saves_c_then_the_process_dies(temporary, destination, **kwargs):
        if destination == path(store, 2) and kwargs.get("keep"):
            rewrite(store, 2, lastFocusedAt=7)
            install(temporary, destination, **kwargs)
            raise Crash()
        return install(temporary, destination, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(mirror, "_install", the_app_saves_c_then_the_process_dies)
        run_crashing(running)
    monkeypatch.setattr(mirror, "TEMPORARY_STALE_S", -1)  # every leftover is stale
    fresh = process()
    assert fresh._sweep_due()
    assert fresh.run_once().state == "ok"
    assert flags(store) == (True,) * 3, "the user's archive stands"
    stray = path(store, 1).with_name(f"{path(store, 1).name}.left0ver{mirror.PUBLISH_SUFFIX}")
    stray.write_text("{}")
    fresh._last_sweep = None
    assert fresh.run_once().state == "ok"
    assert not stray.exists(), "nothing pending: a leftover like any other"


def test_recovery_resolves_by_what_the_pass_read_not_by_a_newer_save(world, monkeypatch):
    """A crash left C unreached. The next pass reads every copy, then the
    user archives C before the pass resolves the record. The pass decides on
    what it read, so C's reference must be what it read too: taken from the
    newer save, C's older read would vote, and the pass would unarchive
    every account the user had archived."""
    process, store = world
    running = synced(process, store, False)
    rewrite(store, 0, isArchived=True)                   # the user archives in A
    install = mirror._install

    def the_app_saves_c_then_the_process_dies(temporary, destination, **kwargs):
        if destination == path(store, 2) and kwargs.get("keep"):
            rewrite(store, 2, lastFocusedAt=7)
            install(temporary, destination, **kwargs)
            raise Crash()
        return install(temporary, destination, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(mirror, "_install", the_app_saves_c_then_the_process_dies)
        run_crashing(running)
    sync = mirror.Mirror.sync_flags

    def the_user_archives_c_after_the_read(engine, folder_files, *args, **kwargs):
        rewrite(store, 2, isArchived=True)
        return sync(engine, folder_files, *args, **kwargs)

    fresh = process()
    with monkeypatch.context() as patch:
        patch.setattr(mirror.Mirror, "sync_flags", the_user_archives_c_after_the_read)
        assert fresh.run_once().state == "ok"
    assert flags(store) == (True,) * 3
    assert fresh.run_once().state == "ok"
    assert flags(store) == (True,) * 3 and record(fresh)["isArchived"] is True


def test_a_record_the_base_already_holds_is_not_resolved_again(world, monkeypatch):
    """The process dies once the base holds the resolution and before the
    record is dropped. The next process drops it without resolving it a
    second time from files that moved since."""
    process, store = world
    running = synced(process, store, False)
    rewrite(store, 0, isArchived=True)

    def dies_before_dropping(engine, temporaries):
        raise Crash()

    with monkeypatch.context() as patch:
        at_install(patch, store, 1, lambda: rewrite(store, 1, lastFocusedAt=5), after=True)
        at_install(patch, store, 2, lambda: rewrite(store, 2, lastFocusedAt=6))
        patch.setattr(mirror.Mirror, "_publish_clear", dies_before_dropping)
        run_crashing(running)
    resolved = record(running)
    assert resolved["isArchived"] is True and resolved["refs"] == {key(2): {"isArchived": False}}
    rewrite(store, 2, isArchived=True)                   # the user archives C too
    fresh = process()
    base_all = fresh._recover_publish(mirror._load(fresh.flags_path), mirror.Options(), {})
    assert base_all[SESSION] == resolved, "resolved once"
    assert not fresh.publish_path.exists()


# --- the rule and the resolution, against the model ----------------------------------

@settings(max_examples=300, deadline=None)
@given(copies=st.lists(st.tuples(st.booleans(), st.booleans()), min_size=1, max_size=4),
       base=st.one_of(st.none(), st.booleans()))
def test_the_decision_is_the_models(copies, base):
    """Differential: `_decide_flag` against the model's `decide` (rule refs),
    for every mix of values and references, with and without a base."""
    values = tuple(value for value, _ref in copies)
    refs = tuple(ref if base is not None else None for _value, ref in copies)
    state = model.State(copy=values, base=base, loaded=0, mem=(None,) * len(values),
                        ref=refs if base is not None else ())
    assert mirror._decide_flag(zip(values, refs), base, True) == model.decide(state)


@settings(max_examples=300, deadline=None)
@given(data=st.data())
def test_the_resolution_is_the_models(data):
    """Differential: `_resolve_publish` against the model's `resolve`, for any
    publish, any set of renames that ran and put-backs that ran, and any
    value a copy it did not reach holds now."""
    n = data.draw(st.integers(min_value=1, max_value=4))
    base = data.draw(st.one_of(st.none(), st.booleans()))
    prior_ref = tuple(data.draw(st.booleans()) if base is not None else None for _ in range(n))
    snap = tuple(data.draw(st.booleans()) for _ in range(n))
    decided = data.draw(st.booleans())
    targets = tuple(a for a in range(n) if snap[a] != decided)
    landed = frozenset(data.draw(st.sets(st.sampled_from(targets))) if targets else ())
    restored = frozenset(data.draw(st.sets(st.sampled_from(sorted(landed)))) if landed else ())
    now = tuple(decided if a in landed and a not in restored else
                data.draw(st.sampled_from((snap[a], decided))) if a in targets else snap[a]
                for a in range(n))
    state = model.State(copy=now, base=base, loaded=0, mem=(None,) * n,
                        ref=prior_ref if base is not None else (),
                        wal=(decided, targets, prior_ref if base is not None else (None,) * n,
                             base),
                        landed=landed, restored=restored)
    want_base, want_ref = model.resolve(state)

    def copy_key(a: int) -> str:
        return f"acct-{a}/org-{a}/local_x.json"

    other = data.draw(st.booleans())
    prior = None if base is None else {
        "isArchived": base, "isStarred": other,
        **({"refs": {copy_key(a): {"isArchived": prior_ref[a]}
                     for a in range(n) if prior_ref[a] != base}}
           if any(prior_ref[a] != base for a in range(n)) else {})}
    row = {"at": 1.0, "prior": prior, "next": {"isArchived": decided, "isStarred": other},
           "copies": [[copy_key(a), f"/t/{a}", a, {"isArchived": snap[a], "isStarred": other}]
                      for a in targets]}
    got = mirror._resolve_publish(row, {copy_key(a) for a in landed},
                                  {copy_key(a) for a in restored},
                                  {copy_key(a): {"isArchived": now[a], "isStarred": other}
                                   for a in targets})
    if want_base is None:
        assert got is None
        return
    assert got["isArchived"] is want_base and got["isStarred"] is other
    assert tuple(mirror._reference(got, copy_key(a), "isArchived") for a in range(n)) == want_ref
