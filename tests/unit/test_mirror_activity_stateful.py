"""The real mirror against both of its publish models at once: C-23.28.

A Hypothesis state machine drives the real `Mirror` on real files, with three
account folders holding one session, and two models in lockstep: the flag
protocol's (`tests/mirror_flags_model.py`) and the date's
(`tests/mirror_activity_model.py`). The two meet in one publish, so each is
told what the other adds to the batch: the flag model gets the copies the pass
decided to raise the date of (`also`) and those the pre-check found already
raised (`skip`); the date model gets the copies a flag is written to, and
decides nothing when the flag's decision is "archived".

The rules interleave user archive and unarchive in the loaded account, account
switches, turns of the session in a folder the app holds, the app's focus
rewrites, its stale re-saves (which write back the flag and the date it held),
full passes, passes with app writes between the read and the pre-check, passes
with app writes between two of the publish's writes (so a write fails and the
batch is put back), and passes cancelled before they publish. After every step
every file's flag and date and the merge base must equal the models'.
`test_mirror_flags_model.py` and `test_mirror_activity_model.py` check each
model's invariants over every reachable state, so this ties the implementation
to them, with both fields moving in one batch.
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (RuleBasedStateMachine, initialize, invariant, rule,
                                 run_state_machine_as_test)

from subfleet.sessions import mirror
from tests import mirror_activity_model as dates_model
from tests import mirror_flags_model as flags_model
from tests import sessions_fixtures as fx

SESSION = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
#: A second session, converged and never touched: it must never be written.
BYSTANDER = "7e7e7e7e-0000-4000-8000-000000000002"
FOLDERS = (("acct-a", "org-a"), ("acct-b", "org-b"), ("acct-c", "org-c"))
#: The model's dates are small numbers; the files hold them past this instant,
#: in the app's milliseconds, with the model's lag as the policy's.
EPOCH = 1_791_000_000_000
#: Turns a trace may take: more than the exhaustive exploration's two.
CLOCK_MAX = 10 * dates_model.JUMP
#: The passes' clock, after every date a trace can write: the models' dates
#: never pass their clock, and the mirror takes a date from its future for no voice.
AFTER = datetime.fromtimestamp(EPOCH / 1000, timezone.utc) + timedelta(days=30)
ENVIRONMENT = ("user_set_true", "user_set_false", "load_0", "load_1", "load_2",
               "focus_0", "focus_1", "focus_2", "stale_0", "stale_1", "stale_2",
               "turn_0", "turn_1", "turn_2")


def rewrite(path: Path, data: dict) -> None:
    """The app's own write: beside the file, then rename (never in place)."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(path)


class MirrorAgainstBothModels(RuleBasedStateMachine):
    def __init__(self, base: Path, monkeypatch):
        super().__init__()
        self.monkeypatch = monkeypatch
        home = fx.claude_home(base, monkeypatch)
        self.store = fx.desktop_store(base, monkeypatch)
        fx.transcript(home, SESSION, fx.completed())
        fx.transcript(home, BYSTANDER, fx.completed())
        self.root = base / "state"
        self.root.mkdir()
        ticks = itertools.count()
        policy = fx.policy(mirror_hot_interval_s=0,
                           mirror_activity_lag_s=dates_model.LAG / 1000)
        self.running = mirror.Mirror(
            self.root, policy, now=lambda: AFTER + timedelta(seconds=next(ticks)))
        self.options = mirror.options_from(policy)
        self.flags: flags_model.State | None = None
        self.dates: dates_model.State | None = None
        self.focus = itertools.count(1)
        self.bystander: list[bytes] = []

    # --- the files ---------------------------------------------------------------

    def path(self, account: int, session: str = SESSION) -> Path:
        name, org = FOLDERS[account]
        return self.store / name / org / f"local_{session}.json"

    def read(self, account: int) -> dict:
        return json.loads(self.path(account).read_text())

    def write(self, account: int, **fields) -> None:
        rewrite(self.path(account), {**self.read(account), **fields})

    # --- one environment action, to both models and to the files ------------------

    def apply(self, action: str) -> None:
        flags, dates = self.flags, self.dates
        kind, _sep, arg = action.rpartition("_")
        if action.startswith("user_set"):
            value = arg == "true"
            nxt = flags_model.user_set(flags, value)
            if nxt is not None:
                self.write(flags.loaded, isArchived=value)
                self.flags, self.dates = nxt, dates_model.focus(dates, flags.loaded)
        elif kind == "load":
            nxt = flags_model.load(flags, int(arg))
            if nxt is not None:
                self.flags, self.dates = nxt, dates_model.load(dates, int(arg))
        elif kind == "focus":
            account = int(arg)
            self.write(account, lastFocusedAt=next(self.focus))
            self.flags = flags_model.focus(flags, account)
            self.dates = dates_model.focus(dates, account)
        elif kind == "turn":
            account = int(arg)
            nxt = dates_model.turn(dates, account, clock_max=CLOCK_MAX)
            if nxt is not None:
                self.write(account, lastActivityAt=EPOCH + nxt.act[account])
                self.flags, self.dates = flags_model.focus(flags, account), nxt
        else:                                               # stale: memory, written whole
            account = int(arg)
            flag = flags_model.app_save(flags, account, stale=True)
            date = dates_model.app_save(dates, account, stale=True)
            if flag is None and date is None:
                return
            self.write(account, isArchived=flags.mem[account],
                       lastActivityAt=EPOCH + dates.mem[account])
            self.flags = flag if flag is not None else flags_model.focus(flags, account)
            self.dates = date if date is not None else dates_model.focus(dates, account)

    # --- one pass, in the models -----------------------------------------------------

    def decide(self) -> None:
        """Both decisions, each told what the other adds to the batch."""
        archived = flags_model.decide(self.flags.copy, self.flags.base)
        for_flag = {a for a, value in enumerate(self.flags.copy) if value != archived}
        dates = dates_model.pass_decide(self.dates, defer=archived, also=for_flag)
        raised = {a for a, goal in enumerate(dates.target) if goal is not None}
        self.flags, self.dates = flags_model.pass_decide(self.flags, also=raised), dates

    def check(self) -> None:
        flags, dates = self.flags, self.dates
        if flags_model.held(flags):                      # a flag moved: nothing is written
            self.flags, self.dates = flags_model.pass_check(flags), dates_model.cancel(dates)
            return
        skip = {a for a in flags.also if dates.act[a] >= dates.target[a]}
        self.flags, self.dates = flags_model.pass_check(flags, skip), dates_model.pass_check(dates)
        assert self.flags.pending == self.dates.pending, "one batch, in one order"

    def publishing(self) -> bool:
        assert (self.flags.phase == flags_model.PUBLISHING) == \
            (self.dates.phase == dates_model.PUBLISHING)
        return self.flags.phase == flags_model.PUBLISHING

    def write_one(self) -> None:
        self.flags = flags_model.pass_write(self.flags)
        self.dates = dates_model.pass_write(self.dates)

    def finish(self) -> None:
        while self.publishing():
            self.write_one()

    # --- rules -------------------------------------------------------------------------

    @initialize(value=st.booleans())
    def seed(self, value):
        for account, org in FOLDERS:
            fx.index_entry(self.store, account, org, SESSION, archived=value,
                           last_activity=EPOCH, settings={"ultracode": True})
            fx.index_entry(self.store, account, org, BYSTANDER, last_activity=EPOCH,
                           settings={"ultracode": True})
        self.flags = flags_model.initial(len(FOLDERS), value)
        self.dates = dates_model.initial(len(FOLDERS))
        self.bystander = [self.path(a, BYSTANDER).read_bytes() for a in range(len(FOLDERS))]

    @rule(action=st.sampled_from(ENVIRONMENT))
    def environment(self, action):
        self.apply(action)

    @rule()
    def full_pass(self):
        assert self.running.run_once(self.options).state == "ok"
        self.decide()
        self.check()
        self.finish()

    @rule(actions=st.lists(st.sampled_from(ENVIRONMENT), min_size=1, max_size=3))
    def pass_with_writes_between_read_and_publish(self, actions):
        sync = mirror.Mirror.sync_flags

        def interleave(engine, folder_files, *args, **kwargs):
            self.decide()
            for action in actions:
                self.apply(action)
            return sync(engine, folder_files, *args, **kwargs)

        with self.monkeypatch.context() as patch:
            patch.setattr(mirror.Mirror, "sync_flags", interleave)
            assert self.running.run_once(self.options).state == "ok"
        self.check()
        self.finish()

    @rule(before=st.lists(st.sampled_from(ENVIRONMENT), max_size=2),
          k=st.integers(min_value=0, max_value=2),
          actions=st.lists(st.sampled_from(ENVIRONMENT), min_size=1, max_size=3))
    def pass_with_writes_during_publish(self, before, k, actions):
        """App and user writes land just before the publish's k-th write."""
        for action in before:
            self.apply(action)
        self.decide()
        self.check()
        for _ in range(k):
            if self.publishing():
                self.write_one()
        reached = self.publishing()
        at_k = (self.flags, self.dates)
        install = mirror._install
        calls = itertools.count()
        applied = []

        def racing(temporary, destination, **kwargs):
            if kwargs.get("expect") is not None and next(calls) == k:
                applied.append(destination)
                self.flags, self.dates = at_k
                for action in actions:
                    self.apply(action)
            return install(temporary, destination, **kwargs)

        with self.monkeypatch.context() as patch:
            patch.setattr(mirror, "_install", racing)
            assert self.running.run_once(self.options).state == "ok"
        assert bool(applied) == reached, "the code and the models reach the same write"
        self.finish()

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
            assert running.run_once(self.options).state == "cancelled"
        running.cancel = None
        self.decide()
        self.flags = flags_model.cancel(self.flags)
        self.dates = dates_model.cancel(self.dates)

    # --- the check -----------------------------------------------------------------------

    @invariant()
    def files_and_base_match_both_models(self):
        if self.flags is None:
            return
        records = [self.read(a) for a in range(len(FOLDERS))]
        assert tuple(bool(item.get("isArchived")) for item in records) == self.flags.copy
        assert tuple(item["lastActivityAt"] - EPOCH for item in records) == self.dates.act
        base = (mirror._load(self.running.flags_path).get(SESSION) or {}).get("isArchived")
        assert (base if isinstance(base, bool) else None) == self.flags.base
        assert [self.path(a, BYSTANDER).read_bytes() for a in range(len(FOLDERS))] == \
            self.bystander, "a converged session is never rewritten"


class _Always:
    """A cancel event that is set."""

    def is_set(self) -> bool:
        return True


def machine(tmp_path, monkeypatch) -> MirrorAgainstBothModels:
    value = MirrorAgainstBothModels(tmp_path, monkeypatch)
    value.seed(False)
    return value


def test_the_date_and_the_flag_travel_in_one_batch_and_fail_together(tmp_path, monkeypatch):
    """C-23.28: the machine's own shape for the 2026-10-10 fix. A turn in A
    and an archive in A are one publish; a save of C before its write puts B
    back, flag and date; the next pass, deciding "archived", raises no date."""
    run = machine(tmp_path, monkeypatch)
    try:
        run.apply("turn_0")
        run.pass_with_writes_during_publish(["user_set_true"], 1, ["focus_2"])
        assert run.flags.copy == (True, False, False) and run.flags.base is None
        assert run.dates.act == (3, 0, 0)
        run.files_and_base_match_both_models()
        run.full_pass()
        assert run.flags.copy == (True,) * 3 and run.dates.act == (3, 0, 0)
        run.files_and_base_match_both_models()
        run.apply("user_set_false")
        run.full_pass()
        assert run.flags.copy == (False,) * 3 and run.dates.act == (3, 2, 2)
        run.files_and_base_match_both_models()
    finally:
        run.teardown()


def test_a_turn_between_the_read_and_the_publish_leaves_that_copy_out(tmp_path, monkeypatch):
    """C-23.28: the pre-check drops the copy the app raised itself; the flag
    model sees it as checked and not written."""
    run = machine(tmp_path, monkeypatch)
    try:
        run.apply("turn_0")
        run.apply("load_1")
        run.pass_with_writes_between_read_and_publish(["turn_1"])
        assert run.dates.act == (3, 6, 2)
        run.files_and_base_match_both_models()
        run.full_pass()
        assert run.dates.act == (5, 6, 5)
        run.files_and_base_match_both_models()
    finally:
        run.teardown()


@pytest.mark.parametrize("seed", [0])
def test_the_mirror_follows_both_models_on_random_interleavings(
        seed, tmp_path_factory, monkeypatch):
    """C-23.28: implementation and models agree step by step, so the invariants
    each model keeps in every reachable state hold for the mirror on every
    trace tried, with flags and dates in one batch."""
    run_state_machine_as_test(
        lambda: MirrorAgainstBothModels(tmp_path_factory.mktemp("trace"), monkeypatch),
        settings=settings(max_examples=150, stateful_step_count=30, deadline=None,
                          derandomize=True, database=None,
                          suppress_health_check=[HealthCheck.too_slow,
                                                 HealthCheck.function_scoped_fixture]))
