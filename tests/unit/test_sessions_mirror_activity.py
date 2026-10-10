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
                         now=lambda: fx.NOW + timedelta(seconds=next(ticks)))


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
    running = engine(world)
    running.run_once(options(running))
    item = row()
    assert item["status"] == doctor.WARN and "Partner" in item["detail"]
    assert "1 session id opens" in item["detail"] and "--status" in item["fix"]
    assert doctor.exit_code([item]) == 0
    rewrite(path(world, 2, name="local_app.json"), isArchived=True)   # archive the fork in C
    running.run_once(options(running))                       # the archive reaches every login
    running.run_once(options(running))                       # the report reads the result
    item = row()
    assert item["status"] == doctor.PASS and "1 split id" in item["detail"]
