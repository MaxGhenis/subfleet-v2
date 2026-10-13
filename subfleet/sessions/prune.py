"""Where each session belongs, and which copies the mirror may remove: C-23.28a.

The mirror used to copy every openable session into every folder the desktop
store holds: on 2026-09-25, 2,064 sessions in up to 120 folders, 227,924
files and 2.96 GB, growing by a copy per folder for every new session; on
2026-10-13, 442,801 files and 9.51 GB in 135 folders. Most of those copies
can never be seen. This module decides, as a pure function of what one pass
read, two things the mirror then acts on:

* **Scope**: the folders a session belongs in. The mirror spreads a session
  only into its scope (`mirror.py`, full and hot passes alike).
* **Removals**: the copies outside a session's scope that the mirror may
  delete, and the copies it must keep although they are outside it.

Three rules, each off at zero or false (`policy.json`, `sessions` section):

* `mirror_skip_switch_folders`: a **switch folder** (`folder_kinds.py`: one
  the app only passes through during an account switch, whose org another
  account owns) is in no session's scope. Nobody sees its sidebar. The home
  folder is never one: a home once named stays seeded.
* `mirror_archive_days`: a session whose every copy is archived and unstarred,
  and that has been quiet this many days, belongs only in the **home
  folder**. Quiet means since the later of its newest activity, focus or
  creation and when it was archived. Archiving leaves the activity times
  alone (of 850 archived records the app wrote into one folder on
  2026-09-22, 621 had been quiet 30 days or more), so the mirror stamps the
  archive in its merge base (`archived_at`). Every other account then lists
  the session neither as a session nor as archived.
* `mirror_dead_days`: a session with no transcript (dead: it shows "no
  messages" everywhere and is never spread) that has been quiet this long
  belongs only in the home folder, or nowhere without one.

The home is resolved by the mirror (`Mirror.resolve_home`) against every
folder the store is known to hold, so a home the pass could not list is
still the home: its sessions are then neither spread nor removed.

Removal is a separate switch, `mirror_prune`, because it deletes: without it
the rules only stop the store from growing further. Every copy in a
session's scope survives, and so do its **newest** copies: the one newest by
the record's own activity, focus and creation times, and the one the
spread itself would copy (`lastActivityAt` first), when no copy in scope is
as new. Copies of one session differ there (on 2026-09-25, 1,396 of 1,906
sessions with more than one copy): the mirror spreads a session once and
syncs only its flags and title, so the copy where the work happened carries
the newest record (a moved `cwd`, a changed model), and a mere focus
elsewhere makes another copy newer by focus alone. An archived session
whose home copy is older therefore keeps two or three copies, and the
mirror never rewrites a copy to make one. Any other copy is removed only
when all of these hold:

* the app's loaded folder is known, and the copy is not in it: the app lists
  a folder into memory when it loads it and saves records from that memory,
  so a record removed under it could be listed with no file behind it, or
  written back (`desktop.py`);
* the copy is outside its session's scope, and is not the home folder's;
* the copy has a session id, and the session was neither held nor rewritten
  by this pass's flag sync, so its flags are settled;
* for an archived or dead session, the home folder was listed this pass;
* for a dead session, a project directory its transcript would be in was
  listed without it, so it is known to be dead (a `projects/` the mirror
  reads in the wrong place, or recreated empty, proves nothing);
* for a copy outside its scope only because its folder is a switch folder,
  the folder registry has learned for `folder_kinds.SWITCH_PRUNE_AFTER_S`:
  a folder used less often than the registry has been learning looks like a
  switch folder until it is used again.

Every removal of an openable session is therefore undone by switching its
rule off: the next full pass spreads the session back into every folder in
the wider scope, from its newest surviving copy by the spread's own ranking
(`test_mirror_prune.py` proves the round trip). A dead session is never
spread, as before; its newest copies stay where they are.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from functools import cached_property
from typing import Iterable, Mapping

from .folder_kinds import SWITCH

DAY_S = 86_400.0

#: Why a copy is outside its session's scope.
SWITCH_FOLDER, ARCHIVED, DEAD = "switch folder", "archived", "dead"
#: Why a copy outside its scope is kept anyway.
HELD_LOADED = "in the loaded folder"
HELD_UNKNOWN_LOAD = "the loaded folder is unknown"
HELD_FLAGS = "flags not settled this pass"
HELD_NO_PRUNE = "pruning is off"
HELD_LEARNING = "the folder registry is still learning which folders are switch folders"
HELD_HOME_UNLISTED = "the home folder was not listed this pass"
HELD_DEAD_UNCONFIRMED = "none of the session's project directories was listed"
#: Defensive holds the plan's own construction never reaches; a property
#: test asserts they never occur.
HELD_NO_SURVIVOR = "no surviving copy is as new"
HELD_HOME = "in the home folder"


@dataclass(frozen=True, slots=True)
class Copy:
    """One `local_*.json` as a pass read it."""

    folder: str                 # "<account>/<org>"
    name: str
    identity: str               # cliSessionId; "" when the record has none
    archived: bool = False
    starred: bool = False
    #: The newest of lastActivityAt, lastFocusedAt and createdAt, epoch ms.
    activity_ms: int = 0
    #: The first of lastActivityAt, lastFocusedAt and createdAt the record
    #: has, epoch ms: the spread's own ranking (`mirror._rank`), so the copy
    #: where the work last happened, not one merely focused since.
    rank_ms: int = 0
    #: The file's mtime, epoch seconds: the app's last write of the record
    #: (the mirror keeps its source's mtime on every copy and flag write).
    #: It is not a clock for age. The app re-saves records it holds for
    #: reasons of its own (on 2026-09-25, 191 archived records in the loaded
    #: folder were rewritten within a day, most of them long quiet), so it is
    #: used only where a record has no times of its own, and once, to date
    #: an archive the mirror did not see happen (`Mirror.sync_flags`).
    mtime: float = 0.0

    @property
    def last_sign(self) -> float:
        """Epoch seconds of this copy's last activity, focus or creation."""
        return self.activity_ms / 1000.0 if self.activity_ms > 0 else self.mtime


@dataclass(frozen=True)
class Rules:
    """The policy, as `policy.json` states it (see the module docstring)."""

    skip_switch_folders: bool = False
    archive_days: float = 0.0
    dead_days: float = 0.0
    prune: bool = False

    @property
    def active(self) -> bool:
        return self.skip_switch_folders or self.archive_days > 0 or self.dead_days > 0


@dataclass(frozen=True)
class Situation:
    """What the pass knows besides the copies."""

    folders: tuple[str, ...]
    #: `folder_kinds.Registry.classify` verdicts; a folder not in it is `unknown`.
    kinds: Mapping[str, str]
    #: The folder old archived and dead sessions live in; None: not resolved.
    home: str | None
    #: The folder the app has loaded; None: unknown, so nothing is removed.
    loaded: str | None
    #: Session ids whose transcript exists.
    openable: frozenset[str]
    #: Session id -> epoch seconds it was archived, as the merge base records it.
    archived_at: Mapping[str, float]
    now: float
    #: The folder registry has learned long enough for its switch verdicts to
    #: justify removing a live session's copies (`folder_kinds.SWITCH_PRUNE_AFTER_S`).
    switch_settled: bool = True

    @cached_property
    def every_folder(self) -> frozenset[str]:
        return frozenset(self.folders)

    def switch(self, folder: str) -> bool:
        """A switch folder, unless it is the home: a home once named stays the
        home and stays seeded, whatever the registry comes to call it."""
        return self.kinds.get(folder) == SWITCH and folder != self.home

    @cached_property
    def unswitched(self) -> frozenset[str]:
        """Every folder but the switch folders."""
        return frozenset(folder for folder in self.folders if not self.switch(folder))


@dataclass(frozen=True)
class Scope:
    kind: str                   # live | archived | dead | dead-young
    folders: frozenset[str]


@dataclass(frozen=True)
class Removal:
    folder: str
    name: str
    identity: str
    reason: str                 # SWITCH_FOLDER | ARCHIVED | DEAD
    #: The copies this removal counts on to survive, `(folder, name)` each:
    #: the newest by activity and the newest by the spread's ranking. The
    #: mirror reads them again right before removing.
    keepers: tuple[tuple[str, str], ...]
    activity_ms: int
    rank_ms: int


@dataclass
class Plan:
    """One pass's decision. Per reason, `outside` = `kept` + held + removals."""

    removals: list[Removal] = field(default_factory=list)
    #: Copies outside their scope, by why they are outside it.
    outside: Counter = field(default_factory=Counter)
    #: Of those, the newest copies of their session, which always stay.
    kept: Counter = field(default_factory=Counter)
    #: Of those, copies that could go but stay this pass, by why.
    held: Counter = field(default_factory=Counter)
    #: Sessions by scope kind.
    sessions: Counter = field(default_factory=Counter)


def seeded(folder: str, situation: Situation, rules: Rules) -> bool:
    """A folder sessions are spread into at all."""
    return not (rules.skip_switch_folders and situation.switch(folder))


@dataclass(slots=True)
class Summary:
    """What a session's scope depends on, folded over its copies.

    Scope needs no more than this, so a pass can decide it for every session
    without holding an object per copy (442,801 copies on 2026-10-13).
    """

    copies: int = 0
    #: Every copy is archived.
    archived: bool = True
    #: Some copy is starred.
    starred: bool = False
    #: The newest `Copy.last_sign` over the copies, epoch seconds.
    last_sign: float = 0.0
    #: Some copy sits in a switch folder.
    in_switch: bool = False

    def add(self, archived: bool, starred: bool, last_sign: float, switch: bool) -> None:
        self.copies += 1
        self.archived = self.archived and archived
        self.starred = self.starred or starred
        self.last_sign = max(self.last_sign, last_sign)
        self.in_switch = self.in_switch or switch


def summarize(copies: Iterable[Copy], situation: Situation) -> Summary:
    summary = Summary()
    for copy in copies:
        summary.add(copy.archived, copy.starred, copy.last_sign, situation.switch(copy.folder))
    return summary


def quiet_since(identity: str, copies: Iterable[Copy], situation: Situation) -> float:
    """Epoch seconds of the session's newest sign of life, over every copy."""
    return max(situation.archived_at.get(identity, 0.0),
               summarize(copies, situation).last_sign)


def scope(identity: str, copies: list[Copy], situation: Situation, rules: Rules) -> Scope:
    """The folders a session belongs in, judged over every copy of it."""
    return scope_of(identity, summarize(copies, situation), situation, rules)


def scope_of(identity: str, summary: Summary, situation: Situation, rules: Rules) -> Scope:
    """The folders a session belongs in, from its `Summary`.

    A session with any unarchived or starred copy is live: flag sync has not
    settled it, or the user wants it seen, and a live session belongs
    everywhere. So does an archived one while it is younger than the rule,
    and every session when its rule is off or no home folder is known.
    """
    everywhere = situation.unswitched if rules.skip_switch_folders else situation.every_folder
    age = situation.now - max(situation.archived_at.get(identity, 0.0), summary.last_sign)
    if identity not in situation.openable:
        if rules.dead_days > 0 and age >= rules.dead_days * DAY_S:
            home = frozenset({situation.home}) if situation.home else frozenset()
            return Scope("dead", home)
        return Scope("dead-young", everywhere)
    if (rules.archive_days > 0 and situation.home is not None
            and summary.copies and summary.archived and not summary.starred
            and age >= rules.archive_days * DAY_S):
        return Scope("archived", frozenset({situation.home}))
    return Scope("live", everywhere)


def may_reach_outside(placed: Scope, summary: Summary, rules: Rules) -> bool:
    """Whether a session can have a copy outside its scope at all: only an
    archived or dead one, or one with a copy in an unseeded switch folder.
    Every other session needs no planning beyond its scope."""
    return (placed.kind in ("archived", "dead")
            or (rules.skip_switch_folders and summary.in_switch))


def _tie(copy: Copy, situation: Situation) -> tuple:
    """Of equally new copies: the home's, then one not in a switch folder,
    then the first by path, so the choice is the same every pass."""
    return (copy.folder != situation.home, situation.switch(copy.folder),
            copy.folder, copy.name)


def survivors(copies: list[Copy], inside: frozenset[str], situation: Situation) -> list[Copy]:
    """The copies that stay whatever the rules say.

    Every copy in scope; then, when no copy kept so far is as new by
    activity, the newest by activity; then, likewise, the newest by the
    spread's ranking. So the newest content of every session survives
    without the mirror rewriting anything (see the module docstring).
    """
    kept = [copy for copy in copies if copy.folder in inside]
    if not copies:
        return kept
    for measure in (lambda copy: copy.activity_ms, lambda copy: copy.rank_ms):
        newest = max(measure(copy) for copy in copies)
        if kept and max(measure(copy) for copy in kept) >= newest:
            continue
        best = min((copy for copy in copies if measure(copy) == newest),
                   key=lambda copy: _tie(copy, situation))
        kept.append(best)
    return kept


def keepers(kept: list[Copy], situation: Situation) -> list[Copy]:
    """The survivors a removal counts on: the newest by each measure."""
    chosen: list[Copy] = []
    for measure in (lambda copy: copy.activity_ms, lambda copy: copy.rank_ms):
        newest = max(measure(copy) for copy in kept)
        best = min((copy for copy in kept if measure(copy) == newest),
                   key=lambda copy: _tie(copy, situation))
        if best not in chosen:
            chosen.append(best)
    return chosen


def plan(copies: Iterable[Copy], situation: Situation, rules: Rules, *,
         held: Iterable[str] = (), unconfirmed: Iterable[str] = (),
         blocked: str | None = None) -> Plan:
    """Which copies to remove this pass, and why every other outsider stays.

    `held` names the sessions flag sync held or rewrote this pass: their
    flags are not settled, so none of their copies is removed.
    `unconfirmed` names the sessions without a transcript whose project
    directory (`projects/<slug of a cwd>`) the pass did not list: the
    transcript was not looked for where it would be, so the session is not
    known to be dead, and its copies stay. `blocked` keeps every copy, for
    that reason (an incomplete inventory).
    """
    result = Plan()
    waiting = set(held)
    unproven = set(unconfirmed)
    listed = set(situation.folders)
    # The home is known but was not listed: its copies cannot be counted on,
    # so no archived or dead session is narrowed to it this pass.
    home_away = situation.home is not None and situation.home not in listed
    groups: dict[str, list[Copy]] = {}
    for copy in copies:
        if copy.identity and copy.folder in listed:
            groups.setdefault(copy.identity, []).append(copy)
    for identity in sorted(groups):
        group = sorted(groups[identity], key=lambda copy: (copy.folder, copy.name))
        placed = scope(identity, group, situation, rules)
        result.sessions[placed.kind] += 1
        inside = placed.folders
        reason = {"archived": ARCHIVED, "dead": DEAD}.get(placed.kind, SWITCH_FOLDER)
        kept = survivors(group, inside, situation)
        chosen = {(copy.folder, copy.name) for copy in kept}
        counted_on = keepers(kept, situation)
        best_activity = max(copy.activity_ms for copy in counted_on)
        best_rank = max(copy.rank_ms for copy in counted_on)
        for copy in group:
            if copy.folder in inside:
                continue
            result.outside[reason] += 1
            if (copy.folder, copy.name) in chosen:
                result.kept[reason] += 1        # a newest copy, outside its scope
                continue
            if not rules.prune:
                result.held[HELD_NO_PRUNE] += 1
            elif blocked is not None:
                result.held[blocked] += 1
            elif situation.loaded is None:
                result.held[HELD_UNKNOWN_LOAD] += 1
            elif copy.folder == situation.loaded:
                result.held[HELD_LOADED] += 1
            elif copy.folder == situation.home:
                result.held[HELD_HOME] += 1     # unreachable: home is in every scope
            elif identity in waiting:
                result.held[HELD_FLAGS] += 1
            elif reason != SWITCH_FOLDER and home_away:
                result.held[HELD_HOME_UNLISTED] += 1
            elif reason == DEAD and identity in unproven:
                result.held[HELD_DEAD_UNCONFIRMED] += 1
            elif reason == SWITCH_FOLDER and not situation.switch_settled:
                result.held[HELD_LEARNING] += 1
            elif copy.activity_ms > best_activity or copy.rank_ms > best_rank:
                result.held[HELD_NO_SURVIVOR] += 1  # unreachable: the newest survive
            else:
                result.removals.append(Removal(
                    copy.folder, copy.name, identity, reason,
                    tuple((keeper.folder, keeper.name) for keeper in counted_on),
                    copy.activity_ms, copy.rank_ms))
    return result


__all__ = ["ARCHIVED", "Copy", "DAY_S", "DEAD", "HELD_DEAD_UNCONFIRMED", "HELD_FLAGS",
           "HELD_HOME", "HELD_HOME_UNLISTED", "HELD_LEARNING", "HELD_LOADED", "HELD_NO_PRUNE", "HELD_NO_SURVIVOR", "HELD_UNKNOWN_LOAD", "Plan",
           "Removal", "Rules", "SWITCH_FOLDER", "Scope", "Situation", "Summary", "keepers",
           "may_reach_outside", "plan", "quiet_since", "scope", "scope_of", "seeded",
           "summarize", "survivors"]
