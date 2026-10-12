"""The desktop sidebar mirror's settings rule, as a finite state machine.

This is the executable twin of the settings half of `Mirror.sync_flags`
(`_settle_settings` and `decide_setting` in `subfleet/sessions/mirror.py`).
`test_mirror_settings_model.py` explores every reachable state of it, and
`test_mirror_settings_stateful.py` drives the real `Mirror` on real files in
lockstep with it, so the implementation and this model mean the same thing.

One session and one settings unit (a model id, say; values are small ints), a
copy of its record in each account folder, and the unit's settings base in
`mirror-flags.json`: the value last decided (`v`), the greatest rank the
deciding pass read (`rank`), and `seen`, every value a deciding pass read or a
publish displaced. Each copy has a value, a rank (`lastActivityAt`) and a
stamp (its file's mtime).

The app is modeled as the 2.9939.2 bundle shows it (2026-09-26 report):

* it holds in memory the record of the folder it loads, as that file was at
  the load, and keeps holding a folder's record after it switches away (a
  session that was running parks and keeps saving); `mem` over-approximates
  that: every folder ever loaded keeps its memory;
* `pick` is the model picker or `set_session_model` in the loaded folder: it
  changes memory and the file, and not the rank (`commitSessionModel` never
  touches `lastActivityAt`). The app ignores a pick of the value it holds;
* `turn` is any activity (a turn, a respawn): it writes memory, raises the
  rank, and records the value in the session's transcript (`ran`), which is
  account-agnostic and append-only;
* `save` is every other save (focus, a PR poll, another field): it writes
  memory and keeps the rank.

Every app write stamps the file with the app's clock (`tick`); the mirror's
writes keep the stamp, as `keep_mtime` does. A memory is stale when the
mirror wrote the copy after the app read it; a stale memory's saves are the
known limit of `2026-09-24-mirror-load-gap.md`. With `honest=True` the app
re-reads a copy the mirror writes, so no memory is ever stale.

A pass is `pass_decide` (read every copy, decide), then `pass_publish`: the
pre-check holds the session if a copy it would write changed value or rank
since the read (nothing written; the base keeps `v` and `rank`, and `seen`
gains the values the publish would have displaced, the code's write-ahead);
otherwise every differing copy gets the decided value and the base advances.
The write-level interleavings inside a publish (rollbacks) are the flag
protocol's, shared code checked by `mirror_flags_model.py`. A pass can be
cancelled before it publishes.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from typing import Iterator

IDLE, DECIDED = "idle", "decided"
VALUES = (0, 1, 2)


@dataclass(frozen=True)
class Base:
    v: int | None                    # the value last decided; None before the first decision
    rank: int                        # the greatest rank the deciding pass read
    seen: frozenset[int]             # every value a deciding pass read or a publish displaced


@dataclass(frozen=True)
class State:
    value: tuple[int, ...]           # the unit in each account's file
    rank: tuple[int, ...]            # each file's lastActivityAt
    stamp: tuple[int, ...]           # each file's mtime: the tick of the app's last write
    mem: tuple[int | None, ...]      # what the app holds for each account's record
    loaded: int                      # the folder the app has loaded
    tick: int                        # the app's clock
    base: Base | None
    phase: str = IDLE
    snap: tuple[tuple[int, int, int], ...] | None = None   # (value, rank, stamp) as read
    decided: int | None = None
    #: Ghost: the value a clean publish converged every copy to, while no
    #: pick and no activity has happened since.
    settled: int | None = None
    #: Ghost: the picks since the last converging publish, (value, whether no
    #: pass had seen it when it was picked).
    picks: tuple[tuple[int, bool], ...] = ()
    #: Ghost: the activity since the last converging publish, each marked by
    #: whether it ran on a stale memory.
    turns: tuple[bool, ...] = ()
    #: Ghost: accounts whose memory predates the mirror's last write there.
    stale: frozenset[int] = frozenset()
    #: The values any turn ran on: the transcript the code reads.
    ran: frozenset[int] = frozenset()
    #: Ghost: values some pick produced. Independent of what the rule reads,
    #: it is what "a pick won" is checked against.
    picked: frozenset[int] = frozenset()


def initial(value: tuple[int, ...], rank: tuple[int, ...], stamp: tuple[int, ...], *,
            loaded: int = 0, base: Base | None = None, ran: frozenset[int] | None = None,
            picked: frozenset[int] = frozenset()) -> State:
    """A store before a pass: the app loaded `loaded` and holds what it read.
    By default every value on disk has run (none is a pick)."""
    mem = tuple(value[a] if a == loaded else None for a in range(len(value)))
    settled = value[0] if base is not None and base.v is not None \
        and all(item == base.v for item in value) else None
    return State(value=value, rank=rank, stamp=stamp, mem=mem, loaded=loaded,
                 tick=max((*rank, *stamp)) + 1, base=base, settled=settled,
                 ran=frozenset(value) if ran is None else ran, picked=picked)


def settled_root(accounts: int, v: int) -> State:
    """Every copy agrees on `v` and a clean pass recorded it."""
    zeros = (0,) * accounts
    return initial((v,) * accounts, zeros, zeros, base=Base(v, 0, frozenset({v})))


# --- the rule (decide_setting) -----------------------------------------------

def _best(snap, indices, key) -> int:
    top = indices[0]
    for index in indices[1:]:
        if key(snap[index]) > key(snap[top]):
            top = index
    return snap[top][0]


def later_only(snap, ran: frozenset[int]) -> list[int]:
    """Copies whose value never ran and only copies written after the last
    activity hold: `decide_setting`'s later picks."""
    last = max(rank for _v, rank, _s in snap)
    return [index for index, (value, _r, _s) in enumerate(snap)
            if value not in ran
            and all(stamp > last for other, _r2, stamp in snap if other == value)]


def decide(snap, base: Base | None, *, ran: frozenset[int] | None) -> int:
    """`decide_setting`: agree, later, first, new, activity, base. `ran` is the
    transcript for the unit that has one (the model), else None."""
    values = {value for value, _r, _s in snap}
    if len(values) == 1:
        return snap[0][0]

    def activity(item):
        return (item[1], item[2])

    everyone = list(range(len(snap)))
    if base is None or base.v is None:
        if ran is not None:
            later = later_only(snap, ran)
            if later:
                return _best(snap, later, lambda item: item[2])
        return _best(snap, everyone, activity)
    new = [index for index, (value, _r, _s) in enumerate(snap) if value not in base.seen]
    if new:
        return _best(snap, new, activity)
    active = [index for index, (_v, rank, _s) in enumerate(snap) if rank > base.rank]
    if active:
        return _best(snap, active, activity)
    return base.v


# --- actions ---------------------------------------------------------------------

def _clock(state: State, limit: int) -> bool:
    return state.tick < limit


def load(state: State, account: int) -> State | None:
    if account == state.loaded:
        return None
    mem = list(state.mem)
    mem[account] = state.value[account]
    return replace(state, loaded=account, mem=tuple(mem), stale=state.stale - {account})


def pick(state: State, value: int, *, limit: int) -> State | None:
    here = state.loaded
    if not _clock(state, limit) or state.mem[here] == value:
        return None
    novel = (state.base is not None and state.base.v is not None
             and value not in state.base.seen)
    return replace(state, value=_set(state.value, here, value), mem=_set(state.mem, here, value),
                   stamp=_set(state.stamp, here, state.tick), tick=state.tick + 1,
                   picks=state.picks + ((value, novel),), settled=None,
                   stale=state.stale - {here}, picked=state.picked | {value})


def turn(state: State, account: int, *, limit: int) -> State | None:
    held = state.mem[account]
    if held is None or not _clock(state, limit):
        return None
    return replace(state, value=_set(state.value, account, held),
                   rank=_set(state.rank, account, state.tick),
                   stamp=_set(state.stamp, account, state.tick), tick=state.tick + 1,
                   turns=state.turns + (account in state.stale,), settled=None,
                   ran=state.ran | {held})


def save(state: State, account: int, *, limit: int) -> State | None:
    held = state.mem[account]
    if held is None or not _clock(state, limit):
        return None
    return replace(state, value=_set(state.value, account, held),
                   stamp=_set(state.stamp, account, state.tick), tick=state.tick + 1)


def pass_decide(state: State, *, picked: bool,
                transcript: frozenset[int] | None = None) -> State | None:
    """Read every copy and decide. `picked` means the unit is the model, whose
    later picks the transcript tells apart; the code reads the transcript a
    moment after the copies, so `transcript` may be a later one."""
    if state.phase != IDLE:
        return None
    snap = tuple(zip(state.value, state.rank, state.stamp))
    ran = (state.ran if transcript is None else transcript) if picked else None
    return replace(state, phase=DECIDED, snap=snap, decided=decide(snap, state.base, ran=ran))


def dirty(state: State) -> tuple[int, ...]:
    assert state.snap is not None
    return tuple(a for a, (value, _r, _s) in enumerate(state.snap) if value != state.decided)


def held(state: State) -> bool:
    assert state.snap is not None
    return any((state.value[a], state.rank[a]) != state.snap[a][:2] for a in dirty(state))


def _idle(state: State) -> State:
    return replace(state, phase=IDLE, snap=None, decided=None)


def pass_publish(state: State, *, honest: bool) -> State | None:
    if state.phase != DECIDED:
        return None
    assert state.snap is not None and state.decided is not None
    old = state.base
    displaced = frozenset(state.snap[a][0] for a in dirty(state))
    seen = (old.seen if old is not None else frozenset()) | displaced
    if held(state):
        base = old
        if old is not None or displaced:
            base = Base(old.v if old else None, old.rank if old else 0, seen)
        return replace(_idle(state), base=base)
    value, mem, stale = list(state.value), list(state.mem), set(state.stale)
    for a in dirty(state):
        value[a] = state.decided
        if mem[a] is not None:
            if honest:
                mem[a] = state.decided          # an app that re-reads what the mirror wrote
            else:
                stale.add(a)
    base = Base(state.decided, max(rank for _v, rank, _s in state.snap),
                seen | {item[0] for item in state.snap} | {state.decided})
    after = replace(_idle(state), value=tuple(value), mem=tuple(mem), base=base,
                    stale=frozenset(stale))
    if all(item == state.decided for item in value):
        after = replace(after, settled=state.decided, picks=(), turns=())
    return after


def cancel(state: State) -> State | None:
    return _idle(state) if state.phase == DECIDED else None


def _set(values: tuple, index: int, item) -> tuple:
    changed = list(values)
    changed[index] = item
    return tuple(changed)


def successors(state: State, *, picked: bool, honest: bool,
               limit: int) -> Iterator[tuple[str, State]]:
    accounts = len(state.value)
    for a in range(accounts):
        for label, step in ((f"load({a})", lambda s: load(s, a)),
                            (f"turn({a})", lambda s: turn(s, a, limit=limit)),
                            (f"save({a})", lambda s: save(s, a, limit=limit))):
            nxt = step(state)
            if nxt is not None:
                yield label, nxt
    for v in VALUES:
        nxt = pick(state, v, limit=limit)
        if nxt is not None:
            yield f"pick({v})", nxt
    for label, nxt in (("pass_decide", pass_decide(state, picked=picked)),
                       ("pass_publish", pass_publish(state, honest=honest)),
                       ("cancel", cancel(state))):
        if nxt is not None:
            yield label, nxt


# --- properties ------------------------------------------------------------------

def _decided_base(state: State) -> bool:
    return state.base is not None and state.base.v is not None


def overwrite_allowed(before: State, a: int, *, picked: bool) -> bool:
    """Newer is never overwritten by older: the mirror writes the decided value
    over copy `a` only if a copy holding that value is at least as active as
    `a` (rank, then stamp), or some pick produced that value (the ghost
    `picked`, which the rule never reads), or the value is a change no pass
    had seen, or `a` has had no activity since the base was decided (so its
    difference is a save from memory, not something newer). At a first
    decision only the first two count: a re-save of a value that ran must
    never beat newer activity."""
    snap, decided, base = before.snap, before.decided, before.base
    assert snap is not None
    if any(snap[b][0] == decided and snap[b][1:] >= snap[a][1:] for b in range(len(snap))):
        return True
    if _decided_base(before):
        return decided not in base.seen or snap[a][1] <= base.rank
    return decided in before.picked


def check_step(before: State, label: str, after: State, *, picked: bool,
               honest: bool) -> list[str]:
    """The step properties, each named for what it proves. Empty: all hold."""
    broken = []
    if label == "pass_decide":
        decided = after.decided
        values = set(before.value)
        if _decided_base(before):
            if len(values) == 1 and before.base.v in values and decided != before.base.v:
                broken.append("idempotence")
            if len(before.picks) == 1 and before.picks[0][1] and decided != before.picks[0][0]:
                broken.append("intent-wins")
            if not before.picks and before.turns:
                latest = max(range(len(before.rank)), key=lambda a: before.rank[a])
                if decided != before.value[latest]:
                    broken.append("activity-wins")
            if not before.picks and before.turns and all(before.turns) \
                    and decided != before.base.v:
                broken.append("no-stale-resurrection")
        if before.settled is not None and decided != before.settled:
            broken.append("never-undo-settled")
    if label == "pass_publish":
        wrote = [a for a in range(len(before.value)) if after.value[a] != before.value[a]]
        if held(before):
            if wrote:
                broken.append("hold-writes-nothing")
            if before.base is not None and (after.base.v, after.base.rank) != \
                    (before.base.v, before.base.rank):
                broken.append("hold-keeps-base")
            if before.base is not None and not before.base.seen <= after.base.seen:
                broken.append("seen-only-grows")
        else:
            if any((before.value[a], before.rank[a]) != before.snap[a][:2] for a in wrote):
                broken.append("no-lost-update")
            if after.base is None or after.base.v != before.decided \
                    or any(after.value[a] != before.decided for a in dirty(before)):
                broken.append("base-agreement")
            if any(not overwrite_allowed(before, a, picked=picked) for a in wrote):
                broken.append("newer-never-overwritten")
            if any(after.stamp[a] != before.stamp[a] or after.rank[a] != before.rank[a]
                   for a in wrote):
                broken.append("keeps-rank-and-mtime")
    if label == "cancel" and (after.value != before.value or after.base != before.base):
        broken.append("cancellation-safety")
    return broken


def converges_in_one_clean_pass(state: State, *, picked: bool, honest: bool) -> bool:
    if state.phase != IDLE:
        return True
    decided = pass_decide(state, picked=picked)
    after = pass_publish(decided, honest=honest)
    return all(item == decided.decided for item in after.value) and \
        after.base.v == decided.decided


def explore(roots: list[State], *, picked: bool, honest: bool,
            limit: int) -> tuple[int, dict[str, tuple]]:
    """Breadth-first over every reachable state; (states, first trace per broken property)."""
    seen: dict[State, tuple] = {root: () for root in roots}
    queue: deque[State] = deque(roots)
    broken: dict[str, tuple] = {}
    while queue:
        state = queue.popleft()
        trace = seen[state]
        if not converges_in_one_clean_pass(state, picked=picked, honest=honest):
            broken.setdefault("convergence", trace)
        for label, nxt in successors(state, picked=picked, honest=honest, limit=limit):
            for name in check_step(state, label, nxt, picked=picked, honest=honest):
                broken.setdefault(name, trace + (label,))
            if nxt not in seen:
                seen[nxt] = trace + (label,)
                queue.append(nxt)
    return len(seen), broken
