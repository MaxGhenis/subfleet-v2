"""The real mirror against its settings rule, on random interleavings: C-23.28.

A Hypothesis state machine drives the real `Mirror` on real files, with three
account folders holding one session whose copies start with random models,
activity and file times, and the model in `tests/mirror_settings_model.py`
in lockstep. The rules interleave the app's picks in the loaded folder,
turns and saves in any folder it holds (the loaded one and parked ones,
including saves of a memory older than the mirror's write), account
switches, full passes, passes with app writes landing between the read and
the publish, passes cancelled before they publish, and passes that cannot
read a copy. After every step every file's model and `lastActivityAt`, its
mtime, and the settings base must equal the model's. The model's invariants
are checked over every reachable state by `test_mirror_settings_model.py`,
so this ties the implementation to them.

Write-level races inside a publish (a save between two of its writes, and the
rollback) are shared with flag sync and held to `mirror_flags_model.py` by
`test_mirror_flags_stateful.py`; `test_sessions_mirror_settings.py` covers
them for a settings write.
"""

from __future__ import annotations

import itertools
import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule, \
    run_state_machine_as_test

from subfleet.sessions import mirror
from tests import mirror_settings_model as model
from tests import sessions_fixtures as fx

SESSION = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
FOLDERS = (("acct-a", "org-a"), ("acct-b", "org-b"), ("acct-c", "org-c"))
#: The app's clock: tick t is T0 + t * STEP ms. A file the app writes at tick
#: t carries mtime one second later, as a save follows the frame that raised
#: `lastActivityAt`; `mirror.SETTLE_MS` (60 s) sits between the two.
T0 = 1_790_000_000_000
STEP = 1_000_000
LIMIT = 10_000
EVENTS = ("pick_0", "pick_1", "pick_2", "load_0", "load_1", "load_2",
          "turn_0", "turn_1", "turn_2", "save_0", "save_1", "save_2")


def name_of(value: int) -> str:
    return f"claude-model-{value}"


def digest_of(value: int) -> str:
    return mirror._unit_digest({"model": name_of(value)})


def rank_ms(tick: int) -> int:
    return T0 + tick * STEP


def mtime_s(tick: int) -> float:
    return (T0 + tick * STEP) / 1000 + 1


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

    # --- the two worlds -------------------------------------------------------

    def path(self, account: int) -> Path:
        name, org = FOLDERS[account]
        return self.store / name / org / f"local_{SESSION}.json"

    def write(self, account: int, value: int, rank: int, stamp: int) -> None:
        """The app's own write: beside the file, then rename, then its mtime."""
        target = self.path(account)
        data = json.loads(target.read_text()) if target.exists() else {}
        data.update({"model": name_of(value), "lastActivityAt": rank_ms(rank)})
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        os.utime(temporary, (mtime_s(stamp), mtime_s(stamp)))
        temporary.replace(target)

    def sync_files(self, before: model.State, after: model.State) -> None:
        for a in range(len(FOLDERS)):
            if (before.value[a], before.rank[a], before.stamp[a]) != \
                    (after.value[a], after.rank[a], after.stamp[a]):
                self.write(a, after.value[a], after.rank[a], after.stamp[a])

    def apply(self, event: str) -> None:
        kind, _sep, arg = event.partition("_")
        state = self.state
        step = {"pick": lambda s: model.pick(s, int(arg), limit=LIMIT),
                "load": lambda s: model.load(s, int(arg)),
                "turn": lambda s: model.turn(s, int(arg), limit=LIMIT),
                "save": lambda s: model.save(s, int(arg), limit=LIMIT)}[kind]
        nxt = step(state)
        if nxt is not None:
            self.sync_files(state, nxt)
            self.state = nxt

    def base(self) -> dict:
        return (mirror._load(self.running.flags_path).get(SESSION) or {}).get("settings") or {}

    # --- rules ----------------------------------------------------------------

    @initialize(value=st.tuples(*[st.sampled_from(model.VALUES)] * 3),
                rank=st.tuples(*[st.sampled_from((0, 1))] * 3),
                late=st.tuples(*[st.booleans()] * 3))
    def seed(self, value, rank, late):
        stamp = tuple(2 if after else r for r, after in zip(rank, late))
        for a, (account, org) in enumerate(FOLDERS):
            fx.index_entry(self.store, account, org, SESSION, settings={"ultracode": True})
            self.write(a, value[a], rank[a], stamp[a])
        self.state = model.initial(value, rank, stamp)

    @rule(event=st.sampled_from(EVENTS))
    def app(self, event):
        self.apply(event)

    @rule()
    def full_pass(self):
        assert self.running.run_once().state == "ok"
        self.state = model.pass_publish(model.pass_decide(self.state, picked=True),
                                        honest=False)

    @rule(events=st.lists(st.sampled_from(EVENTS), min_size=1, max_size=3))
    def pass_with_writes_between_read_and_publish(self, events):
        decided = model.pass_decide(self.state, picked=True)
        sync = mirror.Mirror.sync_flags

        def interleave(engine, folder_files, *args, **kwargs):
            self.state = decided
            for event in events:
                self.apply(event)
            return sync(engine, folder_files, *args, **kwargs)

        with self.monkeypatch.context() as patch:
            patch.setattr(mirror.Mirror, "sync_flags", interleave)
            assert self.running.run_once().state == "ok"
        self.state = model.pass_publish(self.state, honest=False)

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
        self.state = model.cancel(model.pass_decide(self.state, picked=True))

    @rule(account=st.integers(min_value=0, max_value=2))
    def pass_that_cannot_read_a_copy(self, account):
        """A copy that exists and cannot be read (EMFILE) holds its session:
        nothing is decided or written, and the base stands (review round 5)."""
        target = self.path(account)
        data = target.read_text()
        stamp = os.stat(target).st_mtime
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(data, encoding="utf-8")        # new inode: re-read
        os.utime(temporary, (stamp, stamp))
        temporary.replace(target)
        read = mirror._read_entry

        def unreadable(where):
            if str(where) == str(target):
                raise OSError(24, "Too many open files")
            return read(where)

        with self.monkeypatch.context() as patch:
            patch.setattr(mirror, "_read_entry", unreadable)
            result = self.running.run_once()
        assert result.state == "ok" and result.flags_held >= 1
        self.state = model.cancel(model.pass_decide(self.state, picked=True))

    # --- the check --------------------------------------------------------------

    @invariant()
    def files_and_base_match_the_model(self):
        state = self.state
        if state is None:
            return
        for a in range(len(FOLDERS)):
            data = json.loads(self.path(a).read_text())
            assert data["model"] == name_of(state.value[a]), a
            assert data["lastActivityAt"] == rank_ms(state.rank[a]), a
            assert os.stat(self.path(a)).st_mtime == mtime_s(state.stamp[a]), a
        recorded = self.base()
        unit = (recorded.get("units") or {}).get("model") or {}
        if state.base is None:
            assert not unit
            return
        assert set(unit.get("seen") or ()) == {digest_of(v) for v in state.base.seen}
        if state.base.v is None:
            assert "v" not in unit
        else:
            assert unit["v"] == digest_of(state.base.v)
            assert unit["value"] == {"model": name_of(state.base.v)}
            assert recorded["rank"] == rank_ms(state.base.rank)


class _Always:
    def is_set(self) -> bool:
        return True


@pytest.mark.parametrize("seed", [0])
def test_the_mirror_follows_its_settings_rule_on_random_interleavings(
        seed, tmp_path_factory, monkeypatch):
    """C-23.28: implementation and model agree step by step, so the model's
    exhaustively checked invariants hold for the mirror on every trace tried."""
    run_state_machine_as_test(
        lambda: MirrorAgainstModel(tmp_path_factory.mktemp("trace"), monkeypatch),
        settings=settings(max_examples=100, stateful_step_count=30, deadline=None,
                          derandomize=True, database=None,
                          suppress_health_check=[HealthCheck.too_slow,
                                                 HealthCheck.function_scoped_fixture]))
