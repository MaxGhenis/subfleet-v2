"""What the Claude desktop app has loaded, read from the app's own log.

The desktop app puts a Code session in its sidebar only by listing
`claude-code-sessions/<account>/<org>/local_*.json` into memory, and it lists
that folder only when its LocalSessionManager initializes it: at launch, when
the logged-in account or the `lastActiveOrg` changes, at the first login after
a logout, and when a new app window builds its session API. Nothing watches the
folder, and `list_sessions` answers from memory. A file that lands in the
loaded folder after that listing stays out of the running app's sidebar until
the next initialization. (Read from the app bundle 2.7032.0 on 2026-09-24:
`doInitialize` → `loadSessions` → `loadSessionRecords` is the only reader, and
no watcher, timer, focus handler or IPC call reaches it. Confirmed on the live
app the same day: 19 records the mirror copied at 17:13-17:14 ET were "not
found" until the 17:24:47 relaunch listed them.)

So the mirror needs two facts it cannot get from the files: which folder the
app has loaded, and when. The app logs both to `~/Library/Logs/Claude/main.log`:

    2026-09-24 16:38:11 [info] [LocalSessionManager] Initialization succeeded — accountId=A, orgId=O, existingSessions=1795
    2026-09-24 16:38:11 [info] Loaded 361 persisted sessions from <store>/A/O (1566 archived deferred, ...)
    2026-09-24 16:37:53 [info] [LocalSessionManager] Account logged out, marking for re-init on next login
    2026-09-24 16:38:05 [info] [LocalSessionManager] Session storage directory does not exist yet, skipping load: <store>/A/O

The "Initialization succeeded" line is written before the app clears its list
and reads the folder, so a file older than it was listed. The "Loaded" line
names the exact folder read, which matters because the app also logs
initializations for account and org pairs it never loads (the old account with
the new org, mid-switch). `existingSessions` is the size of the in-memory list
before that load: 0 at launch. A load that keeps a non-empty list (a re-login
to the same account and org) adds new ids but does not re-read records it
already holds.

The log is a diagnostic, not an interface: this module reads it read-only,
never depends on it for copying, and reports `None` rather than guessing when
the lines it knows are absent. Timestamps are the app's local wall clock,
whole seconds.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone, tzinfo
from pathlib import Path

#: Where the desktop app writes its main-process log; a test points it elsewhere.
LOG_ENV = "SUBFLEET_DESKTOP_LOG"
#: The app rotates `main.log` to `main1.log` (then `main2.log`, ...) at ~10 MB.
ROTATED_NAME = "main1.log"
#: Never read more than this much of one file in one poll.
READ_LIMIT = 32 * 1024 * 1024

_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[[A-Za-z]+\] (.*)$")
_INIT = re.compile(r"^\[LocalSessionManager\] Initialization succeeded\W+"
                   r"accountId=([^,\s]+), orgId=([^,\s]+), existingSessions=(\d+)")
_LOADED = re.compile(r"^Loaded (\d+) persisted sessions from (.+?)(?: \(.*)?$")
_MISSING = re.compile(r"^\[LocalSessionManager\] Session storage directory does not "
                      r"exist yet, skipping load: (.+)$")
_LOGOUT = re.compile(r"^\[LocalSessionManager\] Account logged out")
#: Cheap byte filters, so a poll decodes only the lines that can matter.
_NEEDLES = (b"[LocalSessionManager]", b"persisted sessions from")


def log_path() -> Path:
    override = os.environ.get(LOG_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Logs" / "Claude" / "main.log"


@dataclass(frozen=True)
class Load:
    """One listing of a session folder into the app's memory."""

    account: str
    org: str
    #: The "Initialization succeeded" instant: the app listed the folder after it.
    started_at: datetime
    #: The start of the latest load of this folder that began from an empty
    #: list (launch, or a switch from another folder). A record the app already
    #: held is re-read only by such a load.
    fresh_started_at: datetime
    #: The "Loaded N persisted sessions" instant; None when the folder was absent.
    loaded_at: datetime | None
    #: How many records the load added to memory (archived ones are deferred).
    count: int | None
    #: The folder did not exist, so nothing was listed.
    missing: bool = False

    @property
    def folder(self) -> str:
        return f"{self.account}/{self.org}"


@dataclass(frozen=True)
class AppState:
    """The app's latest session-folder load, as far as its log says."""

    load: Load | None = None
    #: A logout after that load: the list stays in memory until the next login.
    logged_out_at: datetime | None = None
    error: str | None = None


class DesktopLog:
    """Tail the app's log for the session-folder loads it records.

    `poll()` reads only what was appended since the last call and follows one
    rotation (`main.log` renamed to `main1.log`). The first poll reads the
    rotated file and then the live one in full (each is capped near 10 MB by
    the app), so the latest load is found even right after a rotation.
    """

    def __init__(self, path: str | Path | None = None, *, store: str | Path | None = None,
                 tz: tzinfo | None = None):
        self.path = Path(path) if path is not None else log_path()
        self._store = Path(store) if store is not None else None
        self.tz = tz
        self._inode: int | None = None
        self._offset = 0
        self._partial = b""
        self._pending_init: tuple[str, str, datetime, int] | None = None
        self.state = AppState()

    # --- time and paths ------------------------------------------------------

    def _instant(self, text: str) -> datetime | None:
        try:
            naive = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        if self.tz is not None:
            return naive.replace(tzinfo=self.tz).astimezone(timezone.utc)
        # The app logs local wall-clock time; mktime applies that date's DST.
        return datetime.fromtimestamp(time.mktime(naive.timetuple()), timezone.utc)

    def _store_dir(self) -> Path:
        if self._store is not None:
            return self._store
        from .mirror import store_dir
        return store_dir()

    def _folder(self, text: str) -> tuple[str, str] | None:
        """`<store>/<account>/<org>` to `(account, org)`; None outside the store."""
        store = os.path.normpath(str(self._store_dir()))
        path = os.path.normpath(text.strip())
        if not path.startswith(store + os.sep):
            return None
        parts = path[len(store) + 1:].split(os.sep)
        if len(parts) != 2 or not all(parts):
            return None
        return parts[0], parts[1]

    # --- parsing -------------------------------------------------------------

    def _feed(self, line: str) -> None:
        match = _LINE.match(line)
        if not match:
            return
        at = self._instant(match.group(1))
        if at is None:
            return
        message = match.group(2)
        found = _INIT.match(message)
        if found:
            self._pending_init = (found.group(1), found.group(2), at, int(found.group(3)))
            return
        found = _LOADED.match(message)
        if found:
            folder = self._folder(found.group(2))
            if folder is not None:
                self._record_load(folder, at, int(found.group(1)), missing=False)
            return
        found = _MISSING.match(message)
        if found:
            folder = self._folder(found.group(1))
            if folder is not None:
                self._record_load(folder, at, 0, missing=True)
            return
        if _LOGOUT.match(message):
            self.state = replace(self.state, logged_out_at=at)

    def _record_load(self, folder: tuple[str, str], at: datetime, count: int, *,
                     missing: bool) -> None:
        started, existing = at, None
        init = self._pending_init
        if init is not None and (init[0], init[1]) == folder:
            started, existing = init[2], init[3]
        self._pending_init = None
        previous = self.state.load
        same = previous is not None and (previous.account, previous.org) == folder
        # A load that began from a non-empty list of the SAME folder only adds
        # ids; any other load starts from an empty list.
        fresh = not same or existing == 0 or previous.missing
        load = Load(account=folder[0], org=folder[1], started_at=started,
                    fresh_started_at=started if fresh else previous.fresh_started_at,
                    loaded_at=None if missing else at, count=count, missing=missing)
        self.state = AppState(load=load, logged_out_at=None, error=None)

    def _consume(self, data: bytes, *, final: bool = False) -> None:
        data = self._partial + data
        lines = data.split(b"\n")
        self._partial = b"" if final else lines.pop()
        for raw in lines:
            if any(needle in raw for needle in _NEEDLES):
                self._feed(raw.decode("utf-8", "replace").rstrip("\r"))

    def _read(self, path: Path, start: int) -> tuple[bytes, int]:
        with path.open("rb") as stream:
            stream.seek(start)
            data = stream.read(READ_LIMIT)
            return data, start + len(data)

    # --- the one entry point -------------------------------------------------

    def poll(self) -> AppState:
        """Read what the app appended since the last poll and return the state."""
        try:
            info = os.stat(self.path)
        except OSError as exc:
            self.state = replace(self.state, error=f"cannot read {self.path}: {exc.strerror}")
            return self.state
        try:
            if self._inode is None:
                rotated = self.path.with_name(ROTATED_NAME)
                try:
                    data, _end = self._read(rotated, 0)
                    self._consume(data, final=True)
                except OSError:
                    pass
            elif info.st_ino != self._inode:
                # Rotated: finish the old file, which the app renamed aside.
                rotated = self.path.with_name(ROTATED_NAME)
                try:
                    if os.stat(rotated).st_ino == self._inode:
                        data, _end = self._read(rotated, self._offset)
                        self._consume(data, final=True)
                except OSError:
                    pass
                self._partial = b""
            if info.st_ino != self._inode or info.st_size < self._offset:
                self._inode, self._offset, self._partial = info.st_ino, 0, b""
            data, self._offset = self._read(self.path, self._offset)
            self._consume(data)
        except OSError as exc:
            self.state = replace(self.state, error=f"cannot read {self.path}: {exc.strerror}")
            return self.state
        if self.state.error:
            self.state = replace(self.state, error=None)
        return self.state


__all__ = ["AppState", "DesktopLog", "LOG_ENV", "Load", "log_path"]
