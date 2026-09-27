"""The mirror's settings rule, checked over every reachable state: C-23.28.

`tests/mirror_settings_model.py` is the executable twin of `decide_setting`
and `_settle_settings`. These tests explore it breadth-first for three
account folders and check the invariants the 2026-09-26 brief asks for:

* convergence: one pass with nothing written in between leaves every copy and
  the base on the value decided;
* idempotence: a converged state decides itself and writes nothing;
* newer is never overwritten by older: the mirror writes a value over a copy
  only if a copy holding that value is at least as active, or the value is a
  change no pass had seen (a pick or a move), or the copy has had no activity
  since the base was decided;
* no lost update, all or nothing, base agreement, cancellation safety, and a
  write keeps the copy's rank and mtime;
* never undo a settled value: with no pick and no activity since a clean
  publish, no pass decides anything else, even when the app re-saves a value
  from memory older than the mirror's write. This is what the flag protocol
  cannot promise (2026-09-25 report), and why the rule ranks by activity and
  novelty instead of taking any change from the merge base;
* intent wins: a single pick of a value no pass had seen is decided;
* activity wins: with no pick, the copy with the latest activity is decided.

With an app whose memory is current every property holds. With a stale memory
exactly one fails, "no stale resurrection": a turn (or respawn) that runs on
memory older than the mirror's write spreads what it ran, the known limit.
"""

from __future__ import annotations

import itertools
from dataclasses import replace

import pytest

from tests import mirror_settings_model as model


def settled_roots() -> list[model.State]:
    return [model.settled_root(3, v) for v in (0, 1)]


ON_TIME, PICK, RESAVE = "on-time", "pick", "re-save"


def first_root(value, rank, kinds) -> model.State:
    """A store before its first decision. Each copy was written at its
    activity (its value ran), or later: a pick that never ran, or a re-save
    from memory (focus, a PR poll) of a value that ran."""
    stamp = tuple(r if kind == ON_TIME else 2 for r, kind in zip(rank, kinds))
    ran = frozenset(v for v, kind in zip(value, kinds) if kind != PICK)
    picked = frozenset(v for v, kind in zip(value, kinds) if kind == PICK)
    return model.initial(value, rank, stamp, ran=ran, picked=picked)


def first_roots() -> list[model.State]:
    """Every store a first decision can meet in three folders: each value,
    rank 0 or 1, and each kind of last write."""
    return [first_root(value, rank, kinds)
            for value in itertools.product(model.VALUES, repeat=3)
            for rank in itertools.product((0, 1), repeat=3)
            for kinds in itertools.product((ON_TIME, PICK, RESAVE), repeat=3)]


@pytest.mark.parametrize("honest", [True, False])
def test_every_invariant_but_the_known_limit_holds_in_every_state(honest):
    """C-23.28: exhaustive over the states two app events and any number of
    loads and passes reach from a settled store."""
    states, broken = model.explore(settled_roots(), picked=True, honest=honest, limit=3)
    assert states > 10_000, "the exploration reached the whole space"
    expected = set() if honest else {"no-stale-resurrection"}
    assert set(broken) == expected, broken


def test_a_deeper_exploration_with_stale_memory_breaks_only_the_known_limit():
    """C-23.28: three app events deep, with stale memory, parked folders and
    every interleaving of passes."""
    states, broken = model.explore(settled_roots(), picked=True, honest=False, limit=4)
    assert states > 100_000
    assert set(broken) == {"no-stale-resurrection"}


def test_the_known_limit_is_a_turn_on_memory_older_than_the_mirrors_write():
    """C-23.28, the known limit's own shape: the user picks in one account,
    the mirror spreads it, and a folder the app still holds from before runs
    a turn on the value it remembers."""
    _states, broken = model.explore(settled_roots(), picked=True, honest=False, limit=3)
    trace = broken["no-stale-resurrection"]
    assert [step for step in trace if step.startswith("pick")] == [trace[1]]
    assert trace[-2].startswith("turn(") and trace[-1] == "pass_decide"
    assert trace.index("pass_publish") < trace.index(trace[-2]), \
        "the turn runs after the mirror wrote over what that folder remembers"


def test_a_stale_save_without_activity_never_undoes_a_settled_value():
    """C-23.28: the flag protocol's known limit, closed for settings. The
    loaded folder remembers 0; the mirror spreads the user's 1 from another
    folder; the app re-saves 0 there (focus, a PR poll). The rule keeps 1."""
    state = model.settled_root(3, 0)                      # the app holds folder 0: 0
    state = model.load(state, 1)
    state = model.pick(state, 1, limit=9)                  # the user picks 1 in folder 1
    state = model.pass_publish(model.pass_decide(state, picked=True), honest=False)
    assert state.value == (1, 1, 1) and state.settled == 1
    state = model.save(state, 0, limit=9)                  # folder 0's memory still says 0
    assert state.value == (0, 1, 1)
    decided = model.pass_decide(state, picked=True)
    assert decided.decided == 1
    assert model.pass_publish(decided, honest=False).value == (1, 1, 1)


def test_a_pick_nobody_saw_wins_over_a_more_active_copy():
    """C-23.28: `commitSessionModel` never raises lastActivityAt, so a pick in a
    folder that is not the most active must still win."""
    state = model.initial((0, 0, 0), (5, 1, 1), (5, 1, 1), loaded=1,
                          base=model.Base(0, 5, frozenset({0})))
    state = model.pick(state, 2, limit=9)
    decided = model.pass_decide(state, picked=True)
    assert decided.decided == 2
    assert model.pass_publish(decided, honest=True).value == (2, 2, 2)


def test_activity_decides_a_value_the_session_had_before():
    """C-23.28: the latest turn wins among values a pass has seen."""
    state = model.initial((0, 0, 0), (1, 1, 1), (1, 1, 1), loaded=2,
                          base=model.Base(0, 1, frozenset({0, 1})))
    state = model.pick(state, 1, limit=9)                  # back to a value it had
    assert model.pass_decide(state, picked=True).decided == 0, \
        "intended: a pick of an old value waits for the session's next activity"
    state = model.turn(state, 2, limit=9)
    assert model.pass_decide(state, picked=True).decided == 1


@pytest.mark.parametrize("honest", [True, False])
@pytest.mark.parametrize("picked", [True, False])
def test_every_first_decision_keeps_its_invariants(picked, honest):
    """C-23.28: one clean pass from each of the 5,832 first-decision stores."""
    for root in first_roots():
        decided = model.pass_decide(root, picked=picked)
        after = model.pass_publish(decided, honest=honest)
        broken = model.check_step(decided, "pass_publish", after, picked=picked,
                                  honest=honest)
        assert broken == [], (root, broken)
        assert set(after.value) == {decided.decided} and after.base.v == decided.decided


def test_a_first_decision_takes_a_pick_made_after_the_last_activity():
    """C-23.28, the rollout's shape (2026-09-26): 120 copies ran their last turn
    on one model, and one account picked another afterwards without a turn."""
    root = first_root((0, 0, 1), (1, 1, 0), (ON_TIME, ON_TIME, PICK))
    assert model.pass_decide(root, picked=True).decided == 1
    assert model.pass_decide(root, picked=False).decided == 0, \
        "a place moves with activity, so it takes the most active copy"
    shared = model.initial((1, 0, 1), (0, 1, 0), (0, 1, 2), ran=frozenset({0}))
    assert model.pass_decide(shared, picked=True).decided == 0, \
        "a value some copy held before the last activity is not a later pick"


def test_a_late_save_of_a_value_that_ran_never_beats_newer_activity():
    """C-23.28, review round 1 (PR #49): at a first decision a late write alone
    is no pick. The app re-saves the whole record on focus or a PR poll, so a
    folder can write an old value long after the session last ran elsewhere.
    A value that ran is not a pick; the transcript records what ran."""
    state = model.initial((1, 0, 0), (0, 1, 1), (0, 1, 1))     # 1 and 0 both ran
    state = model.save(state, 0, limit=10)                      # A re-saves 1 late
    decided = model.pass_decide(state, picked=True)
    assert decided.decided == 0, "B and C ran more recently"
    after = model.pass_publish(decided, honest=False)
    assert after.value == (0, 0, 0)
    assert model.check_step(decided, "pass_publish", after, picked=True, honest=False) == []
    unpicked = replace(state, ran=frozenset({0}))               # 1 never ran: a pick
    assert model.pass_decide(unpicked, picked=True).decided == 1


def test_the_first_decision_property_is_not_the_rule_restated():
    """C-23.28, review round 1: "newer never overwritten" judges a first decision
    by the ghost of which values picks produced, which the rule never reads.
    A rule that took every late value as a pick breaks it."""
    root = first_root((1, 0, 0), (0, 1, 1), (RESAVE, ON_TIME, ON_TIME))
    wrong = replace(model.pass_decide(root, picked=True), decided=1)
    after = model.pass_publish(wrong, honest=False)
    assert "newer-never-overwritten" in model.check_step(wrong, "pass_publish", after,
                                                         picked=True, honest=False)


@pytest.mark.parametrize("picked", [True, False])
def test_explorations_from_first_decisions_break_only_the_known_limit(picked):
    """C-23.28: every event and pass after a first decision, from a stratified
    sample of the first-decision stores: three values, a minority value in a
    less active or the most active folder, a tie, and every write-time shape.
    (Every one of the 1,728 stores gets the one-pass check above.)"""
    shapes = {(0, 1, 2), (0, 0, 1), (1, 0, 0)}
    ranks = {(1, 0, 0), (0, 1, 1)}
    kinds = {(ON_TIME, ON_TIME, PICK), (ON_TIME, ON_TIME, RESAVE), (RESAVE, ON_TIME, PICK),
             (PICK, RESAVE, ON_TIME), (ON_TIME, ON_TIME, ON_TIME)}
    roots = [first_root(value, rank, kind)
             for value in shapes for rank in ranks for kind in kinds]
    assert len(roots) == 30
    _states, broken = model.explore(roots, picked=picked, honest=False, limit=4)
    assert set(broken) <= {"no-stale-resurrection"}, broken


def test_intent_wins_is_exercised():
    """C-23.28: the guard for a user's pick fires on reachable decisions."""
    fired = 0
    seen = set(settled_roots())
    queue = list(seen)
    while queue:
        state = queue.pop()
        for label, nxt in model.successors(state, picked=True, honest=False, limit=3):
            if label == "pass_decide" and len(state.picks) == 1 and state.picks[0][1]:
                fired += 1
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    assert fired > 100


# --- why this rule: the two simpler ones, as mutants ------------------------------

def _explore_with(monkeypatch, decide, *, limit: int = 3) -> set[str]:
    monkeypatch.setattr(model, "decide", decide)
    _states, broken = model.explore(settled_roots(), picked=True, honest=False, limit=limit)
    return set(broken)


def test_a_merge_base_rule_spreads_a_stale_save(monkeypatch):
    """C-23.28: the flag protocol's rule (any change from the base wins) would
    spread a value the app re-saved from memory older than the mirror's write,
    with no activity at all."""
    def merge_base(snap, base, *, ran):
        values = {value for value, _r, _s in snap}
        if len(values) == 1:
            return snap[0][0]
        if base is None or base.v is None:
            return model._best(snap, list(range(len(snap))), lambda item: (item[1], item[2]))
        changed = [i for i, (value, _r, _s) in enumerate(snap) if value != base.v]
        return snap[changed[-1]][0] if changed else base.v

    assert "never-undo-settled" in _explore_with(monkeypatch, merge_base)


def test_a_newest_activity_rule_loses_a_users_pick(monkeypatch):
    """C-23.28: "the copy with the greatest lastActivityAt wins" alone would
    overwrite a pick made in any folder but the most active one."""
    def newest(snap, base, *, ran):
        return model._best(snap, list(range(len(snap))), lambda item: (item[1], item[2]))

    assert "intent-wins" in _explore_with(monkeypatch, newest)
