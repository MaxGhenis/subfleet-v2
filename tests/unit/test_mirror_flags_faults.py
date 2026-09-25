"""Flag sync when a copy cannot be read: C-23.28, review round 5.

A pass that decides a session without one of its copies, and advances the
merge base, leaves that copy's old value to read as a user's change on the
next pass, which then undoes the change it just spread. So a session is
decided from every copy or not at all: an unlisted folder's copies are read by
name from its last listing, a copy that exists and cannot be read holds its
session, and bases of sessions a pass did not see survive it.
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
    real = os.scandir

    def scandir(where="."):
        if any(str(where).endswith(folder) for folder in folders):
            raise PermissionError(1, "Operation not permitted", str(where))
        return real(where)

    patch.setattr(mirror.os, "scandir", scandir)


def unreadable(patch, target) -> None:
    real = mirror._read_entry

    def read(where):
        if str(where) == str(target):
            raise OSError(24, "Too many open files")
        return real(where)

    patch.setattr(mirror, "_read_entry", read)


def test_an_unlisted_folder_is_read_by_name_and_the_users_archive_stands(world, monkeypatch):
    running, store = world
    seed(store, False)
    assert running.run_once().state == "ok" and base(running) is False
    rewrite(store, 0, isArchived=True)                   # the user archives in A
    with monkeypatch.context() as patch:
        rewrite(store, 2, lastFocusedAt=7)               # the app saves C
        unlisting(patch, "acct-c/org-c")                  # C cannot be listed
        result = running.run_once()
    assert result.state == "ok" and result.flags_held == 0
    assert flags(store) == (True,) * 3, "C was read and written by name"
    assert running.run_once().state == "ok"
    assert flags(store) == (True,) * 3 and base(running) is True


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


def test_a_pass_that_lists_nothing_still_decides_by_name(world, monkeypatch):
    running, store = world
    seed(store, True)
    assert running.run_once().state == "ok" and base(running) is True
    rewrite(store, 0, isArchived=False)                  # the user unarchives in A
    with monkeypatch.context() as patch:
        patch.setattr(mirror.Mirror, "_sweep_due", lambda self: True)
        unlisting(patch, *(f"{account}/{org}" for account, org in FOLDERS))
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
    record it could not read for an empty one and replace the app's save."""
    running, store = world
    seed(store, False)
    assert running.run_once().state == "ok"
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
