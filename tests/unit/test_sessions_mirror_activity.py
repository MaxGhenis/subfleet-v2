"""The date a sidebar row shows, and session ids that open two conversations: C-23.28.

On 2026-10-10 Max opened a six-week-old copy of a session. Its row showed
today; the row of the session he meant, which had run that morning under
another login, showed "Aug 16", because the mirror placed a copy's
`lastActivityAt` once and never synced it. Both rows had one title, because one
session id opened one conversation under 93 logins and another under 41
(`docs/reports/2026-10-10-mirror-stale-dates.md`).

Every test names the clause it proves (C-20.5). The desktop store lives under
`tmp_path`; nothing here reads or writes the operator's own.
"""

from __future__ import annotations

import io
import itertools
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet.policy import DEFAULT_POLICY_PATH, PolicyError, load_policy
from subfleet.sessions import mirror
from tests import sessions_fixtures as fx

ONE = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
REAL = "67eb7695-0000-4000-8000-000000000001"
FORK = "181ca5ca-0000-4000-8000-000000000002"
FOLDERS = (("acct-a", "org-a"), ("acct-b", "org-b"), ("acct-c", "org-c"))
#: 2026-10-10T12:26:40Z and 55 days before it, in the app's milliseconds.
NEW = 1_791_635_200_000
DAY = 86_400_000
OLD = NEW - 55 * DAY
HOUR = 3_600_000
#: The passes' clock: three days after NEW, so every fixture date is in its past.
CLOCK = datetime.fromtimestamp((NEW + 3 * DAY) / 1000, timezone.utc)
CLOCK_MS = NEW + 3 * DAY


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = fx.claude_home(tmp_path, monkeypatch)
    store = fx.desktop_store(tmp_path, monkeypatch)
    for account, org in FOLDERS:
        (store / account / org).mkdir(parents=True)
    root = tmp_path / "state"
    root.mkdir()
    return home, store, root


def engine(world, **overrides) -> mirror.Mirror:
    """A mirror with no embedded hot service: each pass here is one decision."""
    _home, _store, root = world
    ticks = itertools.count()
    return mirror.Mirror(root, fx.policy(mirror_hot_interval_s=0, **overrides),
                         now=lambda: CLOCK + timedelta(milliseconds=next(ticks)))


def options(running: mirror.Mirror, **overrides) -> mirror.Options:
    return mirror.options_from(running.policy, **overrides)


def place(world, session: str, dates, **fields) -> None:
    """One copy per folder, each with its own date (None: no copy there)."""
    home, store, _root = world
    fx.transcript(home, session, fx.completed())
    for (account, org), date in zip(FOLDERS, dates):
        if date is not None:
            fx.index_entry(store, account, org, session, last_activity=date,
                           settings={"ultracode": True}, **fields)


def path(world, index: int, session: str = ONE, name: str | None = None) -> Path:
    account, org = FOLDERS[index]
    return world[1] / account / org / (name or f"local_{session}.json")


def record(world, index: int, session: str = ONE, name: str | None = None) -> dict:
    return json.loads(path(world, index, session, name).read_text(encoding="utf-8"))


def dates(world, session: str = ONE) -> tuple:
    return tuple(record(world, index, session).get("lastActivityAt")
                 for index in range(len(FOLDERS)))


def rewrite(target: Path, **fields) -> None:
    """The app's own write: beside the file, then rename (never in place)."""
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps({**json.loads(target.read_text()), **fields}))
    temporary.replace(target)


def between_read_and_publish(patch, act) -> None:
    """Run `act` after the pass has read every copy and before it publishes."""
    sync = mirror.Mirror.sync_flags

    def interleave(running, folder_files, *args, **kwargs):
        act()
        return sync(running, folder_files, *args, **kwargs)

    patch.setattr(mirror.Mirror, "sync_flags", interleave)


# --- the date ----------------------------------------------------------------------

def test_a_session_last_run_under_another_login_shows_its_date_everywhere(world):
    """C-23.28: the 2026-10-10 shape. A ran it this morning; B and C still
    held the date they were copied with."""
    place(world, ONE, (NEW, OLD, OLD + 3 * DAY))
    before = [record(world, index) for index in range(3)]
    stamps = [os.stat(path(world, index)).st_mtime_ns for index in range(3)]
    running = engine(world)
    result = running.run_once(options(running))
    assert result.state == "ok" and result.activity_synced == 1 and result.activity_waiting == 0
    assert dates(world) == (NEW, NEW - 1, NEW - 1)
    assert "dates raised 1" in result.summary and result.changed
    for index in range(3):
        after = record(world, index)
        assert {**after, "lastActivityAt": None} == {**before[index], "lastActivityAt": None}, \
            "nothing but the date was written"
        # Kept as the flag writes keep it: through a float of seconds.
        assert abs(os.stat(path(world, index)).st_mtime_ns - stamps[index]) < 1_000


def test_the_copy_the_session_ran_in_is_not_rewritten(world):
    """C-23.28: the lead is kept, and a copy that needs nothing is not a write."""
    place(world, ONE, (NEW, OLD, OLD))
    inode = os.stat(path(world, 0)).st_ino
    running = engine(world)
    running.run_once(options(running))
    assert os.stat(path(world, 0)).st_ino == inode
    assert max(dates(world)) == NEW and dates(world).count(NEW) == 1


def test_a_copy_within_the_lag_is_left_alone_and_a_second_pass_writes_nothing(world):
    """C-23.28: bounded lag and idempotence. An hour is the shipped lag."""
    place(world, ONE, (NEW, NEW - HOUR, NEW - HOUR - 1))
    running = engine(world)
    assert running.run_once(options(running)).activity_synced == 1
    assert dates(world) == (NEW, NEW - HOUR, NEW - 1), "exactly the lag behind is not behind"
    inodes = [os.stat(path(world, index)).st_ino for index in range(3)]
    again = running.run_once(options(running))
    assert again.activity_synced == 0 and not again.changed
    assert [os.stat(path(world, index)).st_ino for index in range(3)] == inodes


def test_a_date_is_never_lowered(world):
    """C-23.28: a copy ahead of the pass's decision keeps its date."""
    place(world, ONE, (NEW, OLD, OLD))
    running = engine(world)
    running.run_once(options(running))
    rewrite(path(world, 1), lastActivityAt=NEW + 5 * HOUR)       # B runs the session
    running.run_once(options(running))
    assert dates(world) == (NEW + 5 * HOUR - 1, NEW + 5 * HOUR, NEW + 5 * HOUR - 1)
    assert min(dates(world)) >= NEW - 1


def test_a_later_date_saved_between_the_read_and_the_publish_is_kept(world, monkeypatch):
    """C-23.28: no lost update. The date is not among the fields whose change
    holds a session, so the publish must not write its older decision over it."""
    place(world, ONE, (NEW, OLD, OLD))
    running = engine(world)
    saved = []

    def app_saves_b():
        rewrite(path(world, 1), lastActivityAt=NEW + 2 * DAY)
        saved.append(os.stat(path(world, 1)).st_ino)

    with monkeypatch.context() as patch:
        between_read_and_publish(patch, app_saves_b)
        result = running.run_once(options(running))
    assert result.state == "ok" and result.flags_held == 0
    assert dates(world) == (NEW, NEW + 2 * DAY, NEW - 1), "B keeps what the app saved"
    assert os.stat(path(world, 1)).st_ino == saved[0], "and is not rewritten to say the same"


@pytest.mark.parametrize("now_holds", [0, None, "soon", 10 ** 400])
def test_a_copy_that_holds_no_date_by_the_time_of_the_write_is_not_written(
        world, monkeypatch, now_holds):
    """C-23.28: the rule at the decision is the rule at the write. The pass
    read a date in B and decided to raise it; before the publish B's field
    became something that is no date. The mirror writes no date over that."""
    place(world, ONE, (NEW, OLD, OLD))
    running = engine(world)
    with monkeypatch.context() as patch:
        between_read_and_publish(patch, lambda: rewrite(path(world, 1),
                                                        lastActivityAt=now_holds))
        result = running.run_once(options(running))
    assert result.state == "ok" and result.flags_held == 0
    assert dates(world) == (NEW, now_holds, NEW - 1)


def test_a_flag_that_moved_since_the_read_holds_the_date_too(world, monkeypatch):
    """C-23.28: all or nothing. A held session writes nothing, its date included."""
    place(world, ONE, (NEW, OLD, OLD))
    running = engine(world)
    with monkeypatch.context() as patch:
        between_read_and_publish(patch, lambda: rewrite(path(world, 2), isStarred=True))
        result = running.run_once(options(running))
    assert result.flags_held == 1
    assert dates(world) == (NEW, OLD, OLD), "nothing written while one copy moved"
    assert ONE not in mirror._load(running.flags_path), "and no base for it"
    running.run_once(options(running))
    assert dates(world) == (NEW, NEW - 1, NEW - 1)
    assert [record(world, index)["isStarred"] for index in range(3)] == [True] * 3


def test_a_failed_write_puts_back_the_date_with_the_flag(world, monkeypatch):
    """C-23.28: one publish. The star and the date reach B; C's write finds C
    rewritten; B gets back the star and the date it had, and the base is held."""
    place(world, ONE, (OLD, OLD, OLD))
    running = engine(world)
    running.run_once(options(running))                       # a base: nothing starred
    rewrite(path(world, 0), isStarred=True, lastActivityAt=NEW)   # A stars it and runs it
    install = mirror._install
    calls = itertools.count()

    def racing(temporary, destination, **kwargs):
        if kwargs.get("expect") is not None and next(calls) == 1:
            rewrite(path(world, 2), lastFocusedAt=1)         # the app saves C
        return install(temporary, destination, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(mirror, "_install", racing)
        result = running.run_once(options(running))
    assert result.flags_held == 1
    assert [record(world, index)["isStarred"] for index in range(3)] == [True, False, False]
    assert dates(world) == (NEW, OLD, OLD), "B is as the pre-check read it"
    assert mirror._load(running.flags_path)[ONE]["isStarred"] is False
    running.run_once(options(running))
    assert [record(world, index)["isStarred"] for index in range(3)] == [True] * 3
    assert dates(world) == (NEW, NEW - 1, NEW - 1)
    assert mirror._load(running.flags_path)[ONE]["isStarred"] is True


def test_an_archived_sessions_dates_are_left_alone_until_it_is_unarchived(world):
    """C-23.28: a session archived everywhere is in no sidebar list. Unarchived,
    it is raised by the pass that spreads the unarchive."""
    place(world, ONE, (NEW, OLD, OLD), archived=True)
    running = engine(world)
    result = running.run_once(options(running))
    assert result.activity_synced == 0 and dates(world) == (NEW, OLD, OLD)
    rewrite(path(world, 0), isArchived=False)
    running.run_once(options(running))
    assert [record(world, index)["isArchived"] for index in range(3)] == [False] * 3
    assert dates(world) == (NEW, NEW - 1, NEW - 1)


def test_the_merge_base_never_holds_a_date(world):
    """C-23.28: the date needs no base. A newer date always wins, so a stale
    re-save cannot read as anyone's change."""
    place(world, ONE, (NEW, OLD, OLD))
    running = engine(world)
    running.run_once(options(running))
    assert set(mirror._load(running.flags_path)[ONE]) <= {"isArchived", "isStarred", "title",
                                                          "ttitle", "tmt"}


def test_a_stale_resave_is_raised_again_and_reaches_no_other_copy(world):
    """C-23.28, the known limit: the app saves B from memory, with the date it
    loaded. Where a flag saved that way wins, the date only waits for a pass."""
    place(world, ONE, (NEW, OLD, OLD))
    running = engine(world)
    running.run_once(options(running))
    rewrite(path(world, 1), lastActivityAt=OLD)              # B's memory comes back
    result = running.run_once(options(running))
    assert result.activity_synced == 1
    assert dates(world) == (NEW, NEW - 1, NEW - 1)


def test_a_new_folder_is_still_copied_from_the_record_the_session_last_ran_in(world):
    """C-23.28: why a raise stops one millisecond short. `_rank` picks the
    record a new folder is copied from by this date; raised to the newest,
    B's old record (an older model) could be the one copied."""
    home, store, _root = world
    fx.transcript(home, ONE, fx.completed())
    fx.index_entry(store, *FOLDERS[1], ONE, last_activity=OLD, model="claude-fable-5-1",
                   settings={"ultracode": True})
    fx.index_entry(store, *FOLDERS[2], ONE, last_activity=NEW, model="claude-opus-5-5",
                   settings={"ultracode": True})
    (store / "acct-a" / "org-a").rmdir()
    running = engine(world)
    running.run_once(options(running))
    assert record(world, 1)["lastActivityAt"] == NEW - 1
    (store / "acct-a" / "org-a").mkdir()                     # a login added afterwards
    running.run_once(options(running))
    assert record(world, 0)["model"] == "claude-opus-5-5"
    assert record(world, 1)["model"] == "claude-fable-5-1", "the mirror syncs no model here"


def test_a_copy_whose_date_is_not_a_number_is_left_as_it_is(world):
    """C-23.28: it is no voice, and the mirror writes no date the app did not."""
    place(world, ONE, (NEW, OLD, OLD))
    rewrite(path(world, 1), lastActivityAt=None)
    running = engine(world)
    running.run_once(options(running))
    assert dates(world) == (NEW, None, NEW - 1)


def test_a_date_from_the_future_is_no_voice_and_spreads_nowhere(world):
    """C-23.28: a raise is never undone, so a bad record must stay one
    folder's. C's date is a day past the pass's clock: B is raised to A's,
    and neither A nor B is raised toward C's."""
    future = CLOCK_MS + DAY
    place(world, ONE, (NEW, OLD, future))
    running = engine(world)
    result = running.run_once(options(running))
    assert result.activity_synced == 1
    assert dates(world) == (NEW, NEW - 1, future)
    assert running.run_once(options(running)).activity_synced == 0


def test_a_session_whose_only_dates_are_from_the_future_is_left_alone(world):
    """C-23.28: with no voice there is no newest, and nothing is written."""
    place(world, ONE, (CLOCK_MS + DAY, CLOCK_MS + 9 * DAY, CLOCK_MS + 2 * DAY))
    running = engine(world)
    result = running.run_once(options(running))
    assert result.activity_synced == 0
    assert dates(world) == (CLOCK_MS + DAY, CLOCK_MS + 9 * DAY, CLOCK_MS + 2 * DAY)


def test_a_date_a_minute_past_the_clock_is_a_voice(world):
    """C-23.28: `ACTIVITY_FUTURE_S` allows for a clock that stepped back. A
    record the app wrote just before the step is still that session's newest."""
    ahead = CLOCK_MS + 60_000
    assert 60 < mirror.ACTIVITY_FUTURE_S < 3600
    place(world, ONE, (ahead, OLD, OLD))
    running = engine(world)
    running.run_once(options(running))
    assert dates(world) == (ahead, ahead - 1, ahead - 1)


@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(offsets=st.lists(st.integers(min_value=-90 * DAY, max_value=30 * DAY),
                        min_size=3, max_size=3))
def test_one_pass_keeps_the_dates_bounds_on_real_files(offsets, tmp_path_factory, monkeypatch):
    """C-23.28: for any three dates around the pass's clock, on real files. No
    date is lowered; a date from the future is not written and is no one's
    target; every other copy ends within the lag of the newest voice, below
    it, and the copy that held the newest voice still does."""
    with monkeypatch.context() as patch:
        base = tmp_path_factory.mktemp("dates")
        home = fx.claude_home(base, patch)
        store = fx.desktop_store(base, patch)
        root = base / "state"
        root.mkdir()
        scene = (home, store, root)
        before = tuple(CLOCK_MS + offset for offset in offsets)
        place(scene, ONE, before)
        running = engine(scene)
        running.run_once(options(running))
        after = dates(scene)
    horizon = CLOCK_MS + mirror.ACTIVITY_FUTURE_S * 1000
    voices = [value for value in before if value <= horizon]
    for was, now in zip(before, after):
        assert now >= was, "never lowered"
        if was > horizon:
            assert now == was, "a date from the future is not written"
    if voices:
        newest = max(voices)
        for was, now in zip(before, after):
            if was <= horizon:
                assert newest - now <= HOUR, "within the lag of the newest voice"
                assert now <= newest and (now == was or now == newest - 1)
        assert [was == newest for was in before if was <= horizon] == \
            [now == newest for was, now in zip(before, after) if was <= horizon]


def test_a_new_folder_can_still_be_copied_from_a_record_with_a_future_date(world):
    """C-23.28, the limit the guard leaves (review of #167): which record a new
    folder is copied from is `_rank`'s rule, older than the date sync, and it
    takes the latest date. The sync itself spreads the bad date nowhere."""
    home, store, _root = world
    fx.transcript(home, ONE, fx.completed())
    future = CLOCK_MS + DAY
    fx.index_entry(store, *FOLDERS[0], ONE, last_activity=NEW, model="claude-opus-5-5",
                   settings={"ultracode": True})
    fx.index_entry(store, *FOLDERS[1], ONE, last_activity=future, model="a-bad-record",
                   settings={"ultracode": True})
    running = engine(world)
    result = running.run_once(options(running))
    assert result.added == 1 and result.activity_synced == 0
    assert record(world, 2)["lastActivityAt"] == future, "copied whole, as before the sync"
    assert record(world, 0)["lastActivityAt"] == NEW, "and never raised toward it"


@pytest.mark.parametrize("bad", [10 ** 400, -(10 ** 400), 2 ** 53, -5, 0, -1e20, 1e300])
def test_no_number_in_the_field_fails_a_pass_or_becomes_a_voice(world, bad):
    """C-23.28 (review of #167): an integer too large for a float made the
    first version raise OverflowError and leave the pass recorded as running.
    A number outside the dates the app writes is no voice; the rest of the
    session is decided without it, and the copy that holds it is not written."""
    place(world, ONE, (NEW, OLD, bad))
    running = engine(world)
    result = running.run_once(options(running))
    assert result.state == "ok" and result.error is None
    assert dates(world) == (NEW, NEW - 1, bad)
    assert running.sidecar()["pass"]["state"] == "ok"
    again = running.run_once(options(running))
    assert again.activity_synced == 0, "and it is not chosen again each pass, taking a place"


def test_a_date_written_as_a_float_is_a_date(world):
    """C-23.28 (review of #167): JSON has one number type, and a record whose
    date parses as a float is raised and raised from like any other."""
    place(world, ONE, (float(NEW), float(OLD), OLD))
    running = engine(world)
    assert running.run_once(options(running)).activity_synced == 1
    after = dates(world)
    assert after == (NEW, NEW - 1, NEW - 1)
    assert isinstance(after[0], float), "the leader's own record is not rewritten"


def test_a_dry_run_counts_the_raise_and_writes_nothing(world):
    """C-17.4: a preview that writes is not a preview."""
    place(world, ONE, (NEW, OLD, OLD))
    running = engine(world)
    result = running.run_once(options(running, dry_run=True))
    assert result.activity_synced == 1 and dates(world) == (NEW, OLD, OLD)


def test_the_hot_pass_raises_a_session_that_just_ran(world):
    """C-23.28: the pass that runs every two seconds carries the date as it
    carries flags, so the copy is in place before the next account switch."""
    place(world, ONE, (OLD, OLD, OLD))
    running = engine(world)
    running.run_once(options(running))
    rewrite(path(world, 0), lastActivityAt=NEW)
    result = running.run_hot(options(running))
    assert result.kind == "hot" and result.activity_synced == 1
    assert dates(world) == (NEW, NEW - 1, NEW - 1)


def test_a_raise_is_journaled_like_any_write_into_the_store(world):
    """C-23.28: the load-gap report counts the mirror's writes the running app
    has not seen; a raised date is one."""
    place(world, ONE, (NEW, OLD, OLD))
    running = engine(world)
    running.run_once(options(running))
    kinds = {(row.folder, row.kind) for row in running.journal.rows()}
    assert ("acct-b/org-b", "updated") in kinds and ("acct-c/org-c", "updated") in kinds
    assert ("acct-a/org-a", "updated") not in kinds


# --- a date is not a flag ----------------------------------------------------------------

def test_a_write_that_changed_only_a_date_is_not_a_flag_write(world):
    """C-23.28 (review of #167): the hot pass tells the full pass when it left
    a flag write standing, so that the full pass reads the flags again. A
    raised date changes no field a flag decision reads."""
    place(world, ONE, (OLD, OLD, OLD))
    running = engine(world)
    running.run_once(options(running))
    rewrite(path(world, 0), lastActivityAt=NEW)
    assert running.run_hot(options(running)).activity_synced == 1
    assert running._flags_moved is False, "a date alone invalidates nothing"
    assert dates(world) == (NEW, NEW - 1, NEW - 1)
    rewrite(path(world, 0), isStarred=True)
    assert running.run_hot(options(running)).flag_synced == 1
    assert running._flags_moved is True, "a flag write still does"


def test_an_app_resaving_an_old_date_cannot_hold_every_sessions_flags(world, monkeypatch):
    """C-23.28 (review of #167, the reviewer's own schedule): while a full pass
    takes its inventory the app saves one record from memory, with its old
    date, each time the pass reads it. The embedded hot pass raises the date
    again each time. Counted as flag writes, those raises invalidated both of
    the full pass's refreshes, and it held every session: another session's
    new title never reached its copies. They are not flag writes."""
    home, store, _root = world
    (store / "acct-c" / "org-c").rmdir()
    busy, renamed = "session-000", "session-001"
    for session in (busy, renamed):
        fx.transcript(home, session, [*fx.completed(),
                                      {"type": "custom-title", "customTitle": "old-title"}])
        for index, date in ((0, OLD), (1, NEW)):
            fx.index_entry(store, *FOLDERS[index], session, last_activity=date,
                           title="old-title", settings={"ultracode": True})
    _home, _store, root = world
    running = mirror.Mirror(root, fx.policy(mirror_hot_interval_s=2), now=lambda: CLOCK)
    assert running.run_once(options(running)).state == "ok"
    assert running.run_hot(options(running)).state == "ok"
    title_file = home / "projects" / fx.project_slug() / f"{renamed}.jsonl"
    with title_file.open("a") as stream:
        stream.write(json.dumps({"type": "custom-title", "customTitle": "NEW TITLE"}) + "\n")

    source = path(world, 0, busy)
    read_entry, take_inventory = running._entry, running._flag_inventory
    clock = mirror.time.monotonic
    offset, phase, edited, refreshes = [0.0], [0], set(), []
    monkeypatch.setattr(mirror.time, "monotonic", lambda: clock() + offset[0])
    running._last_sweep = None

    def inventory(current, opts):
        refreshes.append(len(refreshes) + 1)
        phase[0] = len(refreshes)
        try:
            return take_inventory(current, opts)
        finally:
            phase[0] = 0

    def slow_entry(where, *args, **kwargs):
        data = read_entry(where, *args, **kwargs)
        if Path(where) == source and phase[0] not in edited:
            rewrite(source, lastActivityAt=OLD)         # the app's memory comes back
            edited.add(phase[0])
            offset[0] += 3                              # and the embedded hot pass is due
        return data

    monkeypatch.setattr(running, "_entry", slow_entry)
    monkeypatch.setattr(running, "_flag_inventory", inventory)
    result = running.run_once(options(running))
    assert result.state == "ok", result.error
    assert running._hot_services >= 2, "the embedded hot pass ran, and raised the date"
    assert running._hot_epoch == 0, "and invalidated nothing"
    assert refreshes == [1] and result.flags_held == 0
    assert [record(world, index, renamed)["title"] for index in (0, 1)] == ["NEW TITLE"] * 2
    assert mirror._load(running.flags_path)[renamed]["ttitle"] == "NEW TITLE"


# --- the bound on one pass -----------------------------------------------------------

def test_a_pass_raises_a_bounded_number_of_sessions_furthest_behind_first(world):
    """C-23.28: the publish has no cancellation point, so the backlog a first
    pass finds must not become one publish. The rest wait and are counted."""
    home, store, _root = world
    total = mirror.ACTIVITY_SESSIONS_PER_PASS + 3
    sessions = [f"{index:08d}-0000-4000-8000-000000000000" for index in range(total)]
    for index, session in enumerate(sessions):
        fx.transcript(home, session, fx.completed())
        fx.index_entry(store, *FOLDERS[0], session, last_activity=NEW,
                       settings={"ultracode": True})
        # Session 0 is the least behind, the last one the most.
        fx.index_entry(store, *FOLDERS[1], session, last_activity=NEW - (index + 2) * DAY,
                       settings={"ultracode": True})
    (store / "acct-c" / "org-c").rmdir()
    running = engine(world)

    def raised() -> list[int]:
        return [index for index, session in enumerate(sessions)
                if record(world, 1, session)["lastActivityAt"] == NEW - 1]

    first = running.run_once(options(running))
    assert first.activity_synced == mirror.ACTIVITY_SESSIONS_PER_PASS
    assert first.activity_waiting == 3 and "dates waiting 3" in first.summary
    assert raised() == list(range(3, total)), "the three least behind wait"
    second = running.run_once(options(running))
    assert second.activity_synced == 3 and second.activity_waiting == 0
    assert raised() == list(range(total))
    assert running.run_once(options(running)).activity_synced == 0


WRITABLE = "99999999-0000-4000-8000-00000000beef"


def stuck_and_one_writable(world, monkeypatch, count: int) -> list[str]:
    """`count` sessions thirty days behind in B whose writes always fail, as if
    the app saved each just before, and one two days behind that can be written.
    Returns what each refused write was for, in order."""
    home, store, _root = world
    (store / "acct-c" / "org-c").rmdir()
    stuck = [f"{index:08d}-0000-4000-8000-00000000dead" for index in range(count)]
    for session in (*stuck, WRITABLE):
        fx.transcript(home, session, fx.completed())
        fx.index_entry(store, *FOLDERS[0], session, last_activity=NEW,
                       settings={"ultracode": True})
        fx.index_entry(store, *FOLDERS[1], session, settings={"ultracode": True},
                       last_activity=NEW - (2 if session == WRITABLE else 30) * DAY)
    install = mirror._install
    refused: list[str] = []

    def refusing(temporary, destination, **kwargs):
        if kwargs.get("expect") is not None and any(name in destination.name for name in stuck):
            refused.append(destination.stem.removeprefix("local_")[:8])
            temporary.unlink()
            return False
        return install(temporary, destination, **kwargs)

    monkeypatch.setattr(mirror, "_install", refusing)
    return refused


def app_saves_every_record(world, number: int) -> None:
    """A save that keeps flags and dates, so every session is a hot candidate."""
    for index in (0, 1):
        for record_path in (world[1] / FOLDERS[index][0] / FOLDERS[index][1]).glob("*.json"):
            rewrite(record_path, lastFocusedAt=number)


def test_sessions_that_cannot_be_published_yield_the_bound_to_the_rest(world, monkeypatch):
    """C-23.28 (review of #167): ten sessions furthest behind whose writes
    always fail took the whole bound every pass, and an eleventh that could be
    written never was. A session chosen and not published goes behind those
    that were not."""
    writable = WRITABLE
    stuck_and_one_writable(world, monkeypatch, mirror.ACTIVITY_SESSIONS_PER_PASS)
    running = engine(world)
    first = running.run_once(options(running))
    assert (first.activity_synced, first.activity_waiting, first.flags_held) == (10, 1, 10)
    assert record(world, 1, writable)["lastActivityAt"] == NEW - 2 * DAY
    second = running.run_once(options(running))
    assert second.flags_held == 9, "nine of the ten were chosen again, behind the eleventh"
    assert record(world, 1, writable)["lastActivityAt"] == NEW - 1
    third = running.run_once(options(running))
    assert (third.activity_synced, third.flags_held) == (10, 10), "and the ten are tried again"


def test_two_groups_that_cannot_be_published_do_not_take_turns_for_ever(world, monkeypatch):
    """C-23.28 (second review of #167): with a memory of one sync, twenty
    failing sessions alternated in tens and the twenty-first was never chosen.
    The ledger counts: once each of the twenty has failed once, the one that
    has not comes first."""
    refused = stuck_and_one_writable(world, monkeypatch, 2 * mirror.ACTIVITY_SESSIONS_PER_PASS)
    running = engine(world)
    chosen = []
    for _ in range(4):
        refused.clear()
        result = running.run_once(options(running))
        chosen.append((sorted(refused), result.flags_held,
                       record(world, 1, WRITABLE)["lastActivityAt"]))
    names = [f"{index:08d}" for index in range(20)]
    assert chosen[0] == (names[:10], 10, NEW - 2 * DAY)
    assert chosen[1] == (names[10:], 10, NEW - 2 * DAY)
    assert chosen[2] == (names[:9], 9, NEW - 1), "the writable one, and nine behind it"
    assert chosen[3] == (names[9:19], 10, NEW - 1), "then those that have failed once"
    assert WRITABLE not in running._activity_tries, "one that went through leaves the ledger"
    assert sorted(running._activity_tries.values()) == [1] + [2] * 19


def test_the_hot_pass_keeps_the_same_ledger(world, monkeypatch):
    """C-23.28 (second review of #167): the same schedule through the pass that
    runs every two seconds, with the app saving every record in between."""
    stuck_and_one_writable(world, monkeypatch, 2 * mirror.ACTIVITY_SESSIONS_PER_PASS)
    running = engine(world)
    running.run_once(mirror.Options(activity_lag_s=0))      # an inventory, and no raise
    for number in range(1, 4):
        app_saves_every_record(world, number)
        assert running.run_hot(options(running)).state == "ok"
    assert record(world, 1, WRITABLE)["lastActivityAt"] == NEW - 1


def test_the_embedded_hot_pass_and_the_full_pass_share_one_ledger(world, monkeypatch):
    """C-23.28 (second review of #167): the hot pass a full pass runs at its
    checkpoints chose ten that failed; the full pass's own sync, moments
    later, must not choose the same ten first."""
    refused = stuck_and_one_writable(world, monkeypatch, mirror.ACTIVITY_SESSIONS_PER_PASS)
    _home, _store, root = world
    running = mirror.Mirror(root, fx.policy(mirror_hot_interval_s=2), now=lambda: CLOCK)
    running.run_once(mirror.Options(activity_lag_s=0))
    app_saves_every_record(world, 1)
    checkpoint, serviced = running._checkpoint, []

    def one_hot_service(current, stage=None):
        if stage == "reading entries" and not serviced:
            serviced.append(True)
            running._service_hot()
            running._hot_due = float("inf")
        return checkpoint(current, stage)

    monkeypatch.setattr(running, "_checkpoint", one_hot_service)
    result = running.run_once(options(running))
    assert running._hot_services == 1 and len(refused) >= 10
    assert running._hot_worker is None and result.state == "ok"
    assert record(world, 1, WRITABLE)["lastActivityAt"] == NEW - 1, \
        "the full sync put the ten that had just failed behind the one that had not"
    worker = running._fork_hot()
    assert worker._activity_tries is running._activity_tries


def test_a_session_that_no_longer_needs_a_raise_leaves_the_ledger(world, monkeypatch):
    """C-23.28: the count is of failures since the session last went through.
    One that the app has since run, or that is gone, starts again from nothing."""
    running = engine(world)
    with monkeypatch.context() as patch:
        stuck_and_one_writable(world, patch, 3)
        running.run_once(options(running))
    assert len(running._activity_tries) == 3
    rewrite(path(world, 1, "00000000-0000-4000-8000-00000000dead"),
            lastActivityAt=NEW)                             # the app runs one in B
    for index in (0, 1):                                    # one is deleted
        path(world, index, "00000001-0000-4000-8000-00000000dead").unlink()
    running.run_once(options(running))                      # and the third's write goes through
    assert record(world, 1, "00000002-0000-4000-8000-00000000dead")["lastActivityAt"] == NEW - 1
    assert running._activity_tries == {}


# --- the switch ------------------------------------------------------------------------

def test_a_lag_of_zero_switches_the_sync_off(world):
    """C-6.4: `sessions.mirror_activity_lag_s: 0` is a policy change."""
    place(world, ONE, (NEW, OLD, OLD))
    running = engine(world, mirror_activity_lag_s=0)
    result = running.run_once(options(running))
    assert result.activity_synced == 0 and dates(world) == (NEW, OLD, OLD)


def test_the_lag_is_the_policys(world):
    """C-6.4: a day's lag leaves a copy twelve hours behind alone."""
    place(world, ONE, (NEW, NEW - 12 * HOUR, NEW - 25 * HOUR))
    running = engine(world, mirror_activity_lag_s=86_400)
    running.run_once(options(running))
    assert dates(world) == (NEW, NEW - 12 * HOUR, NEW - 1)


def test_the_shipped_policy_names_the_lag_and_refuses_a_negative_one(tmp_path):
    """C-6.4: the key ships in `default_policy.json` with the code's default,
    and is validated like every window under `sessions`."""
    shipped = load_policy(DEFAULT_POLICY_PATH)
    assert shipped["sessions"]["mirror_activity_lag_s"] == mirror.DEFAULT_ACTIVITY_LAG_S == 3600
    assert json.loads(DEFAULT_POLICY_PATH.read_text())["sessions"]["mirror_activity_lag_s"] == 3600
    assert mirror.Options().activity_lag_s == mirror.DEFAULT_ACTIVITY_LAG_S
    value = json.loads(DEFAULT_POLICY_PATH.read_text())
    value["sessions"]["mirror_activity_lag_s"] = -1
    bad = tmp_path / "policy.json"
    bad.write_text(json.dumps(value))
    with pytest.raises(PolicyError):
        load_policy(bad)
    del value["sessions"]["mirror_activity_lag_s"]
    bad.write_text(json.dumps(value))
    assert load_policy(bad)["sessions"]["mirror_activity_lag_s"] == 3600, \
        "an installed policy without the key gets the default"


# --- one session id, two conversations ---------------------------------------------------

def split(world, *, fork_archived: bool = False) -> None:
    """The 2026-10-10 store: `local_app.json` opens REAL in A and B and FORK in C."""
    home, store, _root = world
    for session in (REAL, FORK):
        fx.transcript(home, session, fx.completed())
    for index in (0, 1):
        fx.index_entry(store, *FOLDERS[index], REAL, name="local_app.json", title="Partner",
                       last_activity=NEW if index == 0 else OLD, starred=True,
                       settings={"ultracode": True})
    fx.index_entry(store, *FOLDERS[2], FORK, name="local_app.json", title="Partner",
                   last_activity=NEW - 40 * DAY, archived=fork_archived,
                   priorCliSessionIds=[REAL], settings={"ultracode": True})


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def test_a_session_id_that_opens_two_conversations_is_reported(world):
    """C-23.28: both transcripts exist, so each gets a row in every folder, the
    second under a name of its own, with one title. The pass says so."""
    split(world)
    running = engine(world)
    assert running.splits() == {"count": 0, "live": 0, "checked_at": None, "sessions": []}
    running.run_once(options(running))
    assert record(world, 2, name=f"local_{REAL}.json")["cliSessionId"] == REAL
    assert record(world, 0, name=f"local_{FORK}.json")["cliSessionId"] == FORK
    report = running.splits()
    assert (report["count"], report["live"]) == (1, 1) and report["checked_at"]
    row = report["sessions"][0]
    assert row["name"] == "local_app.json" and row["live"] is True
    assert [(item["id"], item["folders"], item["archived"], item["title"], item["last_activity"])
            for item in row["conversations"]] == [
        (REAL, 2, False, "Partner", iso(NEW)), (FORK, 1, False, "Partner", iso(NEW - 40 * DAY))]


def test_the_row_that_kept_running_shows_the_later_date_in_every_folder(world):
    """C-23.28: the fix for 2026-10-10 itself. In C the session Max meant sits
    under a name of its own, copied with an old date; it must not look older
    than the fork beside it."""
    split(world)
    running = engine(world)
    running.run_once(options(running))
    for index in range(3):
        name = "local_app.json" if index < 2 else f"local_{REAL}.json"
        fork = f"local_{FORK}.json" if index < 2 else "local_app.json"
        assert record(world, index, name=name)["lastActivityAt"] >= NEW - 1
        assert record(world, index, name=fork)["lastActivityAt"] == NEW - 40 * DAY


def test_a_split_with_one_side_archived_is_counted_and_is_not_live(world):
    """C-23.28: one row shows, so nobody can open the wrong one."""
    split(world, fork_archived=True)
    running = engine(world)
    running.run_once(options(running))
    report = running.splits()
    assert (report["count"], report["live"]) == (1, 0)
    assert [item["archived"] for item in report["sessions"][0]["conversations"]] == [False, True]


def test_a_name_whose_other_conversation_has_no_transcript_is_not_a_split(world):
    """C-23.28: a dead session is in no sidebar, so it is no second row."""
    home, store, _root = world
    fx.transcript(home, REAL, fx.completed())
    fx.index_entry(store, *FOLDERS[0], REAL, name="local_app.json", settings={"ultracode": True})
    fx.index_entry(store, *FOLDERS[1], FORK, name="local_app.json", settings={"ultracode": True})
    running = engine(world)
    running.run_once(options(running))
    assert running.splits()["count"] == 0


def test_the_report_outlives_the_passes_that_do_not_take_an_inventory(world):
    """C-23.28: a hot pass and another process's pass keep the last report."""
    split(world)
    running = engine(world)
    running.run_once(options(running))
    checked = running.splits()["checked_at"]
    running.run_hot(options(running))
    assert running.splits()["checked_at"] == checked
    other = engine(world)                                    # a process that has not inventoried
    other._record(mirror.Pass(started_at="2026-09-05T12:00:00Z"))
    assert other.splits()["live"] == 1


def test_a_pass_that_could_not_list_a_folder_keeps_the_last_whole_report(world, monkeypatch):
    """C-23.28 (review of #167): the folder that did not list held the other
    half of the split. A report without it read "no split", and doctor passed,
    while the id still opened two conversations."""
    from subfleet import doctor
    split(world)
    running = mirror.Mirror(world[2], fx.policy(mirror_hot_interval_s=0))   # doctor's clock
    running.run_once(options(running))
    running.run_once(options(running))
    whole = running.splits()
    assert (whole["count"], whole["live"]) == (1, 1)
    scan = running._scan

    def unlisted(folder, *args, **kwargs):
        if folder == world[1] / "acct-c" / "org-c":
            raise mirror._Unlisted(str(folder), transient=True)
        return scan(folder, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(running, "_scan", unlisted)
        assert running.run_once(options(running)).state == "ok"
    assert record(world, 2, name="local_app.json")["cliSessionId"] == FORK
    assert running.splits() == whole, "the last whole report stands"
    row = next(item for item in doctor.checks(world[2])
               if item["check"] == "desktop sidebar split ids")
    assert row["status"] == doctor.WARN


def test_a_copy_that_could_not_be_read_keeps_the_last_whole_report_too(world, monkeypatch):
    """C-23.28: an unreadable copy may be the one that makes a name a split."""
    split(world)
    running = engine(world)
    running.run_once(options(running))
    running.run_once(options(running))
    whole = running.splits()
    target = path(world, 2, name="local_app.json")
    rewrite(target, lastFocusedAt=7)                         # so the pass must read it again
    read = mirror._read_entry

    def unreadable(where):
        if str(where) == str(target):
            raise OSError(24, "Too many open files")
        return read(where)

    with monkeypatch.context() as patch:
        patch.setattr(mirror, "_read_entry", unreadable)
        assert running.run_once(options(running)).state == "ok"
    assert running.splits() == whole


def test_an_emptied_store_has_no_split(world):
    """C-23.28 (review of #167): an empty store is a whole inventory, and its
    report replaces the last one."""
    import shutil
    split(world)
    running = engine(world)
    running.run_once(options(running))
    assert running.splits()["count"] == 1
    for account, _org in FOLDERS:
        shutil.rmtree(world[1] / account)
    result = running.run_once(options(running))
    assert result.state == "ok" and result.sessions == 0
    report = running.splits()
    assert (report["count"], report["live"], report["sessions"]) == (0, 0, [])


def test_a_process_with_an_older_report_never_puts_it_over_a_later_one(world):
    """C-23.28 (review of #167): the daemon keeps one mirror for its life, and a
    `sessions mirror` pass is another process. Each records the pass it ran;
    neither may replace the other's later inventory with its own earlier one."""
    import shutil
    split(world)
    earlier = engine(world)
    earlier.run_once(options(earlier))
    assert earlier.splits()["count"] == 1
    for account, _org in FOLDERS:
        shutil.rmtree(world[1] / account)
    (world[1] / "acct-a" / "org-a").mkdir(parents=True)
    later = mirror.Mirror(world[2], fx.policy(mirror_hot_interval_s=0),
                          now=lambda: CLOCK + timedelta(minutes=5))
    later.run_once(options(later))
    newest = later.splits()
    assert newest["count"] == 0
    assert earlier._splits is None, "a recorded report is not kept to be written again"
    earlier._record(mirror.Pass(started_at="2026-10-13T12:30:00Z"))   # a heartbeat, no inventory
    assert earlier.splits() == newest


def test_two_inventories_in_one_second_are_ordered_by_the_lock_not_the_clock(world):
    """C-23.28 (second review of #167): `checked_at` has whole seconds, and two
    reports a few hundred milliseconds apart compared equal, so the earlier
    process's next record put its report back. A report is written once, by
    the pass that took the inventory, and passes are ordered by their lock."""
    import shutil
    split(world)
    earlier = mirror.Mirror(world[2], fx.policy(mirror_hot_interval_s=0),
                            now=lambda: CLOCK + timedelta(milliseconds=100))
    earlier.run_once(options(earlier))
    for account, _org in FOLDERS:
        shutil.rmtree(world[1] / account)
    (world[1] / "acct-a" / "org-a").mkdir(parents=True)
    later = mirror.Mirror(world[2], fx.policy(mirror_hot_interval_s=0),
                          now=lambda: CLOCK + timedelta(milliseconds=900))
    later.run_once(options(later))
    newest = later.splits()
    assert newest["count"] == 0 and newest["checked_at"] == mirror._iso(CLOCK)
    earlier._record(mirror.Pass(started_at="2026-10-13T12:26:41Z"))
    assert earlier.splits() == newest


def test_a_dry_runs_report_is_not_recorded_by_a_later_pass(world, monkeypatch):
    """C-17.4 and C-23.28: a preview records nothing, now or later. The real
    pass after it cannot list a folder, so the last whole report must stand,
    not the one the preview computed."""
    split(world, fork_archived=True)
    running = engine(world)
    running.run_once(options(running))
    whole = running.splits()
    assert whole["live"] == 0
    rewrite(path(world, 2, name="local_app.json"), isArchived=False)   # now two rows show
    preview = running.run_once(options(running, dry_run=True))
    assert preview.state == "ok" and running._splits is None, "the preview kept no report"
    scan = running._scan

    def unlisted(folder, *args, **kwargs):
        if folder == world[1] / "acct-a" / "org-a":
            raise mirror._Unlisted(str(folder), transient=True)
        return scan(folder, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(running, "_scan", unlisted)
        assert running.run_once(options(running)).state == "ok"
    assert running.splits() == whole


def listing_fails(patch, where: Path, error: int = 24) -> None:
    scan = mirror.os.scandir

    def failing(target="."):
        if Path(target) == where:
            raise OSError(error, os.strerror(error), str(target))
        return scan(target)

    patch.setattr(mirror.os, "scandir", failing)


def test_a_pass_that_could_not_list_the_transcripts_keeps_the_last_whole_report(
        world, monkeypatch):
    """C-23.28 (second review of #167): with `projects/` unlistable for a
    moment (EMFILE), every conversation looked dead, the pass was `ok`, and
    the report said no id was split. Both transcripts were still there."""
    split(world)
    running = engine(world)
    running.run_once(options(running))
    whole = running.splits()
    assert whole["live"] == 1
    with monkeypatch.context() as patch:
        listing_fails(patch, world[0] / "projects")
        result = running.run_once(options(running))
    assert result.state == "ok" and result.sessions == 0 and running._stems_whole is False
    assert running.splits() == whole


def test_a_project_directory_that_failed_to_list_is_listed_again_next_pass(world, monkeypatch):
    """C-23.28 (second review of #167): the failed listing was kept as an empty
    one, and since the directory's mtime had not moved, the two passes after
    it saw no transcripts there either. It is not kept, and the report waits."""
    split(world)
    running = engine(world)
    running.run_once(options(running))
    running.run_once(options(running))
    whole = running.splits()
    running._last_sweep = None                              # a sweep lists it again
    with monkeypatch.context() as patch:
        listing_fails(patch, world[0] / "projects" / fx.project_slug())
        failed = running.run_once(options(running))
    assert failed.state == "ok" and running._stems_whole is False
    assert running.splits() == whole
    after = running.run_once(options(running))
    assert after.sessions == 2 and running._stems_whole is True, "its transcripts are back"
    report = running.splits()
    assert (report["count"], report["live"]) == (1, 1)


def test_a_project_directory_the_user_may_not_read_does_not_hold_the_report(world, monkeypatch):
    """C-23.28: what the user may not read, the app cannot open either. Its
    sessions are dead to every sidebar, and the inventory is whole without it."""
    split(world)
    running = engine(world)
    running.run_once(options(running))
    running._last_sweep = None
    with monkeypatch.context() as patch:
        listing_fails(patch, world[0] / "projects" / fx.project_slug(), error=13)
        result = running.run_once(options(running))
    assert result.state == "ok" and running._stems_whole is True
    assert running.splits()["count"] == 0


def run_cli(argv) -> tuple[int, str, str]:
    from subfleet import cli
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def test_status_names_both_conversations_and_what_ends_the_split(world, monkeypatch):
    """C-17.4 and C-23.28: `sessions mirror --status` says which id, which two
    conversations, how many folders open each and when each last ran; `--json`
    stays one object."""
    split(world)
    running = engine(world)
    running.run_once(options(running))
    monkeypatch.setenv("SUBFLEET_HOME", str(world[2]))
    _code, out, _err = run_cli(["sessions", "mirror", "--status"])
    assert ("sidebar split: 1 session id opens a different conversation under different "
            "logins, and both rows show") in out
    assert (f"  local_app.json  Partner: {REAL[:8]} in 2 folders, last active {iso(NEW)}; "
            f"{FORK[:8]} in 1 folder, last active {iso(NEW - 40 * DAY)}") in out
    assert "fix: open both rows" in out and "archive" in out
    _code, out, err = run_cli(["sessions", "mirror", "--status", "--json"])
    value = json.loads(out)
    assert value["splits"]["live"] == 1 and value["load_gap"]["status"] and err == ""


def test_status_says_nothing_of_a_split_that_does_not_show(world, monkeypatch):
    """C-17.4: no line for what needs no action."""
    split(world, fork_archived=True)
    running = engine(world)
    running.run_once(options(running))
    monkeypatch.setenv("SUBFLEET_HOME", str(world[2]))
    _code, out, _err = run_cli(["sessions", "mirror", "--status"])
    assert "sidebar split" not in out
    _code, out, _err = run_cli(["sessions", "mirror", "--status", "--json"])
    assert json.loads(out)["splits"]["count"] == 1


def test_doctor_warns_while_both_rows_of_a_split_show(world):
    """C-17.3 and C-23.28: a `warn` row with a fix, which never fails doctor;
    `unknown` before any pass, `pass` once one side is archived."""
    from subfleet import doctor

    def row():
        return next(item for item in doctor.checks(world[2])
                    if item["check"] == "desktop sidebar split ids")

    assert row()["status"] == doctor.UNKNOWN
    split(world)
    # `doctor` has no injected clock: it reports what is true now, so these
    # passes run on the real one, an hour past the fixture's dates' future guard.
    running = mirror.Mirror(world[2], fx.policy(mirror_hot_interval_s=0))
    relaxed = mirror.Options(activity_lag_s=0)
    running.run_once(relaxed)
    item = row()
    assert item["status"] == doctor.WARN and "Partner" in item["detail"]
    assert "1 session id opens" in item["detail"] and "--status" in item["fix"]
    assert doctor.exit_code([item]) == 0
    rewrite(path(world, 2, name="local_app.json"), isArchived=True)   # archive the fork in C
    running.run_once(relaxed)                                # the archive reaches every login
    running.run_once(relaxed)                                # the report reads the result
    item = row()
    assert item["status"] == doctor.PASS and "1 split id" in item["detail"]


def test_doctor_does_not_vouch_for_a_report_no_recent_pass_could_replace(world):
    """C-17.3 and C-23.28: only a whole inventory replaces the report, so one
    that passes keep failing to take leaves an old report standing. Doctor
    says how old it is instead of passing on it."""
    from subfleet import doctor
    split(world, fork_archived=True)
    long_ago = mirror.Mirror(world[2], fx.policy(mirror_hot_interval_s=0),
                             now=lambda: fx.NOW)             # weeks before the real clock
    long_ago.run_once(mirror.Options(activity_lag_s=0))
    assert long_ago.splits()["live"] == 0
    item = next(item for item in doctor.checks(world[2])
                if item["check"] == "desktop sidebar split ids")
    assert item["status"] == doctor.UNKNOWN
    assert "the last whole inventory is from 2026-09-05T11:30:00Z" in item["detail"]
    assert "--once" in item["fix"]
