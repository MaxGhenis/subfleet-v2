"""The mirror's flag protocol, checked over every reachable state: C-23.28.

`tests/mirror_flags_model.py` is the executable twin of
`docs/formal/MirrorFlags.tla`. These tests explore it breadth-first, every
state and every action from every state, for three account folders, and check
the invariants the 2026-09-25 consistency brief asks for:

* convergence: one pass with nothing written in between leaves every copy and
  the base equal to the value decided;
* change wins, both ways: a change from the base (archive or unarchive) is decided;
* idempotence: a converged state with its base decides itself, and writes nothing;
* cancellation safety: a cancelled pass changes no copy and no base;
* no lost update: the mirror writes only a copy nobody rewrote since it last
  checked or wrote it;
* all or nothing: a held session writes nothing, and a failed write puts back
  every copy the publish wrote (except one rewritten since), keeping the base;
* base agreement: after a publish that went through, the base is the value decided;
* intent wins: with a base, a pass decides what the user last set since the
  last publish that converged (the brief's "no resurrection" for a user's
  change);
* never undo a settled value (the same, for a value every copy agreed on).

With an app whose saves never change the flag, all of them hold in every
state. With an app that can re-save a stale value (the known limit in the
2026-09-24 report), exactly the last two fail.
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
    """C-23.28, the known limit, found by the checker: only the app re-saving
    a value it held from before the mirror's write breaks an invariant, and
    the invariant it breaks is "never undo a settled value"."""
    _states, broken = model.explore(3, stale=True)
    assert set(broken) == {"never-undo-settled", "intent-wins"}
    trace = broken["never-undo-settled"]
    # From the unsynced start the shortest case is the bootstrap rule: with no
    # base, archived-anywhere overrides the user's unarchive, and the app's
    # memory of that unarchive then comes back and spreads.
    assert trace[0] == "user_set(False)" and "app_save(0)" in trace
    assert trace[-1] == "pass_write"


def test_from_a_settled_state_a_parked_accounts_stale_save_undoes_the_users_change():
    """C-23.28: the known limit's own shape. The user archives in the loaded
    account and the mirror settles it everywhere; a folder the app still
    holds from before (a parked session) saves its old value, and the next
    pass spreads that to every account."""
    roots = [model.State(copy=(v,) * 3, base=v, loaded=0, mem=(v, None, None), settled=v)
             for v in (False, True)]
    _states, broken = model.explore(3, stale=True, roots=roots)
    assert set(broken) == {"never-undo-settled", "intent-wins"}
    trace = broken["never-undo-settled"]
    acted = [step for step in trace if step.startswith("user_set")]
    saved = [step for step in trace if step.startswith("app_save")]
    assert len(acted) == 1 and len(saved) == 1
    assert trace.index(acted[0]) < trace.index(saved[0])
    assert saved[0] == "app_save(0)" and trace[0] == "load(1)", \
        "the user acts in account 1; account 0's memory predates the mirror's write"


def test_honest_exploration_from_settled_states_finds_nothing():
    """C-23.28: the same roots with an honest app break nothing."""
    roots = [model.State(copy=(v,) * 3, base=v, loaded=0, mem=(v, None, None), settled=v)
             for v in (False, True)]
    _states, broken = model.explore(3, stale=False, roots=roots)
    assert broken == {}


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


def test_a_write_that_finds_its_copy_rewritten_puts_back_the_copies_before_it():
    """C-23.28: all or nothing across the publish. The pass writes B, then
    finds C rewritten since its check: B is put back and the base is kept."""
    state = model.State(copy=(False, False, False), base=False, loaded=0,
                        mem=(False, None, None), settled=False)
    state = model.user_set(state, True)                 # A archives
    state = model.pass_check(model.pass_decide(state))   # publish B and C
    assert state.phase == model.PUBLISHING and state.pending == (1, 2)
    state = model.pass_write(state)                       # B written
    assert state.copy == (True, True, False)
    state = model.focus(state, 2)                         # the app saves C
    after = model.pass_write(state)
    assert after.copy == (True, False, False) and after.base is False
    final = model.pass_publish(model.pass_decide(after))
    assert final.copy == (True,) * 3 and final.base is True


def test_a_rollback_skips_a_copy_rewritten_after_the_mirrors_write():
    """C-23.28: no lost update in the rollback either."""
    state = model.State(copy=(False, False, False), base=False, loaded=0,
                        mem=(False, None, None), settled=False)
    state = model.user_set(state, True)
    state = model.pass_write(model.pass_check(model.pass_decide(state)))
    state = model.focus(model.focus(state, 1), 2)         # the app saves B, then C
    after = model.pass_write(state)
    assert after.copy == (True, True, False), "B keeps what the app saved over it"
    assert after.base is False


def test_intent_wins_is_exercised_for_an_honest_app():
    """C-23.28, review round 5: never-undo-settled cannot fire for an honest
    app (a user action clears `settled`), so the user's change is guarded by
    intent-wins, and that guard fires on reachable decisions."""
    fired = 0
    seen = {model.initial(3, value) for value in (False, True)}
    queue = list(seen)
    while queue:
        state = queue.pop()
        for label, nxt in model.successors(state, stale=False):
            if label == "pass_decide" and isinstance(state.base, bool) \
                    and isinstance(state.intent, bool):
                fired += 1
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    assert fired > 100


def test_a_pass_that_skips_a_copy_would_undo_the_users_change():
    """C-23.28, review round 5: the fault the code now refuses (deciding and
    advancing the base without one copy) breaks intent-wins in the model."""
    state = model.State(copy=(False,) * 3, base=False, loaded=0,
                        mem=(False, None, None), settled=False)
    state = model.user_set(state, True)                   # A archives
    state = model.focus(state, 2)                         # (idle: no effect)
    skipped = model.State(copy=state.copy[:2], base=state.base, loaded=0,
                          mem=state.mem[:2], intent=state.intent)
    partial = model.pass_publish(model.pass_decide(skipped))   # decides without C
    after = model.State(copy=partial.copy + (state.copy[2],), base=partial.base, loaded=0,
                        mem=state.mem, intent=state.intent)
    decided = model.pass_decide(after)
    assert decided.decided is False, "C's old value reads as a change"
    assert "intent-wins" in model.check_step(after, "pass_decide", decided)
