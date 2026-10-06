"""The desktop sidebar mirror's flag protocol, as a finite state machine.

This is the executable twin of `docs/formal/MirrorFlags.tla`: the same
variables, the same actions, the same properties. `test_mirror_flags_model.py`
explores every reachable state of it exhaustively, and
`test_mirror_flags_stateful.py` drives the real `Mirror` on real files in
lockstep with it, so the implementation and this model are held to one
meaning. The TLA+ module states the same thing for TLC, which has not been run
on it (Max, 2026-09-25: skip TLC for now).

One session, one boolean flag (`isArchived`; `isStarred` and the title run
through the same loop field by field), a copy of its record in each account
folder, and the mirror's state in `mirror-flags.json`.

**The app.** It is modeled as the bundle (2.9939.2 and 2.9939.4) behaves,
read in `docs/reports/2026-09-29-mirror-flag-lineage.md`:

* it holds in memory the record of every copy in the folder it loaded, as the
  file was at that load, and of folders where a session was parked at a
  switch (`load(a, park=True)`), and never re-reads a record it holds;
* a user's action changes the loaded folder's memory and saves it;
* any app save writes the whole memory, whatever the file holds now: an
  honest save and a stale re-save are the same action (`app_save`). There is
  no "honest app" switch any more; stale saves are always possible;
* `focus` is a save that changes nothing the protocol reads, so it matters
  only to a publish in progress.

**Lineage.** Every file carries a lineage: 0 for a record no mirror write has
stamped, or a stamp the mirror minted and wrote into `sessionSettings`. The
app round-trips `sessionSettings` untouched, so a save from memory carries
the lineage of the file that memory was loaded from. The mirror keeps two
tables: `known` (lineage -> the value the mirror wrote with a stamp, or, for
a lineage it did not mint, the value its copies held when a publish first
saw it) and `seen` (per copy and lineage, the value the last publish read
there, when it differs from `known`). A copy's reference is `seen`, else
`known`, else the merge base; a copy votes only when its value differs from
its reference. A stale re-save carries the lineage the mirror displaced, and
the value the mirror recorded for it, so it never votes.

**A pass** is a sequence of steps, because the app can write between any two:

* `pass_decide` snapshots every copy and decides: no vote keeps the base (with
  no base, agreement, else archived-anywhere); votes that agree win; votes
  that disagree go to the latest save;
* `pass_check` is the pre-check: if a copy the pass would write changed since
  the snapshot, the session is held (nothing written, nothing recorded).
  Otherwise the publish mints one stamp and records it in `known` BEFORE any
  write (the write-ahead);
* `pass_write` writes one copy, in path order, with the new stamp, keeping the
  file's save time. It fails if the app or the user rewrote that copy since
  the pre-check; the copies already written, and not rewritten since, are then
  put back, and nothing is committed. After the last write the publish
  commits: the base becomes the decision and `seen` records what was read;
* `crash` stops a publish between two writes: the written copies stay, only
  the write-ahead survives, nothing is committed;
* `cancel` stops a pass before its pre-check. A pass that cannot read every
  copy holds the session, which is the same as `cancel` here.

What the model leaves out: an app rename landing between the code's last
signature check and its own `os.replace` (one syscall); retention (the code
forgets a lineage no file has held for `LINEAGE_RETAIN_S`, the model the
moment nothing can hold it); spread copies (the code stamps them like a flag
write, `test_sessions_mirror_lineage.py` covers them).
"""

from __future__ import annotations

from collections import deque
from typing import Iterator, NamedTuple

IDLE, DECIDED, PUBLISHING = "idle", "decided", "publishing"
LEGACY = 0

#: A file: (flag, lineage, save time).
File = tuple[bool, int, int]


class State(NamedTuple):
    files: tuple[File, ...]
    #: What the app holds for each account's copy: (flag, lineage), or None.
    mem: tuple[tuple[bool, int] | None, ...]
    loaded: int
    base: bool | None = None
    #: lineage -> value (the stamp's written value, or a first-seen lineage's).
    known: frozenset[tuple[int, bool]] = frozenset()
    #: (account, lineage, value): what the last publish read there.
    seen: frozenset[tuple[int, int, bool]] = frozenset()
    phase: str = IDLE
    #: What the pass read: (flag, lineage) per copy, and the save times it
    #: decided by are not needed after the decision.
    snap: tuple[tuple[bool, int], ...] | None = None
    decided: bool | None = None
    stamp: int = 0
    #: While publishing: the copies still to write, in path order.
    pending: tuple[int, ...] = ()
    #: While publishing: (account, (flag, lineage) before the write).
    written: tuple[tuple[int, tuple[bool, int]], ...] = ()
    #: While publishing: the copies the app or the user rewrote since the check.
    touched: frozenset[int] = frozenset()
    #: Ghost: the value a clean publish converged every copy to; cleared by
    #: any user action. While set, no pass may decide or write anything else.
    settled: bool | None = None
    #: Ghost: the values the user set since the last converging publish.
    acts: frozenset[bool] = frozenset()
    #: Ghost: the value of the user's last action, if any since then.
    last: bool | None = None
    #: Ghost: per copy, the user's actions on it since the read of the last
    #: publish that went through (saturating at 3).
    unread: tuple[int, ...] = ()
    #: Ghost: the same counts as this pass's read found them.
    unread_at_read: tuple[int, ...] = ()
    #: Ghost: since the last converging publish, some copy took two of the
    #: user's actions between two committed reads (a reversal no publish saw).
    merged: bool = False


# --- the mirror's references --------------------------------------------------

def replace(state: State, **changes) -> State:
    return state._replace(**changes)


def known_value(state: State, lineage: int) -> bool | None:
    for key, value in state.known:
        if key == lineage:
            return value
    return None


def reference(state: State, account: int, lineage: int) -> bool | None:
    """What the mirror knows copy `account` held under `lineage`."""
    for who, key, value in state.seen:
        if who == account and key == lineage:
            return value
    value = known_value(state, lineage)
    return state.base if value is None else value


def votes(state: State, files: tuple[File, ...]) -> list[tuple[int, bool, int]]:
    """(save time, value, account) for every copy that differs from its reference."""
    found = []
    for account, (value, lineage, time) in enumerate(files):
        ref = reference(state, account, lineage)
        if ref is not None and value != ref:
            found.append((time, value, account))
    return found


def decide(state: State, files: tuple[File, ...]) -> bool:
    """`sync_flags`: no vote keeps the base; agreeing votes win; the latest
    save wins a conflict. With no base, agreement, else archived-anywhere."""
    cast = votes(state, files)
    if cast:
        values = {value for _t, value, _a in cast}
        if len(values) == 1:
            return next(iter(values))
        return max(cast)[1]
    if state.base is not None:
        return state.base
    values = {value for value, _l, _t in files}
    return next(iter(values)) if len(values) == 1 else True


# --- construction -----------------------------------------------------------------

def initial(accounts: int, value: bool = False) -> State:
    """Every copy agrees, nothing is stamped or synced; the app loaded account 0."""
    return State(files=((value, LEGACY, 0),) * accounts, loaded=0,
                 mem=((value, LEGACY),) + (None,) * (accounts - 1),
                 unread=(0,) * accounts)


def _now(state: State) -> int:
    return max(time for _v, _l, time in state.files) + 1


def _touch(state: State, account: int) -> frozenset[int]:
    return state.touched | {account} if state.phase == PUBLISHING else state.touched


def _set_file(state: State, account: int, file: File) -> tuple[File, ...]:
    files = list(state.files)
    files[account] = file
    return tuple(files)


def canonical(state: State, *, order: bool = True) -> State:
    """Forget lineages nothing can hold, renumber the rest, and rank the save
    times, so equivalent states compare equal. Without `order` the ghosts only
    the last-action properties read are dropped too; nothing else reads them."""
    order: list[int] = []

    def note(lineage: int) -> None:
        if lineage != LEGACY and lineage not in order:
            order.append(lineage)

    for _v, lineage, _t in state.files:
        note(lineage)
    for held in state.mem:
        if held is not None:
            note(held[1])
    if state.snap is not None:
        for _v, lineage in state.snap:
            note(lineage)
    for _a, (_v, lineage) in state.written:
        note(lineage)
    if state.phase == PUBLISHING:
        note(state.stamp)
    rename = {LEGACY: LEGACY, **{old: new for new, old in enumerate(order, 1)}}
    live = set(rename)
    rank = {time: index for index, time in enumerate(sorted({t for _v, _l, t in state.files}))}

    def file(entry: File) -> File:
        return (entry[0], rename[entry[1]], rank[entry[2]])

    def read(entry: tuple[bool, int]) -> tuple[bool, int]:
        return (entry[0], rename[entry[1]])

    return replace(
        state,
        files=tuple(file(entry) for entry in state.files),
        mem=tuple(None if held is None else (held[0], rename[held[1]]) for held in state.mem),
        known=frozenset((rename[key], value) for key, value in state.known if key in live),
        seen=frozenset((who, rename[key], value) for who, key, value in state.seen
                       if key in live),
        snap=None if state.snap is None else tuple(read(entry) for entry in state.snap),
        written=tuple((a, read(before)) for a, before in state.written),
        stamp=rename.get(state.stamp, 0) if state.phase == PUBLISHING else 0,
        **({} if order else {"last": None, "unread": (0,) * len(state.files),
                             "unread_at_read": (), "merged": False}))


# --- the app ------------------------------------------------------------------------

def load(state: State, account: int, *, park: bool = False) -> State | None:
    """The app loads another account's folder. Without parking it releases the
    folder it held; a record it still holds (a parked one) is reinstated from
    memory, never re-read."""
    if account == state.loaded:
        return None
    mem = list(state.mem)
    if not park:
        mem[state.loaded] = None
    if mem[account] is None:
        value, lineage, _time = state.files[account]
        mem[account] = (value, lineage)
    return replace(state, loaded=account, mem=tuple(mem))


def release(state: State, account: int) -> State | None:
    """A parked session is retired (its account signed out, or the app quit)."""
    if account == state.loaded or state.mem[account] is None:
        return None
    mem = list(state.mem)
    mem[account] = None
    return replace(state, mem=tuple(mem))


def app_save(state: State, account: int) -> State | None:
    """The app saves a record it holds: the whole memory, lineage included."""
    held = state.mem[account]
    if held is None:
        return None
    value, lineage, _time = state.files[account]
    if held == (value, lineage):
        return None                          # a save that changes nothing read: `focus`
    return replace(state, files=_set_file(state, account, (held[0], held[1], _now(state))),
                   touched=_touch(state, account))


def focus(state: State, account: int) -> State:
    """An app save that keeps what the protocol reads; only a publish cares."""
    return replace(state, touched=_touch(state, account))


def user_set(state: State, value: bool) -> State | None:
    """The user changes the flag in the loaded folder: memory, then a save."""
    here = state.loaded
    held = state.mem[here]
    if held is None or held[0] == value:
        return None
    mem = list(state.mem)
    mem[here] = (value, held[1])
    unread = list(state.unread)
    unread[here] = min(unread[here] + 1, 3)
    return replace(state, mem=tuple(mem),
                   files=_set_file(state, here, (value, held[1], _now(state))),
                   touched=_touch(state, here), settled=None,
                   acts=state.acts | {value}, last=value, unread=tuple(unread))


# --- the mirror -----------------------------------------------------------------------

def pass_decide(state: State) -> State | None:
    if state.phase != IDLE:
        return None
    return replace(state, phase=DECIDED, snap=tuple((v, l) for v, l, _t in state.files),
                   decided=decide(state, state.files), unread_at_read=state.unread)


def dirty(state: State) -> tuple[int, ...]:
    assert state.snap is not None
    return tuple(a for a, (value, _l) in enumerate(state.snap) if value != state.decided)


def moved(state: State, account: int) -> bool:
    """The copy's flag or lineage changed since the pass read it."""
    assert state.snap is not None
    return state.files[account][:2] != state.snap[account]


def held(state: State) -> bool:
    return any(moved(state, a) for a in dirty(state))


def _idle(state: State, **changes) -> State:
    return replace(state, phase=IDLE, snap=None, decided=None, stamp=0, pending=(),
                   written=(), touched=frozenset(), unread_at_read=(), **changes)


def _commit(state: State, files: tuple[File, ...]) -> State:
    """The publish went through: the base advances and `seen` records the read."""
    assert state.snap is not None and state.decided is not None
    known = dict(state.known)
    by_lineage: dict[int, list[bool]] = {}
    for value, lineage in state.snap:
        by_lineage.setdefault(lineage, []).append(value)
    for lineage, values in by_lineage.items():
        if lineage not in known:
            # A lineage no stamp of this mirror names: its copies' values as
            # first seen (the majority; a tie goes to the value decided).
            count = sum(values)
            known[lineage] = (count * 2 > len(values)
                              or (count * 2 == len(values) and state.decided))
    seen = {(who, key): value for who, key, value in state.seen}
    for account, (value, lineage) in enumerate(state.snap):
        if value != known[lineage]:
            seen[(account, lineage)] = value
        else:
            seen.pop((account, lineage), None)
    # A converging publish is one that read what it converged: the ghosts
    # reset only when no copy moved since the read, other than by this
    # publish's own writes.
    written = {a for a, _b in state.written} | ({state.pending[0]} if state.pending else set())
    converged = (all(value == state.decided for value, _l, _t in files)
                 and all(files[a][:2] == state.snap[a] for a in range(len(files))
                         if a not in written))
    at_read = state.unread_at_read or (0,) * len(state.unread)
    unread = tuple(max(0, now - then) for now, then in zip(state.unread, at_read))
    merged = state.merged or any(count >= 2 for count in at_read)
    return _idle(state, files=files, base=state.decided, unread=unread,
                 known=frozenset(known.items()),
                 seen=frozenset((who, key, value) for (who, key), value in seen.items()),
                 settled=state.decided if converged else None,
                 acts=frozenset() if converged else state.acts,
                 last=None if converged else state.last,
                 merged=False if converged else merged)


def _fresh_stamp(state: State) -> int:
    used = {lineage for _v, lineage, _t in state.files}
    used |= {held[1] for held in state.mem if held is not None}
    used |= {key for key, _v in state.known}
    return max(used | {LEGACY}) + 1


def pass_check(state: State) -> State | None:
    if state.phase != DECIDED:
        return None
    if held(state):
        return _idle(state)                     # nothing written, nothing recorded
    if not dirty(state):
        return _commit(state, state.files)
    stamp = _fresh_stamp(state)
    return replace(state, phase=PUBLISHING, stamp=stamp, pending=dirty(state), written=(),
                   touched=frozenset(),
                   known=state.known | {(stamp, state.decided)})   # the write-ahead


def rolls_back(state: State) -> bool:
    return state.phase == PUBLISHING and state.pending[0] in state.touched


def pass_write(state: State) -> State | None:
    if state.phase != PUBLISHING:
        return None
    assert state.decided is not None
    target = state.pending[0]
    if target in state.touched:
        files = list(state.files)
        time = _now(state)
        for account, (value, lineage) in state.written:
            if account not in state.touched:
                files[account] = (value, lineage, time)
        return _idle(state, files=tuple(files))
    before = state.files[target]
    files = _set_file(state, target, (state.decided, state.stamp, before[2]))
    if len(state.pending) == 1:
        return _commit(state, files)
    return replace(state, files=files, pending=state.pending[1:],
                   written=state.written + ((target, before[:2]),))


def pass_publish(state: State) -> State:
    """The pre-check and every write, with nothing in between."""
    state = pass_check(state)
    while state.phase == PUBLISHING:
        state = pass_write(state)
    return state


def crash(state: State) -> State | None:
    """The process dies between two writes: the write-ahead and the written
    copies survive; nothing is committed."""
    if state.phase != PUBLISHING or not state.written:
        return None
    return _idle(state)


def cancel(state: State) -> State | None:
    if state.phase != DECIDED:
        return None
    return _idle(state)


def successors(state: State, *, parking: bool = True) -> Iterator[tuple[str, State]]:
    accounts = len(state.files)
    for a in range(accounts):
        for park in ((False, True) if parking else (False,)):
            nxt = load(state, a, park=park)
            if nxt is not None:
                yield f"load({a}{', park' if park else ''})", nxt
        if parking:
            nxt = release(state, a)
            if nxt is not None:
                yield f"release({a})", nxt
        nxt = app_save(state, a)
        if nxt is not None:
            yield f"app_save({a})", nxt
        if state.phase == PUBLISHING and a not in state.touched:
            yield f"focus({a})", focus(state, a)
    for value in (False, True):
        nxt = user_set(state, value)
        if nxt is not None:
            yield f"user_set({value})", nxt
    for name, step in (("pass_decide", pass_decide), ("pass_check", pass_check),
                       ("pass_write", pass_write), ("crash", crash), ("cancel", cancel)):
        nxt = step(state)
        if nxt is not None:
            yield name, nxt


# --- properties ---------------------------------------------------------------------

def check_step(before: State, label: str, after: State) -> list[str]:
    """The step properties; each names what it proves. Empty means all hold."""
    broken = []
    wrote = {a for a in range(len(before.files)) if after.files[a] != before.files[a]}
    if label == "pass_decide":
        cast = votes(before, before.files)
        # Idempotence: a converged state with its base decides itself.
        if before.base is not None and all(v == before.base for v, _l, _t in before.files) \
                and after.decided != before.base:
            broken.append("idempotence")
        # A vote wins: when every vote carries the same value, it is decided.
        if len({value for _t, value, _a in cast}) == 1 and after.decided != cast[0][1]:
            broken.append("vote-wins")
        # Never undo a settled value: once a clean publish converged every
        # copy and no user acted since, no pass decides anything else,
        # whatever the app re-saves from memory.
        if before.settled is not None and after.decided != before.settled:
            broken.append("never-undo-settled")
        # Intent wins: with a base, if the user set only one value since the
        # last publish that converged, a pass decides that value.
        if before.base is not None and len(before.acts) == 1 \
                and after.decided not in before.acts:
            broken.append("intent-wins")
        # The user's last action wins, provided a pass read each copy between
        # any two of the user's actions on it. Expected to hold without
        # parking; with parking an older action saved late by a parked
        # session can outrank it (the latest save wins a conflict).
        if before.base is not None and before.last is not None and not before.merged \
                and max(before.unread) <= 1 and after.decided != before.last:
            broken.append("last-action-wins-when-read")
        # Unconditionally: expected to fail, also when the user reverses an
        # action before any pass read it (the two are the same file).
        if before.base is not None and before.last is not None \
                and after.decided != before.last:
            broken.append("last-action-wins")
    if label == "pass_check":
        if wrote:
            broken.append("hold-writes-nothing")
        if held(before) and (after.base != before.base or after.seen != before.seen):
            broken.append("hold-writes-nothing")
        # The write-ahead: a publish mints a stamp no lineage had, and records
        # its value before the first write carries it.
        if not held(before) and dirty(before):
            if known_value(before, after.stamp) is not None or \
                    known_value(after, after.stamp) != before.decided:
                broken.append("write-ahead")
        if not held(before) and not dirty(before) and after.base != before.decided:
            broken.append("base-agreement")
    if label == "pass_write":
        # No lost update: the mirror writes only a copy nobody rewrote since it
        # last checked it (a pending copy) or wrote it (a written one).
        written = {a for a, _f in before.written}
        if any(a in before.touched or not (not moved(before, a) or a in written)
               for a in wrote):
            broken.append("no-lost-update")
        if rolls_back(before):
            if (after.base != before.base or after.seen != before.seen
                    or any(after.files[a][:2] != f for a, f in before.written
                           if a not in before.touched)
                    or any(a not in written for a in wrote)):
                broken.append("all-or-nothing")
        else:
            if before.settled is not None and any(after.files[a][0] != before.settled
                                                  for a in wrote):
                broken.append("never-undo-settled")
            # The mirror's own writes never vote: they carry a stamp whose
            # known value is what they hold.
            target = before.pending[0]
            if reference(after, target, after.files[target][1]) != after.files[target][0]:
                broken.append("own-write-votes")
            if after.phase == IDLE and (
                    after.base != before.decided
                    or any(after.files[a][0] != before.decided
                           for a in dirty(before) if a not in before.touched)):
                broken.append("base-agreement")
    if label == "cancel" and (after.files != before.files or after.base != before.base
                              or after.seen != before.seen):
        broken.append("cancellation-safety")
    if label == "crash" and (after.base != before.base or after.seen != before.seen
                             or after.files != before.files):
        broken.append("crash-safety")
    return broken


def converges_in_one_clean_pass(state: State) -> bool:
    """From an idle state, one pass with nothing written in between leaves
    every copy and the base equal to the value decided."""
    if state.phase != IDLE:
        return True
    decided = pass_decide(state)
    after = pass_publish(decided)
    return (all(value == decided.decided for value, _l, _t in after.files)
            and after.base == decided.decided)


def explore(accounts: int = 3, *, parking: bool = True, roots: list[State] | None = None,
            limit: int | None = None, depth: int | None = None, order: bool = False,
            ignore: frozenset[str] = frozenset({"last-action-wins"}),
            ) -> tuple[int, dict[str, tuple]]:
    """Breadth-first over every reachable state (up to `limit` states, or
    within `depth` steps of a root); returns (states, first counterexample
    trace per broken property). The
    last-action properties are checked only with `order`, which keeps their
    ghosts and so explores a larger space."""
    if roots is None:
        roots = [initial(accounts, value) for value in (False, True)]
    roots = [canonical(root, order=order) for root in roots]
    seen: dict[State, tuple] = {root: () for root in roots}
    queue: deque[State] = deque(roots)
    broken: dict[str, tuple] = {}
    while queue:
        state = queue.popleft()
        trace = seen[state]
        if depth is not None and len(trace) >= depth:
            continue
        if not converges_in_one_clean_pass(state):
            broken.setdefault("convergence", trace)
        for label, nxt in successors(state, parking=parking):
            for name in check_step(state, label, nxt):
                if name not in ignore:
                    broken.setdefault(name, trace + (label,))
            nxt = canonical(nxt, order=order)
            if nxt not in seen and (limit is None or len(seen) < limit):
                seen[nxt] = trace + (label,)
                queue.append(nxt)
    return len(seen), broken
