"""The mirror's flag protocol, checked over every reachable state: C-23.28.

`tests/mirror_flags_model.py` is the executable twin of
`docs/formal/MirrorFlags.tla`. These tests explore it breadth-first, every
state and every action from every state, for three account folders, and check
the invariants the 2026-09-25 consistency brief asks for:

* convergence: one pass with nothing written in between leaves every copy, the
  base and every copy's reference equal to the value decided;
* change wins, both ways: a change from a copy's reference (archive or
  unarchive) is decided;
* idempotence: a converged state with its base decides itself, and writes nothing;
* cancellation safety: a cancelled pass changes no copy, no base and no reference;
* no lost update: the mirror writes only a copy nobody rewrote since it last
  checked or wrote it;
* all or nothing: a held session writes nothing and records nothing, a failed
  write puts back every copy the publish wrote (except one rewritten since),
  and a publish every write of which was put back keeps the base and every
  reference;
* base agreement: after a publish that went through, the base and every
  reference are the value decided;
* intent wins: with a base, if the user set only one value since the last
  publish that converged, a pass decides that value (a user who set both
  values since is exempt);
* never undo a settled value (the same, for a value every copy agreed on);
* the mirror's own writes do not vote (review round 5, finding F2): a copy
  whose file is the mirror's own last write holds its reference;
* latest wins (causal): with a base, a pass decides the value of the user's
  latest action unless a value they had not seen stands against it.

The rule the code implements (`refs`) keeps all of them for an app that saves
what it shows, with or without the faults F2 is about: a put-back write that
fails, a merge-base write that fails, and a crash anywhere in the publish.
The rule before it (`base`) and the design first proposed for F2 (`mine`) do
not, and the tests below keep their counterexamples. With an app that can
re-save a stale value (the known limit in the 2026-09-24 report), exactly
"intent wins" and "never undo a settled value" fail, as before.

The causal ghost behind "latest wins" makes the space too large to explore in
full (tens of gigabytes, 2026-09-29), so it is explored to a state limit.
"""

from __future__ import annotations

import pytest

from tests import mirror_flags_model as model

#: How far the causal ghost is explored: every state within the depth this
#: many states reach (about 0.35 GB and ten seconds).
CAUSAL_LIMIT = 150_000


@pytest.mark.parametrize("faults", [False, True], ids=["no-faults", "faults"])
def test_every_invariant_holds_in_every_state_for_an_app_that_saves_what_it_shows(faults):
    """C-23.28: exhaustive over all reachable states of three account folders,
    including put-back writes that fail, base writes that fail and crashes."""
    states, broken = model.explore(3, stale=False, faults=faults)
    assert states > 100_000, "the exploration reached the whole space"
    assert broken == {}


def test_latest_wins_holds_to_a_bound_with_faults():
    """C-23.28: the causal ghost, explored to a state limit with every fault."""
    with model.causal():
        states, broken = model.explore(3, stale=False, faults=True, limit=CAUSAL_LIMIT)
    assert states == CAUSAL_LIMIT
    assert broken == {}


def test_a_stale_resave_is_the_one_way_to_undo_a_settled_value():
    """C-23.28, the known limit, found by the checker: only the app re-saving
    a value it held from before the mirror's write breaks an invariant, and
    the invariants it breaks are "never undo a settled value" and "intent wins"."""
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
    """C-23.28: the same roots with an honest app break nothing, faults or not."""
    roots = [model.State(copy=(v,) * 3, base=v, loaded=0, mem=(v, None, None), settled=v)
             for v in (False, True)]
    _states, broken = model.explore(3, stale=False, faults=True, roots=roots)
    assert broken == {}


# --- F2: the mirror's own writes voting after a failure (review round 5) ----------

def broken_along(rule: str, labels: tuple[str, ...], *, faults: bool,
                 start: bool = False) -> set[str]:
    """Every property the steps break, from the unsynced start, under `rule`."""
    with model.causal():
        steps = model.replay(model.initial(3, start, rule), labels, faults=faults)
    return {name for before, label, after in steps
            for name in model.check_step(before, label, after)}


#: The user archives; the publish writes one copy and the process dies; the
#: user unarchives in the same account.
CRASH_THEN_REVERT = ("pass_decide", "user_set(True)", "pass_check", "pass_decide",
                     "user_set(False)", "pass_check", "pass_write", "crash",
                     "pass_recover", "pass_decide")
#: The user archives in account 0; the publish writes account 1 and the
#: process dies; the user, who saw the archive in account 1, unarchives there.
CROSS_ACCOUNT_REVERT = ("pass_decide", "user_set(True)", "load(1)", "pass_check",
                        "pass_decide", "pass_check", "pass_write", "user_set(False)",
                        "crash", "pass_recover", "pass_decide")
#: No fault at all: the publish writes account 1, the app saves 1 and 2, so
#: the write to 2 fails and the put-back skips 1; the user, who saw the
#: archive in account 1, unarchives there.
ROLLBACK_SKIP_REVERT = ("pass_decide", "user_set(True)", "load(1)", "pass_check",
                        "pass_decide", "pass_check", "focus(2)", "pass_write",
                        "user_set(False)", "pass_write", "rollback_write", "pass_commit",
                        "pass_decide")


def without_recovery(labels: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(label for label in labels if label != "pass_recover")


def test_a_crash_lets_the_old_rules_writes_vote_and_undo_the_users_revert():
    """F2 in the model: under the rule before this one the mirror's surviving
    write reads as a change, so the pass undoes the user's unarchive."""
    broken = broken_along(model.BASE, without_recovery(CRASH_THEN_REVERT), faults=True)
    assert {"own-writes-do-not-vote", "latest-wins"} <= broken
    assert "intent-wins" not in broken, "the 2026-09-25 ghost could not see F2"


def test_the_proposed_rule_undoes_a_revert_made_where_the_archive_was_seen():
    """The design first proposed for F2 (the mirror's own writes do not vote)
    fixes the same-account revert but not one made in the account the user
    saw the mirror's archive in: that copy is the user's now, and it and the
    original archive disagree against a base the crash left behind."""
    assert "latest-wins" not in broken_along(model.MINE, without_recovery(CRASH_THEN_REVERT),
                                             faults=True)
    assert "latest-wins" in broken_along(model.MINE, without_recovery(CROSS_ACCOUNT_REVERT),
                                         faults=True)


@pytest.mark.parametrize("rule", [model.BASE, model.MINE])
def test_without_any_fault_a_skipped_put_back_undoes_the_revert_under_the_other_rules(rule):
    """Not even a fault is needed: a put-back the app's save made the mirror
    skip leaves the archive standing in the account the user reverts in."""
    assert "latest-wins" in broken_along(rule, ROLLBACK_SKIP_REVERT, faults=False)


@pytest.mark.parametrize("labels,faults", [(CRASH_THEN_REVERT, True),
                                           (CROSS_ACCOUNT_REVERT, True),
                                           (ROLLBACK_SKIP_REVERT, False)],
                         ids=["crash", "cross-account", "rollback-skip"])
def test_per_copy_references_keep_every_revert(labels, faults):
    """The rule the code implements: every copy's reference is what the last
    decision gave it or read there, so each of those reverts wins."""
    assert broken_along(model.REFS, labels, faults=faults) == set()
    with model.causal():
        final = model.replay(model.initial(3, False, model.REFS), labels, faults=faults)[-1][2]
    assert final.decided is False, "the user's unarchive is decided"


# --- examples ---------------------------------------------------------------------

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
    assert after.base is False and after.wal is None
    final = model.pass_publish(model.pass_decide(after))
    assert final.copy == (True,) * 3 and final.base is True


def test_a_write_that_finds_its_copy_rewritten_puts_back_the_copies_before_it():
    """C-23.28: all or nothing across the publish. The pass writes B, then
    finds C rewritten since its check: B is put back, and with nothing left
    standing the base and every reference are kept."""
    state = model.State(copy=(False, False, False), base=False, loaded=0,
                        mem=(False, None, None), settled=False)
    state = model.user_set(state, True)                 # A archives
    state = model.pass_check(model.pass_decide(state))   # publish B and C
    assert state.phase == model.PUBLISHING and state.pending == (1, 2)
    assert state.wal is not None, "recorded before the first write"
    state = model.pass_write(state)                       # B written
    assert state.copy == (True, True, False)
    state = model.focus(state, 2)                         # the app saves C
    after = model.finish(state)
    assert after.copy == (True, False, False) and after.base is False
    assert model.refs(after) == (False,) * 3 and after.wal is None
    final = model.pass_publish(model.pass_decide(after))
    assert final.copy == (True,) * 3 and final.base is True


def test_a_rollback_skips_a_copy_rewritten_after_the_mirrors_write():
    """C-23.28: no lost update in the rollback either. The mirror's write to
    B stands (the app saved over it), so the archive stands for the copies it
    reached, and C, which it did not reach, keeps its own value as its
    reference (F2)."""
    state = model.State(copy=(False, False, False), base=False, loaded=0,
                        mem=(False, None, None), settled=False)
    state = model.user_set(state, True)
    state = model.pass_write(model.pass_check(model.pass_decide(state)))
    state = model.focus(model.focus(state, 1), 2)         # the app saves B, then C
    after = model.finish(state)
    assert after.copy == (True, True, False), "B keeps what the app saved over it"
    assert after.base is True and model.refs(after) == (True, True, False)
    final = model.pass_publish(model.pass_decide(after))
    assert final.copy == (True,) * 3 and final.base is True and final.ref == ()


@pytest.mark.parametrize("fault", ["crash", "commit_fails", "rollback_write_fails"])
def test_every_fault_leaves_the_mirrors_surviving_write_its_own_reference(fault):
    """F2: a crash or a failed base write stops the pass with the publish
    record, saved before the first write, still standing, and the next pass
    resolves it by what landed; a failed put-back does not stop the pass,
    which resolves the record itself. Either way the archive the mirror's
    write carries stands, and that write holds its own reference."""
    state = model.State(copy=(False, False, False), base=False, loaded=0,
                        mem=(False, None, None), settled=False)
    state = model.user_set(state, True)
    state = model.pass_write(model.pass_check(model.pass_decide(state)))   # B written
    if fault == "rollback_write_fails":
        state = model.rollback_write(model.pass_write(model.focus(state, 2)), fails=True)
        resolved = model.pass_commit(state)
    else:
        state = (model.commit_fails(model.to_commit(state)) if fault == "commit_fails"
                 else model.crash(state))
        assert state.phase == model.IDLE and state.wal is not None
        resolved = model.pass_recover(state)
    assert resolved.base is True and resolved.wal is None and 1 in resolved.mine
    assert all(model.refs(resolved)[a] == resolved.copy[a] for a in resolved.mine)


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


def test_latest_wins_is_exercised_within_its_bound():
    """The causal guard fires on reachable decisions, faults included."""
    fired = 0
    with model.causal():
        seen = {model.initial(3, value) for value in (False, True)}
        queue = list(seen)
        while queue and len(seen) < CAUSAL_LIMIT // 5:
            state = queue.pop(0)
            for label, nxt in model.successors(state, stale=False, faults=True):
                if label == "pass_decide" and isinstance(state.base, bool) \
                        and isinstance(state.latest, bool):
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
