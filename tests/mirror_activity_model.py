"""The desktop sidebar mirror's date sync, as a finite state machine.

This is the executable twin of `docs/formal/MirrorActivity.tla`: the same
variables, the same actions, the same properties. `test_mirror_activity_model.py`
explores every reachable state of it, and `test_mirror_activity_stateful.py`
drives the real `Mirror` on real files in lockstep with it and with the flag
protocol's twin (`tests/mirror_flags_model.py`), so the implementation and both
models are held to one meaning. TLC has not been run on the TLA+ module.

One session, a copy of its record in each account folder, and in each copy the
date the sidebar shows (`lastActivityAt`, a number). The app writes that date
only where it runs the session, so the other folders' copies keep the date they
were copied with. The mirror raises them in the flag protocol's own publish:

* `pass_decide` snapshots every copy and decides which to raise
  (`targets`: a copy more than `LAG` behind the newest goes to one before the
  newest). A pass may decide nothing for the session (`defer`): it is archived,
  or the pass's bound on sessions was spent on others further behind;
* `pass_check` is the pre-check. A date that moved since the snapshot does
  not hold the session; the check only drops a copy that already holds the
  target or more. What holds a session is a flag field that moved or a copy
  that cannot be read, and a held session writes nothing: `cancel` here;
* `pass_write` writes one copy, in path order, and fails if the app rewrote
  that copy since the pre-check. The copies already written, and not rewritten
  since, are then put back to what the pre-check read.

`also` names the copies the same publish writes for a flag's sake (any subset:
the flag is the other twin's business). A write of one changes no date here,
but it is a write that can fail and put the batch back.

The app is modeled as in the flag twin: it holds in memory the record of the
folder it loaded and of folders where the session still runs from an earlier
account. `turn` is the session running in a folder the app holds: the date
there becomes now, later than any date written before, so no date here is ever
past the clock (the code takes one that is for no voice, a case outside this
model). `focus` is a save that keeps the date. With `stale=True` the app may save a date it held from before
the mirror raised the copy (`app_save`): the documented limit that the app
writes a record from memory. Unlike a flag, that old date is no one's change:
it lowers one copy, the mirror never spreads it, and the next pass raises the
copy again.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from itertools import combinations
from typing import Iterable, Iterator

IDLE, DECIDED, PUBLISHING = "idle", "decided", "publishing"

#: How far behind the newest a copy may be (`sessions.mirror_activity_lag_s`).
LAG = 2
#: A turn's date is this much later than any date written before: more than
#: `LAG`, as a real turn is later than the last by more than a raise's one tick.
JUMP = 3
#: The latest date the exploration lets a turn write.
CLOCK_MAX = 6


@dataclass(frozen=True)
class State:
    act: tuple[int, ...]                # the date in each account's file
    loaded: int                         # the account folder the app has loaded
    mem: tuple[int | None, ...]         # the date the app holds for each account's copy
    clock: int = 0                      # the latest date any turn has written
    phase: str = IDLE
    snap: tuple[int, ...] | None = None
    #: Per account: the date the pass decided to raise it to, or None.
    target: tuple[int | None, ...] | None = None
    #: The copies the publish also writes, for a flag.
    also: frozenset[int] = frozenset()
    #: While publishing: each copy's date as the pre-check read it.
    checked: tuple[int, ...] | None = None
    pending: tuple[int, ...] = ()
    written: tuple[int, ...] = ()
    touched: frozenset[int] = frozenset()
    #: Ghost: a clean pass left every copy within `LAG` of the newest and the
    #: session has not run since. While set, every copy stays within `LAG`.
    fresh: bool = False


def targets(dates: tuple[int, ...], lag: int = LAG) -> tuple[int | None, ...]:
    """`mirror.activity_targets`, restated: a copy more than `lag` behind the
    newest is raised to one before the newest."""
    newest = max(dates)
    return tuple(newest - 1 if newest - date > lag else None for date in dates)


def within_lag(dates: tuple[int, ...], lag: int = LAG) -> bool:
    return max(dates) - min(dates) <= lag


def initial(accounts: int) -> State:
    """Every copy holds the same date; the app loaded account 0."""
    return State(act=(0,) * accounts, loaded=0, mem=(0,) + (None,) * (accounts - 1))


def _touch(state: State, account: int) -> frozenset[int]:
    return state.touched | {account} if state.phase == PUBLISHING else state.touched


def _idle(state: State) -> State:
    return replace(state, phase=IDLE, snap=None, target=None, also=frozenset(), checked=None,
                   pending=(), written=(), touched=frozenset())


# --- actions (each returns the next state, or None when not enabled) --------------

def load(state: State, account: int) -> State | None:
    if account == state.loaded:
        return None
    mem = list(state.mem)
    mem[account] = state.act[account]       # a fresh load reads the file
    return replace(state, loaded=account, mem=tuple(mem))


def turn(state: State, account: int, *, clock_max: int = CLOCK_MAX) -> State | None:
    """The session runs in a folder the app holds: its date becomes now."""
    if state.mem[account] is None or state.clock + JUMP > clock_max:
        return None
    now = state.clock + JUMP
    act, mem = list(state.act), list(state.mem)
    act[account] = mem[account] = now
    return replace(state, act=tuple(act), mem=tuple(mem), clock=now, fresh=False,
                   touched=_touch(state, account))


def app_save(state: State, account: int, *, stale: bool) -> State | None:
    """An app save that changes the file's date: only a stale memory can."""
    held = state.mem[account]
    if not stale or held is None or held == state.act[account]:
        return None
    act = list(state.act)
    act[account] = held
    return replace(state, act=tuple(act), touched=_touch(state, account))


def focus(state: State, account: int) -> State:
    """An app save that keeps the date: it matters only to a publish in progress."""
    return replace(state, touched=_touch(state, account))


def pass_decide(state: State, *, defer: bool = False,
                also: Iterable[int] = ()) -> State | None:
    if state.phase != IDLE:
        return None
    decided = (None,) * len(state.act) if defer else targets(state.act)
    return replace(state, phase=DECIDED, snap=state.act, target=decided, also=frozenset(also))


def to_write(state: State) -> tuple[int, ...]:
    """The batch after the pre-check: a copy still below its target, or one a
    flag is written to."""
    assert state.target is not None
    raising = {a for a, goal in enumerate(state.target)
               if goal is not None and state.act[a] < goal}
    return tuple(sorted(raising | state.also))


def pass_check(state: State) -> State | None:
    if state.phase != DECIDED:
        return None
    pending = to_write(state)
    if not pending:
        return _finish(state)
    return replace(state, phase=PUBLISHING, checked=state.act, pending=pending, written=(),
                   touched=frozenset())


def _finish(state: State) -> State:
    """A publish that went through. The session is fresh if a pass that
    decided for it left every copy within the lag."""
    assert state.snap is not None
    return replace(_idle(state), fresh=state.fresh or within_lag(state.act))


def rolls_back(state: State) -> bool:
    return state.phase == PUBLISHING and state.pending[0] in state.touched


def pass_write(state: State) -> State | None:
    if state.phase != PUBLISHING:
        return None
    assert state.target is not None and state.checked is not None
    account = state.pending[0]
    act = list(state.act)
    if account in state.touched:
        for a in state.written:
            if a not in state.touched:
                act[a] = state.checked[a]
        return replace(_idle(state), act=tuple(act))
    goal = state.target[account]
    if goal is not None and act[account] < goal:
        act[account] = goal
    after = replace(state, act=tuple(act), pending=state.pending[1:],
                    written=state.written + (account,))
    return _finish(after) if not after.pending else after


def pass_publish(state: State) -> State:
    """The pre-check and every write, with nothing in between."""
    state = pass_check(state)
    while state.phase == PUBLISHING:
        state = pass_write(state)
    return state


def cancel(state: State) -> State | None:
    """A pass cancelled before it publishes, or a held session: nothing written."""
    if state.phase != DECIDED:
        return None
    return _idle(state)


def subsets(items: Iterable[int]) -> Iterator[frozenset[int]]:
    pool = sorted(items)
    for size in range(len(pool) + 1):
        for chosen in combinations(pool, size):
            yield frozenset(chosen)


def successors(state: State, *, stale: bool,
               clock_max: int = CLOCK_MAX) -> Iterator[tuple[str, State]]:
    accounts = len(state.act)
    for a in range(accounts):
        nxt = load(state, a)
        if nxt is not None:
            yield f"load({a})", nxt
        nxt = turn(state, a, clock_max=clock_max)
        if nxt is not None:
            yield f"turn({a})", nxt
        nxt = app_save(state, a, stale=stale)
        if nxt is not None:
            yield f"app_save({a})", nxt
        if state.phase == PUBLISHING and a not in state.touched:
            yield f"focus({a})", focus(state, a)
    if state.phase == IDLE:
        for also in subsets(range(accounts)):
            yield "pass_decide", pass_decide(state, also=also)
            yield "pass_defer", pass_decide(state, defer=True, also=also)
    for name, step in (("pass_check", pass_check), ("pass_write", pass_write),
                       ("cancel", cancel)):
        nxt = step(state)
        if nxt is not None:
            yield name, nxt


# --- properties -----------------------------------------------------------------

def leaders(dates: tuple[int, ...]) -> frozenset[int]:
    newest = max(dates)
    return frozenset(a for a, date in enumerate(dates) if date == newest)


def check_state(state: State) -> list[str]:
    """Stays fresh: once a clean pass left every copy within the lag, each
    stays there until the session next runs. An honest app keeps it; an app
    that re-saves an older date breaks it (the known limit), for one copy and
    until the next pass."""
    return [] if not state.fresh or within_lag(state.act) else ["stays-fresh"]


def check_step(before: State, label: str, after: State) -> list[str]:
    """The step properties; each names what it proves. Empty means all hold."""
    broken = []
    wrote = {a for a in range(len(before.act)) if after.act[a] != before.act[a]}
    if label in ("pass_decide", "pass_defer"):
        goals = after.target
        newest = max(before.act)
        if wrote:
            broken.append("decision-writes-nothing")
        if label == "pass_defer" and any(goal is not None for goal in goals):
            broken.append("deferral-decides-nothing")
        # A raise is a raise, and never to the newest date or past it: the
        # copy the app last ran the session in stays the only newest one.
        if any(goal is not None and not (before.act[a] < goal < newest)
               for a, goal in enumerate(goals)):
            broken.append("raise-below-newest")
        # Idempotence: with every copy within the lag, nothing is decided.
        if within_lag(before.act) and any(goal is not None for goal in goals):
            broken.append("idempotence")
        # Bounded lag: every copy more than the lag behind is decided for.
        if label == "pass_decide" and any(
                goal is None and newest - before.act[a] > LAG for a, goal in enumerate(goals)):
            broken.append("bounded-lag")
    if label == "pass_check" and wrote:
        broken.append("check-writes-nothing")
    if label == "pass_write":
        # No lost update: the mirror writes only a copy nobody rewrote since
        # its pre-check (a pending copy) or since the mirror wrote it.
        if any(a in before.touched for a in wrote):
            broken.append("no-lost-update")
        if rolls_back(before):
            # All or nothing: a failed write puts back every copy the publish
            # wrote, except one rewritten since, to what the pre-check read.
            if (any(after.act[a] != before.checked[a]
                    for a in before.written if a not in before.touched)
                    or any(a not in before.written for a in wrote)):
                broken.append("all-or-nothing")
        else:
            if wrote - {before.pending[0]}:
                broken.append("no-lost-update")
            # Never lowered, and the newest date and who holds it are as
            # they were: the mirror neither invents a date nor moves the lead.
            if any(after.act[a] < before.act[a] for a in wrote):
                broken.append("never-lowered")
            if max(after.act) != max(before.act) or leaders(after.act) != leaders(before.act):
                broken.append("leader-kept")
            if any(after.act[a] >= max(before.snap) for a in wrote):
                broken.append("raise-below-newest")
    if label == "cancel" and wrote:
        broken.append("cancellation-safety")
    return broken


def converges_in_one_clean_pass(state: State) -> bool:
    """From an idle state, one pass that decides for the session, with nothing
    written in between, leaves every copy within the lag, whichever copies a
    flag adds to its batch."""
    if state.phase != IDLE:
        return True
    for also in subsets(range(len(state.act))):
        after = pass_publish(pass_decide(state, also=also))
        if not within_lag(after.act) or leaders(after.act) != leaders(state.act):
            return False
    return True


def explore(accounts: int = 3, *, stale: bool, roots: list[State] | None = None,
            clock_max: int = CLOCK_MAX) -> tuple[int, dict[str, tuple]]:
    """Breadth-first over every reachable state; returns (states, first
    counterexample trace per broken property)."""
    if roots is None:
        roots = [initial(accounts)]
    seen: dict[State, tuple] = {root: () for root in roots}
    queue: deque[State] = deque(roots)
    broken: dict[str, tuple] = {}
    while queue:
        state = queue.popleft()
        trace = seen[state]
        for name in check_state(state):
            broken.setdefault(name, trace)
        if not converges_in_one_clean_pass(state):
            broken.setdefault("convergence", trace)
        for label, nxt in successors(state, stale=stale, clock_max=clock_max):
            for name in check_step(state, label, nxt):
                broken.setdefault(name, trace + (label,))
            if nxt not in seen:
                seen[nxt] = trace + (label,)
                queue.append(nxt)
    return len(seen), broken
