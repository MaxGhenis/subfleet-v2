"""The mirror's date sync, checked over every reachable state: C-23.28.

`tests/mirror_activity_model.py` is the executable twin of
`docs/formal/MirrorActivity.tla`. These tests explore it breadth-first, every
state and every action from every state, for three account folders, and check
the invariants the 2026-10-10 report states for the date a sidebar row shows:

* bounded lag: a pass that decides for a session decides for every copy more
  than the lag behind its newest, and one clean pass leaves every copy within
  the lag;
* raise below newest: a raise is a raise, and never to the newest date or past
  it, at the decision and again at the write;
* the lead is kept: no write changes the newest date or which copies hold it;
* never lowered: a write that goes through lowers no copy;
* idempotence: with every copy within the lag, nothing is decided;
* no lost update: the mirror writes only a copy nobody rewrote since its
  pre-check or since the mirror wrote it;
* all or nothing: a decision and a pre-check write nothing, and a failed
  write puts back every copy the publish wrote, except one rewritten since;
* cancellation safety: a cancelled pass, or a held session, changes no copy;
* stays fresh: once a pass left every copy within the lag, each stays there
  until the session next runs.

With an app whose saves never change the date except by running the session,
all of them hold in every state. With an app that can re-save an older date
(the known limit), exactly the last fails, and never because of a mirror write.

The pure decision the mirror ships (`mirror.activity_targets`) is held to the
same properties on random inputs, and to the model's own statement of it.

The models are of one session. Which sessions a sync raises when more are
behind than its bound allows is `mirror.take_turns`, first come first served;
its property is here too, for any schedule of sessions falling behind,
catching up, and being taken.
"""

from __future__ import annotations

import math

from hypothesis import given, settings
from hypothesis import strategies as st

from subfleet.sessions import mirror
from tests import mirror_activity_model as model


# --- every reachable state ---------------------------------------------------------

def test_every_invariant_holds_in_every_state_for_an_app_that_saves_what_it_shows():
    """C-23.28: exhaustive over all reachable states of three account folders,
    through two turns of the session."""
    states, broken = model.explore(3, stale=False)
    assert states > 100_000, "the exploration reached the whole space"
    assert broken == {}


def test_a_stale_resave_lowers_one_copy_and_breaks_nothing_else():
    """C-23.28, the known limit: the app re-saving a date it held from before
    the mirror's raise is the one way a raised copy falls behind again, and the
    last step of the shortest case is that save, not a write of the mirror's."""
    _states, broken = model.explore(3, stale=True, clock_max=model.JUMP)
    assert set(broken) == {"stays-fresh"}
    trace = broken["stays-fresh"]
    assert trace[-1].startswith("app_save("), "the app lowers it; no pass does"
    assert "pass_write" in trace and trace[0].startswith("turn(")


def test_the_next_pass_raises_a_copy_the_app_lowered_and_spreads_the_old_date_nowhere():
    """C-23.28: where a stale flag re-save reads as the user's change and wins,
    a stale date is only behind. The other copies keep theirs."""
    state = model.turn(model.initial(3), 0)                 # the session runs in A
    state = model.load(state, 1)                            # B is loaded, date 0
    state = model.pass_publish(model.pass_decide(state))
    assert state.act == (3, 2, 2) and state.fresh
    lowered = model.app_save(state, 1, stale=True)          # B's memory comes back
    assert lowered.act == (3, 0, 2)
    again = model.pass_publish(model.pass_decide(lowered))
    assert again.act == (3, 2, 2), "raised again; A and C never moved"


# --- the shapes the report names ----------------------------------------------------

def test_a_session_run_under_another_login_is_raised_everywhere_else():
    """C-23.28: the 2026-10-10 shape. The copy where the session ran stays the
    only newest one; the others land one tick behind it."""
    state = model.turn(model.initial(3), 0)
    after = model.pass_publish(model.pass_decide(state))
    assert after.act == (3, 2, 2)
    assert model.leaders(after.act) == frozenset({0})


def test_a_converged_pass_writes_nothing():
    """C-23.28: idempotence."""
    state = model.pass_publish(model.pass_decide(model.turn(model.initial(3), 0)))
    decided = model.pass_decide(state)
    assert decided.target == (None, None, None)
    assert model.pass_publish(decided).act == state.act


def test_a_turn_between_the_read_and_the_publish_is_kept_and_holds_nothing():
    """C-23.28: no lost update. The date is not among the fields whose change
    holds a session; the copy the app raised itself is left out of the writes."""
    state = model.turn(model.initial(3), 0)
    state = model.load(state, 1)
    decided = model.pass_decide(state)                      # B and C go to 2
    moved = model.turn(decided, 1)                          # B runs before the publish
    assert moved.act == (3, 6, 0)
    checked = model.pass_check(moved)
    assert checked.pending == (2,), "B needs no write"
    after = model.pass_write(checked)
    assert after.act == (3, 6, 2), "nothing lowered B to the pass's target"


def test_a_write_that_finds_its_copy_rewritten_puts_back_the_copies_before_it():
    """C-23.28: all or nothing across the publish, dates included."""
    state = model.turn(model.initial(3), 0)
    state = model.pass_check(model.pass_decide(state))
    assert state.pending == (1, 2)
    state = model.pass_write(state)                          # B raised
    assert state.act == (3, 2, 0)
    state = model.focus(state, 2)                            # the app saves C
    after = model.pass_write(state)
    assert after.act == (3, 0, 0), "B is put back to what the pre-check read"
    assert model.pass_publish(model.pass_decide(after)).act == (3, 2, 2)


def test_a_flag_write_that_fails_puts_back_a_raised_date():
    """C-23.28: one batch. The copy written only for a flag's sake fails, and
    the date raised before it is put back with the rest."""
    state = model.turn(model.initial(3), 0)
    state = model.pass_check(model.pass_decide(state, also={0}))
    assert state.pending == (0, 1, 2)
    state = model.pass_write(model.pass_write(state))        # A (flag only), then B
    assert state.act == (3, 2, 0)
    after = model.pass_write(model.focus(state, 2))
    assert after.act == (3, 0, 0)


def test_a_deferred_pass_decides_no_date_and_still_publishes_its_flag_copies():
    """C-23.28: an archived session, or one past the pass's bound, keeps its dates."""
    state = model.turn(model.initial(3), 0)
    decided = model.pass_decide(state, defer=True, also={1})
    assert decided.target == (None, None, None)
    assert model.pass_check(decided).pending == (1,)
    assert model.pass_publish(decided).act == state.act


# --- whose turn it is ------------------------------------------------------------------

@settings(max_examples=400, deadline=None)
@given(bound=st.integers(min_value=1, max_value=4),
       syncs=st.lists(st.tuples(
           st.dictionaries(st.integers(min_value=0, max_value=11),
                           st.integers(min_value=1, max_value=9), max_size=12),
           st.sets(st.integers(min_value=0, max_value=11), max_size=4)),
           min_size=1, max_size=14))
def test_a_session_waits_only_for_those_that_were_waiting_before_it(bound, syncs):
    """C-23.28 (the three reviews of #167 each found a schedule that starved a
    session under a rule that looked at outcomes): first come, first served,
    for any schedule at all. Each sync names the sessions behind, with how far,
    and some that turned out to need no raise. Whatever arrives, leaves, or is
    taken and comes back:

    * a sync takes `bound` of them, or all, and never one that was not behind;
    * no session is passed over for one that started waiting later;
    * a session that started waiting with `r` others waiting no later than it
      is passed over at most `r // bound` times."""
    since: dict[str, int] = {}
    waiting: dict[str, list[int]] = {}        # session -> [its stamp, r, times passed over]
    for turn, (lags, done) in enumerate(syncs, start=1):
        behind = {str(identity): lag for identity, lag in lags.items()}
        for identity in [str(key) for key in done if str(key) not in behind]:
            since.pop(identity, None)          # the caller's pruning: it needs no raise
            waiting.pop(identity, None)
        before = dict(since)
        taken = mirror.take_turns(behind, since, turn, bound)
        assert len(taken) == min(bound, len(behind)) and set(taken) <= set(behind)
        stamp = {identity: before.get(identity, 2 * turn) for identity in behind}
        passed = [identity for identity in behind if identity not in taken]
        assert all(stamp[one] <= stamp[other] for one in taken for other in passed), \
            "no one is passed over for a session that started waiting later"
        assert all(since[identity] == 2 * turn + 1 for identity in taken)
        assert all(since[identity] == stamp[identity] for identity in passed)
        for identity in behind:
            if identity not in waiting or waiting[identity][0] != stamp[identity]:
                others = sum(1 for other, value in {**before, **stamp}.items()
                             if other != identity and value <= stamp[identity])
                waiting[identity] = [stamp[identity], others, 0]
        for identity in passed:
            waiting[identity][2] += 1
            assert waiting[identity][2] <= waiting[identity][1] // bound, \
                "it waited longer than for those that were waiting before it"
        for identity in taken:
            waiting.pop(identity, None)


def test_those_that_started_waiting_together_go_furthest_behind_first():
    """C-23.28: the first pass after the sync is switched on finds every stale
    session at once, and the stalest rows are raised first."""
    since: dict[str, int] = {}
    assert mirror.take_turns({"a": 5, "b": 90, "c": 30, "d": 30}, since, 1, 3) == ["b", "c", "d"]
    assert since == {"a": 2, "b": 3, "c": 3, "d": 3}
    assert mirror.take_turns({"a": 5, "b": 90, "e": 400}, since, 2, 1) == ["a"], \
        "the one already waiting, before the one further behind that has just arrived"
    assert mirror.take_turns({"b": 90, "e": 400}, since, 3, 1) == ["b"], \
        "then the one that had its turn first"


# --- the shipped decision -----------------------------------------------------------

DATES = st.lists(st.integers(min_value=0, max_value=2_000_000_000_000), min_size=1, max_size=8)
LAGS = st.integers(min_value=1, max_value=10_000_000)


def applied(dates: list, raises: dict[int, object]) -> list:
    return [raises.get(index, value) for index, value in enumerate(dates)]


@settings(max_examples=400, deadline=None)
@given(dates=DATES, lag=LAGS)
def test_the_decision_keeps_its_bounds_for_every_input(dates, lag):
    """C-23.28: raise below newest, never lowered, bounded lag, the lead kept,
    and idempotence, for `mirror.activity_targets` on any dates and any lag."""
    raises = mirror.activity_targets(dates, lag)
    newest = max(dates)
    for index, value in raises.items():
        assert dates[index] < value < newest, "a raise is a raise, and below the newest"
        assert newest - dates[index] > lag, "only a copy more than the lag behind"
    after = applied(dates, raises)
    assert max(after) == newest
    assert [i for i, v in enumerate(after) if v == newest] == \
        [i for i, v in enumerate(dates) if v == newest], "the same copies lead"
    assert all(newest - value <= lag for value in after), "one pass leaves every copy within it"
    assert mirror.activity_targets(after, lag) == {}, "a second pass decides nothing"
    assert all(index not in raises for index, value in enumerate(dates)
               if newest - value <= lag), "a copy within the lag is not written"


@settings(max_examples=400, deadline=None)
@given(dates=st.lists(st.integers(min_value=0, max_value=40), min_size=1, max_size=6),
       lag=st.integers(min_value=1, max_value=12))
def test_the_decision_is_the_models(dates, lag):
    """C-23.28: the shipped function and the model's statement of it agree."""
    expected = {index: goal for index, goal in enumerate(model.targets(tuple(dates), lag))
                if goal is not None}
    assert mirror.activity_targets(dates, lag) == expected


@settings(max_examples=200, deadline=None)
@given(dates=DATES, lag=LAGS, data=st.data())
def test_the_decision_does_not_depend_on_folder_order(dates, lag, data):
    """C-23.28: the same copies get the same dates in any order of the folders."""
    order = data.draw(st.permutations(range(len(dates))))
    shuffled = [dates[index] for index in order]
    raises = mirror.activity_targets(shuffled, lag)
    assert {order[index]: value for index, value in raises.items()} == \
        mirror.activity_targets(dates, lag)


JUNK = st.one_of(st.none(), st.booleans(), st.text(max_size=3), st.just(math.nan),
                 st.just(math.inf), st.just(-math.inf), st.lists(st.integers(), max_size=1))


@settings(max_examples=300, deadline=None)
@given(values=st.lists(st.one_of(st.integers(min_value=0, max_value=10**13), JUNK),
                       min_size=0, max_size=8), lag=LAGS)
def test_a_date_that_is_not_a_number_is_no_voice_and_is_never_written(values, lag):
    """C-23.28: the app's own record check wants a number; anything else the
    mirror leaves alone, and decides the rest as if that copy were not there."""
    raises = mirror.activity_targets(values, lag)
    numbers = [(index, value) for index, value in enumerate(values)
               if isinstance(value, int) and not isinstance(value, bool)
               and -mirror.SAFE_MS < value < mirror.SAFE_MS]
    assert set(raises) <= {index for index, _value in numbers}
    only = mirror.activity_targets([value for _index, value in numbers], lag)
    assert raises == {numbers[position][0]: value for position, value in only.items()}


ANY_NUMBER = st.one_of(
    st.integers(min_value=0, max_value=2 * 10**12),
    st.floats(min_value=0, max_value=2e12),
    st.integers(min_value=-10**30, max_value=10**30),
    st.sampled_from([10**400, -(10**400), 2**53, 2**53 - 1, -(2**53), 0, -1]),
    st.floats(allow_nan=True, allow_infinity=True))


@settings(max_examples=600, deadline=None)
@given(values=st.lists(st.one_of(ANY_NUMBER, JUNK), min_size=0, max_size=8),
       lag=st.floats(min_value=-10, max_value=1e9, allow_nan=False))
def test_the_decision_never_fails_and_keeps_its_promise_for_any_numbers(values, lag):
    """C-23.28 (review of #167): whatever number a record holds, the decision
    raises nothing (an integer too large for a float once did), and every date
    it returns is above the copy's own and below the newest voice. Beyond
    JavaScript's safe integers a number is no voice: "one millisecond before"
    is not a different number there."""
    raises = mirror.activity_targets(values, lag)
    voices = [value for value in values if mirror._instant_ms(value)]
    assert all(isinstance(value, (int, float)) and not isinstance(value, bool)
               and -mirror.SAFE_MS < value < mirror.SAFE_MS for value in voices)
    if lag <= 0 or not voices:
        assert raises == {}
        return
    newest = max(voices)
    for index, goal in raises.items():
        assert mirror._instant_ms(values[index]), "only a voice is written"
        assert values[index] < goal < newest
    after = applied(values, raises)
    assert max(value for value in after if mirror._instant_ms(value)) == newest
    assert mirror.activity_targets(after, lag) == {}, "a second decision writes nothing"


@settings(max_examples=600, deadline=None)
@given(values=st.lists(st.floats(min_value=-(2.0 ** 53), max_value=2.0 ** 53,
                                 exclude_min=True, exclude_max=True),
                       min_size=1, max_size=6),
       lag=st.floats(min_value=1e-3, max_value=1e6))
def test_the_promise_holds_for_every_float_within_the_safe_integers(values, lag):
    """C-23.28 (second review of #167): above its own date and below the
    newest, for floats of either sign anywhere in the range, and a second
    decision never chooses a copy the first one raised."""
    raises = mirror.activity_targets(values, lag)
    newest = max(values)
    assert all(values[index] < goal < newest for index, goal in raises.items())
    after = applied(values, raises)
    assert max(after) == newest
    assert not set(mirror.activity_targets(after, lag)) & set(raises)


@settings(max_examples=400, deadline=None)
@given(dates=st.lists(st.one_of(st.integers(min_value=0, max_value=2 * 10**12),
                                st.floats(min_value=0, max_value=2e12)),
                      min_size=1, max_size=8), lag=LAGS)
def test_the_decision_keeps_its_bounds_for_dates_that_parse_as_floats(dates, lag):
    """C-23.28 (review of #167): a record's date can parse as a float. The
    bounds are the integers' own; a copy within a millisecond of the newest
    cannot be raised, so there the lag is "within the lag, or that close"."""
    raises = mirror.activity_targets(dates, lag)
    newest = max(dates)
    assert all(dates[index] < goal < newest for index, goal in raises.items())
    after = applied(dates, raises)
    assert [value == newest for value in after] == [value == newest for value in dates]
    assert all(newest - value <= lag or value >= newest - 1 for value in after)
    assert mirror.activity_targets(after, lag) == {}


@settings(max_examples=100, deadline=None)
@given(dates=DATES, lag=st.one_of(st.just(0), st.floats(max_value=0, allow_nan=False)))
def test_a_lag_of_zero_switches_the_sync_off(dates, lag):
    """C-6.4: zero is off, as for every window under `sessions`."""
    assert mirror.activity_targets(dates, lag) == {}


def test_a_float_whose_one_before_rounds_onto_the_copy_is_not_raised_to_itself():
    """C-23.28 (second review of #167): the two dates straddle a power of two.
    One before the newest is not a number a float there can hold, and it rounds
    onto the older copy's own date. Without the decision's last test that copy
    was "raised" to the date it already held, on every pass."""
    older, newest = -2251799813685249.0, -2251799813685247.8
    assert newest - older > 1 and newest - 1 == older
    assert mirror.activity_targets([older, newest], 1) == {}
    assert mirror.activity_targets([-older - 3, -newest], 1) == {0: -newest - 1}, \
        "the same magnitudes above zero, where one before is exact"


def test_a_voice_is_a_number_within_the_safe_integers_and_nothing_else():
    """C-23.28: the edges, each side. JavaScript's largest safe integer is a
    date; the next integer is not."""
    assert mirror.SAFE_MS == 2 ** 53
    for value in (1, 0, -1, 2 ** 53 - 1, -(2 ** 53) + 1, 2.0 ** 52 + 0.5, 1.5, 2 ** 52 + 1):
        assert mirror._instant_ms(value), value
    for value in (2 ** 53, -(2 ** 53), 2.0 ** 53, 10 ** 400, float("inf"), float("nan"),
                  True, False, None, "5", [5], {"at": 5}):
        assert not mirror._instant_ms(value), value
    upper = 2 ** 53 - 1
    assert mirror.activity_targets([upper, upper - 10], 5) == {1: upper - 1}


def test_a_lag_under_one_millisecond_still_raises_only_what_it_can_move():
    """C-23.28: the target is one millisecond before the newest, so a copy
    that close cannot be raised; the lag is at least that."""
    assert mirror.activity_targets([10, 9, 8], 0.25) == {2: 9}
