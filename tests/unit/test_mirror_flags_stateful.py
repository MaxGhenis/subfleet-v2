"""The real mirror against its flag protocol, on random interleavings: C-23.28.

A Hypothesis state machine drives the real `Mirror` on real files, with three
account folders holding one session, and the model in
`tests/mirror_flags_model.py` in lockstep. The rules interleave user archive
and unarchive in the loaded account, account switches, the app's focus
rewrites (a save that keeps the flag), the app's stale re-saves (a save that
puts back a value it held), full passes, passes with app writes landing
between the read and the pre-check, passes with app writes landing between
two of the publish's writes (so a write fails and the rollback runs), passes
that cannot list a folder or read a copy, and passes cancelled before they
publish. After every step every file's flag and
the merge base must equal the model's. `test_mirror_flags_model.py` checks
the model's invariants over every reachable state, so this ties the
implementation to them; the same model is `docs/formal/MirrorFlags.tla`, which
TLC has not been run on.
"""

from __future__ import annotations

import itertools
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (RuleBasedStateMachine, initialize, invariant, precondition,
                                 rule, run_state_machine_as_test)

from subfleet.sessions import mirror
from tests import mirror_flags_model as model
from tests import sessions_fixtures as fx

SESSION = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
#: A second session, converged and never touched: a hold that should be
#: SESSION's alone must not take it too.
BYSTANDER = "7e7e7e7e-0000-4000-8000-000000000002"
FOLDERS = (("acct-a", "org-a"), ("acct-b", "org-b"), ("acct-c", "org-c"))
ENVIRONMENT = ("user_set_true", "user_set_false", "load_0", "load_1", "load_2",
               "focus_0", "focus_1", "focus_2", "stale_0", "stale_1", "stale_2")


def rewrite(path: Path, data: dict) -> None:
    """The app's own write: beside the file, then rename (never in place)."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(path)


class MirrorAgainstModel(RuleBasedStateMachine):
    def __init__(self, base: Path, monkeypatch):
        super().__init__()
        self.monkeypatch = monkeypatch
        home = fx.claude_home(base, monkeypatch)
        self.store = fx.desktop_store(base, monkeypatch)
        fx.transcript(home, SESSION, fx.completed())
        self.root = base / "state"
        self.root.mkdir()
        ticks = itertools.count()
        self.running = mirror.Mirror(
            self.root, fx.policy(), now=lambda: fx.NOW + timedelta(seconds=next(ticks)))
        self.state: model.State | None = None
        self.focus = itertools.count(1)
        #: Folders some earlier pass listed (the code reads an unlisted
        #: folder's copies by name from its last listing).
        self.listed: set[int] = set()

    # --- the two worlds -------------------------------------------------------

    def path(self, account: int) -> Path:
        name, org = FOLDERS[account]
        return self.store / name / org / f"local_{SESSION}.json"

    def real_copy(self) -> tuple[bool, ...]:
        return tuple(bool(json.loads(self.path(a).read_text()).get("isArchived"))
                     for a in range(len(FOLDERS)))

    def real_base(self) -> bool | None:
        flags = mirror._load(self.running.flags_path).get(SESSION) or {}
        value = flags.get("isArchived")
        return value if isinstance(value, bool) else None

    def set_flag(self, account: int, value: bool) -> None:
        path = self.path(account)
        rewrite(path, {**json.loads(path.read_text()), "isArchived": value})

    def apply(self, action: str) -> None:
        """One environment action, to the model and to the files alike."""
        state = self.state
        kind, _sep, arg = action.rpartition("_")
        if action.startswith("user_set"):
            value = arg == "true"
            nxt = model.user_set(state, value)
            if nxt is not None:
                self.set_flag(state.loaded, value)
        elif kind == "load":
            nxt = model.load(state, int(arg))
        elif kind == "focus":
            path = self.path(int(arg))
            rewrite(path, {**json.loads(path.read_text()), "lastFocusedAt": next(self.focus)})
            nxt = model.focus(state, int(arg))
        else:                                               # stale
            account = int(arg)
            nxt = model.app_save(state, account, stale=True)
            if nxt is not None:
                self.set_flag(account, state.mem[account])
        if nxt is not None:
            self.state = nxt

    # --- rules ----------------------------------------------------------------

    @initialize(value=st.booleans())
    def seed(self, value):
        for account, org in FOLDERS:
            fx.index_entry(self.store, account, org, SESSION, archived=value,
                           settings={"ultracode": True})
            fx.index_entry(self.store, account, org, BYSTANDER, settings={"ultracode": True})
        self.state = model.initial(len(FOLDERS), value)

    @rule(action=st.sampled_from(ENVIRONMENT))
    def environment(self, action):
        self.apply(action)

    def flip(self) -> None:
        """The user changes the flag in the loaded account, so the copies disagree."""
        here = self.state.loaded
        self.apply("user_set_false" if self.state.copy[here] else "user_set_true")

    @rule()
    def user_flip(self):
        self.flip()

    @rule()
    def full_pass(self):
        assert self.running.run_once().state == "ok"
        self.state = model.pass_publish(model.pass_decide(self.state))
        self.listed.update(range(len(FOLDERS)))

    @rule(actions=st.lists(st.sampled_from(ENVIRONMENT), min_size=1, max_size=3))
    def pass_with_writes_between_read_and_publish(self, actions):
        decided = model.pass_decide(self.state)
        sync = mirror.Mirror.sync_flags

        def interleave(engine, folder_files, *args, **kwargs):
            self.state = decided
            for action in actions:
                self.apply(action)
            return sync(engine, folder_files, *args, **kwargs)

        with self.monkeypatch.context() as patch:
            patch.setattr(mirror.Mirror, "sync_flags", interleave)
            assert self.running.run_once().state == "ok"
        self.state = model.pass_publish(self.state)
        self.listed.update(range(len(FOLDERS)))

    @rule(flip=st.booleans(), k=st.integers(min_value=0, max_value=2),
          actions=st.lists(st.sampled_from(ENVIRONMENT), min_size=1, max_size=3))
    def pass_with_writes_during_publish(self, flip, k, actions):
        """App and user writes land just before the publish's k-th write (after
        a user's flip, so that there is something to publish)."""
        if flip:
            self.flip()
        state = model.pass_check(model.pass_decide(self.state))
        for _ in range(k):
            if state.phase != model.PUBLISHING:
                break
            state = model.pass_write(state)
        reached = state.phase == model.PUBLISHING
        at_k = state
        install = mirror._install
        calls = itertools.count()
        applied = []

        def racing(temporary, destination, **kwargs):
            if kwargs.get("expect") is not None and next(calls) == k:
                applied.append(destination)
                self.state = at_k
                for action in actions:
                    self.apply(action)
            return install(temporary, destination, **kwargs)

        with self.monkeypatch.context() as patch:
            patch.setattr(mirror, "_install", racing)
            assert self.running.run_once().state == "ok"
        assert bool(applied) == reached, "the code and the model reach the same write"
        if reached:
            state = self.state
            while state.phase == model.PUBLISHING:
                state = model.pass_write(state)
        self.state = state
        self.listed.update(range(len(FOLDERS)))

    @rule(account=st.integers(min_value=0, max_value=2),
          mode=st.sampled_from(("unlisted", "unreadable", "both")))
    def pass_that_cannot_read_a_copy(self, account, mode):
        """A folder that cannot be listed, a copy that cannot be read (EMFILE),
        or both (review rounds 5 and 6). An unlisted folder unchanged since
        its last listing is read by name; one that changed, or was never
        listed, holds every session; an unreadable copy holds its own. The
        model sees a held session as a cancelled pass."""
        target = self.path(account)
        folder = target.parent
        if mode != "unlisted":
            self.apply(f"focus_{account}")          # so the pass must read it again
        known = self.running._folders.get(folder)
        info = os.stat(folder)
        unchanged = known is not None and known.signature == (
            info.st_dev, info.st_ino, info.st_mtime_ns)
        scandir, read = os.scandir, mirror._read_entry

        def unlisting(where="."):
            if str(where) == str(folder):
                raise OSError(24, "Too many open files", str(where))
            return scandir(where)

        def unreadable(where):
            if str(where) == str(target):
                raise OSError(24, "Too many open files")
            return read(where)

        with self.monkeypatch.context() as patch:
            if mode != "unreadable":
                patch.setattr(mirror.os, "scandir", unlisting)
            if mode != "unlisted":
                patch.setattr(mirror, "_read_entry", unreadable)
            result = self.running.run_once()
        assert result.state == "ok"
        if mode == "unlisted" and unchanged:
            self.state = model.pass_publish(model.pass_decide(self.state))
            assert result.flags_held == 0
        else:
            self.state = model.cancel(model.pass_decide(self.state))
            # A folder whose contents are unknown, or a copy never read before
            # (whose session nobody can name), holds both sessions.
            everything = mode != "unreadable" or account not in self.listed
            assert result.flags_held == (2 if everything else 1)
        self.listed.update(a for a in range(len(FOLDERS)) if a != account or mode == "unreadable")

    @rule()
    def pass_cancelled_before_publish(self):
        checkpoint = mirror.Mirror._checkpoint
        running = self.running

        def cancel_at_publish(engine, current, stage=None):
            if stage == "publishing flags":
                running.cancel = _Always()
            return checkpoint(engine, current, stage)

        with self.monkeypatch.context() as patch:
            patch.setattr(mirror.Mirror, "_checkpoint", cancel_at_publish)
            assert running.run_once().state == "cancelled"
        running.cancel = None
        self.state = model.cancel(model.pass_decide(self.state))
        self.listed.update(range(len(FOLDERS)))

    # --- the check --------------------------------------------------------------

    @invariant()
    def files_and_base_match_the_model(self):
        if self.state is None:
            return
        assert self.real_copy() == self.state.copy
        assert self.real_base() == self.state.base


class _Always:
    """A cancel event that is set."""

    def is_set(self) -> bool:
        return True


@pytest.mark.parametrize("seed", [0])
def test_the_mirror_follows_its_flag_protocol_on_random_interleavings(
        seed, tmp_path_factory, monkeypatch):
    """C-23.28: implementation and model agree step by step, so the model's
    exhaustively checked invariants hold for the mirror on every trace tried."""
    run_state_machine_as_test(
        lambda: MirrorAgainstModel(tmp_path_factory.mktemp("trace"), monkeypatch),
        settings=settings(max_examples=100, stateful_step_count=30, deadline=None,
                          derandomize=True, database=None,
                          suppress_health_check=[HealthCheck.too_slow,
                                                 HealthCheck.function_scoped_fixture]))
