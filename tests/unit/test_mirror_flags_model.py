"""The mirror's flag protocol, checked over every reachable state: C-23.28.

`tests/mirror_flags_model.py` is the executable twin of
`docs/formal/MirrorFlags.tla`. These tests explore it breadth-first, every
state and every action from every state, for three account folders, and check
the invariants the 2026-09-25 consistency brief asks for:

* convergence: one uninterrupted pass leaves every copy and the base equal;
* change wins, both ways: a change from the base (archive or unarchive) is decided;
* idempotence: a converged state with its base decides itself, and writes nothing;
* cancellation safety: a cancelled pass changes no copy and no base;
* no lost update: the mirror writes only copies still as it read them;
* all or nothing: a held batch writes nothing and keeps the base;
* base agreement: after a publish that went through, the base is the value decided;
* never undo a settled value (the brief's "no resurrection", both directions).

With an app whose saves never change the flag, all of them hold in every
state. With an app that can re-save a stale value (the known limit in the
2026-09-24 report), "never undo a settled value" fails, and the checker's
shortest counterexample is the incident shape: the mirror spreads the stale
re-save to every account.
"""

from __future__ import annotations

import pytest

from tests import mirror_flags_model as model


def test_every_invariant_holds_in_every_state_for_an_app_that_saves_what_it_shows():
    """C-23.28: exhaustive over all reachable states of three account folders."""
    states, broken = model.explore(3, stale=False)
    assert states > 1_000, "the exploration reached the whole space"
    assert broken == {}


def test_a_stale_resave_is_the_one_way_to_undo_a_settled_value():
    """C-23.28, the known limit, found and shown by the checker: only the app
    re-saving a value it held from before the mirror's write breaks an
    invariant, and the invariant it breaks is "never undo a settled value"."""
    _states, broken = model.explore(3, stale=True)
    assert set(broken) == {"never-undo-settled"}
    trace = broken["never-undo-settled"]
    assert any(step.startswith("app_save") for step in trace)
    assert trace[-1] == "pass_publish"


@pytest.mark.parametrize("base", [False, True])
def test_a_single_accounts_change_wins_in_both_directions(base):
    """C-23.28: from a settled state, one account's archive or unarchive is decided."""
    state = model.State(copy=(base, base, base), base=base, loaded=1,
                        mem=(None, base, None), settled=base)
    state = model.user_set(state, not base)
    after = model.pass_publish(model.pass_decide(state))
    assert after.copy == (not base,) * 3 and after.base == (not base)


def test_a_converged_pass_writes_nothing():
    """C-23.28: idempotence."""
    state = model.State(copy=(True, True, True), base=True, loaded=0,
                        mem=(True, None, None), settled=True)
    after = model.pass_publish(model.pass_decide(state))
    assert after.copy == state.copy and after.base == state.base


def test_a_change_after_the_snapshot_holds_the_batch():
    """C-23.28: all or nothing; the user's newer value is never overwritten."""
    state = model.State(copy=(False, False, False), base=False, loaded=0,
                        mem=(False, None, None), settled=False)
    state = model.user_set(state, True)                 # A archives
    decided = model.pass_decide(state)                   # pass reads {T, F, F}
    moved = model.load(decided, 1)
    moved = model.user_set(moved, True)                  # B archives before publish
    after = model.pass_publish(moved)
    assert after.copy == (True, True, False), "held: nothing written"
    assert after.base is False
    final = model.pass_publish(model.pass_decide(after))
    assert final.copy == (True,) * 3 and final.base is True
