"""The desktop sidebar mirror's flag protocol, as a finite state machine.

This is the executable twin of `docs/formal/MirrorFlags.tla`: the same
variables, the same actions, the same properties. `test_mirror_flags_model.py`
explores every reachable state of it exhaustively, and
`test_mirror_flags_stateful.py` drives the real `Mirror` on real files in
lockstep with it, so the implementation and this model are held to one
meaning. The TLA+ module states the same thing for TLC, which has not been run
on it (Max, 2026-09-25: skip TLC for now).

One session, one boolean flag (`isArchived`; `isStarred` runs through the same
loop with its own bootstrap value), a copy of its record in each account
folder, and the merge base in `mirror-flags.json`. A pass is a sequence of
steps, because the app can write between any two:

* `pass_decide` snapshots every copy and decides by the merge base;
* `pass_check` is `sync_flags`' pre-check: if any copy the pass would write no
  longer holds the value it read, the session is held (nothing written, base
  kept); otherwise the publish starts;
* `pass_write` writes one copy, in path order. The write fails if the app or
  the user rewrote that copy since the pre-check (the code's signature check
  before the rename); the copies already written, and not rewritten since,
  are then put back, and the base is kept. After the last write the base
  advances.

A pass can be cancelled only between the decision and the pre-check: the
code's last cancellation point is before the publish. A pass that cannot read
every copy of the session holds it (writes nothing, keeps the base), which is
the same as a cancelled pass here; `test_mirror_flags_faults.py` and the
stateful test's unreadable-copy rule hold the code to that.

The app is modeled as `mirror.py`'s docstring and `desktop.py` describe it: it
holds in memory the record of the folder it loaded (as it was on disk at that
load) and of folders where a session still runs from an earlier account; a
user's action changes the loaded folder's copy and the app's memory; any app
save writes memory. `focus` is an app save that keeps the flag (an activity or
focus update): it changes the file, so it matters only to a publish in
progress. With `stale=False` the app never saves a flag that differs from the
file (as when its memory is current). With `stale=True` it may: that is the
re-save of a value the mirror changed after the app's load, the known limit
in the 2026-09-24 report.

What the model does not cover: an app rename landing between the code's last
signature check and its own `os.replace` (a window of one syscall), which the
code can narrow but not close with rename(2).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from typing import Iterator

IDLE, DECIDED, PUBLISHING = "idle", "decided", "publishing"
CONFLICT = "conflict"


@dataclass(frozen=True)
class State:
    copy: tuple[bool, ...]          # the flag in each account's file
    base: bool | None               # the merge base; None before the first sync
    loaded: int                     # the account folder the app has loaded
    mem: tuple[bool | None, ...]    # what the app holds for each account's copy
    phase: str = IDLE
    snap: tuple[bool, ...] | None = None
    decided: bool | None = None
    #: While publishing: the copies still to write, in path order.
    pending: tuple[int, ...] = ()
    #: While publishing: the copies this publish has written.
    written: tuple[int, ...] = ()
    #: While publishing: the copies the app or the user rewrote since the pre-check.
    touched: frozenset[int] = frozenset()
    #: Ghost: the value a clean publish converged every copy to, cleared by
    #: any user action. While set, no pass may write anything else.
    settled: bool | None = None
    #: Ghost: what the user set since the last publish that converged every
    #: copy (None: nothing; CONFLICT: both values). While a bool, a pass with
    #: a base must decide it: the user's change is never undone.
    intent: bool | str | None = None


def decide(copy: tuple[bool, ...], base: bool | None) -> bool:
    """`sync_flags`: agreement wins; otherwise the change from the base wins,
    and with no base archived-anywhere (True) wins."""
    values = set(copy)
    if len(values) == 1:
        return next(iter(values))
    return (not base) if isinstance(base, bool) else True


def initial(accounts: int, value: bool = False) -> State:
    """Every copy agrees and nothing has been synced; the app loaded account 0."""
    copy = (value,) * accounts
    return State(copy=copy, base=None, loaded=0,
                 mem=(value,) + (None,) * (accounts - 1))


def _touch(state: State, account: int) -> frozenset[int]:
    return state.touched | {account} if state.phase == PUBLISHING else state.touched


def _idle(state: State) -> State:
    return replace(state, phase=IDLE, snap=None, decided=None, pending=(), written=(),
                   touched=frozenset())


def _finish(state: State, copy: tuple[bool, ...]) -> State:
    converged = all(value == state.decided for value in copy)
    return replace(_idle(state), copy=copy, base=state.decided,
                   settled=state.decided if converged else None,
                   intent=None if converged else state.intent)


# --- actions (each returns the next state, or None when not enabled) --------------

def load(state: State, account: int) -> State | None:
    if account == state.loaded:
        return None
    mem = list(state.mem)
    mem[account] = state.copy[account]      # a fresh load reads the file
    return replace(state, loaded=account, mem=tuple(mem))


def user_set(state: State, value: bool) -> State | None:
    here = state.loaded
    if state.copy[here] == value:
        return None
    copy, mem = list(state.copy), list(state.mem)
    copy[here] = mem[here] = value
    intent = value if state.intent in (None, value) else CONFLICT
    return replace(state, copy=tuple(copy), mem=tuple(mem), settled=None,
                   touched=_touch(state, here), intent=intent)


def app_save(state: State, account: int, *, stale: bool) -> State | None:
    """An app save that changes the file's flag: only a stale memory can."""
    held = state.mem[account]
    if not stale or held is None or held == state.copy[account]:
        return None
    copy = list(state.copy)
    copy[account] = held
    return replace(state, copy=tuple(copy), touched=_touch(state, account))


def focus(state: State, account: int) -> State:
    """An app save that keeps the flag. It changes nothing the protocol reads,
    except that a publish in progress must not write over it."""
    return replace(state, touched=_touch(state, account))


def pass_decide(state: State) -> State | None:
    if state.phase != IDLE:
        return None
    return replace(state, phase=DECIDED, snap=state.copy,
                   decided=decide(state.copy, state.base))


def dirty(state: State) -> tuple[int, ...]:
    assert state.snap is not None
    return tuple(a for a, seen in enumerate(state.snap) if seen != state.decided)


def held(state: State) -> bool:
    assert state.snap is not None
    return any(state.copy[a] != state.snap[a] for a in dirty(state))


def pass_check(state: State) -> State | None:
    if state.phase != DECIDED:
        return None
    if held(state):
        return _idle(state)                 # nothing written, base kept
    if not dirty(state):
        return _finish(state, state.copy)
    return replace(state, phase=PUBLISHING, pending=dirty(state), written=(),
                   touched=frozenset())


def rolls_back(state: State) -> bool:
    return state.phase == PUBLISHING and state.pending[0] in state.touched


def pass_write(state: State) -> State | None:
    if state.phase != PUBLISHING:
        return None
    assert state.snap is not None and state.decided is not None
    target = state.pending[0]
    copy = list(state.copy)
    if target in state.touched:
        for a in state.written:
            if a not in state.touched:
                copy[a] = state.snap[a]
        return replace(_idle(state), copy=tuple(copy))
    copy[target] = state.decided
    if len(state.pending) == 1:
        return _finish(state, tuple(copy))
    return replace(state, copy=tuple(copy), pending=state.pending[1:],
                   written=state.written + (target,))


def pass_publish(state: State) -> State:
    """The pre-check and every write, with nothing in between."""
    state = pass_check(state)
    while state.phase == PUBLISHING:
        state = pass_write(state)
    return state


def cancel(state: State) -> State | None:
    if state.phase != DECIDED:
        return None
    return _idle(state)


def successors(state: State, *, stale: bool) -> Iterator[tuple[str, State]]:
    accounts = len(state.copy)
    for a in range(accounts):
        nxt = load(state, a)
        if nxt is not None:
            yield f"load({a})", nxt
        nxt = app_save(state, a, stale=stale)
        if nxt is not None:
            yield f"app_save({a})", nxt
        if state.phase == PUBLISHING and a not in state.touched:
            yield f"focus({a})", focus(state, a)
    for value in (False, True):
        nxt = user_set(state, value)
        if nxt is not None:
            yield f"user_set({value})", nxt
    for name, step in (("pass_decide", pass_decide), ("pass_check", pass_check),
                       ("pass_write", pass_write), ("cancel", cancel)):
        nxt = step(state)
        if nxt is not None:
            yield name, nxt


# --- properties -----------------------------------------------------------------

def check_step(before: State, label: str, after: State) -> list[str]:
    """The step properties; each names what it proves. Empty means all hold."""
    broken = []
    wrote = {a for a in range(len(before.copy)) if after.copy[a] != before.copy[a]}
    if label == "pass_decide":
        values = set(before.copy)
        # Idempotence: a converged state with its base decides itself.
        if len(values) == 1 and before.base in values and after.decided != before.base:
            broken.append("idempotence")
        # Change wins, both ways: when every copy that differs from the base
        # carries the same value, that value is decided.
        if isinstance(before.base, bool):
            moved = {v for v in before.copy if v != before.base}
            if len(moved) == 1 and after.decided not in moved:
                broken.append("change-wins")
        # Intent wins: with a base, a pass decides what the user last set since
        # the last publish that converged. This is what "no resurrection"
        # means for an honest app; never-undo-settled covers the rest.
        if isinstance(before.base, bool) and isinstance(before.intent, bool) \
                and after.decided != before.intent:
            broken.append("intent-wins")
    if label == "pass_check":
        # The pre-check writes nothing. A held session keeps its base (all or
        # nothing); one with nothing to write advances it (base agreement).
        if wrote:
            broken.append("hold-writes-nothing")
        if held(before) and after.base != before.base:
            broken.append("hold-writes-nothing")
        if not held(before) and not dirty(before) and after.base != before.decided:
            broken.append("base-agreement")
    if label == "pass_write":
        # No lost update: the mirror writes only a copy nobody rewrote since it
        # last checked it (a pending copy) or wrote it (a written one).
        if any(a in before.touched
               or not (before.copy[a] == before.snap[a] or a in before.written)
               for a in wrote):
            broken.append("no-lost-update")
        if rolls_back(before):
            # All or nothing: a failed write puts back every copy the publish
            # wrote, except one rewritten since, and keeps the base.
            if (after.base != before.base
                    or any(after.copy[a] != before.snap[a]
                           for a in before.written if a not in before.touched)
                    or any(a not in before.written for a in wrote)):
                broken.append("all-or-nothing")
        elif after.phase == IDLE:
            # Base agreement: after a publish that went through, the base is
            # the value decided and every copy the pass read differently holds
            # it, unless the app or the user rewrote it since.
            if (after.base != before.decided
                    or any(after.copy[a] != before.decided
                           for a in dirty(before) if a not in before.touched)):
                broken.append("base-agreement")
        # Never undo a settled value: once a clean publish converged every copy
        # and no user acted since, no pass writes anything else. This is the
        # brief's "no resurrection", in both directions.
        if before.settled is not None and any(after.copy[a] != before.settled for a in wrote):
            broken.append("never-undo-settled")
    if label == "cancel" and (after.copy != before.copy or after.base != before.base):
        broken.append("cancellation-safety")
    return broken


def converges_in_one_clean_pass(state: State) -> bool:
    """From an idle state, one pass with nothing written in between leaves
    every copy and the base equal to the value decided."""
    if state.phase != IDLE:
        return True
    decided = pass_decide(state)
    after = pass_publish(decided)
    return all(value == decided.decided for value in after.copy) and after.base == decided.decided


def explore(accounts: int = 3, *, stale: bool,
            roots: list[State] | None = None) -> tuple[int, dict[str, tuple]]:
    """Breadth-first over every reachable state; returns (states, first
    counterexample trace per broken property)."""
    if roots is None:
        roots = [initial(accounts, value) for value in (False, True)]
    seen: dict[State, tuple] = {root: () for root in roots}
    queue: deque[State] = deque(roots)
    broken: dict[str, tuple] = {}
    while queue:
        state = queue.popleft()
        trace = seen[state]
        if not converges_in_one_clean_pass(state):
            broken.setdefault("convergence", trace)
        for label, nxt in successors(state, stale=stale):
            for name in check_step(state, label, nxt):
                broken.setdefault(name, trace + (label,))
            if nxt not in seen:
                seen[nxt] = trace + (label,)
                queue.append(nxt)
    return len(seen), broken
