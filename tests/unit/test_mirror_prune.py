"""The mirror's scope rules and pruning on real files: C-23.28a.

The store, the transcripts and the app's log live under a temporary
directory; nothing here reads the operator's own. Four folders, as on
2026-09-25 in miniature:

* `acct-home/org-home`: the home folder, where old archived sessions live;
* `acct-work/org-work`: the folder the app has loaded;
* `acct-other/org-other`: another account the app settles in;
* `acct-home/org-work`: a switch folder, the home account paired with the
  work account's org for two seconds while the app switched.

`test_mirror_prune_plan.py` states the planner's invariants for every input;
`test_the_mirror_removes_exactly_what_the_planner_plans` below holds the real
mirror to that planner on random stores.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import tempfile
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from subfleet.sessions import desktop, folder_kinds, mirror
from subfleet.sessions import prune as prune_rules
from tests import sessions_fixtures as fx

UTC = timezone.utc
NOW_S = fx.NOW.timestamp()                       # 2026-09-05 11:30 UTC
DAY = prune_rules.DAY_S
HOME, WORK = ("acct-home", "org-home"), ("acct-work", "org-work")
OTHER, SWITCH = ("acct-other", "org-other"), ("acct-home", "org-work")
FOLDERS = (HOME, WORK, OTHER, SWITCH)
SEEDED = {HOME, WORK, OTHER}
ONE, TWO = "11111111-aaaa-4000-8000-000000000001", "22222222-bbbb-4000-8000-000000000002"


def key(folder: tuple[str, str]) -> str:
    return f"{folder[0]}/{folder[1]}"


def loads(store: Path, folder: tuple[str, str], at: str) -> list[str]:
    """The two lines the app writes when it lists a session folder."""
    account, org = folder
    return [f"{at} [info] [LocalSessionManager] Initialization succeeded — "
            f"accountId={account}, orgId={org}, existingSessions=0",
            f"{at} [info] Loaded 3 persisted sessions from {store / account / org} "
            "(10 archived deferred, 10 of them unopened; hint used)"]


def say(log: Path, *lines: str) -> None:
    """Append lines as the app does; the file's mtime is its last line's (UTC)."""
    with log.open("a", encoding="utf-8") as stream:
        for line in lines:
            stream.write(line + "\n")
    from datetime import datetime
    stamp = datetime.strptime(lines[-1][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    os.utime(log, (stamp.timestamp(), stamp.timestamp()))


def history(store: Path, *, since: str = "2026-08-01 08:00:00") -> list[str]:
    """The home folder since `since` (by default over a month back, so the
    registry has learned `SWITCH_PRUNE_AFTER_S`), an hour in another
    account's, then a switch to work: two seconds in the home account with
    work's org, then work, loaded since 10:00:02 (an hour and a half by the
    fixture's clock)."""
    return [*loads(store, HOME, since),
            *loads(store, OTHER, "2026-09-05 09:00:00"),
            *loads(store, SWITCH, "2026-09-05 10:00:00"),
            *loads(store, WORK, "2026-09-05 10:00:02")]


def make_world(base: Path, monkeypatch, *, log_lines: list[str] | None = None) -> SimpleNamespace:
    home = fx.claude_home(base, monkeypatch)
    store = fx.desktop_store(base, monkeypatch)
    for account, org in FOLDERS:
        (store / account / org).mkdir(parents=True, exist_ok=True)
    log = base / "logs" / "main.log"
    log.parent.mkdir(exist_ok=True)
    log.write_text("", encoding="utf-8")
    lines = history(store) if log_lines is None else log_lines
    if lines:
        say(log, *lines)
    monkeypatch.setenv(desktop.LOG_ENV, str(log))
    root = base / "state"
    root.mkdir(exist_ok=True)
    return SimpleNamespace(home=home, store=store, log=log, root=root)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return make_world(tmp_path, monkeypatch)


def engine(world) -> mirror.Mirror:
    """A mirror on the fixture clock that reads the fixture log as UTC."""
    running = mirror.Mirror(world.root, fx.policy(), now=lambda: fx.NOW)
    running._desktop = desktop.DesktopLog(world.log, store=world.store, tz=UTC)  # noqa: SLF001
    return running


def options(**changes) -> mirror.Options:
    values = dict(skip_switch_folders=True, archive_days=14.0, dead_days=7.0,
                  dead_home=key(HOME), restore=False)
    values.update(changes)
    return mirror.Options(**values)


def put(world, session: str, folders, *, archived: bool = False, starred: bool = False,
        days: float = 1.0, written: float | None = None, openable: bool = True,
        project: bool = True, title: str = "a session") -> None:
    """Copies of one session, quiet `days`, last written `written` days ago.

    A session that is not `openable` has no transcript; its project
    directory is there all the same (as for 120 of the 126 dead sessions on
    2026-10-13) unless `project` is false."""
    if openable:
        fx.transcript(world.home, session, fx.completed())
    elif project:
        (world.home / "projects" / fx.project_slug()).mkdir(parents=True, exist_ok=True)
    for account, org in folders:
        path = fx.index_entry(world.store, account, org, session, archived=archived,
                              starred=starred, title=title,
                              last_activity=int((NOW_S - days * DAY) * 1000),
                              settings={"ultracode": True})
        stamp = NOW_S - (days if written is None else written) * DAY
        os.utime(path, (stamp, stamp))


def where(world, session: str) -> set[tuple[str, str]]:
    found = set()
    for path in world.store.glob("*/*/local_*.json"):
        if json.loads(path.read_text(encoding="utf-8")).get("cliSessionId") == session:
            found.add((path.parent.parent.name, path.parent.name))
    return found


def record(world, folder, session: str = ONE) -> Path:
    return world.store / folder[0] / folder[1] / f"local_{session}.json"


def rewrite(path: Path, **changes) -> None:
    """The app's own write: beside the record, then renamed over it (a new inode)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(changes)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(path)


def base(world) -> dict:
    return json.loads((world.root / "sessions" / mirror.FLAGS_NAME).read_text())


def graves(world) -> list[Path]:
    return list(world.store.glob(f"*/*/*{mirror.GRAVE_SUFFIX}"))


# --- spreading only where a session belongs ------------------------------------

def test_switch_folders_are_never_seeded_by_either_pass(world):
    """C-23.28a: a folder the app only passes through mid-switch, whose org
    another account owns, is in no session's scope: the full pass and the
    2 s hot pass both leave it out. With the rule off it is seeded as before."""
    put(world, ONE, [HOME])
    running = engine(world)
    result = running.run_once(options())
    assert where(world, ONE) == SEEDED
    assert result.prune["folders"] == {"settled": 3, "switch": 1, "unknown": 0}
    put(world, TWO, [WORK])                          # a new session, in the loaded folder
    running.run_hot(options())
    assert where(world, TWO) == SEEDED, "the hot pass spreads it, but not into the switch folder"
    running.run_once(options(skip_switch_folders=False))
    assert SWITCH in where(world, ONE) and SWITCH in where(world, TWO)


def test_an_old_archived_session_is_not_spread_and_a_young_one_is(world):
    """C-23.28a: archived, unstarred and quiet `mirror_archive_days`: it
    belongs in the home only, so neither pass spreads it, even after the app
    re-saves a copy (the archive stamp does not move). A young one spreads."""
    put(world, ONE, [HOME, OTHER], archived=True, days=30)
    put(world, TWO, [HOME], archived=True, days=3)
    running = engine(world)
    running.run_once(options())
    assert where(world, ONE) == {HOME, OTHER}
    assert where(world, TWO) == SEEDED
    assert base(world)[ONE]["archived_at"] == pytest.approx(NOW_S - 30 * DAY, abs=1)
    rewrite(record(world, OTHER), lastFocusedAt=int((NOW_S - 30 * DAY) * 1000))   # a re-save
    running.run_hot(options())
    running.run_once(options())
    assert where(world, ONE) == {HOME, OTHER}
    assert base(world)[ONE]["archived_at"] == pytest.approx(NOW_S - 30 * DAY, abs=1)


@pytest.mark.parametrize("change", [{"starred": True}, {"archived": False}])
def test_a_starred_or_unarchived_old_session_belongs_everywhere(world, change):
    """C-23.28a: a star, or any unarchived copy, keeps a session live."""
    put(world, ONE, [HOME], **{"archived": True, "days": 40, **change})
    engine(world).run_once(options())
    assert where(world, ONE) == SEEDED


# --- removal ------------------------------------------------------------------

def test_removal_is_off_by_default_and_the_pass_says_what_it_would_remove(world):
    """C-23.28a: the shipped policy narrows where the mirror spreads but
    removes nothing until `sessions.mirror_prune` is on; each pass says what
    removal would do (sidecar, `--dry-run`, `--json`)."""
    (world.home / mirror.CONFIG_NAME).write_text(json.dumps({"dead_home": "org-home"}))
    put(world, ONE, FOLDERS, archived=True, days=30)
    chosen = mirror.options_from(fx.policy())
    assert (chosen.prune, chosen.skip_switch_folders, chosen.archive_days,
            chosen.dead_days) == (False, True, 14.0, 7.0)
    result = engine(world).run_once(chosen)
    assert where(world, ONE) == set(FOLDERS) and result.pruned == 0
    assert result.prune["home"] == key(HOME)
    assert result.prune["outside"] == {prune_rules.ARCHIVED: 3}
    assert result.prune["if_on"]["removable"] == 2, "the loaded folder's copy stays"
    assert result.prune["if_on"]["held"] == {prune_rules.HELD_LOADED: 1}


def test_with_removal_off_the_account_is_planned_on_the_sweep_and_carried_between(
        world, monkeypatch):
    """C-23.28a: with removal off the plan only reports, so a pass between
    ten-minute sweeps carries the last account forward instead of planning
    every copy every minute (442,801 copies on 2026-10-13)."""
    put(world, ONE, FOLDERS, archived=True, days=30)
    running = engine(world)
    first = running.run_once(options())
    put(world, TWO, [HOME], archived=True, days=30)
    planned, calls = prune_rules.plan, []

    def counted(*args, **kwargs):
        calls.append(1)
        return planned(*args, **kwargs)

    monkeypatch.setattr(prune_rules, "plan", counted)
    between = running.run_once(options())
    assert not between.swept and not calls, "no planning between sweeps"
    assert between.prune == first.prune
    running._last_sweep = None                       # noqa: SLF001 - the sweep is due
    swept = running.run_once(options())
    assert swept.swept and calls
    assert swept.prune["sessions"]["archived"] == 2


def test_old_archived_copies_go_except_the_homes_and_the_loaded_folders(world):
    """C-23.28a: the copies outside the home go; the loaded folder's waits
    until the app has left it, since the app saves what it holds."""
    put(world, ONE, FOLDERS, archived=True, days=30)
    running = engine(world)
    result = running.run_once(options(prune=True))
    assert where(world, ONE) == {HOME, WORK}
    assert result.pruned == 2 and result.prune["removed"] == {prune_rules.ARCHIVED: 2}
    assert result.prune["held"] == {prune_rules.HELD_LOADED: 1}
    again = running.run_once(options(prune=True))
    assert (again.pruned, again.added) == (0, 0), "nothing comes back, nothing more goes"
    say(world.log, *loads(world.store, HOME, "2026-09-05 11:29:00"))
    running.run_once(options(prune=True))
    assert where(world, ONE) == {HOME}, "the app left work, so work's copy goes too"
    assert not graves(world)


def test_the_newest_copy_stays_beside_an_older_home_copy(world):
    """C-23.28a: copies differ in activity; the newest content survives
    without the mirror rewriting the home's."""
    put(world, ONE, [HOME], archived=True, days=40)
    put(world, ONE, [OTHER], archived=True, days=30)
    put(world, ONE, [SWITCH], archived=True, days=35)
    result = engine(world).run_once(options(prune=True))
    assert where(world, ONE) == {HOME, OTHER}
    assert result.prune["kept_newest"] == {prune_rules.ARCHIVED: 1}


def test_nothing_goes_while_the_loaded_folder_is_unknown(tmp_path, monkeypatch):
    """C-23.28a: without the app's log the mirror cannot know which folder
    the app holds in memory, so it removes nothing."""
    world = make_world(tmp_path, monkeypatch, log_lines=[])
    put(world, ONE, FOLDERS, archived=True, days=30)
    result = engine(world).run_once(options(prune=True))
    assert where(world, ONE) == set(FOLDERS)
    assert result.prune["loaded"] is None
    assert result.prune["held"] == {prune_rules.HELD_UNKNOWN_LOAD: 3}


def test_a_switch_after_the_plan_keeps_the_newly_loaded_folders_copy(world, monkeypatch):
    """C-23.28a: the loaded folder is read again from the log right before
    each session's removals; a switch since the plan protects its folder."""
    put(world, ONE, FOLDERS, archived=True, days=30)
    planned = prune_rules.plan

    def then_switch(*args, **kwargs):
        decided = planned(*args, **kwargs)
        say(world.log, *loads(world.store, OTHER, "2026-09-05 11:29:00"))
        return decided

    monkeypatch.setattr(prune_rules, "plan", then_switch)
    result = engine(world).run_once(options(prune=True))
    assert where(world, ONE) == {HOME, WORK, OTHER}
    assert result.prune["held"] == {prune_rules.HELD_LOADED: 2}


def test_an_app_save_landing_during_a_removal_is_put_back(world, monkeypatch):
    """C-23.28a: a removal takes only the file it checked. The app renames a
    save over the copy after the check; the removal finds a new file in its
    hands, and the app's save is back under its name."""
    put(world, ONE, FOLDERS, archived=True, days=30)
    real = mirror._remove

    def racing(path, expect):
        if path.parent.name == OTHER[1]:
            rewrite(path, title="the app's save")
        return real(path, expect)

    monkeypatch.setattr(mirror, "_remove", racing)
    result = engine(world).run_once(options(prune=True))
    assert json.loads(record(world, OTHER).read_text())["title"] == "the app's save"
    assert not record(world, SWITCH).exists()
    assert result.prune["held"]["the copy changed since the pass read it"] == 1
    assert not graves(world)


def after_the_plan(monkeypatch, act) -> None:
    """Run `act` after the pass plans each session and before it removes any
    of that session's copies: what the app may do while the pass works."""
    planned = prune_rules.plan

    def then_act(*args, **kwargs):
        decided = planned(*args, **kwargs)
        act()
        return decided

    monkeypatch.setattr(prune_rules, "plan", then_act)


def test_a_keeper_that_changed_since_the_plan_stops_its_sessions_removals(world, monkeypatch):
    """C-23.28a: the kept copy is read again right before the removals that
    count on it. Unarchived since the plan (the user, in the home folder), the
    session is live again, and no copy of it goes."""
    put(world, ONE, FOLDERS, archived=True, days=30)
    after_the_plan(monkeypatch, lambda: rewrite(record(world, HOME), isArchived=False))
    result = engine(world).run_once(options(prune=True))
    assert where(world, ONE) == set(FOLDERS)
    assert result.prune["held"]["the keeper changed since the pass read it"] == 2


def test_a_copy_that_changed_since_the_plan_stays(world, monkeypatch):
    """C-23.28a: each copy is read again right before it goes. One the app
    unarchived since the plan stays; so does one now newer than the keeper."""
    put(world, ONE, FOLDERS, archived=True, days=30)

    def app_writes():
        rewrite(record(world, OTHER), isArchived=False)
        rewrite(record(world, SWITCH), lastActivityAt=int(NOW_S * 1000))

    after_the_plan(monkeypatch, app_writes)
    result = engine(world).run_once(options(prune=True))
    assert where(world, ONE) == set(FOLDERS)
    assert result.prune["held"]["the copy changed since the pass read it"] == 2


def test_a_grave_whose_name_cannot_be_linked_is_kept(tmp_path, monkeypatch):
    """C-23.28a: a grave is dropped only once its name holds a record, put
    back or newer; if the link fails any other way, it stays for the next
    listing rather than taking the record with it."""
    path = tmp_path / "local_x.json"
    path.write_text('{"v": 1}')
    handle, grave = mirror._temporary(path, mirror.GRAVE_SUFFIX)
    os.close(handle)
    os.rename(path, grave)

    def refused(*_args, **_kwargs):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(mirror.os, "link", refused)
    mirror._bury(grave)
    assert grave.exists() and not path.exists()
    monkeypatch.undo()
    mirror._bury(grave)
    assert json.loads(path.read_text()) == {"v": 1} and not grave.exists()


def test_a_session_whose_flags_move_this_pass_waits_a_pass(world):
    """C-23.28a: flag sync rewrote it, so its copies are no longer as the
    pass read them: none goes until a pass finds it settled."""
    put(world, ONE, FOLDERS, archived=True, days=30)
    running = engine(world)
    running.run_once(options())
    rewrite(record(world, OTHER), title="renamed", titleSource="manual")
    result = running.run_once(options(prune=True))
    assert result.retitled == 1 and result.pruned == 0
    assert result.prune["held"] == {prune_rules.HELD_LOADED: 1, prune_rules.HELD_FLAGS: 2}
    assert running.run_once(options(prune=True)).pruned == 2
    assert json.loads(record(world, HOME).read_text())["title"] == "renamed"


def test_an_incomplete_inventory_removes_nothing(world, monkeypatch):
    """C-23.28a: a copy that cannot be read (EMFILE on 2026-09-25) could be
    the newest, or the session's only other copy: nothing goes that pass."""
    put(world, ONE, FOLDERS, archived=True, days=30)
    read = mirror._read_entry

    def failing(path):
        if path.parent.name == OTHER[1]:
            raise OSError(errno.EMFILE, "Too many open files", str(path))
        return read(path)

    monkeypatch.setattr(mirror, "_read_entry", failing)
    result = engine(world).run_once(options(prune=True))
    assert where(world, ONE) == set(FOLDERS) and result.pruned == 0
    assert set(result.prune["held"]) == {"the inventory is incomplete this pass"}


def test_an_old_dead_session_keeps_its_newest_copy(world):
    """C-23.28a: no transcript, so never spread; quiet `mirror_dead_days`, it
    keeps the home's copy or else its newest. A young one is left alone
    outside switch folders: it may be a session whose transcript is coming."""
    put(world, ONE, [OTHER, SWITCH], openable=False, days=30)
    put(world, TWO, [OTHER, WORK], openable=False, days=2)
    result = engine(world).run_once(options(prune=True))
    assert where(world, ONE) == {OTHER}
    assert where(world, TWO) == {OTHER, WORK}
    assert result.prune["sessions"] == {"dead": 1, "dead-young": 1}


# --- review round 1: what must also stop a removal ------------------------------

def test_nothing_goes_while_the_transcripts_cannot_all_be_listed(world):
    """C-23.28a, review r1 P1-1: a session whose transcript was not listed
    reads as dead, and the dead rule would remove its copies. An unlisted
    transcript directory blocks every removal that pass."""
    put(world, ONE, FOLDERS, days=10)                    # live, openable, quiet 10 days
    projects = world.home / "projects"
    project = next(projects.iterdir())
    project.chmod(0)
    try:
        result = engine(world).run_once(options(prune=True))
    finally:
        project.chmod(0o755)
    assert where(world, ONE) == set(FOLDERS)
    assert set(result.prune["held"]) == {"the transcripts could not all be listed this pass"}


def test_nothing_goes_while_the_log_cannot_be_read(world):
    """C-23.28a, review r1 P1-3: a log that cannot be read leaves the last
    load it read in place; that is not knowing which folder the app holds."""
    put(world, ONE, FOLDERS, archived=True, days=30)
    running = engine(world)
    running.run_once(options())                          # it read: work is loaded
    say(world.log, *loads(world.store, OTHER, "2026-09-05 11:29:00"))   # the app switches
    world.log.rename(world.log.with_name("gone.log"))   # and its log can no longer be read
    result = running.run_once(options(prune=True))
    assert where(world, ONE) == set(FOLDERS)
    assert result.prune["loaded"] is None


def test_a_log_lost_after_the_plan_stops_the_removals(world, monkeypatch):
    """C-23.28a: the loaded folder is read again before each session's
    removals; a log that cannot be read then is no answer either."""
    put(world, ONE, FOLDERS, archived=True, days=30)
    after_the_plan(monkeypatch, lambda: world.log.rename(world.log.with_name("gone.log")))
    result = engine(world).run_once(options(prune=True))
    assert where(world, ONE) == set(FOLDERS)
    assert result.prune["held"] == {prune_rules.HELD_LOADED: 1,
                                    prune_rules.HELD_UNKNOWN_LOAD: 2}


def test_a_dead_session_whose_transcript_returns_keeps_its_copies(world, monkeypatch):
    """C-23.28a: a dead session's removals check right before that it still
    has no transcript (one restored or synced back makes it live again)."""
    put(world, ONE, [OTHER, SWITCH], openable=False, days=30)
    after_the_plan(monkeypatch, lambda: fx.transcript(world.home, ONE, fx.completed()))
    result = engine(world).run_once(options(prune=True))
    assert where(world, ONE) == {OTHER, SWITCH}
    assert result.prune["held"] == {"the session has a transcript now": 1}


def test_a_live_sessions_switch_folder_copy_waits_while_the_registry_learns(
        tmp_path, monkeypatch):
    """C-23.28a, review r1 P1-4: a folder used less often than the registry
    has learned reads as a switch folder; a live session's copy there stays
    until the registry has learned `SWITCH_PRUNE_AFTER_S`."""
    world = make_world(tmp_path, monkeypatch,
                       log_lines=history(tmp_path / "claude-code-sessions",
                                         since="2026-09-05 08:00:00"))
    put(world, ONE, FOLDERS)
    result = engine(world).run_once(options(prune=True))
    assert SWITCH in where(world, ONE)
    assert result.prune["switch_removal"] is False
    assert result.prune["held"] == {prune_rules.HELD_LEARNING: 1}


def test_the_home_an_org_id_named_stays_when_a_second_folder_of_it_settles(world):
    """C-23.28a, review r1 P2-1: once another account settles in the home's
    org, the org id names two folders. The home stays where it was, rather
    than unhoming every archived session and spreading them all back."""
    put(world, ONE, [HOME], archived=True, days=30)
    running = engine(world)
    first = running.run_once(options(dead_home="org-home"))
    assert first.prune["home"] == key(HOME)
    second = ("acct-second", "org-home")
    (world.store / second[0] / second[1]).mkdir(parents=True)
    say(world.log, *loads(world.store, second, "2026-09-05 11:00:00"),
        *loads(world.store, WORK, "2026-09-05 11:10:00"))
    running._last_sweep = None                          # noqa: SLF001 - plan afresh
    again = running.run_once(options(dead_home="org-home"))
    assert again.prune["home"] == key(HOME) and "kept" in again.prune["home_detail"]
    assert where(world, ONE) == {HOME}, "nothing spread back"


def test_a_star_since_the_plan_stops_the_removal(world, monkeypatch):
    """C-23.28a, review r1 P2-2: a star (here every copy's, as flag sync
    spreads one) makes the session live; the re-reads see it."""
    put(world, ONE, FOLDERS, archived=True, days=30)
    after_the_plan(monkeypatch, lambda: [rewrite(record(world, folder), isStarred=True)
                                         for folder in FOLDERS])
    engine(world).run_once(options(prune=True))
    assert where(world, ONE) == set(FOLDERS)


def test_a_copy_that_now_holds_another_session_stays(world, monkeypatch):
    """C-23.28a: the copy re-read must still hold the session the plan saw
    there (the app moves a record to a new session id on /clear)."""
    put(world, ONE, FOLDERS, archived=True, days=30)
    after_the_plan(monkeypatch, lambda: rewrite(record(world, OTHER), cliSessionId=TWO))
    engine(world).run_once(options(prune=True))
    assert record(world, OTHER).exists()
    assert not record(world, SWITCH).exists()


def test_another_mirror_removing_from_the_store_blocks_this_ones_removals(world):
    """C-23.28a, review r1 P2-3: removals take a lock keyed by the store, so a
    mirror with another state root cannot remove the copy this one keeps."""
    put(world, ONE, FOLDERS, archived=True, days=30)
    other = mirror.Mirror(world.root.parent / "other-state", fx.policy(), now=lambda: fx.NOW)
    held = other._store_lock()                          # noqa: SLF001 - the other mirror
    try:
        result = engine(world).run_once(options(prune=True))
    finally:
        held.close()
    assert where(world, ONE) == set(FOLDERS)
    assert "another mirror is removing from this store" in result.prune["held"]


def test_a_stale_empty_copy_outside_the_scope_is_still_repaired(world):
    """C-23.28a, review r1 P2-4: a frozen copy with no session id already
    stands in its folder's sidebar as a session with no messages, so the
    spread repairs it even where it would add no new copy."""
    put(world, ONE, [HOME], archived=True, days=30)
    stale = fx.index_entry(world.store, *OTHER, "", name=f"local_{ONE}.json",
                           settings={"ultracode": True})
    engine(world).run_once(options())
    repaired = json.loads(stale.read_text())
    assert repaired["cliSessionId"] == ONE and repaired["isArchived"] is True
    assert where(world, ONE) == {HOME, OTHER}, "repaired, and added nowhere else"


def test_an_empty_grave_is_dropped_not_put_back(tmp_path):
    """C-23.28a, review r1 P3-4: a removal that crashed after making its grave
    name and before its rename leaves an empty placeholder, never a record."""
    path = tmp_path / "local_x.json"
    handle, grave = mirror._temporary(path, mirror.GRAVE_SUFFIX)
    os.close(handle)
    mirror._bury(grave)
    assert not grave.exists() and not path.exists()


def test_only_sessions_that_can_reach_outside_their_scope_are_planned(world, monkeypatch):
    """C-23.28a: a live session with no copy in a switch folder has nothing
    to plan; the prune step plans none of them."""
    put(world, ONE, SEEDED)
    put(world, TWO, FOLDERS, archived=True, days=30)
    planned, calls = prune_rules.plan, []

    def counted(copies, *args, **kwargs):
        calls.append({copy.identity for copy in copies})
        return planned(copies, *args, **kwargs)

    monkeypatch.setattr(prune_rules, "plan", counted)
    engine(world).run_once(options(prune=True))
    assert calls and all(identities == {TWO} for identities in calls)


# --- review round 2: the home, the transcripts, the lock -------------------------

def unlisted(monkeypatch, folder: tuple[str, str]) -> None:
    """`folder` cannot be listed from now on (EMFILE, as on 2026-09-25)."""
    scan = mirror.Mirror._scan

    def failing(self, path, *args, **kwargs):
        if (path.parent.name, path.name) == folder:
            raise mirror._Unlisted(str(path), transient=True)
        return scan(self, path, *args, **kwargs)

    monkeypatch.setattr(mirror.Mirror, "_scan", failing)


def test_a_home_unlisted_for_one_pass_spreads_nothing_back(world, monkeypatch):
    """C-23.28a, review r2 N1: the home is resolved over every folder the store
    holds, so one failed listing of it does not unhome the archive, which
    would spread every archived session into every folder that pass."""
    put(world, ONE, [HOME, OTHER], archived=True, days=30)
    running = engine(world)
    running.run_once(options())
    assert where(world, ONE) == {HOME, OTHER}
    unlisted(monkeypatch, HOME)
    result = running.run_once(options(prune=True))
    assert where(world, ONE) == {HOME, OTHER}, "nothing spread back, nothing removed"
    assert result.prune["home"] == key(HOME) and result.added == 0


def test_a_first_pass_that_cannot_list_the_home_still_knows_it(world, monkeypatch):
    """C-23.28a, review r2 N1: with no home recorded yet, the home is found
    among every folder the store holds, listed this pass or not, so a first
    pass that cannot list it does not widen the archive to every folder."""
    put(world, ONE, [HOME, OTHER], archived=True, days=30)
    unlisted(monkeypatch, HOME)
    result = engine(world).run_once(options())
    assert result.prune["home"] == key(HOME)
    assert where(world, ONE) == {HOME, OTHER}, "nothing spread"


def test_a_home_unlisted_for_one_pass_does_not_move(world, monkeypatch):
    """C-23.28a, review r2 N1: with an org id and a second settled folder of
    the org, a pass that cannot list the home neither moves it there nor
    records the move."""
    put(world, ONE, [HOME], archived=True, days=30)
    running = engine(world)
    assert running.run_once(options(dead_home="org-home")).prune["home"] == key(HOME)
    second = ("acct-second", "org-home")
    (world.store / second[0] / second[1]).mkdir(parents=True)
    say(world.log, *loads(world.store, second, "2026-09-05 11:00:00"),
        *loads(world.store, WORK, "2026-09-05 11:10:00"))
    unlisted(monkeypatch, HOME)
    running._last_sweep = None                          # noqa: SLF001 - plan afresh
    result = running.run_once(options(dead_home="org-home"))
    assert result.prune["home"] == key(HOME)
    assert running._registry.homes == {"org-home": key(HOME)}   # noqa: SLF001
    assert where(world, ONE) == {HOME}, "the archive was not copied to a new home"


def test_a_new_home_is_not_recorded_from_an_incomplete_inventory(world, monkeypatch):
    """C-23.28a, review r2 N1: a first pass that cannot list the home's
    account sees only another folder of the org. It may use that folder for
    the pass, but records nothing, so the next full inventory decides."""
    settled, other = ("acct-a", "org-z"), ("acct-b", "org-z")
    for folder in (settled, other):
        (world.store / folder[0] / folder[1]).mkdir(parents=True)
    # A minute in one: settled, and nobody owns the org, so the other folder,
    # never loaded, is unknown rather than a switch folder.
    say(world.log, *loads(world.store, settled, "2026-09-05 11:00:00"),
        *loads(world.store, WORK, "2026-09-05 11:01:00"))
    account = os.fspath(world.store / settled[0])
    scandir = os.scandir

    def failing(path="."):
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) == account:
            raise OSError(errno.EMFILE, "Too many open files", account)
        return scandir(path)

    monkeypatch.setattr(mirror.os, "scandir", failing)
    running = engine(world)
    assert running.run_once(options(dead_home="org-z")).prune["home"] == key(other)
    assert running._registry.homes == {}              # noqa: SLF001
    monkeypatch.setattr(mirror.os, "scandir", scandir)
    running._last_sweep = None                          # noqa: SLF001 - plan afresh
    assert running.run_once(options(dead_home="org-z")).prune["home"] == key(settled)
    assert running._registry.homes == {"org-z": key(settled)}  # noqa: SLF001


def test_a_home_gone_from_the_store_is_kept_and_its_archive_stays_put(world):
    """C-23.28a, review r2 N1: a home the store no longer holds (and that
    nothing else replaces) is kept rather than every archived session's
    scope widened to every folder; nothing is removed for it meanwhile."""
    put(world, ONE, [HOME, OTHER, SWITCH], archived=True, days=30)
    running = engine(world)
    running.run_once(options())
    shutil.rmtree(world.store / HOME[0] / HOME[1])
    result = running.run_once(options(prune=True))
    assert where(world, ONE) == {OTHER, SWITCH}
    assert result.prune["home"] == key(HOME)
    assert "kept home folder" in result.prune["home_detail"]
    assert result.prune["held"] == {prune_rules.HELD_HOME_UNLISTED: 1}
    # Review r3 R6: not only a held count; the account and `--status` warn.
    [warning] = result.prune["warnings"]
    assert warning.startswith(f"the home folder {key(HOME)} is not in the store")
    assert warning in running.health()["detail"]


def test_a_home_named_exactly_is_honoured_when_it_reads_as_a_switch_folder(world):
    """C-23.28a, review r2 N4: the user chose it. It is the home, it is
    seeded, and the conflict is reported."""
    put(world, ONE, [SWITCH, OTHER], archived=True, days=30)
    put(world, TWO, sorted(SEEDED), days=1)
    result = engine(world).run_once(options(dead_home=key(SWITCH), prune=True))
    assert result.prune["home"] == key(SWITCH)
    assert "switch folder" in result.prune["home_detail"]
    assert where(world, ONE) == {SWITCH}
    assert where(world, TWO) == set(FOLDERS), "the home is seeded"


def test_an_org_home_that_comes_to_read_as_a_switch_folder_stays_put(world):
    """C-23.28a, review r2 N4: the org's only folder, never settled in, is the
    home; once another account owns the org it reads as a switch folder.
    The home stays, rather than the whole archive moving to the owner's."""
    lone, owner = ("acct-lone", "org-lone"), ("acct-owner", "org-lone")
    (world.store / lone[0] / lone[1]).mkdir(parents=True)
    put(world, ONE, [lone, OTHER], archived=True, days=30)
    running = engine(world)
    assert running.run_once(options(dead_home="org-lone")).prune["home"] == key(lone)
    (world.store / owner[0] / owner[1]).mkdir(parents=True)
    say(world.log, *loads(world.store, owner, "2026-09-05 11:00:00"),
        *loads(world.store, WORK, "2026-09-05 11:15:00"))
    result = running.run_once(options(dead_home="org-lone", prune=True))
    assert result.prune["home"] == key(lone)
    assert "switch folder" in result.prune["home_detail"]
    assert where(world, ONE) == {lone}


def test_a_missing_projects_directory_removes_nothing(world, monkeypatch):
    """C-23.28a, review r2 N2: no `projects/` where the mirror looks (another
    home, another `SUBFLEET_CLAUDE_DIR`, or moved) makes every session read
    as dead. That is no answer, so nothing goes."""
    put(world, ONE, FOLDERS, days=10)
    monkeypatch.setattr(mirror, "projects_dir", lambda: world.home / "elsewhere")
    result = engine(world).run_once(options(prune=True))
    assert where(world, ONE) == set(FOLDERS)
    assert set(result.prune["held"]) == {"the transcripts could not all be listed this pass"}


def test_a_projects_directory_recreated_empty_proves_no_session_dead(world):
    """C-23.28a, review r2 N2: a `projects/` that is there but holds none of the
    session's project directories was not where its transcript was looked
    for; the dead rule waits for a listed project directory without it."""
    put(world, ONE, FOLDERS, days=10)
    projects = world.home / "projects"
    shutil.rmtree(projects)
    projects.mkdir()
    result = engine(world).run_once(options(prune=True))
    assert where(world, ONE) == set(FOLDERS)
    assert result.prune["sessions"] == {"dead": 1}
    assert result.prune["held"] == {prune_rules.HELD_LOADED: 1,
                                    prune_rules.HELD_DEAD_UNCONFIRMED: 2}


def test_a_symlinked_project_directory_is_listed_through_its_link(world):
    """C-23.28a, review r2 N5: the app opens `projects/<slug>/<id>.jsonl`
    through a symlinked project directory, so its sessions are live, not
    dead and pruned."""
    put(world, ONE, FOLDERS, days=10)
    linked = world.home / "projects" / fx.project_slug()
    real = world.home / "elsewhere"
    linked.rename(real)
    linked.symlink_to(real, target_is_directory=True)
    result = engine(world).run_once(options(prune=True))
    assert result.prune["sessions"] == {"live": 1}
    assert where(world, ONE) == SEEDED, "only the switch folder's copy went"


@pytest.mark.parametrize("carrier", [SWITCH, OTHER], ids=["the copy", "the keeper"])
def test_a_dead_sessions_copy_is_checked_for_a_transcript_under_its_own_cwd(
        world, monkeypatch, carrier):
    """C-23.28a, review r2 N6: a copy may carry a cwd the others do not (a
    worktree moved). A transcript appearing under the cwd of the copy to be
    removed, or of the copy kept, after the plan makes the session live, and
    the copy stays."""
    put(world, ONE, [OTHER, SWITCH], openable=False, days=30)
    moved = "/Users/someone/moved-worktree"
    target = record(world, carrier)
    rewrite(target, cwd=moved)
    os.utime(target, (NOW_S - 30 * DAY,) * 2)
    after_the_plan(monkeypatch,
                   lambda: fx.transcript(world.home, ONE, fx.completed(), cwd=moved))
    result = engine(world).run_once(options(prune=True))
    assert where(world, ONE) == {OTHER, SWITCH}
    assert result.prune["held"] == {"the session has a transcript now": 1}


def test_the_spreads_ranking_reads_times_as_the_planner_does():
    """C-23.28a, review r2 N7: a time that does not parse is skipped by the
    spread's ranking as by the planner's, not ranked as raw text."""
    data = {"lastActivityAt": "not a time", "lastFocusedAt": 1_790_000_000_000}
    assert mirror._rank(data) == mirror._rank_ms(data) == 1_790_000_000_000


def test_the_store_lock_does_not_follow_tmpdir(world, tmp_path, monkeypatch):
    """C-23.28a, review r2 N3: launchd's agent has no `TMPDIR` and a Terminal
    shell has one; two mirrors so started still take one lock."""
    first = mirror.store_lock_dir()
    assert first is not None
    other = mirror.Mirror(world.root.parent / "other-state", fx.policy(), now=lambda: fx.NOW)
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_path / "a"))
    monkeypatch.setattr(tempfile, "tempdir", None)
    assert tempfile.gettempdir() == str(tmp_path / "a")   # what the lock once followed
    held = other._store_lock()                              # noqa: SLF001
    assert held is not None
    try:
        monkeypatch.setenv("TMPDIR", str(tmp_path / "b"))
        monkeypatch.setattr(tempfile, "tempdir", None)
        assert mirror.store_lock_dir() == first
        running = engine(world)
        assert running._store_lock() is None              # noqa: SLF001
        assert running._store_lock_why == "another mirror is removing from this store"  # noqa: SLF001
    finally:
        held.close()


def test_off_macos_the_lock_lives_in_a_private_directory_of_the_users(
        world, tmp_path, monkeypatch):
    """C-23.28a, review r2 N3: `/tmp/subfleet-<uid>`, made private; one opened
    to others, or a link in its place, is not used, and nothing is removed,
    for a reason `--status` names."""
    monkeypatch.setattr(mirror.sys, "platform", "linux")
    base = tmp_path / "tmp"
    base.mkdir()
    monkeypatch.setattr(mirror, "_LOCK_FALLBACK_BASE", base)
    private = mirror.store_lock_dir()
    assert private == base / f"subfleet-{os.getuid()}"
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    private.chmod(0o755)
    assert mirror.store_lock_dir() is None
    private.rmdir()
    elsewhere = base / "planted"
    elsewhere.mkdir(mode=0o700)
    private.symlink_to(elsewhere, target_is_directory=True)
    assert mirror.store_lock_dir() is None
    put(world, ONE, FOLDERS, archived=True, days=30)
    result = engine(world).run_once(options(prune=True))
    assert where(world, ONE) == set(FOLDERS)
    assert set(result.prune["held"]) == {
        "the store's removal lock has no private directory to live in"}


def test_a_lock_that_cannot_be_taken_is_not_called_busy(world, tmp_path, monkeypatch):
    """C-23.28a, review r2 N3: a lock file that cannot be opened (here a
    directory stands at its name) holds every removal, and the account says
    so rather than blaming another mirror."""
    monkeypatch.setattr(mirror, "store_lock_dir", lambda: tmp_path)
    running = engine(world)
    first = running._store_lock()                       # noqa: SLF001
    assert first is not None
    first.close()
    [lock] = tmp_path.glob("subfleet-mirror-store-*.lock")
    lock.unlink()
    lock.mkdir()
    assert running._store_lock() is None                # noqa: SLF001
    assert running._store_lock_why.startswith(f"the store's removal lock {lock} cannot be taken")  # noqa: SLF001


def test_a_blocked_pass_does_not_take_the_store_lock(world, monkeypatch):
    """C-23.28a, review r2 N9: a pass that will remove nothing leaves the lock
    to another mirror that would."""
    put(world, ONE, FOLDERS, archived=True, days=30)
    taken = []
    monkeypatch.setattr(mirror.Mirror, "_store_lock", lambda self: taken.append(self))
    result = engine(world).run_once(options(prune=True, flag_sync=False))
    assert taken == [] and result.pruned == 0
    assert set(result.prune["held"]) == {"flag sync is off, so no session's flags are settled"}


# --- review round 3 -------------------------------------------------------------

def test_a_looping_symlink_in_projects_fails_only_itself(world):
    """C-23.28a, review r3 R1: following a project directory's symlink can
    fail; a loop is no directory, for the app as for the mirror, and the rest
    of `projects/` is listed, spread and pruned as ever."""
    put(world, ONE, FOLDERS, days=1)
    loop = world.home / "projects" / "loop"
    loop.symlink_to(loop)
    result = engine(world).run_once(options(prune=True))
    assert result.sessions == 1
    assert where(world, ONE) == SEEDED, "only the switch folder's copy went"


def test_a_symlink_to_a_project_directory_that_cannot_be_read_holds_only_removal(
        world, tmp_path):
    """C-23.28a, review r3 R1: a symlinked project directory whose target
    cannot be read now leaves the transcripts unknown, as an unlistable
    directory does: nothing is removed, but every other session is still
    listed and spread."""
    put(world, ONE, sorted(SEEDED), days=1)
    put(world, TWO, FOLDERS, archived=True, days=30)
    hidden = tmp_path / "hidden"
    (hidden / "sub").mkdir(parents=True)
    (world.home / "projects" / "linked").symlink_to(hidden / "sub", target_is_directory=True)
    hidden.chmod(0)
    try:
        result = engine(world).run_once(options(prune=True))
    finally:
        hidden.chmod(0o755)
    assert result.sessions == 2
    assert where(world, TWO) == set(FOLDERS)
    assert set(result.prune["held"]) == {"the transcripts could not all be listed this pass"}


def test_a_restart_that_cannot_list_the_homes_account_keeps_the_recorded_home(
        world, monkeypatch):
    """C-23.28a, review r3 R2: only a complete inventory may change the home.
    A new process whose first listing of the home's account fails keeps the
    home it recorded, rather than copying the archive into another folder
    of the org for a pass."""
    put(world, ONE, [HOME, OTHER], archived=True, days=30)
    assert engine(world).run_once(options(dead_home="org-home")).prune["home"] == key(HOME)
    second = ("acct-second", "org-home")
    (world.store / second[0] / second[1]).mkdir(parents=True)
    say(world.log, *loads(world.store, second, "2026-09-05 11:00:00"),
        *loads(world.store, WORK, "2026-09-05 11:10:00"))
    account, scandir = os.fspath(world.store / HOME[0]), os.scandir

    def failing(path="."):
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) == account:
            raise OSError(errno.EMFILE, "Too many open files", account)
        return scandir(path)

    monkeypatch.setattr(mirror.os, "scandir", failing)
    result = engine(world).run_once(options(dead_home="org-home"))   # a new process
    assert result.prune["home"] == key(HOME)
    assert "incomplete" in result.prune["home_detail"]
    assert key(second) not in where(world, ONE)
    # Review r4 P3-a: said as a warning too, while it lasts.
    assert result.prune["warnings"] == [
        f"the home folder {key(HOME)} could not be listed this pass, so old archived "
        "and dead sessions are neither spread nor removed"]


def test_a_home_that_reads_as_a_switch_folder_plans_no_live_session(world, monkeypatch):
    """C-23.28a, review r3 R4: the home is no switch folder to the planning
    set either, so a live session seeded there is not planned on every sweep."""
    put(world, ONE, FOLDERS, days=1)
    planned, calls = prune_rules.plan, []

    def counted(copies, *args, **kwargs):
        calls.append({copy.identity for copy in copies})
        return planned(copies, *args, **kwargs)

    monkeypatch.setattr(prune_rules, "plan", counted)
    result = engine(world).run_once(options(dead_home=key(SWITCH), prune=True))
    assert calls == [] and where(world, ONE) == set(FOLDERS)
    assert result.prune["sessions"] == {"live": 1}


def test_a_registry_that_cannot_read_the_logs_says_so(world):
    """C-23.28a, review r3 R5: while a rotated log cannot be read the registry
    learns nothing, every folder is unknown (so seeded), and the account and
    `--status` say why."""
    rotated = world.log.with_name("main1.log")
    rotated.write_text("2026-07-30 08:00:00 [info] an older day\n", encoding="utf-8")
    rotated.chmod(0)
    running = engine(world)
    try:
        result = running.run_once(options())
    finally:
        rotated.chmod(0o644)
    assert result.prune["folders"] == {"settled": 0, "switch": 0, "unknown": 4}
    [warning] = result.prune["warnings"]
    assert "folder registry is not learning" in warning and "main1.log" in warning
    assert running.health()["warnings"] == [warning]
    assert warning in running.health()["detail"]


def test_a_home_that_names_no_folder_is_a_warning(world):
    """C-23.28a, review r4 P3-a: a `dead_home` set but naming no folder turns
    the archive rule off; the account and `--status` say so."""
    running = engine(world)
    result = running.run_once(options(dead_home="acct-none/org-none"))
    assert result.prune["home"] is None
    [warning] = result.prune["warnings"]
    assert warning.startswith("the home folder acct-none/org-none is not in the store: "
                              "old archived sessions stay in every folder")
    assert running.health()["warnings"] == [warning]


def test_status_keeps_the_warnings_while_a_pass_is_in_flight(world):
    """C-23.28a, review r4 P3-b: the warnings are kept beside the pass, so
    `--status` says them while the next pass runs or after one fails before
    its account; a pass that finishes with the rules off clears them."""
    running = engine(world)
    running.run_once(options(dead_home="acct-none/org-none"))
    [warning] = running.health()["warnings"]
    running._record(mirror.Pass(started_at="2026-09-05T11:30:00Z"))   # noqa: SLF001 - in flight
    assert running.health()["warnings"] == [warning]
    running._record(mirror.Pass(started_at="2026-09-05T11:30:00Z", state="error",  # noqa: SLF001
                                error="cancelled before its account"))
    assert running.health()["warnings"] == [warning]
    running.run_once(options(skip_switch_folders=False, archive_days=0.0, dead_days=0.0))
    assert "warnings" not in running.health()


def test_the_home_in_use_stays_the_newest_record(world):
    """C-23.28a, review r4 P3-c: the value in use moves to the newest record
    when a one-off value was recorded after it, so one-off values never push
    the daemon's home out of the registry."""
    daemon = engine(world)
    daemon.run_once(options(dead_home="org-home"))
    engine(world).run_once(options(dead_home=key(OTHER)))     # a one-off pass
    daemon._registry = None                                   # noqa: SLF001 - as after a restart
    daemon._last_sweep = None                                 # noqa: SLF001
    daemon.run_once(options(dead_home="org-home"))
    registry = folder_kinds.Registry(folder_kinds.registry_path(world.root))
    assert list(registry.homes) == [key(OTHER), "org-home"]


def test_each_home_value_keeps_its_own_record(world):
    """C-23.28a, review r3 R7: a one-off `--dead-home` pass in the daemon's
    state root records its own home beside the daemon's, so a restarted
    daemon still finds the home it had."""
    engine(world).run_once(options(dead_home="org-home"))
    engine(world).run_once(options(dead_home=key(OTHER)))     # a one-off pass
    registry = folder_kinds.Registry(folder_kinds.registry_path(world.root))
    assert registry.homes == {"org-home": key(HOME), key(OTHER): key(OTHER)}


# --- undoing it ---------------------------------------------------------------

def test_unarchiving_a_pruned_session_brings_it_back_everywhere(world):
    """C-23.28a: the user unarchives it where it still is; flag sync spreads
    the change, and the live session is spread to every seeded folder."""
    put(world, ONE, FOLDERS, archived=True, days=30)
    running = engine(world)
    running.run_once(options(prune=True))
    assert where(world, ONE) == {HOME, WORK}
    rewrite(record(world, WORK), isArchived=False)
    running.run_once(options(prune=True))
    assert where(world, ONE) == SEEDED
    assert not any(json.loads(record(world, folder).read_text())["isArchived"]
                   for folder in SEEDED)
    assert base(world)[ONE]["isArchived"] is False and "archived_at" not in base(world)[ONE]


def test_switching_the_rules_off_spreads_every_session_back(world):
    """C-23.28a: every removal is undone by switching its rule off."""
    put(world, ONE, FOLDERS, archived=True, days=30)
    put(world, TWO, FOLDERS)
    running = engine(world)
    running.run_once(options(prune=True))
    assert where(world, ONE) == {HOME, WORK} and where(world, TWO) == SEEDED
    running.run_once(options(archive_days=0.0, skip_switch_folders=False))
    assert where(world, ONE) == set(FOLDERS) and where(world, TWO) == set(FOLDERS)


# --- the archive stamp --------------------------------------------------------

def test_the_archive_stamp_is_set_once_and_dropped_on_unarchive(world):
    """C-23.28a: an archive the mirror did not see happen is stamped with the
    app's last write of any copy; one it sees is stamped then; the stamp
    stays while the session stays archived and goes when it is unarchived."""
    put(world, ONE, [HOME, OTHER], archived=True, days=40, written=20)
    put(world, TWO, [HOME, WORK], days=1)
    running = engine(world)
    running.run_once(options())
    assert base(world)[ONE]["archived_at"] == pytest.approx(NOW_S - 20 * DAY, abs=1)
    assert "archived_at" not in base(world)[TWO]
    rewrite(record(world, WORK, TWO), isArchived=True)          # the user archives it
    running.run_once(options())
    assert base(world)[TWO]["archived_at"] == pytest.approx(NOW_S, abs=1)
    rewrite(record(world, WORK, TWO), lastFocusedAt=int(NOW_S * 1000))
    rewrite(record(world, OTHER), title="still archived")
    running.run_once(options())
    assert base(world)[ONE]["archived_at"] == pytest.approx(NOW_S - 20 * DAY, abs=1)
    assert base(world)[TWO]["archived_at"] == pytest.approx(NOW_S, abs=1)
    rewrite(record(world, WORK, TWO), isArchived=False)
    running.run_once(options())
    assert "archived_at" not in base(world)[TWO]


# --- the home folder ----------------------------------------------------------

@pytest.mark.parametrize("value,kinds,previous,expected", [
    ("org-home", {"acct-home/org-home": "settled", "acct-work/org-home": "switch"}, None,
     "acct-home/org-home"),
    ("org-x", {"a/org-x": "unknown"}, None, "a/org-x"),
    ("org-x", {"a/org-x": "settled", "b/org-x": "settled"}, None, None),
    ("org-x", {"a/org-x": "unknown", "b/org-x": "unknown"}, None, None),
    ("org-x", {"a/org-x": "switch"}, None, None),
    ("a/org-x", {"a/org-x": "unknown"}, None, "a/org-x"),
    ("a/org-x", {"a/org-x": "switch"}, None, "a/org-x"),            # named: honoured
    ("a/org-y", {"a/org-x": "settled"}, None, None),
    ("", {"a/org-x": "settled"}, None, None),
    # A home once resolved stays while the store holds it (review r2 N1, N4).
    ("org-x", {"a/org-x": "settled", "b/org-x": "settled"}, "b/org-x", "b/org-x"),
    ("org-x", {"a/org-x": "switch", "b/org-x": "settled"}, "a/org-x", "a/org-x"),
    ("org-x", {"b/org-x": "settled"}, "a/org-x", "b/org-x"),           # gone: anew
    ("org-x", {"a/org-y": "settled"}, "a/org-y", None),                # another org's
])
def test_the_home_is_one_folder_nobody_merely_passes_through(value, kinds, previous, expected):
    """C-23.28a: v1 stored an org id; several accounts hold a folder for one
    org, so it names the folder the app settles in or the only one there is,
    and never chooses a switch folder. A folder named exactly is the home,
    and a home once resolved stays, whatever the registry comes to call it."""
    home, why = mirror.Mirror.resolve_home(value, kinds, previous)
    assert home == expected and why
    if home is not None and kinds.get(home) == "switch":
        assert "switch folder" in why


# --- a removal takes only the file it checked -----------------------------------

def test_remove_takes_only_the_file_it_checked(tmp_path, monkeypatch):
    """C-23.28a: `_remove` removes the file it checked, puts back a file the
    app renamed over it since, and leaves the name to an even newer save."""
    path = tmp_path / "local_x.json"
    path.write_text('{"v": 0}')
    assert mirror._remove(path, mirror._signature_of(path))
    assert not path.exists() and not list(tmp_path.iterdir())

    path.write_text('{"v": 1}')
    checked = mirror._signature_of(path)
    rewrite(path, v=2)                               # a new inode under the name
    assert not mirror._remove(path, checked)
    assert json.loads(path.read_text()) == {"v": 2}
    assert [item.name for item in tmp_path.iterdir()] == ["local_x.json"]

    checked = mirror._signature_of(path)
    rewrite(path, v=3)
    link = os.link

    def newest_first(source, destination, **kwargs):
        Path(destination).write_text('{"v": 4}')     # the app saves again meanwhile
        return link(source, destination, **kwargs)

    monkeypatch.setattr(mirror.os, "link", newest_first)
    assert not mirror._remove(path, checked)
    assert json.loads(path.read_text()) == {"v": 4}
    assert [item.name for item in tmp_path.iterdir()] == ["local_x.json"]


def test_a_crashed_removal_is_settled_at_the_next_listing(world):
    """C-23.28a: a crash between the rename and the check leaves a grave. The
    next listing puts it back if its name is free, drops it if a newer save
    holds the name, and a dry run leaves it as it is."""
    put(world, ONE, SEEDED)
    put(world, TWO, SEEDED)
    for session in (ONE, TWO):
        path = record(world, OTHER, session)
        handle, grave = mirror._temporary(path, mirror.GRAVE_SUFFIX)
        os.close(handle)
        os.rename(path, grave)
    newer = record(world, OTHER, TWO)                 # the app saved it again since
    newer.write_text(json.dumps({**json.loads(record(world, HOME, TWO).read_text()),
                                 "model": "the newer save"}))
    engine(world).run_once(options(dry_run=True))
    assert len(graves(world)) == 2
    engine(world).run_once(options())
    assert not graves(world)
    assert json.loads(record(world, OTHER, ONE).read_text())["cliSessionId"] == ONE
    assert json.loads(newer.read_text())["model"] == "the newer save"


# --- the real mirror against the planner --------------------------------------

LIVE, ARCHIVED_OLD, ARCHIVED_YOUNG, DEAD = "live", "archived-old", "archived-young", "dead"


@st.composite
def stores(draw):
    """Random sessions in shapes a converged store holds, so the pass writes
    nothing but its removals: live and young archived sessions in every seeded
    folder, old archived ones at least in the home, dead ones anywhere."""
    specs = []
    for number in range(draw(st.integers(1, 5))):
        kind = draw(st.sampled_from((LIVE, ARCHIVED_OLD, ARCHIVED_YOUNG, DEAD)))
        starred = kind in (LIVE, ARCHIVED_YOUNG) and draw(st.booleans())
        if kind in (LIVE, ARCHIVED_YOUNG):
            folders = sorted(SEEDED) + ([SWITCH] if draw(st.booleans()) else [])
        elif kind == ARCHIVED_OLD:
            folders = [HOME] + draw(st.lists(st.sampled_from([WORK, OTHER, SWITCH]),
                                             unique=True))
        else:
            folders = draw(st.lists(st.sampled_from(list(FOLDERS)), min_size=1, unique=True))
        low, high = (0.2, 13.0) if kind in (LIVE, ARCHIVED_YOUNG) else (14.5, 60.0)
        if kind == DEAD and draw(st.booleans()):
            low, high = 0.2, 6.5
        ages = {}
        for folder in folders:
            # Activity, and sometimes a later focus in the same bounds: the
            # two measures of "newest" can then name different copies.
            active = draw(st.floats(low, high))
            focused = draw(st.floats(low, active)) if draw(st.booleans()) else None
            ages[folder] = (active, focused)
        specs.append((f"{number:08d}-cccc-4000-8000-000000000000", kind, starred, ages))
    return specs


def snapshot(world) -> list[prune_rules.Copy]:
    found = []
    for path in world.store.glob("*/*/local_*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        found.append(prune_rules.Copy(
            folder=f"{path.parent.parent.name}/{path.parent.name}", name=path.name,
            identity=data["cliSessionId"], archived=bool(data["isArchived"]),
            starred=bool(data["isStarred"]), activity_ms=mirror._activity_ms(data),
            rank_ms=mirror._rank_ms(data), mtime=path.stat().st_mtime))
    return found


@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(stores())
def test_the_mirror_removes_exactly_what_the_planner_plans(specs):
    """C-23.28a, differential: on a converged store the real pass (its I/O,
    its re-checks, its rename-and-compare removal) removes exactly the copies
    the pure planner plans, from the same inputs: the folder kinds the log
    gives, the loaded folder, and the archive stamps a first pass makes."""
    with tempfile.TemporaryDirectory() as scratch, pytest.MonkeyPatch.context() as patch:
        world = make_world(Path(scratch), patch)
        (world.home / "projects" / fx.project_slug()).mkdir(parents=True, exist_ok=True)
        for session, kind, starred, ages in specs:
            if kind != DEAD:
                fx.transcript(world.home, session, fx.completed())
            for (account, org), (days, focused) in ages.items():
                extra = ({"lastFocusedAt": int((NOW_S - focused * DAY) * 1000)}
                         if focused is not None else {})
                path = fx.index_entry(world.store, account, org, session,
                                      archived=kind in (ARCHIVED_OLD, ARCHIVED_YOUNG),
                                      starred=starred,
                                      last_activity=int((NOW_S - days * DAY) * 1000),
                                      settings={"ultracode": True}, **extra)
                os.utime(path, (NOW_S - days * DAY,) * 2)
        before = snapshot(world)
        stamps = {}
        for session, kind, _starred, ages in specs:
            if kind in (ARCHIVED_OLD, ARCHIVED_YOUNG):
                stamps[session] = max(copy.mtime for copy in before if copy.identity == session)
        situation = prune_rules.Situation(
            folders=tuple(sorted(key(folder) for folder in FOLDERS)),
            kinds={key(HOME): folder_kinds.SETTLED, key(WORK): folder_kinds.SETTLED,
                   key(OTHER): folder_kinds.SETTLED, key(SWITCH): folder_kinds.SWITCH},
            home=key(HOME), loaded=key(WORK),
            openable=frozenset(session for session, kind, _s, _a in specs if kind != DEAD),
            archived_at=stamps, now=NOW_S)
        planned = prune_rules.plan(before, situation, options(prune=True).rules)
        result = engine(world).run_once(options(prune=True))
        after = {(copy.folder, copy.name) for copy in snapshot(world)}
        removed = {(copy.folder, copy.name) for copy in before} - after
        assert result.state == "ok" and result.added == 0 and result.flag_synced == 0
        assert removed == {(item.folder, item.name) for item in planned.removals}
        assert result.pruned == len(planned.removals)
        event(f"removed {min(len(removed), 3)}{'+' if len(removed) > 3 else ''}")
        event("kept a newest copy outside its scope" if planned.kept else "no newest kept")
