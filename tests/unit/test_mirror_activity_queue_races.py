"""The date sync's queue against stale views and folders that will not list: C-23.28.

The fourth review of #167 executed each of these on real files with real app
saves, and found that a session lost its place in the queue when a pass saw it
caught up. Places were then dropped on catching up; now nothing but a turn
moves a place (`mirror.take_turns`). The tests are the reviewer's, adapted:
`~/reviews/subfleet-v2-pr167/r4-evidence/scheduler/`.
"""

from __future__ import annotations

import errno
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from subfleet.sessions import mirror
from tests import sessions_fixtures as fx

NEW = 1_791_635_200_000
DAY = 86_400_000
CLOCK = datetime.fromtimestamp((NEW + 3 * DAY) / 1000, timezone.utc)
WRITABLE = "99999999-0000-4000-8000-00000000beef"
REAL = "67eb7695-0000-4000-8000-000000000001"
FORK = "181ca5ca-0000-4000-8000-000000000002"
SHARED = "local_00000000-1111-4000-8000-000000000009.json"


def date(path: Path):
    return json.loads(path.read_text())[mirror.ACTIVITY_FIELD]


def atomic_save(path: Path, **fields) -> None:
    """The app's own write: beside the file, then rename."""
    temporary = path.with_suffix(".app-save")
    temporary.write_text(json.dumps({**json.loads(path.read_text()), **fields}))
    temporary.replace(path)


def setup(tmp_path, monkeypatch, stuck_count: int):
    """`stuck_count` sessions thirty days behind in B whose writes always fail,
    and one two days behind that can be written."""
    home = fx.claude_home(tmp_path, monkeypatch)
    store = fx.desktop_store(tmp_path, monkeypatch)
    monkeypatch.setenv("SUBFLEET_DESKTOP_LOG", str(tmp_path / "logs" / "main.log"))
    stuck = [f"{index:08d}-0000-4000-8000-00000000dead" for index in range(stuck_count)]
    for session in (*stuck, WRITABLE):
        fx.transcript(home, session, fx.completed())
        fx.index_entry(store, "acct-a", "org-a", session, last_activity=NEW,
                       settings={"ultracode": True})
        fx.index_entry(store, "acct-b", "org-b", session,
                       last_activity=NEW - (2 if session == WRITABLE else 30) * DAY,
                       settings={"ultracode": True})
    running = mirror.Mirror(tmp_path / "state", fx.policy(mirror_hot_interval_s=0),
                            now=lambda: CLOCK)
    install = mirror._install
    attempted: list[str] = []

    def refusing(temporary, destination, **kwargs):
        if kwargs.get("expect") is not None and any(name in destination.name for name in stuck):
            attempted.append(destination.stem.removeprefix("local_")[:8])
            temporary.unlink()
            return False
        return install(temporary, destination, **kwargs)

    monkeypatch.setattr(mirror, "_install", refusing)
    target = store / "acct-b" / "org-b" / f"local_{WRITABLE}.json"
    return running, target, attempted, stuck


def test_a_parents_older_view_does_not_cost_a_session_its_place(tmp_path, monkeypatch):
    """The fourth review's first finding. The full pass reads A's copy of the
    session (no lag yet), and then the app saves A lower and B higher: the
    session is behind throughout. The hot pass the full pass runs at its next
    checkpoint sees it behind and gives it a place. The full pass's own sync,
    from its older view, saw it caught up and dropped that place, so the next
    sync put it behind ten that had arrived after it."""
    install = mirror._install
    running, target_b, _attempted, stuck = setup(tmp_path, monkeypatch, 10)
    target_a = target_b.parents[2] / "acct-a" / "org-a" / target_b.name
    running.run_once(mirror.Options(activity_lag_s=0))
    running.policy["sessions"]["mirror_hot_interval_s"] = 2
    options = mirror.options_from(running.policy)
    races = []

    def app_saves_before_install(temporary, destination, **kwargs):
        if destination.parent == target_b.parent and kwargs.get("expect") is not None \
                and destination.stem.removeprefix("local_") in stuck:
            races.append(destination.name)
            atomic_save(destination, lastFocusedAt=len(races))
        return install(temporary, destination, **kwargs)

    monkeypatch.setattr(mirror, "_install", app_saves_before_install)
    for index, identity in enumerate(stuck):          # only these are the first hot pass's
        atomic_save(target_b.parent / f"local_{identity}.json", lastFocusedAt=9000 + index)
    checkpoint, inventory, reader = running._checkpoint, running._flag_inventory, mirror._read_entry
    mode = {"inventory": False, "read_clean_a": False, "first_hot": False, "late_hot": False}
    late = {}

    def observe(where):
        value = reader(where)
        if mode["inventory"] and not mode["late_hot"] and where == target_a:
            assert json.loads(value[0])[mirror.ACTIVITY_FIELD] == NEW
            mode["read_clean_a"] = True
        return value

    def inventory_with_app_save(current, opts):
        mode["inventory"] = True
        atomic_save(target_a, lastFocusedAt=2000)     # so the refresh reads A again
        try:
            return inventory(current, opts)
        finally:
            mode["inventory"] = False

    def service(current, stage=None):
        running._hot_due = float("inf")
        if current.kind == "full":
            if stage == "reading entries" and not mode["first_hot"]:
                mode["first_hot"] = True
                running._service_hot()
            elif mode["inventory"] and mode["read_clean_a"] and not mode["late_hot"]:
                mode["late_hot"] = True
                assert [date(target_a), date(target_b)] == [NEW, NEW - 2 * DAY]
                atomic_save(target_a, lastActivityAt=NEW - 4 * DAY, lastFocusedAt=3000)
                atomic_save(target_b, lastActivityAt=NEW, lastFocusedAt=3001)
                running._service_hot()
                late["place"] = running._activity_turns.since.get(WRITABLE)
        running._hot_due = float("inf")
        return checkpoint(current, stage)

    monkeypatch.setattr(mirror, "_read_entry", observe)
    monkeypatch.setattr(running, "_flag_inventory", inventory_with_app_save)
    monkeypatch.setattr(running, "_checkpoint", service)
    result = running.run_once(options)
    assert result.state == "ok"
    assert mode["first_hot"] and mode["late_hot"] and mode["read_clean_a"]
    assert running._hot_epoch == 0
    assert late["place"] is not None
    assert running._activity_turns.since.get(WRITABLE) == late["place"], \
        "the full pass keeps the place its own hot pass gave"
    monkeypatch.setattr(running, "_flag_inventory", inventory)
    monkeypatch.setattr(running, "_checkpoint", checkpoint)
    monkeypatch.setattr(mirror, "_read_entry", reader)
    after = running.run_once(options)
    assert after.state == "ok"
    assert date(target_a) == NEW - 1, "and the next sync takes it, before the ten"


def test_a_folder_that_will_not_list_keeps_the_report_and_every_place(tmp_path, monkeypatch):
    """The fourth review's second finding, on the real filesystem: B's folder
    loses read permission and keeps write and search, so a known name can still
    be opened and saved. The folder held the other half of a split and the
    session waiting for a raise. The report read clean and the place was lost,
    once for every time the permission came and went."""
    home = fx.claude_home(tmp_path, monkeypatch)
    store = fx.desktop_store(tmp_path, monkeypatch)
    monkeypatch.setenv("SUBFLEET_DESKTOP_LOG", str(tmp_path / "desktop.log"))
    for identity in (REAL, FORK):
        fx.transcript(home, identity, fx.completed())
    fx.index_entry(store, "acct-a", "org-a", REAL, name=SHARED, last_activity=NEW,
                   settings={"ultracode": True})
    target = fx.index_entry(store, "acct-b", "org-b", REAL, last_activity=NEW - 2 * DAY,
                            settings={"ultracode": True})
    fx.index_entry(store, "acct-b", "org-b", FORK, name=SHARED, last_activity=NEW,
                   settings={"ultracode": True})
    stuck = {f"{index:08d}-0000-4000-8000-00000000dead" for index in range(10)}
    for identity in stuck:
        fx.transcript(home, identity, fx.completed())
        fx.index_entry(store, "acct-a", "org-a", identity, last_activity=NEW,
                       settings={"ultracode": True})
        fx.index_entry(store, "acct-b", "org-b", identity, last_activity=NEW - 30 * DAY,
                       settings={"ultracode": True})
    install = mirror._install
    saves = []

    def race(temporary, destination, **kwargs):
        if destination.parent == target.parent and kwargs.get("expect") is not None \
                and destination.stem.removeprefix("local_") in stuck:
            saves.append(destination.name)
            atomic_save(destination, lastFocusedAt=len(saves))
        return install(temporary, destination, **kwargs)

    monkeypatch.setattr(mirror, "_install", race)
    running = mirror.Mirror(tmp_path / "state", fx.policy(mirror_hot_interval_s=0),
                            now=lambda: CLOCK)
    options = mirror.options_from(running.policy)
    assert running.run_once(options).state == "ok"
    whole = running.splits()
    place = running._activity_turns.since[REAL]
    assert whole["live"] == 1 and date(target) == NEW - 2 * DAY and place == 2
    folder = target.parent
    folder.chmod(0o300)
    try:
        with pytest.raises(PermissionError) as error:
            with os.scandir(folder) as listing:
                list(listing)
        assert error.value.errno == errno.EACCES
        atomic_save(target, lastFocusedAt=999)          # a known name still opens
        assert running.run_once(options).state == "ok"
        assert running.splits() == whole
        assert running._activity_turns.since[REAL] == place
        for _cycle in range(3):
            folder.chmod(0o700)
            assert running.run_once(options).state == "ok"
            folder.chmod(0o300)
            assert running.run_once(options).state == "ok"
            assert REAL in running._activity_turns.since
    finally:
        folder.chmod(0o700)
    assert date(target) == NEW - 1, "its turn came while the folder could be listed"


def test_a_preview_of_an_empty_store_moves_no_place(tmp_path, monkeypatch):
    """The fourth review's note: a dry run with every folder excluded took the
    empty store for a whole inventory and emptied the queue."""
    running, _target, _attempted, _stuck = setup(tmp_path, monkeypatch, 10)
    running.run_once(mirror.options_from(running.policy))
    before = (running._activity_turns.turn, dict(running._activity_turns.since))
    preview = running.run_once(mirror.options_from(running.policy, dry_run=True,
                                                   exclude=["acct-a", "acct-b"]))
    assert preview.state == "ok" and preview.sessions == 0
    assert (running._activity_turns.turn, dict(running._activity_turns.since)) == before


def test_sessions_that_fall_behind_later_never_pass_one_already_waiting(tmp_path, monkeypatch):
    """Thirty sessions further behind are ahead of the writable one at the
    start, and ten more arrive, further behind still, every sync. With a bound
    of ten the writable one is taken at the fourth sync."""
    running, target, _attempted, stuck = setup(tmp_path, monkeypatch, 30)
    store = target.parents[2]
    dates = []
    for cycle in range(6):
        if cycle:
            for index in range(10):
                identity = f"{100 + cycle * 10 + index:08d}-0000-4000-8000-00000000dead"
                stuck.append(identity)
                fx.transcript(tmp_path / "claude", identity, fx.completed())
                fx.index_entry(store, "acct-a", "org-a", identity, last_activity=NEW,
                               settings={"ultracode": True})
                fx.index_entry(store, "acct-b", "org-b", identity,
                               last_activity=NEW - 50 * DAY, settings={"ultracode": True})
        assert running.run_once(mirror.options_from(running.policy)).state == "ok"
        dates.append(date(target))
    assert dates[:3] == [NEW - 2 * DAY] * 3 and dates[3:] == [NEW - 1] * 3
