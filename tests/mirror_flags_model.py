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

* `pass_recover` (rule `refs` only) resolves a publish record a failed or
  crashed pass left behind, before anything is decided;
* `pass_decide` snapshots every copy and decides;
* `pass_check` is `sync_flags`' pre-check: if any copy the pass would write no
  longer holds the value it read, the session is held (nothing written,
  nothing recorded); otherwise the publish starts (rule `refs`: after the
  publish record is saved);
* `pass_write` writes one copy, in path order. The write fails if the app or
  the user rewrote that copy since the pre-check (the code's signature check
  before the rename); the rollback then starts;
* `rollback_write` puts one written copy back, newest first, unless it was
  rewritten since the mirror's write;
* `pass_commit` writes the merge base.

Three rules decide a session, so the checker can compare them:

* `base`, the rule on `main` before 2026-09-26: agreement wins, otherwise the
  change from the one merge base wins, and with no base archived-anywhere wins.
  The base advances only after a publish that wrote every copy.
* `mine`, the design first proposed for review round 5's finding F2: the same,
  except that a copy whose file is the mirror's own last write does not vote
  (unless no copy is left to vote). The mirror's writes are assumed durably
  journaled.
* `refs`, the rule the code implements: each copy has its own reference, the
  value the mirror's last decision gave it. A copy votes only when it differs
  from its reference; voters that agree win; with no voter the last decision
  stands; voters that disagree fall back to the change from the last
  decision. A publish is recorded before its first write, and what the files
  show afterwards (which writes landed, which were put back) decides the
  record: a decision stands for the copies it reached, and a copy it did not
  reach keeps its own reference.

Faults (`faults=True`): a rollback write that raises (`rollback_write_fails`),
a merge-base write that raises (`commit_fails`), and a crash anywhere after
the decision (`crash`). The code's write-ahead record and the prepared
temporary files make every one of them recoverable for rule `refs`: a crash
keeps the publish record, and `pass_recover` reads which writes landed.

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

Two ghosts say what the user wants. `intent` is the one the 2026-09-25 model
used: what the user set since the last publish that converged every copy and
the base, or `CONFLICT` once they set both values. `latest` is causal: it is
the value of the user's latest action, unless some copy (or a decision in
flight) still holds the other value from an action the user did not know
about when they acted, in which case it is `CONFLICT`. Knowledge follows
values: acting on a copy knows everything that copy's value was derived from,
and a value the mirror writes is derived from the copies it read that held
the decided value. So a user who archives in one account and unarchives in
the same account, or in another account after seeing the archive there, means
"unarchived", whatever the mirror wrote in between.

What the model does not cover: an app rename landing between the code's last
signature check and its own `os.replace` (a window of one syscall), which the
code can narrow but not close with rename(2).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from typing import Iterator

IDLE, DECIDED, PUBLISHING, ROLLING, COMMITTING = (
    "idle", "decided", "publishing", "rolling", "committing")
CONFLICT = "conflict"
BASE, MINE, REFS = "base", "mine", "refs"
RULES = (BASE, MINE, REFS)
EMPTY: frozenset[int] = frozenset()
#: Track the causal ghost (`latest`). Off, every provenance stays empty and
#: `latest` stays None, which leaves every other property as it is and makes
#: the state space several times smaller.
CAUSAL = True


@dataclass(frozen=True, slots=True)
class State:
    copy: tuple[bool, ...]          # the flag in each account's file
    base: bool | None               # the merge base: the last decision; None before the first
    loaded: int                     # the account folder the app has loaded
    mem: tuple[bool | None, ...]    # what the app holds for each account's copy
    rule: str = REFS
    #: Rule `refs`: each copy's own reference (durable). `()` means every
    #: copy's reference is the base, which is also how a converged record is
    #: stored.
    ref: tuple[bool | None, ...] = ()
    phase: str = IDLE
    snap: tuple[bool, ...] | None = None
    decided: bool | None = None
    #: While publishing: the copies still to write, in path order.
    pending: tuple[int, ...] = ()
    #: While publishing or rolling back: the copies this publish has written.
    written: tuple[int, ...] = ()
    #: The copies the app or the user rewrote since the pre-check.
    touched: frozenset[int] = EMPTY
    #: While rolling back: the written copies still to put back, newest first.
    back: tuple[int, ...] = ()
    #: A forward write failed: the publish did not go through.
    failed: bool = False
    #: Rule `refs`, durable: the publish record, saved before the first write
    #: and cleared when the merge base is written: `(decided, targets, prior
    #: references, prior base)`.
    wal: tuple | None = None
    #: Rule `refs`, durable facts the record is resolved by: the targets whose
    #: prepared file was renamed into place, and those put back since.
    landed: frozenset[int] = EMPTY
    restored: frozenset[int] = EMPTY
    #: The copies whose file is the mirror's own last write (its decided
    #: value, landed and not rewritten since). Rule `mine` decides by it; for
    #: the other rules it is a ghost.
    mine: frozenset[int] = EMPTY
    #: `mine` at the snapshot: a rollback puts a copy's provenance back too.
    smine: frozenset[int] = EMPTY
    #: Ghost: the value a clean publish converged every copy to, cleared by
    #: any user action. While set, no pass may write anything else.
    settled: bool | None = None
    #: Ghost: what the user set since the last publish that converged every
    #: copy and the base (None: nothing; CONFLICT: both values).
    intent: bool | str | None = None
    #: Ghost, causal: the value of the user's latest action, or CONFLICT when
    #: a value the user had not seen when they acted still stands against it.
    latest: bool | str | None = None
    #: Ghost provenance, per copy: the user actions its value derives from
    #: (`sup`) and the actions its value knows of (`know`, a superset). Action
    #: ids are renumbered after every step, so the state space stays finite.
    #: `()` means empty for every copy.
    sup: tuple[frozenset[int], ...] = ()
    know: tuple[frozenset[int], ...] = ()
    #: The decision in flight: what it derives from and knows of.
    dsup: frozenset[int] = EMPTY
    dknow: frozenset[int] = EMPTY
    #: The snapshot's provenance, per copy, for a rollback to put back.
    ssup: tuple[frozenset[int], ...] = ()
    sknow: tuple[frozenset[int], ...] = ()


# --- helpers ----------------------------------------------------------------------

def refs(state: State) -> tuple[bool | None, ...]:
    """Each copy's reference; for rules `base` and `mine`, the base."""
    if state.rule == REFS and state.ref:
        return state.ref
    return (state.base,) * len(state.copy)


def _sets(value: tuple[frozenset[int], ...], n: int) -> tuple[frozenset[int], ...]:
    return value if value else (EMPTY,) * n


def voters(state: State) -> tuple[int, ...]:
    """The copies the rule counts as a change."""
    n = len(state.copy)
    if state.rule == REFS:
        reference = refs(state)
        return tuple(a for a in range(n) if state.copy[a] != reference[a])
    if state.rule == MINE:
        return tuple(a for a in range(n) if a not in state.mine) or tuple(range(n))
    return tuple(range(n))


def decide(state: State) -> bool:
    """`sync_flags`' decision under the state's rule."""
    base = state.base
    if state.rule == REFS:
        values = {state.copy[a] for a in voters(state)}
        if not values:
            assert isinstance(base, bool), "with no base every copy votes"
            return base                     # nobody changed anything: the decision stands
        if len(values) == 1:
            return next(iter(values))       # every change agrees
        return (not base) if isinstance(base, bool) else True
    values = {state.copy[a] for a in voters(state)}
    if len(values) == 1:
        return next(iter(values))
    return (not base) if isinstance(base, bool) else True


def initial(accounts: int, value: bool = False, rule: str = REFS) -> State:
    """Every copy agrees and nothing has been synced; the app loaded account 0."""
    copy = (value,) * accounts
    return State(copy=copy, base=None, loaded=0, mem=(value,) + (None,) * (accounts - 1),
                 rule=rule)


def _canon(state: State) -> State:
    """Normalize a state: uniform references as `()`, provenance ids renumbered
    in creation order, knowledge of ids no value derives from dropped."""
    n = len(state.copy)
    ref = state.ref
    if state.rule != REFS or (ref and all(value == state.base for value in ref)):
        ref = ()
    sup, know = _sets(state.sup, n), _sets(state.know, n)
    ssup, sknow = _sets(state.ssup, n), _sets(state.sknow, n)
    live = set().union(*sup, *ssup, state.dsup)
    order = {old: new for new, old in enumerate(sorted(live))}

    def keep(ids: frozenset[int]) -> frozenset[int]:
        return frozenset(order[i] for i in ids if i in order)

    sup = tuple(keep(s) for s in sup)
    know = tuple(keep(k) for k in know)
    ssup = tuple(keep(s) for s in ssup)
    sknow = tuple(keep(k) for k in sknow)
    empty = (EMPTY,) * n
    return replace(state, ref=ref,
                   sup=() if sup == empty and know == empty else sup,
                   know=() if sup == empty and know == empty else know,
                   ssup=() if ssup == empty and sknow == empty else ssup,
                   sknow=() if ssup == empty and sknow == empty else sknow,
                   dsup=keep(state.dsup), dknow=keep(state.dknow))


def _fresh(state: State) -> int:
    n = len(state.copy)
    ids = set().union(*_sets(state.sup, n), *_sets(state.know, n), *_sets(state.ssup, n),
                      *_sets(state.sknow, n), state.dsup, state.dknow)
    return max(ids) + 1 if ids else 0


def _touch(state: State, account: int) -> frozenset[int]:
    if state.phase in (PUBLISHING, ROLLING, COMMITTING):
        return state.touched | {account}
    return state.touched


def _idle(state: State) -> State:
    return replace(state, phase=IDLE, snap=None, decided=None, pending=(), written=(),
                   touched=EMPTY, back=(), failed=False, smine=EMPTY, dsup=EMPTY,
                   dknow=EMPTY, ssup=(), sknow=())


def _record(state: State, base: bool | None, ref: tuple[bool | None, ...], *,
            finish: bool) -> State:
    """Write the merge base. `finish`: a publish went through, so the ghosts
    of the 2026-09-25 model's Finish apply: a converged state settles, and the
    user's intent since the last convergence is delivered."""
    if finish:
        converged = all(value == base for value in state.copy)
        state = replace(state, settled=base if converged else None,
                        intent=None if converged else state.intent)
    return replace(state, base=base, ref=ref if state.rule == REFS else (), wal=None,
                   landed=EMPTY, restored=EMPTY)


# --- resolution (rule refs) -------------------------------------------------------

def resolve(state: State) -> tuple[bool | None, tuple[bool | None, ...]]:
    """The record a publish leaves, from what its files show.

    A publish none of whose writes stands (none landed, or every one put back)
    changes nothing. Otherwise its decision stands: the base is the value
    decided and every copy has it as its reference, except a target the
    publish did not reach, whose reference is the value the pass read there
    (the other value: that is what made it a target).
    """
    decided, targets, prior_ref, prior_base = state.wal
    reached = {t for t in targets if t in state.landed and t not in state.restored}
    if targets and not reached:
        return prior_base, prior_ref
    ref = tuple((not decided) if a in targets and a not in reached else decided
                for a in range(len(state.copy)))
    return decided, ref


def _resolved(state: State) -> State:
    """Resolve the publish record. It is a Finish only if every target holds
    the decision the mirror wrote there: the publish went through, whether or
    not the pass that made it lived to write the base."""
    base, ref = resolve(state)
    decided, targets = state.wal[0], state.wal[1]
    through = all(t in state.landed and t not in state.restored for t in targets)
    return _record(state, base, ref, finish=through)


# --- environment actions (each returns the next state, or None when not enabled) --

def load(state: State, account: int) -> State | None:
    if account == state.loaded:
        return None
    mem = list(state.mem)
    mem[account] = state.copy[account]      # a fresh load reads the file
    return replace(state, loaded=account, mem=tuple(mem))


def _latest(state: State, here: int, value: bool, know: frozenset[int]) -> bool | str:
    """The causal intent of an action setting `value` that knows `know`."""
    n = len(state.copy)
    sup = _sets(state.sup, n)
    against = [sup[c] for c in range(n) if c != here and state.copy[c] != value]
    if state.phase in (DECIDED, PUBLISHING) and state.decided != value:
        against.append(state.dsup)          # a decision in flight still to be written
    if state.phase in (PUBLISHING, ROLLING):
        ssup = _sets(state.ssup, n)         # a value a rollback could put back
        restorable = state.written if state.phase == PUBLISHING else state.back
        against += [ssup[b] for b in restorable if state.snap[b] != value]
    return value if all(ids <= know for ids in against) else CONFLICT


def user_set(state: State, value: bool) -> State | None:
    here = state.loaded
    if state.copy[here] == value:
        return None
    n = len(state.copy)
    sup, know = list(_sets(state.sup, n)), list(_sets(state.know, n))
    latest = state.latest
    if CAUSAL:
        action = _fresh(state)
        knows = know[here] | {action}
        latest = _latest(state, here, value, knows)
        sup[here], know[here] = frozenset({action}), knows
    copy, mem = list(state.copy), list(state.mem)
    copy[here] = mem[here] = value
    intent = value if state.intent in (None, value) else CONFLICT
    return _canon(replace(state, copy=tuple(copy), mem=tuple(mem), settled=None,
                          touched=_touch(state, here), intent=intent, latest=latest,
                          sup=tuple(sup), know=tuple(know), mine=state.mine - {here}))


def app_save(state: State, account: int, *, stale: bool) -> State | None:
    """An app save that changes the file's flag: only a stale memory can. Its
    value comes from before the mirror's write, so nothing the user did since
    knows of it: it gets a fresh provenance."""
    held = state.mem[account]
    if not stale or held is None or held == state.copy[account]:
        return None
    n = len(state.copy)
    sup, know = list(_sets(state.sup, n)), list(_sets(state.know, n))
    if CAUSAL:
        sup[account] = know[account] = frozenset({_fresh(state)})
    copy = list(state.copy)
    copy[account] = held
    return _canon(replace(state, copy=tuple(copy), touched=_touch(state, account),
                          sup=tuple(sup), know=tuple(know), mine=state.mine - {account}))


def focus(state: State, account: int) -> State:
    """An app save that keeps the flag. It changes nothing the protocol reads,
    except that a publish in progress must not write over it, and that the
    file is no longer the mirror's own write."""
    return replace(state, touched=_touch(state, account), mine=state.mine - {account})


# --- the pass ---------------------------------------------------------------------

def pass_recover(state: State) -> State | None:
    """Rule `refs`: resolve a publish record left by a failed or crashed pass."""
    if state.phase != IDLE or state.wal is None:
        return None
    return _canon(_resolved(state))


def pass_decide(state: State) -> State | None:
    if state.phase != IDLE or state.wal is not None:
        return None
    n = len(state.copy)
    decided = decide(state)
    sup, know = _sets(state.sup, n), _sets(state.know, n)
    supporters = [a for a in range(n) if state.copy[a] == decided]
    # With no base the bootstrap rule (archived-anywhere) may override the
    # user by design: what they did before the decision is exempt.
    exempt = state.base is None
    return _canon(replace(state, phase=DECIDED, snap=state.copy, decided=decided,
                          smine=state.mine,
                          intent=None if exempt else state.intent,
                          latest=None if exempt else state.latest,
                          dsup=frozenset().union(*(sup[a] for a in supporters)),
                          dknow=frozenset().union(*(know[a] for a in supporters)),
                          ssup=sup, sknow=know))


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
        return _canon(_idle(state))          # nothing written, nothing recorded
    if not dirty(state):
        after = _record(state, state.decided, (state.decided,) * len(state.copy), finish=True)
        return _canon(_idle(after))
    wal = (state.decided, dirty(state), refs(state), state.base) if state.rule == REFS else None
    return replace(state, phase=PUBLISHING, pending=dirty(state), written=(),
                   touched=EMPTY, wal=wal, landed=EMPTY, restored=EMPTY)


def rolls_back(state: State) -> bool:
    return state.phase == PUBLISHING and state.pending[0] in state.touched


def pass_write(state: State) -> State | None:
    if state.phase != PUBLISHING:
        return None
    assert state.snap is not None and state.decided is not None
    target = state.pending[0]
    if target in state.touched:
        back = tuple(reversed(state.written))
        return replace(state, phase=ROLLING if back else COMMITTING, back=back, pending=(),
                       failed=True)
    n = len(state.copy)
    copy = list(state.copy)
    copy[target] = state.decided
    sup, know = list(_sets(state.sup, n)), list(_sets(state.know, n))
    sup[target], know[target] = state.dsup, state.dknow
    rest = state.pending[1:]
    # A bootstrap publish overwrites by design, whatever the user did since
    # its decision: the pre-check compares values, so it cannot see a user
    # who flipped a copy and flipped it back (`intent` calls that CONFLICT).
    exempt = state.base is None
    return _canon(replace(state, copy=tuple(copy), pending=rest,
                          latest=None if exempt else state.latest,
                          written=state.written + (target,),
                          landed=state.landed | {target} if state.rule == REFS else EMPTY,
                          mine=state.mine | {target}, sup=tuple(sup), know=tuple(know),
                          phase=PUBLISHING if rest else COMMITTING))


def rollback_write(state: State, *, fails: bool = False) -> State | None:
    """Put back the newest written copy, unless it was rewritten since the
    mirror's write (the code's signature check). `fails`: the write raises,
    and the mirror's write stands (the code journals it)."""
    if state.phase != ROLLING:
        return None
    target, rest = state.back[0], state.back[1:]
    after = replace(state, back=rest, phase=ROLLING if rest else COMMITTING)
    if fails or target in state.touched:
        return after
    assert state.snap is not None
    n = len(state.copy)
    copy = list(state.copy)
    copy[target] = state.snap[target]
    sup, know = list(_sets(state.sup, n)), list(_sets(state.know, n))
    sup[target], know[target] = _sets(state.ssup, n)[target], _sets(state.sknow, n)[target]
    mine = state.mine | {target} if target in state.smine else state.mine - {target}
    return _canon(replace(after, copy=tuple(copy), sup=tuple(sup), know=tuple(know), mine=mine,
                          restored=state.restored | {target} if state.rule == REFS else EMPTY))


def pass_commit(state: State) -> State | None:
    """Write the merge base: rule `refs` resolves its record; the other rules
    advance the base only after a publish that wrote every copy."""
    if state.phase != COMMITTING:
        return None
    if state.rule == REFS:
        return _canon(_idle(_resolved(state)))
    if state.failed:
        return _canon(_idle(state))
    return _canon(_idle(_record(state, state.decided, (), finish=True)))


def commit_fails(state: State) -> State | None:
    """The merge-base write raises: the pass fails. Rule `refs` keeps its record."""
    if state.phase != COMMITTING:
        return None
    return _canon(_idle(state))


def crash(state: State) -> State | None:
    """The process dies after the decision: what is on disk stays."""
    if state.phase not in (PUBLISHING, ROLLING, COMMITTING):
        return None
    return _canon(_idle(state))


def pass_publish(state: State) -> State:
    """The pre-check, every write and the base write, with nothing in between."""
    state = pass_check(state)
    while state.phase == PUBLISHING:
        state = pass_write(state)
    while state.phase == ROLLING:
        state = rollback_write(state)
    if state.phase == COMMITTING:
        state = pass_commit(state)
    return state


def full_pass(state: State) -> State:
    """Recovery (if a record is left), then one pass with nothing in between."""
    if state.wal is not None and state.phase == IDLE:
        state = pass_recover(state)
    return pass_publish(pass_decide(state))


def cancel(state: State) -> State | None:
    if state.phase != DECIDED:
        return None
    return _canon(_idle(state))


def successors(state: State, *, stale: bool, faults: bool = False
               ) -> Iterator[tuple[str, State]]:
    accounts = len(state.copy)
    for a in range(accounts):
        nxt = load(state, a)
        if nxt is not None:
            yield f"load({a})", nxt
        nxt = app_save(state, a, stale=stale)
        if nxt is not None:
            yield f"app_save({a})", nxt
        if a in state.mine or (state.phase in (PUBLISHING, ROLLING) and a not in state.touched):
            yield f"focus({a})", focus(state, a)
    for value in (False, True):
        nxt = user_set(state, value)
        if nxt is not None:
            yield f"user_set({value})", nxt
    steps = [("pass_recover", pass_recover), ("pass_decide", pass_decide),
             ("pass_check", pass_check), ("pass_write", pass_write),
             ("rollback_write", rollback_write), ("pass_commit", pass_commit),
             ("cancel", cancel)]
    if faults:
        steps += [("rollback_write_fails", lambda s: rollback_write(s, fails=True)),
                  ("commit_fails", commit_fails), ("crash", crash)]
    for name, step in steps:
        nxt = step(state)
        if nxt is not None:
            yield name, nxt


# --- properties -----------------------------------------------------------------

def check_step(before: State, label: str, after: State) -> list[str]:
    """The step properties; each names what it proves. Empty means all hold."""
    broken = []
    n = len(before.copy)
    wrote = {a for a in range(n) if after.copy[a] != before.copy[a]}
    kept = after.base == before.base and refs(after) == refs(before)
    if label == "pass_decide":
        reference = refs(before)
        # Idempotence: a converged state (every copy holds its reference, and
        # every reference is the base) decides the base.
        if (isinstance(before.base, bool) and all(v == before.base for v in before.copy)
                and all(r == before.base for r in reference) and after.decided != before.base):
            broken.append("idempotence")
        # Change wins, both ways: when every copy that differs from its
        # reference holds the same value, that value is decided.
        if isinstance(before.base, bool):
            moved = {before.copy[a] for a in range(n) if before.copy[a] != reference[a]}
            if len(moved) == 1 and after.decided not in moved:
                broken.append("change-wins")
        # Intent wins (the 2026-09-25 ghost): with a base, a pass decides what
        # the user set since the last publish that converged.
        if isinstance(before.base, bool) and isinstance(before.intent, bool) \
                and after.decided != before.intent:
            broken.append("intent-wins")
        # Latest wins (causal): with a base, a pass decides the value of the
        # user's latest action unless a value they had not seen stands against it.
        if isinstance(before.base, bool) and isinstance(before.latest, bool) \
                and after.decided != before.latest:
            broken.append("latest-wins")
        # The mirror's own writes do not vote: a copy whose file is the
        # mirror's own last write holds its reference.
        if before.rule != MINE and isinstance(before.base, bool) \
                and any(before.copy[a] != reference[a] for a in before.mine):
            broken.append("own-writes-do-not-vote")
    if label == "pass_check":
        # The pre-check writes nothing. A held session keeps its record (all
        # or nothing); one with nothing to write advances it (base agreement).
        if wrote:
            broken.append("hold-writes-nothing")
        if held(before) and not kept:
            broken.append("hold-writes-nothing")
        if not held(before) and not dirty(before) and (
                after.base != before.decided or any(r != before.decided for r in refs(after))):
            broken.append("base-agreement")
        if not held(before) and dirty(before) and not kept:
            broken.append("record-before-writes")
    if label in ("pass_write", "rollback_write", "rollback_write_fails"):
        # No lost update: the mirror writes only a copy nobody rewrote since it
        # last checked it (a pending copy) or wrote it (a written one).
        if any(a in before.touched
               or not (before.copy[a] == before.snap[a] or a in before.written)
               for a in wrote):
            broken.append("no-lost-update")
        if not kept:
            broken.append("record-before-writes")
        # Never undo a settled value: once a clean publish converged every copy
        # and no user acted since, no pass writes anything else.
        if before.settled is not None and any(after.copy[a] != before.settled for a in wrote):
            broken.append("never-undo-settled")
    if label == "rollback_write":
        # All or nothing: the rollback puts a written copy back unless it was
        # rewritten since the mirror's write.
        target = before.back[0]
        if target not in before.touched and after.copy[target] != before.snap[target]:
            broken.append("all-or-nothing")
        if any(a != target for a in wrote):
            broken.append("all-or-nothing")
    if label == "pass_commit" and before.rule == REFS:
        reached = {t for t in before.wal[1] if t in before.landed and t not in before.restored}
        if before.failed and not reached and not kept:
            # All or nothing: a publish every write of which was put back keeps
            # the base and every reference.
            broken.append("all-or-nothing")
        if not before.failed:
            # Base agreement: after a publish that went through, the base and
            # every reference are the value decided, and every copy the pass
            # read differently holds it unless rewritten since.
            if (after.base != before.decided
                    or any(r != before.decided for r in refs(after))
                    or any(after.copy[a] != before.decided
                           for a in dirty(before) if a not in before.touched)):
                broken.append("base-agreement")
    if label == "pass_commit" and before.rule != REFS:
        if before.failed and after.base != before.base:
            broken.append("all-or-nothing")
        if not before.failed and (after.base != before.decided or any(
                after.copy[a] != before.decided for a in dirty(before) if a not in before.touched)):
            broken.append("base-agreement")
    if label in ("pass_recover", "pass_commit", "commit_fails", "crash") and wrote:
        broken.append("no-lost-update")
    if label == "cancel" and (after.copy != before.copy or not kept):
        broken.append("cancellation-safety")
    return broken


def converges_in_one_clean_pass(state: State) -> bool:
    """From an idle state, one pass with nothing written in between (after
    recovering a record left behind) leaves every copy, the base and every
    reference equal to the value decided."""
    if state.phase != IDLE:
        return True
    if state.wal is not None:
        state = pass_recover(state)
    decided = pass_decide(state)
    after = pass_publish(decided)
    value = decided.decided
    return (all(v == value for v in after.copy) and after.base == value
            and all(r == value for r in refs(after)) and after.wal is None)


def explore(accounts: int = 3, *, stale: bool, faults: bool = False, rule: str = REFS,
            roots: list[State] | None = None, limit: int | None = None
            ) -> tuple[int, dict[str, tuple]]:
    """Breadth-first over every reachable state; returns (states, first
    counterexample trace per broken property). Each state keeps only its
    parent and the step that reached it, so memory is linear in states.

    `limit` stops the search after that many states: every state within the
    depth it reached is checked, none beyond. With the causal ghost on, the
    full space runs to tens of gigabytes (2026-09-29), so it is only ever
    explored to a limit."""
    if roots is None:
        roots = [initial(accounts, value, rule) for value in (False, True)]
    parent: dict[State, tuple[State | None, str]] = {root: (None, "") for root in roots}
    queue: deque[State] = deque(roots)
    broken: dict[str, tuple] = {}

    def trace(state: State) -> tuple[str, ...]:
        steps: list[str] = []
        while True:
            before, label = parent[state]
            if before is None:
                return tuple(reversed(steps))
            steps.append(label)
            state = before

    while queue and (limit is None or len(parent) < limit):
        state = queue.popleft()
        if not converges_in_one_clean_pass(state) and "convergence" not in broken:
            broken["convergence"] = trace(state)
        for label, nxt in successors(state, stale=stale, faults=faults):
            for name in check_step(state, label, nxt):
                if name not in broken:
                    broken[name] = trace(state) + (label,)
            if nxt not in parent:
                parent[nxt] = (state, label)
                queue.append(nxt)
    return len(parent), broken
