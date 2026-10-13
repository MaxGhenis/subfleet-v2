"""Where each session belongs, and which copies the mirror may remove: C-23.28a.

The mirror used to copy every openable session into every folder the desktop
store holds: on 2026-09-25, 1,898 sessions in each of 120 folders, 227,923
files and 2.96 GB, growing by a copy per folder for every new session. Most
of those copies can never be seen. This module decides, as a pure function of
what one pass read, two things the mirror then acts on:

* **Scope**: the folders a session belongs in. The mirror spreads a session
  only into its scope (`mirror.py`, full and hot passes alike).
* **Removals**: the copies outside a session's scope that the mirror may
  delete, and the copies it must keep although they are outside it.

Three rules, each off at zero or false (`policy.json`, `sessions` section):

* `mirror_skip_switch_folders`: a **switch folder** (`folder_kinds.py`: one
  the app only passes through during an account switch, whose org another
  account owns) is in no session's scope. Nobody sees its sidebar.
* `mirror_archive_days`: a session whose every copy is archived and unstarred,
  and that has been quiet this many days, belongs only in the **home
  folder**. Quiet means since the later of its newest activity, focus or
  creation and when it was archived. Archiving leaves the activity times
  alone (850 records the app archived on 2026-09-22 were mostly a month
  quiet), so the mirror stamps the archive in its merge base
  (`archived_at`). Every other account then lists the session neither as a
  session nor as archived.
* `mirror_dead_days`: a session with no transcript (dead: it shows "no
  messages" everywhere and is never spread) that has been quiet this long
  keeps one copy: the home folder's, or the newest one.

Removal is a separate switch, `mirror_prune`, because it deletes: without it
the rules only stop the store from growing further. Every copy in a session's
scope survives, and so does the session's newest copy by its own activity,
focus and creation times: copies of one session often differ there (on
2026-09-25, 1,396 of 1,906 sessions with more than one copy), and the home
copy was the newest for about a quarter of the archived ones. An archived
session whose home copy is older therefore keeps two copies, and the mirror
never rewrites a copy to make one. Any other copy is removed only when all of
these hold:

* the app's loaded folder is known, and the copy is not in it: the app lists
  a folder into memory when it loads it and saves records from that memory,
  so a record removed under it could be listed with no file behind it, or
  written back (`desktop.py`);
* the copy is outside its session's scope, and is not the home folder's;
* the copy has a session id, and the session was neither held nor rewritten
  by this pass's flag sync, so its flags are settled.

Every removal of an openable session is therefore undone by switching its
rule off: the next full pass spreads the session back into every folder in
the wider scope, from its newest surviving copy by the spread's own ranking
(`test_mirror_prune.py` proves the round trip). A dead session is never
spread, as before; its newest copy stays where it is.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .folder_kinds import SWITCH

DAY_S = 86_400.0

#: Why a copy is outside its session's scope.
SWITCH_FOLDER, ARCHIVED, DEAD = "switch folder", "archived", "dead"
#: Why a copy outside its scope is kept anyway.
HELD_LOADED = "in the loaded folder"
HELD_UNKNOWN_LOAD = "the loaded folder is unknown"
HELD_NO_SURVIVOR = "no surviving copy is as new"
HELD_FLAGS = "flags not settled this pass"
HELD_NO_PRUNE = "pruning is off"
HELD_HOME = "in the home folder"


@dataclass(frozen=True, slots=True)
class Copy:
    """One `local_*.json` as a pass read it."""

    folder: str                 # "<account>/<org>"
    name: str
    identity: str               # cliSessionId; "" when the record has none
    archived: bool = False
    starred: bool = False
    #: The newest of lastActivityAt, lastFocusedAt and createdAt, epoch ms:
    #: how new the record's content is. Copies of one session often differ
    #: here (the mirror spreads a session once and syncs only its flags and
    #: title), so the newest copy is the one that must survive.
    activity_ms: int = 0
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
    #: `folders.Registry.classify` verdicts; a folder not in it is `unknown`.
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
    #: The copy this removal counts on to survive: `(folder, name)`, in scope
    #: and at least as new. The mirror checks it again right before removing.
    keeper: tuple[str, str]
    newness: float


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
    return not (rules.skip_switch_folders and situation.kinds.get(folder) == SWITCH)


def quiet_since(identity: str, copies: Iterable[Copy], situation: Situation) -> float:
    """Epoch seconds of the session's newest sign of life, over every copy."""
    latest = situation.archived_at.get(identity, 0.0)
    for copy in copies:
        latest = max(latest, copy.last_sign)
    return latest


def scope(identity: str, copies: list[Copy], situation: Situation, rules: Rules) -> Scope:
    """The folders a session belongs in, judged over every copy of it.

    A session with any unarchived or starred copy is live: flag sync has not
    settled it, or the user wants it seen, and a live session belongs
    everywhere. So does an archived one while it is younger than the rule,
    and every session when its rule is off or no home folder is known.
    """
    everywhere = frozenset(folder for folder in situation.folders
                           if seeded(folder, situation, rules))
    age = situation.now - quiet_since(identity, copies, situation)
    if identity not in situation.openable:
        if rules.dead_days > 0 and age >= rules.dead_days * DAY_S:
            home = frozenset({situation.home}) if situation.home else frozenset()
            return Scope("dead", home)
        return Scope("dead-young", everywhere)
    if (rules.archive_days > 0 and situation.home is not None
            and copies and all(copy.archived for copy in copies)
            and not any(copy.starred for copy in copies)
            and age >= rules.archive_days * DAY_S):
        return Scope("archived", frozenset({situation.home}))
    return Scope("live", everywhere)


def survivors(copies: list[Copy], inside: frozenset[str], situation: Situation) -> list[Copy]:
    """The copies that stay whatever the rules say.

    Every copy in scope, and the newest copy by the record's own times when
    no copy in scope is as new. So the newest content of every session
    survives without the mirror rewriting anything: an archived session whose
    home copy is older keeps two copies, the home's and the newest. Of equal
    copies the home's wins, then one in a folder the app settles in, then the
    first by path, so the choice is the same every pass.
    """
    kept = [copy for copy in copies if copy.folder in inside]
    if not copies:
        return kept
    newest = max(copy.activity_ms for copy in copies)
    if kept and max(copy.activity_ms for copy in kept) >= newest:
        return kept

    def order(copy: Copy):
        return (-copy.activity_ms, copy.folder != situation.home,
                situation.kinds.get(copy.folder) == SWITCH, copy.folder, copy.name)
    return kept + [min(copies, key=order)]


def plan(copies: Iterable[Copy], situation: Situation, rules: Rules, *,
         held: Iterable[str] = (), blocked: str | None = None) -> Plan:
    """Which copies to remove this pass, and why every other outsider stays.

    `held` names the sessions flag sync held or rewrote this pass: their
    flags are not settled, so none of their copies is removed. `blocked`
    keeps every copy, for that reason (an incomplete inventory).
    """
    result = Plan()
    waiting = set(held)
    listed = set(situation.folders)
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
        keeper = max(kept, key=lambda copy: (copy.activity_ms, copy.folder == situation.home))
        for copy in group:
            if copy.folder in inside:
                continue
            result.outside[reason] += 1
            if (copy.folder, copy.name) in chosen:
                result.kept[reason] += 1        # the newest copy, outside its scope
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
            elif copy.activity_ms > keeper.activity_ms:
                result.held[HELD_NO_SURVIVOR] += 1  # unreachable: the newest survives
            else:
                result.removals.append(Removal(copy.folder, copy.name, identity, reason,
                                               (keeper.folder, keeper.name),
                                               copy.activity_ms))
    return result


__all__ = ["ARCHIVED", "Copy", "DAY_S", "DEAD", "HELD_FLAGS", "HELD_HOME", "HELD_LOADED",
           "HELD_NO_PRUNE", "HELD_NO_SURVIVOR", "HELD_UNKNOWN_LOAD", "Plan", "Removal",
           "Rules", "SWITCH_FOLDER", "Scope", "Situation", "plan", "quiet_since", "scope",
           "seeded", "survivors"]
