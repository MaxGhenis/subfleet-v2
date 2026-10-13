"""Which desktop session folders the app settles in, learned from its own log.

The desktop app keeps one session folder per `<account>/<org>` pair it has
ever initialized, and it initializes pairs nobody works in. During 39 of the
75 account switches in its log from 2026-09-14 to 09-25 (`main4.log` to
`main.log`), it first initialized the account it was leaving with the first
org of the account it was going to, listed that folder, and moved on within
4 s; when the folder was missing it created it, writing
`scheduled-tasks.json` (2.9939.2's scheduled-tasks store does that on an
empty load). Nothing in the app checks that the account belongs to the org.
On 2026-09-25 the store held 120 folders for 17 accounts and 18 orgs:

* 17 folders the app settled in, one per account, no two sharing an org;
  15 of them had a stay of 53 minutes or more;
* 26 more it only passed through mid-switch, 11 of them created that way in
  the log's window;
* 77 it never loaded in the window. 74 of them have a `scheduled-tasks.json`
  born within 0.4 s of the folder, the mark of that creation.

A folder the app only passes through shows its sidebar to nobody, so seeding
it with every session costs disk and buys nothing: at 120 folders, 103 such
folders held most of the store. The rule this module supports is narrow. A
folder is a **switch folder** (never seeded, see `prune.py`) only when both
hold:

* the app has never settled in it: no stay of `SETTLE_S`, and no stay a
  logout ended; and
* its org is **owned** by a different account: the app stayed at least
  `OWNER_S` in that account's folder for the org.

On the 2026-09-25 logs this makes 87 switch folders. Everything else is seeded
as before, including a folder the log has never mentioned and an org nobody
owns yet: 16 folders of three such orgs, one of them a second org an account
really belongs to. The two thresholds err the same way: a false "settled" or
a false "owner" can only seed more. A folder someone does use is excluded
while the registry has not seen anyone settle in it and another account owns
its org: a new account's first seconds, or a folder used less often than the
registry has been learning (at first, the week or so of log the app keeps).
It gets no new sessions until someone stays in it `SETTLE_S`; the mirror
then seeds it within a pass, and the app lists what was seeded at its next
load, as for any new folder. Removing a live session's copies from a switch
folder waits until the registry has learned for `SWITCH_PRUNE_AFTER_S`.

The facts are persisted in the state root, because the app keeps only about
ten days of log: a folder the app settled in once stays settled after the log
that showed it has rotated away. Every fact is a minimum, a maximum or a flag,
so reading the same log lines twice (a restart, or a second process) changes
nothing. A registry starts from all the history the app kept, or from none:
until a replay has read every rotated log, it folds nothing in (`learn`).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .desktop import Event, Load

REGISTRY_NAME = "mirror-folders.json"
#: A load that stays the app's latest this long, or that a logout ends, is a
#: folder someone used. On 2026-09-14..25 the app left every folder it only
#: passed through within 19 s (the mid-switch pairs within 4 s), and two real
#: logins lasted 27 s and 55 s before a logout.
SETTLE_S = 20.0
#: An account that stayed this long in `<account>/<org>` owns the org. Much
#: higher than `SETTLE_S`, because ownership is what excludes other folders:
#: every folder the app settled in had a stay of 53 minutes or more.
OWNER_S = 600.0
#: A folder the registry has never seen settled may still be one someone
#: uses less often than the registry has been learning, so a live session's
#: copies there are removed (`prune.py`) only once the registry has learned
#: this long, counting from the oldest load or logout it folded in. On
#: 2026-10-13 the app's own log reached back 7.5 days.
SWITCH_PRUNE_AFTER_S = 30 * 86_400.0

SETTLED, SWITCH, UNKNOWN = "settled", "switch", "unknown"


@dataclass
class Facts:
    """What the log has shown about one folder, as epoch seconds."""

    first_load: float | None = None
    last_load: float | None = None
    #: A missing load: the app created the folder.
    created: float | None = None
    #: The start of the first load that stayed `SETTLE_S`.
    settled: float | None = None
    #: The start of the first load that stayed `OWNER_S`.
    owned: float | None = None

    @classmethod
    def from_dict(cls, value: Any) -> "Facts":
        if not isinstance(value, dict):
            return cls()
        fields: dict[str, float | None] = {}
        for key in ("first_load", "last_load", "created", "settled", "owned"):
            item = value.get(key)
            fields[key] = float(item) if isinstance(item, (int, float)) and not isinstance(
                item, bool) else None
        return cls(**fields)


def _earliest(current: float | None, value: float) -> float:
    return value if current is None or value < current else current


def _latest(current: float | None, value: float) -> float:
    return value if current is None or value > current else current


class Registry:
    """The folders the app has loaded and how long it stayed, kept across restarts."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.folders: dict[str, Facts] = {}
        #: The last event folded in: `(epoch s, folder or None, keys at that second)`.
        #: Its folder is the open load, whose stay is still running.
        self.last_at: float | None = None
        self.last_folder: str | None = None
        self._keys: set[tuple[str | None, bool]] = set()
        self.replayed = False
        #: The oldest event folded in, epoch seconds: how long it has learned.
        self.since: float | None = None
        #: The home folder last resolved, `{"value": dead_home, "folder": ...}`,
        #: kept so an ambiguity later keeps the home rather than unhoming every
        #: archived session (`Mirror.resolve_home`).
        self.home: dict[str, str] | None = None
        self._read()

    # --- persistence -----------------------------------------------------------

    def _read(self) -> None:
        from .mirror import _load
        value = _load(self.path)
        folders = value.get("folders")
        if isinstance(folders, dict):
            self.folders = {str(key): Facts.from_dict(item) for key, item in folders.items()}
        last = value.get("last")
        if isinstance(last, dict) and isinstance(last.get("at"), (int, float)):
            self.last_at = float(last["at"])
            folder = last.get("folder")
            self.last_folder = folder if isinstance(folder, str) else None
            keys = last.get("keys")
            if isinstance(keys, list):
                self._keys = {(item[0] if isinstance(item[0], str) else None, bool(item[1]))
                              for item in keys if isinstance(item, list) and len(item) == 2}
        self.replayed = bool(value.get("replayed"))
        since = value.get("since")
        if isinstance(since, (int, float)) and not isinstance(since, bool):
            self.since = float(since)
        home = value.get("home")
        if (isinstance(home, dict) and isinstance(home.get("value"), str)
                and isinstance(home.get("folder"), str)):
            self.home = {"value": home["value"], "folder": home["folder"]}

    def to_dict(self) -> dict[str, Any]:
        return {"version": 1, "replayed": self.replayed, "since": self.since,
                "home": self.home,
                "folders": {key: asdict(item) for key, item in sorted(self.folders.items())},
                "last": {"at": self.last_at, "folder": self.last_folder,
                         "keys": sorted([list(key) for key in self._keys],
                                        key=lambda item: (str(item[0]), item[1]))}}

    def learned_for(self, now: float) -> float:
        """Seconds since the oldest event the registry has folded in (0 if none)."""
        return 0.0 if self.since is None else max(0.0, now - self.since)

    def save(self) -> None:
        from .mirror import _write_json
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        _write_json(self.path, self.to_dict())

    # --- learning --------------------------------------------------------------

    def _stayed(self, folder: str, started: float, seconds: float, *, logout: bool) -> None:
        facts = self.folders.setdefault(folder, Facts())
        if seconds >= SETTLE_S or logout:
            facts.settled = _earliest(facts.settled, started)
        if seconds >= OWNER_S:
            facts.owned = _earliest(facts.owned, started)

    def observe(self, events: Iterable[Event]) -> bool:
        """Fold the log's loads and logouts in, oldest first; True if anything changed.

        A load's stay is the time until the next load or logout; a stay a
        logout ends was a real login, however short. Events at or before the
        last one folded in are skipped, so a replay after a restart, or a
        second reader, adds nothing twice.
        """
        before = self.to_dict()
        for event in events:
            at = event.at.timestamp()
            key = (event.folder, event.missing)
            if self.last_at is not None and (at < self.last_at or (
                    at == self.last_at and key in self._keys)):
                continue
            self.since = _earliest(self.since, at)
            if self.last_folder is not None and self.last_at is not None:
                self._stayed(self.last_folder, self.last_at, at - self.last_at,
                             logout=event.folder is None)
            if event.folder is not None:
                facts = self.folders.setdefault(event.folder, Facts())
                facts.first_load = _earliest(facts.first_load, at)
                facts.last_load = _latest(facts.last_load, at)
                if event.missing:
                    facts.created = _earliest(facts.created, at)
            if self.last_at is None or at > self.last_at:
                self._keys = set()
            self.last_at, self.last_folder = at, event.folder
            self._keys.add(key)
        return self.to_dict() != before

    # --- judging ---------------------------------------------------------------

    def _open_stay(self, now: float, logged_out: datetime | None) -> float:
        """How long the open load has stayed so far (0 when none or logged out)."""
        if self.last_folder is None or self.last_at is None:
            return 0.0
        end = logged_out.timestamp() if logged_out is not None else now
        return max(0.0, end - self.last_at)

    def classify(self, folders: Iterable[str], *, now: float,
                 logged_out: datetime | None = None) -> dict[str, str]:
        """`settled`, `switch` or `unknown` for each `<account>/<org>` folder.

        `logged_out` is the app's logout after its latest load, if any: the
        open load's stay ends there.
        """
        stay = self._open_stay(now, logged_out)

        def settled(folder: str) -> bool:
            facts = self.folders.get(folder)
            if facts is not None and facts.settled is not None:
                return True
            return folder == self.last_folder and stay >= SETTLE_S

        owners: dict[str, set[str]] = {}
        for folder, facts in self.folders.items():
            account, _sep, org = folder.partition("/")
            if facts.owned is not None or (folder == self.last_folder and stay >= OWNER_S):
                owners.setdefault(org, set()).add(account)
        verdicts: dict[str, str] = {}
        for folder in folders:
            account, _sep, org = folder.partition("/")
            if settled(folder):
                verdicts[folder] = SETTLED
            elif owners.get(org, set()) - {account}:
                verdicts[folder] = SWITCH
            else:
                verdicts[folder] = UNKNOWN
        return verdicts

    def owner_of(self, org: str) -> list[str]:
        """The accounts that own `org`, for reports."""
        return sorted(folder.partition("/")[0] for folder, facts in self.folders.items()
                      if folder.partition("/")[2] == org and facts.owned is not None)


def registry_path(root: str | Path) -> Path:
    return Path(root) / "sessions" / REGISTRY_NAME


def learn(registry: Registry, log: Any, *, replay: bool) -> bool:
    """Fold a `DesktopLog`'s events into the registry; True if it changed.

    A registry first learns from a replay of every rotated log, so it starts
    from all the history the app kept rather than the newest rotation. Until
    a replay has found history and read every log (`replay` asks for one
    now), it folds in nothing, not even the live poll's events: each event
    folded moves the watermark, and history older than the watermark is
    never folded, so a partial start would lose the rest for good (review
    r2 N8). The live events it passes over are in the logs the next replay
    reads.
    """
    if registry.replayed:
        return registry.observe(log.drain())
    if not replay:
        log.drain()
        return False
    found = log.replay()
    if found.unread or not found.events:
        # No log, an empty one, or one that could not be read: tried again.
        log.drain()
        return False
    registry.observe(found.events)
    registry.replayed = True
    registry.observe(log.drain())
    return True


def current_folder(load: Load | None) -> str | None:
    return load.folder if load is not None else None


__all__ = ["OWNER_S", "REGISTRY_NAME", "Registry", "SETTLED", "SETTLE_S", "SWITCH",
           "UNKNOWN", "Facts", "current_folder", "learn", "registry_path"]
