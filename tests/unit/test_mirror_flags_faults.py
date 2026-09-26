"""Flag sync when a copy cannot be read: C-23.28, review rounds 5 and 6.

A pass that decides a session without one of its copies, and advances the
merge base, leaves that copy's old value to read as a user's change on the
next pass, which then undoes the change it just spread. So a session is
decided from every copy or not at all:

* an unlisted folder unchanged since its last listing is read by name from
  that listing; one that changed, or was never listed, holds every session;
* an account that did not list keeps the org folders known under it;
* a copy that exists and cannot be read holds the session its last listing
  saw in it, never one guessed from a same-named file elsewhere;
* a directory or file the user may not read is in no sidebar (the app runs as
  the same user), so it holds nothing and cannot freeze flag sync;
* bases of sessions a pass did not see survive it.
"""

from __future__ import annotations

import itertools
import json
import os
from datetime import timedelta

import pytest

from subfleet.sessions import mirror
from tests import sessions_fixtures as fx

SESSION = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
FOLDERS = (("acct-a", "org-a"), ("acct-b", "org-b"), ("acct-c", "org-c"))


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = fx.claude_home(tmp_path, monkeypatch)
    store = fx.desktop_store(tmp_path, monkeypatch)
    fx.transcript(home, SESSION, fx.completed())
    root = tmp_path / "state"
    root.mkdir()
    ticks = itertools.count()
    running = mirror.Mirror(root, fx.policy(), now=lambda: fx.NOW + timedelta(seconds=next(ticks)))
    return running, store


def seed(store, value: bool) -> None:
    for account, org in FOLDERS:
        fx.index_entry(store, account, org, SESSION, archived=value, settings={"ultracode": True})


def path(store, index: int):
    account, org = FOLDERS[index]
    return store / account / org / f"local_{SESSION}.json"


def flags(store) -> tuple[bool, ...]:
    return tuple(json.loads(path(store, i).read_text())["isArchived"] for i in range(3))


def rewrite(store, index: int, **fields) -> None:
    target = path(store, index)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps({**json.loads(target.read_text()), **fields}))
    temporary.replace(target)


def base(running):
    return (mirror._load(running.flags_path).get(SESSION) or {}).get("isArchived")


def unlisting(patch, *folders: str) -> None:
    """The listing fails as it did on 2026-09-25: too many open files."""
    real = os.scandir

    def scandir(where="."):
        if any(str(where).endswith(folder) for folder in folders):
            raise OSError(24, "Too many open files", str(where))
        return real(where)

    patch.setattr(mirror.os, "scandir", scandir)


def unreadable(patch, target) -> None:
    real = mirror._read_entry

    def read(where):
        if str(where) == str(target):
            raise OSError(24, "Too many open files")
        return real(where)

    patch.setattr(mirror, "_read_entry", read)


def test_an_unlisted_folder_unchanged_since_its_listing_is_read_by_name(world, monkeypatch):
    running, store = world
    seed(store, False)
    assert running.run_once().state == "ok" and base(running) is False
    rewrite(store, 0, isArchived=True)                   # the user archives in A
    with monkeypatch.context() as patch:
        unlisting(patch, "acct-c/org-c")                  # C cannot be listed
        result = running.run_once()
    assert result.state == "ok" and result.flags_held == 0
    assert flags(store) == (True,) * 3, "C was read and written by name"
    assert running.run_once().state == "ok"
    assert flags(store) == (True,) * 3 and base(running) is True


def test_an_unlisted_folder_that_changed_since_its_listing_holds_every_session(
        world, monkeypatch):
    running, store = world
    seed(store, False)
    assert running.run_once().state == "ok" and base(running) is False
    rewrite(store, 0, isArchived=True)                   # the user archives in A
    rewrite(store, 2, lastFocusedAt=7)                   # the app saves C
    with monkeypatch.context() as patch:
        unlisting(patch, "acct-c/org-c")
        result = running.run_once()
    assert result.state == "ok" and result.flags_held == 1
    assert flags(store) == (True, False, False) and base(running) is False
    assert running.run_once().state == "ok"
    assert flags(store) == (True,) * 3


def test_an_unreadable_copy_holds_its_session_and_the_users_unarchive_stands(world, monkeypatch):
    running, store = world
    seed(store, True)
    assert running.run_once().state == "ok" and base(running) is True
    rewrite(store, 0, isArchived=False)                  # the user unarchives in A
    rewrite(store, 2, lastFocusedAt=7)                   # the app saves C
    with monkeypatch.context() as patch:
        unreadable(patch, path(store, 2))                 # EMFILE on C's read
        result = running.run_once()
    assert result.state == "ok" and result.flags_held == 1
    assert flags(store) == (False, True, True) and base(running) is True, "held: nothing written"
    assert running.run_once().state == "ok"
    assert flags(store) == (False,) * 3 and base(running) is False


def test_a_pass_that_lists_nothing_holds_and_keeps_every_base(world, monkeypatch):
    running, store = world
    seed(store, True)
    assert running.run_once().state == "ok" and base(running) is True
    rewrite(store, 0, isArchived=False)                  # the user unarchives in A
    with monkeypatch.context() as patch:
        patch.setattr(mirror.Mirror, "_sweep_due", lambda self: True)
        unlisting(patch, *(f"{account}/{org}" for account, org in FOLDERS))
        result = running.run_once()
    assert result.state == "ok" and result.flags_held == 1, "A changed since its listing"
    assert flags(store) == (False, True, True) and base(running) is True
    assert running.run_once().state == "ok"
    assert flags(store) == (False,) * 3 and base(running) is False


def test_a_pass_that_can_read_nothing_keeps_every_base(world, monkeypatch):
    running, store = world
    seed(store, True)
    assert running.run_once().state == "ok" and base(running) is True
    rewrite(store, 0, isArchived=False)                  # the user unarchives in A
    real = mirror._read_entry

    def emfile(_where):
        raise OSError(24, "Too many open files")

    with monkeypatch.context() as patch:
        patch.setattr(mirror.Mirror, "_sweep_due", lambda self: True)
        unlisting(patch, *(f"{account}/{org}" for account, org in FOLDERS))
        patch.setattr(mirror, "_read_entry", emfile)
        result = running.run_once()
    assert result.state == "ok" and result.flags_held == 1
    assert base(running) is True, "a pass that saw nothing wipes no base"
    assert flags(store) == (False, True, True)
    assert running.run_once().state == "ok"
    assert flags(store) == (False,) * 3, "the user's unarchive stands"


def test_a_folder_never_listed_holds_every_session(world, monkeypatch):
    running, store = world
    seed(store, False)
    rewrite(store, 0, isArchived=True)
    with monkeypatch.context() as patch:
        unlisting(patch, "acct-c/org-c")                  # the first pass cannot list C
        result = running.run_once()
    assert result.state == "ok" and result.flags_held == 1
    assert flags(store) == (True, False, False), "which sessions C holds is unknown"
    assert running.run_once().state == "ok"
    assert flags(store) == (True,) * 3


def test_a_base_that_cannot_be_written_fails_the_pass(world, monkeypatch):
    running, store = world
    seed(store, False)
    assert running.run_once().state == "ok"
    rewrite(store, 0, isArchived=True)
    write = mirror._write_json

    def disk_full(target, value, **kwargs):
        if target == running.flags_path:
            raise OSError(28, "No space left on device")
        return write(target, value, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(mirror, "_write_json", disk_full)
        result = running.run_once()
    assert result.state == "error", "a base that was not written is not a synced pass"


def test_an_unreadable_copy_is_never_repaired_over(world, monkeypatch):
    """C-23.28: the spread's repair of a stale empty record must not take a
    record it could not read for an empty one and replace the app's save.
    On a process's first pass nothing names the unreadable copy's session, so
    the repair's own re-check is what stands in the way."""
    running, store = world
    seed(store, False)
    rewrite(store, 2, title="the app's newest", lastFocusedAt=7)
    load = mirror._load

    def unreadable_load(where, *args, **kwargs):
        if str(where) == str(path(store, 2)):
            raise OSError(24, "Too many open files")
        return load(where, *args, **kwargs)

    with monkeypatch.context() as patch:
        unreadable(patch, path(store, 2))
        patch.setattr(mirror, "_load", unreadable_load)
        result = running.run_once()
    assert result.repaired == 0
    assert json.loads(path(store, 2).read_text())["title"] == "the app's newest"


def test_a_session_no_copy_of_which_could_be_read_keeps_its_base(world, monkeypatch):
    """C-23.28: a session this pass saw no copy of keeps its merge base; losing
    it would hand the user's next change to the bootstrap rule."""
    running, store = world
    seed(store, True)
    assert running.run_once().state == "ok" and base(running) is True
    for index in range(3):
        rewrite(store, index, lastFocusedAt=7)           # every copy must be read again
    real = mirror._read_entry

    def emfile(where):
        if str(where).endswith(f"local_{SESSION}.json"):
            raise OSError(24, "Too many open files")
        return real(where)

    with monkeypatch.context() as patch:
        patch.setattr(mirror, "_read_entry", emfile)
        assert running.run_once().state == "ok"
    assert base(running) is True
    rewrite(store, 0, isArchived=False)                  # the user unarchives in A
    assert running.run_once().state == "ok"
    assert flags(store) == (False,) * 3, "the change wins; the bootstrap rule would archive"


OTHER = "7e7e7e7e-0000-4000-8000-000000000002"


def test_an_account_that_does_not_list_keeps_its_folders(world, monkeypatch):
    """Review round 6: an account whose listing fails must not drop its org
    folders from the decision (it would undo the user's archive next pass)."""
    running, store = world
    seed(store, False)
    assert running.run_once().state == "ok" and base(running) is False
    rewrite(store, 0, isArchived=True)                   # the user archives in A
    real = os.scandir

    def scandir(where="."):
        if str(where).endswith("acct-c"):
            raise OSError(24, "Too many open files", str(where))
        return real(where)

    with monkeypatch.context() as patch:
        patch.setattr(mirror.os, "scandir", scandir)
        assert running.run_once().state == "ok"
    assert running.run_once().state == "ok"
    assert flags(store) == (True,) * 3 and base(running) is True


def test_a_copy_the_mirror_spread_after_the_listing_is_not_missed(world, monkeypatch):
    """Review round 6: the mirror's own copy into a folder makes its last
    listing stale; a by-name read of that listing would miss the copy."""
    running, store = world
    fx.index_entry(store, *FOLDERS[0], SESSION, archived=False, settings={"ultracode": True})
    for account, org in FOLDERS[1:]:
        (store / account / org).mkdir(parents=True)
    assert running.run_once().state == "ok"               # spreads to B and C
    assert flags(store) == (False,) * 3
    rewrite(store, 0, isArchived=True)                   # the user archives in A
    with monkeypatch.context() as patch:
        unlisting(patch, "acct-c/org-c")
        result = running.run_once()
    assert result.flags_held == 1
    assert running.run_once().state == "ok"
    assert flags(store) == (True,) * 3


def test_a_folder_the_user_may_not_read_freezes_nothing(world):
    """Review round 6: permission denied is no one's sidebar, not a hold."""
    running, store = world
    seed(store, False)
    closed = store / "acct-d" / "org-d"
    closed.mkdir(parents=True)
    os.chmod(closed, 0)
    try:
        rewrite(store, 0, isArchived=True)
        result = running.run_once()
        assert result.state == "ok" and result.flags_held == 0
        assert flags(store) == (True,) * 3
    finally:
        os.chmod(closed, 0o700)


def test_a_record_the_user_may_not_read_freezes_nothing(world):
    running, store = world
    seed(store, False)
    dead = fx.index_entry(store, "acct-b", "org-b", OTHER)   # a dead, unspread session
    assert running.run_once().state == "ok"
    os.chmod(dead, 0)
    try:
        rewrite(store, 0, isArchived=True)
        for _ in range(2):
            result = running.run_once()
            assert result.state == "ok" and result.flags_held == 0
        assert flags(store) == (True,) * 3
    finally:
        os.chmod(dead, 0o600)


def test_a_persistently_unreadable_copy_holds_only_its_own_session(world, monkeypatch):
    """Review round 6: the owner of an unreadable copy survives its failed
    reads (through the folder's listing), so other sessions keep syncing."""
    running, store = world
    seed(store, False)
    for account, org in FOLDERS:
        fx.index_entry(store, account, org, OTHER, settings={"ultracode": True})
    assert running.run_once().state == "ok"
    other_c = store / "acct-c" / "org-c" / f"local_{OTHER}.json"
    temporary = other_c.with_name(other_c.name + ".tmp")
    temporary.write_text(json.dumps({**json.loads(other_c.read_text()), "lastFocusedAt": 3}))
    temporary.replace(other_c)
    with monkeypatch.context() as patch:
        unreadable(patch, other_c)
        for _ in range(3):
            rewrite(store, 0, isArchived=not flags(store)[0])   # the user toggles SESSION
            result = running.run_once()
            assert result.state == "ok" and result.flags_held == 1
            assert len(set(flags(store))) == 1, "SESSION still syncs"


def test_a_same_named_copy_elsewhere_never_takes_the_hold(world, monkeypatch):
    """Review round 6: names are not unique across accounts. An unreadable
    copy holds the session its folder's listing saw, not the session another
    folder keeps under the same name."""
    running, store = world
    name = "local_shared.json"
    for account, org in FOLDERS[:2]:
        fx.index_entry(store, account, org, OTHER, name=name, settings={"ultracode": True})
        fx.index_entry(store, account, org, SESSION, settings={"ultracode": True})
    fx.index_entry(store, *FOLDERS[2], SESSION, name=name, settings={"ultracode": True})
    c_copy = store / "acct-c" / "org-c" / name
    assert running.run_once().state == "ok"
    rewrite(store, 0, isStarred=True)                    # the user stars SESSION in A
    assert running.run_once().state == "ok"               # the mirror writes C's copy
    assert json.loads(c_copy.read_text())["isStarred"] is True
    rewrite(store, 0, isArchived=True)                   # then archives it in A
    with monkeypatch.context() as patch:
        unreadable(patch, c_copy)
        result = running.run_once()
    assert result.flags_held == 1
    assert running.run_once().state == "ok"
    assert json.loads(path(store, 0).read_text())["isArchived"] is True
    assert json.loads(c_copy.read_text())["isArchived"] is True


def test_a_failed_check_after_a_good_read_still_counts_the_copy(world, monkeypatch):
    """Review round 6: a stat that fails after the read succeeded leaves the
    copy read (uncached), not unknown."""
    running, store = world
    seed(store, False)
    assert running.run_once().state == "ok"
    rewrite(store, 0, isArchived=True)
    rewrite(store, 2, lastFocusedAt=9)
    target = path(store, 2)
    real = mirror.Mirror._signature
    calls = {"n": 0}

    def signature(where):
        if str(where) == str(target):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(24, "Too many open files")
        return real(where)

    with monkeypatch.context() as patch:
        patch.setattr(mirror.Mirror, "_signature", staticmethod(signature))
        result = running.run_once()
    assert result.flags_held == 0
    assert flags(store) == (True,) * 3


def test_a_store_that_does_not_list_fails_the_pass_and_writes_nothing(world, monkeypatch):
    """Review round 6: unknown is not empty. A store listing that fails is not
    an empty store (which would forget every folder); the pass fails."""
    running, store = world
    seed(store, False)
    assert running.run_once().state == "ok"
    rewrite(store, 0, isArchived=True)
    real = os.scandir

    def scandir(where="."):
        if str(where) == str(store):
            raise OSError(24, "Too many open files", str(where))
        return real(where)

    with monkeypatch.context() as patch:
        patch.setattr(mirror.os, "scandir", scandir)
        result = running.run_once()
    assert result.state == "error" and "Too many open files" in (result.error or "")
    assert flags(store) == (True, False, False)
    assert running.run_once().state == "ok"
    assert flags(store) == (True,) * 3


def test_an_account_never_listed_holds_every_session(world, monkeypatch):
    """Review round 6: an account that fails to list on the process's first
    pass hides which sessions its folders hold."""
    running, store = world
    seed(store, False)
    rewrite(store, 0, isArchived=True)
    real = os.scandir

    def scandir(where="."):
        if str(where).endswith("acct-c"):
            raise OSError(24, "Too many open files", str(where))
        return real(where)

    with monkeypatch.context() as patch:
        patch.setattr(mirror.os, "scandir", scandir)
        result = running.run_once()
    assert result.state == "ok" and result.flags_held == 1
    assert flags(store) == (True, False, False)
    assert running.run_once().state == "ok"
    assert flags(store) == (True,) * 3


def test_health_names_the_sessions_a_pass_held(world, monkeypatch):
    """Review round 6: a hold is visible, not only in the sidecar's counters."""
    running, store = world
    seed(store, False)
    assert running.run_once().state == "ok"
    rewrite(store, 2, lastFocusedAt=7)
    with monkeypatch.context() as patch:
        unreadable(patch, path(store, 2))
        assert running.run_once().flags_held == 1
    health = running.health()
    assert health["status"] == "healthy" and health["flags_held"] == 1
    assert "flags held for 1 session (" in health["detail"]
    assert "unreadable (EMFILE)" in health["detail"]
    assert health["held_by"][0]["path"].endswith(f"acct-c/org-c/local_{SESSION}.json")



def failing_scandir(patch, *suffixes: str) -> None:
    real = os.scandir

    def scandir(where="."):
        if any(str(where).endswith(suffix) for suffix in suffixes):
            raise OSError(24, "Too many open files", str(where))
        return real(where)

    patch.setattr(mirror.os, "scandir", scandir)


def test_an_account_whose_folders_are_not_all_known_holds(world, monkeypatch):
    """Review round 7: a failed account counts as known only if every org
    folder its last listing named has been listed itself."""
    running, store = world
    seed(store, False)
    fx.index_entry(store, "acct-c", "org-c2", SESSION, archived=False,
                   settings={"ultracode": True})
    second = store / "acct-c" / "org-c2" / f"local_{SESSION}.json"
    with monkeypatch.context() as patch:
        failing_scandir(patch, "acct-c/org-c2")             # first pass: org-c2 not listed
        result = running.run_once()
    assert result.flags_held == 1
    rewrite(store, 0, isArchived=True)                   # the user archives in A
    with monkeypatch.context() as patch:
        failing_scandir(patch, "/acct-c")                  # the account does not list
        result = running.run_once()
    assert result.flags_held == 1, "org-c2 was never listed: what it holds is unknown"
    assert any("never listed" in item["reason"] for item in result.held_by)
    assert running.run_once().state == "ok"
    assert flags(store) == (True,) * 3
    assert json.loads(second.read_text())["isArchived"] is True


def test_a_hot_pass_that_cannot_list_the_store_forgets_nothing(world, monkeypatch):
    running, store = world
    seed(store, False)
    assert running.run_once().state == "ok"
    known = set(running._folders)
    with monkeypatch.context() as patch:
        failing_scandir(patch, str(store))
        result = running.run_hot()
    assert result.state == "error"
    assert set(running._folders) == known


def test_a_hot_pass_keeps_the_folders_of_an_account_that_did_not_list(world, monkeypatch):
    running, store = world
    seed(store, False)
    assert running.run_once().state == "ok"
    rewrite(store, 0, isArchived=True)
    with monkeypatch.context() as patch:
        failing_scandir(patch, "/acct-c")
        assert running.run_hot().state == "ok"
        assert store / "acct-c" / "org-c" in running._folders
        result = running.run_once()                      # still failing: read by name
    assert result.flags_held == 0
    assert flags(store) == (True,) * 3


def test_an_excluded_account_that_does_not_list_holds_nothing(world, monkeypatch):
    running, store = world
    seed(store, False)
    (store / "acct-x" / "org-x").mkdir(parents=True)
    rewrite(store, 0, isArchived=True)
    with monkeypatch.context() as patch:
        failing_scandir(patch, "/acct-x")
        result = running.run_once(mirror.Options(exclude=("acct-x",)))
    assert result.flags_held == 0
    assert flags(store) == (True,) * 3


def test_a_directory_named_like_a_record_holds_nothing(world):
    """Review round 7: what the app cannot read either (EISDIR here) is no
    one's sidebar, so it cannot hold flag sync for good."""
    running, store = world
    seed(store, False)
    (store / "acct-b" / "org-b" / "local_odd.json").mkdir()
    rewrite(store, 0, isArchived=True)
    for _ in range(2):
        result = running.run_once()
        assert result.state == "ok" and result.flags_held == 0
    assert flags(store) == (True,) * 3


def test_a_symlink_beside_the_org_folders_does_not_drop_the_account(world):
    running, store = world
    seed(store, False)
    assert running.run_once().state == "ok"
    closed = store.parent / "closed"
    closed.mkdir()
    os.chmod(closed, 0)
    (store / "acct-c" / "zz-link").symlink_to(closed / "inside")
    try:
        rewrite(store, 0, isArchived=True)
        result = running.run_once()
        assert result.flags_held == 0
        assert flags(store) == (True,) * 3
    finally:
        os.chmod(closed, 0o700)


def test_a_hold_at_publish_is_counted_and_named(world, monkeypatch):
    running, store = world
    seed(store, False)
    assert running.run_once().state == "ok"
    rewrite(store, 0, isStarred=True)
    install = mirror._install
    raced = {"once": True}

    def racing(temporary, destination, **kwargs):
        if raced["once"] and destination == path(store, 2) and kwargs.get("expect") is not None:
            raced["once"] = False
            temporary.unlink()
            return False
        return install(temporary, destination, **kwargs)

    monkeypatch.setattr(mirror, "_install", racing)
    result = running.run_once()
    assert result.flags_held == 1
    assert result.held_by == [{"path": f"session {SESSION}",
                               "reason": "a copy changed while the pass published"}]
