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


def history(store: Path) -> list[str]:
    """An hour in the home folder and in another account's, then a switch to
    work: two seconds in the home account with work's org, then work, loaded
    since 10:00:02 (an hour and a half by the fixture's clock)."""
    return [*loads(store, HOME, "2026-09-05 08:00:00"),
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
        title: str = "a session") -> None:
    """Copies of one session, quiet `days`, last written `written` days ago."""
    if openable:
        fx.transcript(world.home, session, fx.completed())
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

@pytest.mark.parametrize("value,kinds,expected", [
    ("org-home", {"acct-home/org-home": "settled", "acct-work/org-home": "switch"},
     "acct-home/org-home"),
    ("org-x", {"a/org-x": "unknown"}, "a/org-x"),
    ("org-x", {"a/org-x": "settled", "b/org-x": "settled"}, None),
    ("org-x", {"a/org-x": "unknown", "b/org-x": "unknown"}, None),
    ("org-x", {"a/org-x": "switch"}, None),
    ("a/org-x", {"a/org-x": "unknown"}, "a/org-x"),
    ("a/org-x", {"a/org-x": "switch"}, None),
    ("a/org-y", {"a/org-x": "settled"}, None),
    ("", {"a/org-x": "settled"}, None),
])
def test_the_home_is_one_folder_nobody_merely_passes_through(value, kinds, expected):
    """C-23.28a: v1 stored an org id; several accounts hold a folder for one
    org, so it names the folder the app settles in or the only one there is,
    and never a switch folder."""
    home, why = mirror.Mirror.resolve_home(value, kinds)
    assert home == expected and why


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
        ages = {folder: draw(st.floats(low, high)) for folder in folders}
        specs.append((f"{number:08d}-cccc-4000-8000-000000000000", kind, starred, ages))
    return specs


def snapshot(world) -> list[prune_rules.Copy]:
    found = []
    for path in world.store.glob("*/*/local_*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        found.append(prune_rules.Copy(
            folder=f"{path.parent.parent.name}/{path.parent.name}", name=path.name,
            identity=data["cliSessionId"], archived=bool(data["isArchived"]),
            starred=bool(data["isStarred"]), activity_ms=int(data["lastActivityAt"]),
            mtime=path.stat().st_mtime))
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
        for session, kind, starred, ages in specs:
            if kind != DEAD:
                fx.transcript(world.home, session, fx.completed())
            for (account, org), days in ages.items():
                path = fx.index_entry(world.store, account, org, session,
                                      archived=kind in (ARCHIVED_OLD, ARCHIVED_YOUNG),
                                      starred=starred,
                                      last_activity=int((NOW_S - days * DAY) * 1000),
                                      settings={"ultracode": True})
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
