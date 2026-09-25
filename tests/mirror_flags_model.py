"""The desktop sidebar mirror's flag protocol, as a finite state machine.

This is the executable twin of `docs/formal/MirrorFlags.tla`: the same
variables, the same actions, the same properties. `test_mirror_flags_model.py`
explores every reachable state of it exhaustively, and
`test_mirror_flags_stateful.py` drives the real `Mirror` on real files in
lockstep with it, so the implementation, this model and the TLA+ spec are held
to one meaning.

One session, one boolean flag (`isArchived`; `isStarred` is the same code
path), a copy of its record in each account folder, and the merge base in
`mirror-flags.json`. A full pass is two steps, because the app can write
between them: `decide` snapshots every copy and decides by the merge base;
`publish` writes the copies that disagree, all or nothing, only if each is
still what the pass read, and then advances the base. A pass can be cancelled
between the two (nothing is written: the code has no checkpoint inside
publish).

The app is modeled as `mirror.py`'s docstring and `desktop.py` describe it:
it holds in memory the record of the folder it loaded (as it was on disk at
that load) and of folders where a session still runs from an earlier account;
a user's action changes the loaded folder's copy and the app's memory; any
app save writes memory. With `stale=False` the app never saves a value that
differs from the file (every save is a no-op for the flag, as when its memory
is current). With `stale=True` it may: that is the re-save of a value the
mirror changed after the app's load, the known limit in the 2026-09-24 report.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from typing import Iterator

IDLE, DECIDED = "idle", "decided"


@dataclass(frozen=True)
class State:
    copy: tuple[bool, ...]          # the flag in each account's file
    base: bool | None               # the merge base; None before the first sync
    loaded: int                     # the account folder the app has loaded
    mem: tuple[bool | None, ...]    # what the app holds for each account's copy
    phase: str = IDLE
    snap: tuple[bool, ...] | None = None
    decided: bool | None = None
    #: Ghost: the value a clean publish converged every copy to, cleared by
    #: any user action. While set, no pass may write anything else.
    settled: bool | None = None


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
    return replace(state, copy=tuple(copy), mem=tuple(mem), settled=None)


def app_save(state: State, account: int, *, stale: bool) -> State | None:
    """An app save that changes the file's flag: only a stale memory can."""
    held = state.mem[account]
    if not stale or held is None or held == state.copy[account]:
        return None
    copy = list(state.copy)
    copy[account] = held
    return replace(state, copy=tuple(copy))


def pass_decide(state: State) -> State | None:
    if state.phase != IDLE:
        return None
    return replace(state, phase=DECIDED, snap=state.copy,
                   decided=decide(state.copy, state.base))


def pass_publish(state: State) -> State | None:
    if state.phase != DECIDED:
        return None
    assert state.snap is not None and state.decided is not None
    dirty = [a for a, seen in enumerate(state.snap) if seen != state.decided]
    done = replace(state, phase=IDLE, snap=None, decided=None)
    if any(state.copy[a] != state.snap[a] for a in dirty):
        return done                         # held: nothing written, base kept
    copy = list(state.copy)
    for a in dirty:
        copy[a] = state.decided
    converged = all(value == state.decided for value in copy)
    return replace(done, copy=tuple(copy), base=state.decided,
                   settled=state.decided if converged else None)


def cancel(state: State) -> State | None:
    if state.phase != DECIDED:
        return None
    return replace(state, phase=IDLE, snap=None, decided=None)


def successors(state: State, *, stale: bool) -> Iterator[tuple[str, State]]:
    accounts = len(state.copy)
    for a in range(accounts):
        nxt = load(state, a)
        if nxt is not None:
            yield f"load({a})", nxt
        nxt = app_save(state, a, stale=stale)
        if nxt is not None:
            yield f"app_save({a})", nxt
    for value in (False, True):
        nxt = user_set(state, value)
        if nxt is not None:
            yield f"user_set({value})", nxt
    for name, step in (("pass_decide", pass_decide), ("pass_publish", pass_publish),
                       ("cancel", cancel)):
        nxt = step(state)
        if nxt is not None:
            yield name, nxt


# --- properties -----------------------------------------------------------------

def check_step(before: State, label: str, after: State) -> list[str]:
    """The step properties; each names what it proves. Empty means all hold."""
    broken = []
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
    if label == "pass_publish" and before.snap is not None:
        wrote = {a for a in range(len(before.copy)) if after.copy[a] != before.copy[a]}
        dirty = [a for a, seen in enumerate(before.snap) if seen != before.decided]
        held = any(before.copy[a] != before.snap[a] for a in dirty)
        # No lost update: the mirror writes only copies still as it read them.
        if any(before.copy[a] != before.snap[a] for a in wrote):
            broken.append("no-lost-update")
        # A held batch writes nothing and keeps the base (all or nothing).
        if held and (wrote or after.base != before.base):
            broken.append("hold-writes-nothing")
        # Base agreement: after a publish that went through, the base is the
        # value decided and every copy the pass read differently now holds it.
        if not held and (after.base != before.decided
                         or any(after.copy[a] != before.decided for a in dirty)):
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
    """From an idle state, one uninterrupted pass leaves every copy and the base equal."""
    if state.phase != IDLE:
        return True
    after = pass_publish(pass_decide(state))
    return len(set(after.copy)) == 1 and after.base == after.copy[0]


def explore(accounts: int = 3, *, stale: bool,
            start: tuple[bool, ...] | None = None) -> tuple[int, dict[str, tuple]]:
    """Breadth-first over every reachable state; returns (states, first
    counterexample trace per broken property)."""
    roots = [initial(accounts, value) for value in (False, True)]
    if start is not None:
        roots = [State(copy=start, base=None, loaded=0,
                       mem=(start[0],) + (None,) * (accounts - 1))]
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
