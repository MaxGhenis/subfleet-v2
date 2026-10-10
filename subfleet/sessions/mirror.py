"""The desktop sidebar mirror: one Claude Code sidebar across every account.

The Claude desktop app stores one small JSON index file per Code session at
`~/Library/Application Support/Claude/claude-code-sessions/<account>/<org>/local_<id>.json`,
and the sidebar shows only the folder of the account that is currently logged
in — so every session vanishes when Max switches accounts, which he does daily.
The transcript itself lives in `~/.claude/projects/<slug(cwd)>/<cli>.jsonl` and is
account-agnostic: only the folder an index file sits in decides which account
"owns" a session. The mechanism is to copy each index file into every folder;
there is no provider call anywhere in this module (plan decision 8, C-23.28).

**A copy reaches a sidebar only at the app's next load of that folder.** The
app lists `local_*.json` into memory only when it initializes a folder: at
launch, when the logged-in account or org changes, at the first login after a
logout, and when a new app window builds its session API. Nothing watches the
folder, and the sidebar and `list_sessions` answer from memory (`desktop.py`
has the evidence). Copying
therefore unifies the sidebars only for records that are already in a folder
when the app loads it. On 2026-09-24 a switch at 16:38 ET loaded
`d1e7c8a9…/8b35fb7b…` without 19 sessions last active under other accounts;
the mirror, whose passes were then taking up to two hours, copied them at
17:13-17:14, and the running app listed none of them until it relaunched at
17:24:47. Four rules follow:

* **Win the race to the switch.** A hot pass runs every
  `sessions.mirror_hot_interval_s` (2 s). It re-lists only the folders whose
  directory changed since it last looked, puts each new session whose
  transcript exists into every folder that lacks it, and fills stale empty
  copies. The app writes a record by write-then-rename and writes in place
  only when the rename fails (no in-place rewrite among 217,706 files on
  2026-09-24), so a record write normally changes its folder. That includes
  the old account's folder, where sessions still running at a switch keep
  saving. Both timers share one worker and one writer lock. While a full
  pass runs, its checkpoints service a flag-only hot pass at that cadence,
  using a separate inventory; an archive, star, title or setting edit does
  not wait for the full inventory to finish. A hot pass gathers every copy
  of each changed session and retries held candidates. New-session spreading
  waits for the worker; revival, pruning and in-place sweep detection remain
  the full pass's work. Both passes read candidate transcripts' title tails;
  the full pass also discovers transcript-only title changes.
* **Stay incremental.** A pass that re-parsed every file could not keep up:
  its payload cache held 128 MB of the ~300 MB of distinct parsed records,
  so warm passes re-read most of 218k files, and a daemon restart threw away
  what was left. The cache now holds only the fields the mirror reads, one
  payload per distinct file content (9,619 payloads, 16.4 MB, on 2026-09-24).
  A folder whose directory is unchanged is not re-listed, and a changed one
  is diffed by inode. A full stat sweep every `SWEEP_INTERVAL_S` catches a
  write that kept its inode.
* **Report what lost the race.** Every copy is journaled with the file's
  ctime. The app's log names the folder it loaded and when. A journaled copy
  into that folder that postdates the load, and that the app has not since
  rewritten, is a session the running app cannot list. `sessions mirror
  --status`, `sessions list` and `doctor` say how many, and that a relaunch
  (or a switch away and back) lists them.
* **A write into the loaded folder does not reach the running app.** The app
  serializes each record from memory on its next save, with no read-merge.
  So a flag, title or setting the mirror writes into the folder the app has
  loaded, or into the folder of a session still running from an earlier
  account, does not reach that app's memory, and its next save of the record
  writes the old value back. The load-gap report counts those in the loaded
  folder as `stale`. The merge base then reads that re-save as a user's change
  and spreads it; telling the two apart needs knowledge the mirror does not
  reliably have (three review rounds found holes in each attempt), so this
  stays a known limit, recorded in the 2026-09-24 report.

Ported from v1 `bin/subfleet-mirror` v5.0, whose hard-won identity rules survive
intact:

* A session's stable identity is its **filename** (`local_<id>.json`), not its
  `cliSessionId`: resuming under another account keeps the filename while the
  `cliSessionId` is filled in later, so an early copy can freeze with an empty
  id and render as "no messages". The mirror spreads the *resolvable* copy and
  overwrites stale empty ones.
* A session whose transcript Claude Code has pruned is **dead** — it shows "no
  messages" in every account — so dead sessions are never spread.
* `isArchived`, `isStarred` and the title live inside each per-account copy, so
  archiving in one account never propagated. Flag sync uses a **merge base**: a
  copy that differs from the last synced value is a user action and the *change*
  wins in both directions. mtimes are not evidence — the app rewrites these files
  on mere focus.
* The transcript's last `custom-title` record is the newest intended name,
  account-agnostic and append-only, so it survives an index write the app skipped.

Two rules date from 2026-10-10, when Max opened a six-week-old fork of a
session for the session itself (`docs/reports/2026-10-10-mirror-stale-dates.md`):

* `lastActivityAt`, the date a row shows, is written by the app only in the
  folder where the session runs, and every other copy kept the date it was
  copied with. That day 663 of the 999 unarchived rows in the loaded folder
  showed a date more than an hour older than their session's newest copy,
  the furthest by 61 days. The flag publish now raises a copy more than
  `sessions.mirror_activity_lag_s` behind (`activity_targets`). The date
  needs no merge base: a later one always wins, so the app's re-save of an
  older date lowers one copy until the next pass and reaches no other.
* A record's name is the app's id for a session, and one name can hold one
  conversation in some folders and another in the rest (12 names that day;
  how each came to be was not established). Both transcripts exist, so each
  gets a row in every folder, with one title. The full pass reports those
  ids (`_split_report`) and changes nothing about them.

What v2 changes is only where state lives and how health is judged.

The merge base moves out of `~/.claude/cc-mirror-state.json` and into the state
root, so v2's mirror never edits v1's file and the two can run side by side
during the shadow period. Health becomes C-23.28: a **per-pass sidecar** records
each pass as it starts and again as it ends, so "a pass the sidecar records as in
flight is healthy until thirty minutes after its recorded start, and only then is
the mirror stalled". v1 inferred an in-flight pass from `pgrep -f
bin/subfleet-mirror`, which a rename would have silently broken and which made
every long pass a coin flip; and it judged staleness from log recency, which fired
a false "stalled" on 2026-08-19 07:08 because `--quiet` keeps the log silent on a
no-op pass. Health is not reach: a healthy mirror can still have copied records
the running app will not list until it reloads, which is the load gap above.
"""

from __future__ import annotations

import errno
import fcntl
import glob as globbing
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import transcripts
from .desktop import AppState, DesktopLog

#: Where the desktop app keeps its per-account Code session index.
STORE_ENV = "SUBFLEET_SESSION_STORE"
#: Where transcripts live; shared with `transcripts.claude_dir`.
CLAUDE_ENV = "SUBFLEET_CLAUDE_DIR"

#: v1's own per-user settings, so the launchd job needed no CLI arguments:
#: `{"dead_home": "<orgUuid>", "archive": "<recursive glob>", "exclude": [...]}`.
#: v2's timer takes no arguments either, so it reads the same file — read-only,
#: and every value is still overridable per call.
CONFIG_NAME = "cc-mirror.json"

SIDECAR_NAME = "mirror.json"
FLAGS_NAME = "mirror-flags.json"
LOCK_NAME = "mirror.lock"
#: The copies and rewrites the mirror made, for the load-gap report.
JOURNAL_NAME = "mirror-writes.json"

#: A pass the sidecar records as in flight is healthy until this long after its
#: recorded start (C-23.28). An 8.5-minute pass was observed on 2026-08-18
#: during app-churn re-seeding; a 45-minute one is hung.
DEFAULT_HANG_MIN = 30.0
#: How long after a finished pass the mirror is still considered fresh.
DEFAULT_STALL_MIN = 10.0
# Bound retained metadata and projected payloads separately. Equal mirrored
# copies share a payload; a cache limit never limits which sessions a pass
# processes. 217,706 entries with 9,619 distinct contents (16.4 MB of payloads)
# were measured on 2026-09-24, growing by 4.7k-10k entries a day (every new
# session copied into 120 folders), so the entry limit is months, not years,
# away. Past it, folders with uncached entries are re-listed every pass:
# slower, never wrong. The store's growth itself needs pruning before then.
ENTRY_CACHE_LIMIT = 1_000_000
PAYLOAD_CACHE_BYTES = 128 * 1024 * 1024
PROGRESS_INTERVAL_S = 5.0
#: Listing itself may lose time reacquiring the GIL after readdir. Keep its
#: batches short enough to service flags before entry processing begins.
LISTING_CHECKPOINT_ENTRIES = 64
#: A full pass stats every entry at least this often. Between sweeps a folder
#: whose directory is unchanged is not re-listed, and a re-listed one is diffed
#: by inode; both rely on the app's write-then-rename, and the sweep bounds how
#: long its in-place fallback write can go unseen (C-23.28).
SWEEP_INTERVAL_S = 600.0
#: The archive glob is re-walked when a new dead session appears or this long
#: after the last walk; on 2026-09-24 it held 56k files and none of the 126
#: dead sessions.
ARCHIVE_RESCAN_S = 1800.0
#: The hot pass keeps retrying a new record whose transcript has not appeared
#: yet for this long; after that the full pass alone handles it.
UNRESOLVED_RETRY_S = 3600.0
#: Journaled writes into folders the app has not loaded are kept this long, so
#: a load the log reports late still finds the copies that followed it.
JOURNAL_WINDOW_S = 900.0
#: When a pass cannot see which folder the app loaded, it prunes the shared
#: journal only by this age, so it never drops rows a better-informed process
#: (the daemon) still needs.
JOURNAL_UNSURE_S = 48 * 3600.0
JOURNAL_LIMIT = 50_000
#: Sample idle hot progress at most this often; changes and errors record at once.
HOT_RECORD_S = 60.0

#: The fields a pass reads from an index entry; the cache keeps nothing else.
#: Records average 12 KB, three quarters of it MCP configuration the mirror
#: never looks at (measured 2026-09-24).
PROJECTED = ("sessionId", "cliSessionId", "isArchived", "isStarred", "title",
             "titleSource", "lastActivityAt", "lastFocusedAt", "createdAt", "cwd",
             "originCwd", "sessionSettings", "priorCliSessionIds")
#: What flag sync decides on. A copy whose on-disk values of these moved since
#: the pass read it is left for the next pass instead of being overwritten.
FLAG_FIELDS = ("cliSessionId", "isArchived", "isStarred", "title", "titleSource",
               "sessionSettings")
FLAG_WRITES = ("isArchived", "isStarred", "title", "titleSource", "sessionSettings")
#: The date a sidebar row shows. The app writes it only in the folder where the
#: session runs, so every other folder's copy kept the date it was copied with
#: (the 2026-10-10 report). The publish raises it and never lowers it; see
#: `activity_targets`.
ACTIVITY_FIELD = "lastActivityAt"
#: `sessions.mirror_activity_lag_s`: how far behind its session's newest copy a
#: copy's date may fall before the mirror raises it. Zero switches the sync off.
#: Not every change: one raise rewrites the session's record in every other
#: folder (133 files on 2026-10-10).
DEFAULT_ACTIVITY_LAG_S = 3600.0
#: How many sessions' dates one flag sync raises, furthest behind first. The
#: publish has no cancellation point, so the backlog a first pass finds (750
#: sessions and 94,383 copies on 2026-10-10) must not become one publish; the
#: rest wait for the next pass and are counted in `activity_waiting`. Ten
#: sessions are about 1,300 writes, 0.4 s at the 0.30 ms measured for one.
ACTIVITY_SESSIONS_PER_PASS = 10
#: A date more than this past the pass's own clock is no voice. The app writes
#: its clock's now, so a later date is a bad record, and a raise is never
#: undone: raised from, a bad date would sit in every folder and be raised
#: again from each. The date sync therefore never spreads one. `_rank` still
#: takes it for the latest when a new folder needs a record to copy, as it did
#: before the sync existed.
ACTIVITY_FUTURE_S = 300.0
#: How many split ids the full pass's report lists by name.
SPLIT_REPORT_LIMIT = 10


#: Beyond JavaScript's safe integers a number is not a date the app wrote, and
#: one millisecond before it is no longer a different number.
SAFE_MS = 2 ** 53


def _instant_ms(value: Any) -> bool:
    """A date as the app writes it: a number within JavaScript's safe integers
    (every one of the 410,585 records read on 2026-10-10 held an integer of
    milliseconds). Compared, never converted: an integer of any size must not
    raise here, and NaN and the infinities compare false."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return -SAFE_MS < value < SAFE_MS


def activity_targets(values: Sequence[Any], lag_ms: float) -> dict[int, Any]:
    """Which copies of one session get a later `lastActivityAt`: `{index: value}`.

    `values` holds each copy's date as the pass read it. A copy more than
    `lag_ms` behind the newest one is raised to one millisecond before the
    newest. One millisecond short, so that the copy the app last ran the
    session in stays the only newest one: `_rank` picks the record a new
    folder is copied from by this date, and that copy alone holds the model
    and folder the session last ran with. So the function never returns a
    value at or above the newest it was given, never one below the copy's
    own, and nothing once every copy is within `lag_ms`, for any numbers at
    all. A copy whose date is not a number within JavaScript's safe integers
    is no voice and is never written; `sync_flags` passes a date from the
    future, or one not above zero, as None for the same treatment.
    """
    known = [value for value in values if _instant_ms(value)]
    if not known or lag_ms <= 0:
        return {}
    newest = max(known)
    lag = max(float(lag_ms), 1.0)       # a raise must move the copy: newest - 1 > its date
    goal = newest - 1
    # The last test is the promise itself. For integers it never decides: one
    # before the newest is exact. For a negative float just above a power of
    # two it does: `newest - 1` rounds onto the copy's own date there, and
    # without the test the copy would be "raised" to itself on every pass
    # (second review of #167; the dates a pass passes in are positive).
    return {index: goal for index, value in enumerate(values)
            if _instant_ms(value) and newest - value > lag and value < goal < newest}


class _Cancelled(Exception):
    pass


#: How many causes of held flag sync a pass records.
HELD_BY_LIMIT = 5


class _Unlisted(Exception):
    """A folder could not be listed this pass; it is skipped, not taken as empty.

    `transient` is False when nothing in it is anyone's sidebar: it vanished,
    or permission is denied, which denies the app (the same user) as well.
    """

    def __init__(self, where: str, *, transient: bool = True):
        super().__init__(where)
        self.transient = transient


#: Errors that deny the app too (it runs as the same user and reads the same
#: paths): what they hide is in no sidebar and is not a voice in flag sync.
NO_ONES_SIDEBAR = frozenset({errno.EACCES, errno.EPERM, errno.EISDIR, errno.ELOOP,
                             errno.ENOTDIR, errno.ENXIO, errno.ENAMETOOLONG})


def _directory_signature(path: Path) -> tuple[int, ...] | None:
    """`(st_dev, st_ino, st_mtime_ns)` of a directory: what its listing can change."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino, info.st_mtime_ns)


def _permanent(exc: OSError) -> bool:
    """The app cannot read it either (see NO_ONES_SIDEBAR). If such a copy is
    later readable again, its old value reads as a change, as an app's stale
    re-save does: the known limit, not a hold that could never end. Anything
    but a regular file where a record belongs (a directory, a FIFO, a device)
    holds no record either; `transcripts.open_regular` refuses each of them as
    `NotRegularFile` (EINVAL), a directory included."""
    return exc.errno in NO_ONES_SIDEBAR or isinstance(exc, transcripts.NotRegularFile)


@dataclass
class _Payload:
    value: dict[str, Any]
    size: int
    refs: int = 0


@dataclass
class _Folder:
    """One account folder as its last listing saw it."""

    #: `(st_dev, st_ino, st_mtime_ns)` of the directory, read before listing it.
    signature: tuple[int, ...] | None
    names: frozenset[str]
    #: cliSessionId -> the names that hold it.
    ids: dict[str, list[str]]
    #: Every entry read cleanly and is cached, so an unchanged listing can be
    #: reused without touching a file.
    complete: bool


@dataclass
class _Write:
    """One file the mirror placed in the desktop store."""

    folder: str                 # "<account>/<org>"
    name: str
    identity: str
    title: str
    kind: str                   # added | repaired | updated
    at: float                   # epoch seconds, on the mirror's clock
    ctime_ns: int               # the file's ctime right after the write

    def to_row(self) -> list[Any]:
        return [self.folder, self.name, self.identity, self.title, self.kind,
                self.at, self.ctime_ns]

    @classmethod
    def from_row(cls, row: Any) -> "_Write | None":
        try:
            folder, name, identity, title, kind, at, ctime_ns, *_rest = row
            return cls(str(folder), str(name), str(identity), str(title), str(kind),
                       float(at), int(ctime_ns))
        except (TypeError, ValueError):
            return None


def _json_size(value: Any) -> int:
    """Account for the retained Python objects, not only serialized bytes."""
    pending, seen, total = [value], set(), 0
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return total


def _project(value: Any) -> dict[str, Any]:
    """The fields a pass reads, copied out of a parsed index entry."""
    if not isinstance(value, dict):
        return {}
    projected = {key: value[key] for key in PROJECTED if key in value}
    settings = projected.get("sessionSettings")
    if isinstance(settings, dict):
        projected["sessionSettings"] = dict(settings)
    return projected


def store_dir() -> Path:
    override = os.environ.get(STORE_ENV)
    if override:
        return Path(override).expanduser()
    return (Path.home() / "Library" / "Application Support" / "Claude"
            / "claude-code-sessions")


def projects_dir() -> Path:
    return transcripts.projects_dir()


def slug(cwd: str) -> str:
    """The `~/.claude/projects` folder name for a session cwd.

    v1 verified this rule empirically against 400 live transcripts on 2026-07-04.
    """
    return re.sub(r"[^A-Za-z0-9-]", "-", cwd)


def _load(path: Path, *, strict: bool = False) -> dict[str, Any]:
    """A JSON object from an index record, `cc-mirror.json` or a state file.

    Every read the mirror makes outside its state root opens the file only as a
    regular file (`transcripts.open_regular`): the daemon's mirror worker runs
    it, `Timers.stop()` waits for that worker, and a FIFO with no writer had
    made open() wait for one (C-23.28).
    """
    try:
        with transcripts.open_regular(path, "r", encoding="utf-8") as stream:
            value = json.loads(stream.read())
    except (OSError, ValueError):
        if strict:
            raise
        return {}
    return value if isinstance(value, dict) else {}


def _read_entry(path: Path) -> tuple[bytes, os.stat_result]:
    """An index entry's bytes: the one read the inventory makes per changed file.

    The store is listed by name alone, so whatever holds a record's name is
    opened only as a regular file (see `_load`). Keep its descriptor's stat:
    a cold entry needs no preliminary path stat. O_NONBLOCK is harmless on a
    regular file, so a raw descriptor also avoids the stream wrapper's stats
    and two fcntls to clear it. With GIL contention each needless syscall can
    cost a full thread switch interval after the syscall has already returned.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise transcripts.NotRegularFile(errno.EINVAL, "not a regular file", str(path))
        chunks = []
        while chunk := os.read(fd, 64 * 1024):
            chunks.append(chunk)
        return b"".join(chunks), info
    finally:
        os.close(fd)


def _copy_regular(source: str | Path, out) -> os.stat_result:
    """Copy `source`'s bytes into the open file `out`, reading `source` only as a
    regular file; returns `source`'s status as read.

    shutil refused a FIFO only by a stat before its own open(), and copied a
    device (see `_load`).
    """
    with transcripts.open_regular(source) as stream:
        shutil.copyfileobj(stream, out)
        return os.fstat(stream.fileno())


#: The end of every temporary the mirror makes beside a record. Never `*.json`
#: or `*.json.tmp`: the app lists the first and promotes the second on load.
TEMPORARY_SUFFIX = ".tmp-subfleet"
#: A temporary older than this is a pass's leftover (a daemon killed mid-write),
#: which the sweep of a pass that writes removes; a write finishes with its
#: temporary in well under it.
TEMPORARY_STALE_S = 3600


def _temporary(path: Path, suffix: str = TEMPORARY_SUFFIX) -> tuple[int, Path]:
    """A new file beside `path` for its next version: `(descriptor, name)`.

    Made with O_CREAT | O_EXCL | O_NOFOLLOW under a name no one else holds
    (`tempfile.mkstemp`), so it never opens what already stands there: a FIFO
    at the fixed name the mirror had used blocked the write's open() (a revival
    held `Timers.stop()` that way), and a symlink there was followed. The write,
    its times and its fsync all go through the one descriptor; nothing reopens
    the name (reviews of 8172685)."""
    fd, name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=suffix, dir=path.parent)
    return fd, Path(name)


def _same_file(temporary: Path, inode: int) -> None:
    """Raise unless `temporary` still names the regular file the write made."""
    info = os.lstat(temporary)
    if not stat.S_ISREG(info.st_mode) or info.st_ino != inode:
        raise transcripts.NotRegularFile(errno.EINVAL, "not the file the write made", str(temporary))


def _remove_leftovers(paths: Iterable[str]) -> None:
    """Remove temporaries of the mirror's that a killed pass left behind: made
    under names of their own (`_temporary`), they are not reused. Only one whose
    status last changed `TEMPORARY_STALE_S` ago: a write in flight (another state
    root's mirror, say) set its times moments ago, which changes its ctime."""
    now = time.time()
    for path in paths:
        try:
            if now - os.lstat(path).st_ctime > TEMPORARY_STALE_S:
                os.unlink(path)
        except OSError:
            pass


def _stat_signature(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size, info.st_ctime_ns)


def _signature_of(path: str | Path) -> tuple[int, ...]:
    return _stat_signature(os.stat(path))


def _install(temporary: Path, destination: Path, *, expect: tuple[int, ...] | None,
             exclusive: bool, inode: int | None = None) -> bool:
    """Put a finished temporary file in place; False if the destination moved.

    `exclusive` is create-only: a hard link fails if the name was taken since
    the pass looked, so a file the app just created is never replaced.
    `expect` is the signature the pass decided on; the destination is re-read
    right before the rename, which narrows the window in which an app save can
    be lost to the rename itself. `inode` is the temporary the write made: a
    name that holds anything else by now (a FIFO put there) is never put in
    place (`_same_file`).
    """
    if inode is not None:
        _same_file(temporary, inode)
    if exclusive:
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            temporary.unlink()
            return False
        temporary.unlink()
        return True
    if expect is not None:
        try:
            current = _signature_of(destination)
        except FileNotFoundError:
            current = None
        if current != expect:
            temporary.unlink()
            return False
    os.replace(temporary, destination)
    return True


def _write_json(path: Path, value: Any, *, keep_mtime: bool = False,
                mtime: float | None = None, expect: tuple[int, ...] | None = None,
                exclusive: bool = False, sync: bool = False) -> int | None:
    """Atomic write, owner-only like the app's own files.

    Returns the new file's inode, or None when `expect` or `exclusive` found
    the destination changed and nothing was written. Per-account sidebar
    ordering is the file's mtime, so a flag-sync write that changed one boolean
    must not reorder the sidebar: the previous mtime (or the one given) is set
    on the temporary file before it is put in place, so the live file is never
    touched after that.
    """
    stamp = mtime
    if keep_mtime and stamp is None:
        try:
            stamp = path.stat().st_mtime
        except OSError:
            stamp = None
    handle, temporary = _temporary(path)
    try:
        with open(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, separators=(",", ":"), ensure_ascii=False))
            stream.flush()
            if stamp is not None:
                os.utime(stream.fileno(), (stamp, stamp))
            if sync:                           # a record the app loads: as it does
                os.fsync(stream.fileno())
            inode = os.fstat(stream.fileno()).st_ino
        if not _install(temporary, path, expect=expect, exclusive=exclusive, inode=inode):
            return None
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return inode


def _copy_entry(source: Path, destination: Path, *, expect: tuple[int, ...] | None = None,
                exclusive: bool = False) -> int | None:
    """Copy an index file atomically, owner-only, keeping its mtime.

    The app lists a folder in one sweep at load, so a copy must never be visible
    half-written: it is assembled beside the destination and put in place in
    one step. Returns the inode, or None when the destination changed (see
    `_install`).
    """
    handle, temporary = _temporary(destination)
    try:
        with open(handle, "wb") as out:
            info = _copy_regular(source, out)
            out.flush()
            os.utime(out.fileno(), ns=(info.st_atime_ns, info.st_mtime_ns))   # as copy2 did
            os.fchmod(out.fileno(), 0o600)
            os.fsync(out.fileno())             # as the app does before its rename
            inode = os.fstat(out.fileno()).st_ino
        if not _install(temporary, destination, expect=expect, exclusive=exclusive, inode=inode):
            return None
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return inode


# --- the pass ----------------------------------------------------------------

@dataclass
class Pass:
    """One mirroring pass, as the sidecar records it (C-23.28)."""

    started_at: str
    finished_at: str | None = None
    state: str = "running"                  # running | ok | error | cancelled
    added: int = 0
    repaired: int = 0
    revived: int = 0
    pruned: int = 0
    flag_synced: int = 0
    retitled: int = 0
    transcript_retitled: int = 0
    accounts: int = 0
    sessions: int = 0
    error: str | None = None
    dry_run: bool = False
    stage: str = "starting"
    entries_scanned: int = 0
    kind: str = "full"                      # full | hot
    folders_scanned: int = 0
    swept: bool = False
    skipped: int = 0                        # writes that failed and wait for a later pass
    #: Sessions whose flags were not synced this pass: a copy could not be
    #: read, a folder's contents were unknown, or a copy changed while the
    #: pass published.
    flags_held: int = 0
    #: Why, for the first few: `{"path", "reason"}`.
    held_by: list[dict[str, str]] | None = None
    #: Sessions whose stale copies this pass decided to give a later date.
    activity_synced: int = 0
    #: Sessions with a stale copy that `ACTIVITY_SESSIONS_PER_PASS` left for a later pass.
    activity_waiting: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in (
            "started_at", "finished_at", "state", "added", "repaired", "revived",
            "pruned", "flag_synced", "retitled", "transcript_retitled",
            "accounts", "sessions", "error", "dry_run", "stage", "entries_scanned",
            "kind", "folders_scanned", "swept", "skipped", "flags_held", "held_by",
            "activity_synced", "activity_waiting")}

    @property
    def changed(self) -> bool:
        return any((self.added, self.repaired, self.revived, self.pruned,
                    self.flag_synced, self.retitled, self.transcript_retitled,
                    self.activity_synced))

    @property
    def summary(self) -> str:
        return (f"added {self.added}, repaired {self.repaired}, revived {self.revived}, "
                f"pruned {self.pruned}, flag-synced {self.flag_synced}, "
                f"retitled {self.retitled}, t-retitled {self.transcript_retitled}"
                + (f", dates raised {self.activity_synced}" if self.activity_synced else "")
                + (f", dates waiting {self.activity_waiting}" if self.activity_waiting else "")
                + (f", flags held {self.flags_held}" if self.flags_held else ""))


@dataclass
class Options:
    """Everything a pass may be told to do or not do."""

    dry_run: bool = False
    prune: bool = False
    dead_home: str = ""
    exclude: tuple[str, ...] = ()
    flag_sync: bool = True
    restore: bool = True
    archive: str = ""
    ultracode_default: bool = True
    #: Seconds a copy's `lastActivityAt` may trail its session's newest; 0 is off.
    activity_lag_s: float = DEFAULT_ACTIVITY_LAG_S


def load_config(path: Path | None = None) -> dict[str, Any]:
    """v1's `~/.claude/cc-mirror.json`, read and never written.

    Without it the daemon's timer — which passes no flags — would silently lose
    the archive-restore and the dead-session home, because both are per-user
    facts v1 kept here rather than in code.
    """
    return _load(path or (transcripts_dir() / CONFIG_NAME))


def transcripts_dir() -> Path:
    return transcripts.claude_dir()


def options_from(policy: dict[str, Any], **overrides: Any) -> Options:
    """Policy, then v1's saved defaults and caller flags; exclusions accumulate."""
    settings = policy.get("sessions", {})
    config = load_config()
    values: dict[str, Any] = {
        "ultracode_default": bool(settings.get("mirror_ultracode_default", True)),
        "activity_lag_s": float(settings.get("mirror_activity_lag_s", DEFAULT_ACTIVITY_LAG_S))}
    if isinstance(config.get("dead_home"), str):
        values["dead_home"] = config["dead_home"]
    if isinstance(config.get("archive"), str):
        values["archive"] = config["archive"]
    configured_exclude = config.get("exclude")
    if not isinstance(configured_exclude, list):
        configured_exclude = []
    excluded = tuple(item for item in configured_exclude
                     if isinstance(item, str) and item)
    values["exclude"] = tuple(overrides.get("exclude") or ()) + excluded
    values.update({key: value for key, value in overrides.items()
                   if key != "exclude" and value is not None})
    return Options(**values)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _instant(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _clock(value: datetime) -> str:
    """A log-style local wall-clock time, as the app itself prints it."""
    return value.astimezone().strftime("%H:%M:%S")


def _short(account: str, org: str) -> str:
    return f"{account[:8]}…/{org[:8]}…"


#: The dates a record is ranked by: the first of them it holds.
RANK_FIELDS = ("lastActivityAt", "lastFocusedAt", "createdAt")


def _rank(data: dict, fields: Sequence[str] = RANK_FIELDS) -> Any:
    """How recent a record is: the first date it holds among `fields`, or 0.

    The callers compare two ranks with `>`, and a record is whatever the file
    held. A string, list or object in one of these fields used to be returned
    as the rank, and the comparison raised TypeError out of the pass (second
    review of #167). Every full pass then failed the same way and left the
    sidecar at `running`, so the mirror stopped for every session while its
    health read `running`. So only a number is a date here. Any other value
    ranks as no date, as a missing field and a zero always did, and so does
    NaN, which has no place in an order. A bool is not a number.

    Not `_instant_ms`: that also refuses a number beyond JavaScript's safe
    integers, and which record the numbers choose must not change. For
    numbers this returns what `a or b or c or 0` returned.
    """
    for field in fields:
        value = data.get(field)
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and value == value and value):
            return value
    return 0


class _Journal:
    """The mirror's own writes into the desktop store, kept in the state root.

    More than one process writes it: the daemon keeps one Mirror for its life,
    and a `sessions mirror` pass journals its own copies in between. Rows this
    process added and has not saved are kept apart, so reading the file again
    (when another process saved it) or saving over it merges instead of
    dropping anyone's rows.
    """

    def __init__(self, path: Path):
        self.path = path
        self._rows: list[_Write] | None = None
        self._added: list[_Write] = []
        self._seen: tuple[int, ...] | None = None

    def _stat(self) -> tuple[int, ...] | None:
        try:
            info = os.stat(self.path)
        except OSError:
            return None
        return (info.st_ino, info.st_mtime_ns, info.st_size)

    def _read(self) -> list[_Write]:
        self._seen = self._stat()
        raw = _load(self.path).get("writes")
        rows = [_Write.from_row(item) for item in raw] if isinstance(raw, list) else []
        return [row for row in rows if row is not None]

    def rows(self) -> list[_Write]:
        if self._rows is None:
            self._rows = self._read() + self._added
        return self._rows

    def refresh(self) -> None:
        """Re-read the file if another process saved it since this one looked."""
        if self._rows is not None and self._stat() != self._seen:
            on_disk = self._read()
            known = {json.dumps(row.to_row(), sort_keys=True, default=str) for row in on_disk}
            self._rows = on_disk + [row for row in self._added
                                    if json.dumps(row.to_row(), sort_keys=True,
                                                  default=str) not in known]

    def add(self, row: _Write) -> None:
        self.rows().append(row)
        self._added.append(row)

    def save(self, keep: Callable[[_Write], bool]) -> None:
        self.refresh()
        rows = self.rows()
        kept = [row for row in rows if keep(row)][-JOURNAL_LIMIT:]
        if not self._added and len(kept) == len(rows):
            return
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        _write_json(self.path, {"version": 1, "writes": [row.to_row() for row in kept]})
        self._rows, self._added = kept, []
        self._seen = self._stat()


class Mirror:
    """A mirroring pass against one state root and one desktop session store."""

    def __init__(self, root: str | Path, policy: dict[str, Any] | None = None, *,
                 now=None, cancel=None):
        self.root = Path(root)
        self.policy = policy or {}
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.dir = self.root / "sessions"
        self.cancel = cancel
        # Keyed by path string: a `Path` caches its parsed parts, which cost
        # ~300 MiB across 218k entries on 2026-09-24; a string costs ~35 MiB.
        self._entries: dict[str, tuple[tuple[int, ...], bytes]] = {}
        self._payloads: dict[bytes, _Payload] = {}
        self._payload_bytes = 0
        self._pass_payloads: dict[bytes, dict] = {}
        self._progress_due = 0.0
        self._folders: dict[Path, _Folder] = {}
        #: Folders this process wrote into or found busy; re-listed next pass.
        self._dirty: set[Path] = set()
        #: `<project dir> -> (st_mtime_ns, {stem: transcript})`, depth one.
        self._stem_dirs: dict[str, tuple[int, dict[str, Path], frozenset[str]]] = {}
        #: The last full pass's `<session id> -> transcript`.
        self._stems: dict[str, Path] = {}
        self._archive: tuple[str, float, dict[str, tuple[int, Path]], frozenset[str]] | None = None
        self._last_sweep: float | None = None
        #: True once a full inventory finished in this process: the hot pass
        #: needs one to know what every folder already holds.
        self._inventoried = False
        #: New records the hot pass could not spread yet (no transcript).
        self._retry: dict[str, float] = {}
        self._desktop: DesktopLog | None = None
        self.journal = _Journal(self.dir / JOURNAL_NAME)
        #: `(path, inode, ctime) -> projection` for the load-gap report's reads.
        self._gap_reads: dict[tuple[str, int, int], dict] = {}
        #: This pass's entries that exist but could not be read: path -> the
        #: session id their last good read held ("" if none). Flag sync must
        #: not decide a session without one of its copies (C-23.28).
        self._unread: dict[str, str] = {}
        #: Why each unread path or unknown folder holds, for the pass record.
        self._why: dict[str, str] = {}
        #: `<account> -> (its directory's signature, its org folders)` from its
        #: last successful listing.
        self._account_orgs: dict[Path, tuple[tuple[int, ...] | None, frozenset[Path]]] = {}
        #: What the last `folders()` call could not list.
        self._store_error: OSError | None = None
        self._unlisted_accounts: list[Path] = []
        #: Flag candidates held by the previous pass must be retried even if
        #: no directory changes again.
        self._flag_retry: set[str] = set()
        self._hot_recorded: tuple[float, tuple] | None = None
        #: A full pass services flags synchronously under its existing flock.
        #: Its helper has separate inventory bookkeeping, never another writer.
        self._hot_worker: Mirror | None = None
        self._hot_parent: Mirror | None = None
        self._hot_options: Options | None = None
        self._hot_due = 0.0
        self._hot_services = 0
        self._hot_epoch = 0
        #: A published flag decision or copy write left standing this hot pass.
        self._flags_moved = False
        self._flags_active = False
        #: The split-id report of the inventory the running pass took, until
        #: that pass records it. Never kept: a process that wrote a report it
        #: took earlier could put it over a later one of another process's.
        self._splits: dict[str, Any] | None = None
        #: `<session> -> the flag syncs that chose its date raise and could not
        #: publish it`, since it last went through. A sync takes the sessions
        #: with the fewest first, so those that never publish take turns behind
        #: the rest instead of holding the bound (second review of #167: with a
        #: memory of one sync, two groups of ten alternated for ever). One
        #: ledger for the process: the embedded hot worker shares it.
        self._activity_tries: dict[str, int] = {}
        #: The last transcript discovery listed every project directory.
        self._stems_whole = True

    @staticmethod
    def _signature(path: Path) -> tuple[int, ...]:
        return _signature_of(path)

    def _invalidate_folder(self, path: Path) -> None:
        """A known write invalidates both cooperating inventories.

        Directory timestamps need not distinguish two writes. Each inventory
        keeps its own entries and payload references, but neither may retain a
        folder listing from before a write the other one made.
        """
        self._dirty.add(path)
        if self._hot_worker is not None:
            self._hot_worker._dirty.add(path)
        if self._hot_parent is not None:
            self._hot_parent._dirty.add(path)

    def _forget(self, path: str | Path) -> None:
        old = self._entries.pop(os.fspath(path), None)
        if old is not None:
            payload = self._payloads[old[1]]
            payload.refs -= 1
            if not payload.refs:
                self._payload_bytes -= payload.size
                del self._payloads[old[1]]

    def _remember(self, path: Path, signature: tuple[int, ...], digest: bytes,
                  data: dict) -> dict:
        existing = self._payloads.get(digest)
        # The pass also shares uncached payloads: a cold scan must not retain a
        # copy of the same projection for every account.
        value = existing.value if existing else self._pass_payloads.setdefault(digest, data)
        self._forget(path)
        if len(self._entries) >= ENTRY_CACHE_LIMIT:
            return value
        payload = self._payloads.get(digest)
        if payload is None:
            size = _json_size(value)
            if self._payload_bytes + size > PAYLOAD_CACHE_BYTES:
                return value
            payload = self._payloads[digest] = _Payload(value, size)
            self._payload_bytes += size
        payload.refs += 1
        self._entries[os.fspath(path)] = (signature, digest)
        return payload.value

    def _entry(self, path: Path, *, listing: os.DirEntry | None = None) -> dict:
        """An immutable projection of one entry, read only when its signature moved.

        Equal bytes are parsed once: the 217,706 entries of 2026-09-24 held
        9,619 distinct contents. Only flag sync makes writable copies. A file
        that exists and cannot be read is recorded in `_unread` with the
        session its last good read held (C-23.28, review rounds 5 and 6).
        """
        key = os.fspath(path)
        cached = self._entries.get(key)
        known = (self._payloads[cached[1]].value.get("cliSessionId") or "") if cached else ""
        try:
            if cached is not None:
                signature = (_stat_signature(listing.stat()) if listing is not None
                             else self._signature(path))
                if cached[0] == signature:
                    return self._payloads[cached[1]].value
            self._forget(path)
            raw, info = _read_entry(path)
            signature = _stat_signature(info)
        except FileNotFoundError:
            self._forget(path)                  # gone: no longer anyone's copy
            return {}
        except OSError as exc:
            self._forget(path)
            if not _permanent(exc):
                # Unknown, not absent (EMFILE on 2026-09-25): flag sync holds
                # the session it belongs to.
                self._unread[key] = str(known) or self._unread.get(key, "")
                self._why[key] = f"unreadable ({errno.errorcode.get(exc.errno or 0, exc)})"
            return {}
        try:
            digest = hashlib.sha256(raw).digest()
            hit = self._payloads.get(digest)
            if hit is not None:
                data = hit.value
            else:
                data = self._pass_payloads.get(digest)
                if data is None:
                    data = _project(json.loads(raw))
        except ValueError:
            return {}                           # not a record the app wrote
        try:
            unchanged = self._signature(path) == signature
        except OSError:
            return data                         # read; just not a cache entry
        # A concurrent app replacement is not a valid cross-pass cache hit.
        if unchanged:
            return self._remember(path, signature, digest, data)
        return data

    def _file(self, folder: Path, name: str) -> dict:
        """A listed entry's projection, from the cache when it holds one."""
        cached = self._entries.get(os.path.join(folder, name))
        if cached is not None:
            return self._payloads[cached[1]].value
        return self._entry(folder / name)

    def _checkpoint(self, current: Pass, stage: str | None = None) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise _Cancelled("mirror pass cancelled; partial copies will be reconciled next pass")
        changed = stage is not None and stage != current.stage
        if stage is not None:
            current.stage = stage
        if current.kind != "full":
            return                  # a hot pass never rewrites the full pass's record
        instant = time.monotonic()
        if changed or instant >= self._progress_due:
            self._record(current)
            self._progress_due = instant + PROGRESS_INTERVAL_S
        if (self._hot_options is not None and not self._flags_active
                and instant >= self._hot_due):
            self._service_hot()

    def _fork_hot(self) -> "Mirror":
        """Snapshot the inventory before a full scan starts changing it.

        Projections and completed folder listings are immutable. Only payload
        reference counts and the inventory containers need their own copies.
        The journal is shared: both passes write sequentially under one flock.
        """
        worker = Mirror(self.root, self.policy, now=self.now, cancel=self.cancel)
        worker._hot_parent = self
        worker._entries = dict(self._entries)
        worker._payloads = {key: _Payload(row.value, row.size, row.refs)
                            for key, row in self._payloads.items()}
        worker._payload_bytes = self._payload_bytes
        worker._folders = dict(self._folders)
        worker._dirty = set(self._dirty)
        worker._account_orgs = dict(self._account_orgs)
        worker._stems = dict(self._stems)
        worker._flag_retry = set(self._flag_retry)
        worker._activity_tries = self._activity_tries      # shared, as the journal is
        worker._hot_recorded = self._hot_recorded
        worker._desktop = self._desktop
        worker.journal = self.journal
        return worker

    def _service_hot(self) -> None:
        """Service flags at a full-pass checkpoint without releasing its lock.

        No revival, spreading, repair or pruning here: a full pass's retained
        liveness and empty-copy decisions remain valid. A flag transaction is
        never interrupted by another one; its base and writes commit together.
        """
        worker, options = self._hot_worker, self._hot_options
        if worker is None or options is None:
            return
        worker.now, worker.cancel = self.now, self.cancel
        worker._run_hot_locked(options, spread=False)
        self._hot_recorded = worker._hot_recorded
        self._hot_services += 1
        if worker._flags_moved:
            # A no-op or held candidate does not invalidate a refresh. A base
            # can advance even when every copy already agrees and none is written.
            self._hot_epoch += 1
        interval = float(self.policy.get("sessions", {}).get("mirror_hot_interval_s", 2))
        # Completion-based: a slow hot pass cannot recursively starve the full
        # inventory. A due service starts at the next checkpoint after this.
        self._hot_due = time.monotonic() + interval

    # --- state files ---------------------------------------------------------

    @property
    def sidecar_path(self) -> Path:
        return self.dir / SIDECAR_NAME

    @property
    def flags_path(self) -> Path:
        return self.dir / FLAGS_NAME

    def sidecar(self) -> dict[str, Any]:
        return _load(self.sidecar_path)

    def _record(self, current: Pass, *, last_ok: str | None = None,
                load_gap: dict[str, Any] | None = None) -> None:
        if current.dry_run:
            return
        self.dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        previous = self.sidecar()
        value = {key: previous[key] for key in ("hot", "load_gap", "splits") if key in previous}
        value.update({
            "pass": current.to_dict(),
            "last_ok_at": last_ok or previous.get("last_ok_at"),
            "interval_s": self.policy.get("sessions", {}).get("mirror_interval_s", 60),
            "updated_at": _iso(self.now()),
        })
        if load_gap is not None:
            value["load_gap"] = load_gap
        if self._splits is not None:
            # Recorded once, by the pass that took the inventory, inside its
            # lock: passes are ordered by that lock, so the latest report is
            # the last one written, whatever any clock says.
            value["splits"], self._splits = self._splits, None
        _write_json(self.sidecar_path, value)

    def _record_hot(self, current: Pass, load_gap: dict[str, Any] | None) -> None:
        """The hot pass's own block; never the heartbeat C-23.28 judges by."""
        if current.dry_run:
            return
        instant = time.monotonic()
        seen = (current.state, current.flags_held, current.held_by,
                (load_gap or {}).get("status"), (load_gap or {}).get("pending"))
        last = self._hot_recorded
        if last is not None and instant - last[0] < HOT_RECORD_S:
            if current.state == "running":
                return
            if (current.state == "ok" and not current.changed and not self._flags_moved
                    and last[1] == seen):
                return
        self.dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        value = self.sidecar()
        value["hot"] = current.to_dict()
        if load_gap is not None:
            value["load_gap"] = load_gap
        _write_json(self.sidecar_path, value)
        self._hot_recorded = (instant, seen)

    # --- the store -----------------------------------------------------------

    def folders(self, exclude: Iterable[str]) -> list[tuple[str, str, Path]]:
        """Every `<account>/<org>` directory the desktop store holds.

        What cannot be listed is recorded, not taken as empty (review round 6):
        `_store_error` when the store itself could not be listed, and
        `_unlisted_accounts` for accounts whose org folders are unknown this
        call. A vanished directory, or one the user may not read, is in no
        sidebar and is left out.
        """
        excluded = [value for value in exclude if value]
        found: list[tuple[str, str, Path]] = []
        self._store_error = None
        self._unlisted_accounts = []
        base = store_dir()

        def subdirectories(where: Path) -> list[Path]:
            # No stat per entry (a symlink would be followed, and one failed
            # stat would fail the whole directory): the listing's own type.
            with os.scandir(where) as listing:
                return sorted(Path(item.path) for item in listing
                              if item.is_dir(follow_symlinks=False))

        try:
            accounts = subdirectories(base)
        except FileNotFoundError:
            return found
        except OSError as exc:
            if not _permanent(exc):
                self._store_error = exc
            return found
        for account in accounts:
            if any(value in account.name for value in excluded):
                continue                        # excluded whole: never a voice
            signature = _directory_signature(account)
            try:
                orgs = subdirectories(account)
            except FileNotFoundError:
                continue
            except OSError as exc:
                if not _permanent(exc):
                    self._unlisted_accounts.append(account)
                continue
            self._account_orgs[account] = (signature, frozenset(orgs))
            for org in orgs:
                if any(value in account.name or value in org.name for value in excluded):
                    continue
                found.append((account.name, org.name, org))
        return found

    def _listing_gaps(self, options: "Options") -> tuple[list[tuple[str, str, Path]],
                                                        list[tuple[str, str]]]:
        """After `folders()`: the known org folders of accounts that did not
        list, and why the pass cannot know what they hold (empty if it can).

        Unknown is not empty: a store that did not list raises, and a failed
        account keeps the folders its last listing named. If it never listed,
        or one of those folders was never listed itself, what it holds now is
        unknown (review round 7).
        """
        if self._store_error is not None:
            raise self._store_error
        kept: list[tuple[str, str, Path]] = []
        unknown: list[tuple[str, str]] = []
        for account in self._unlisted_accounts:
            last = self._account_orgs.get(account)
            if last is None:
                unknown.append((os.fspath(account), "account not listed, never listed"))
                continue
            signature, orgs = last
            if signature is None or _directory_signature(account) != signature:
                # A folder created since (another process may have filled it)
                # is in no listing this pass has.
                unknown.append((os.fspath(account), "account not listed, changed since its listing"))
                continue
            for org in sorted(orgs):
                if any(value and (value in account.name or value in org.name)
                       for value in options.exclude):
                    continue
                if org in self._folders:
                    kept.append((account.name, org.name, org))
                else:
                    unknown.append((os.fspath(org), "account not listed, folder never listed"))
        return kept, unknown

    def transcript_stems(self, current: Pass | None = None, *,
                         sweep: bool = True, tidy: bool = False) -> dict[str, Path]:
        """`<session id> -> transcript`; a session is openable iff it is a key.

        A session transcript is `projects/<slug>/<id>.jsonl`, one level down;
        the deeper `.jsonl` files are subagent logs (34k of them on 2026-09-24,
        none named like a session). A project directory is re-listed only when
        its mtime moved, which creating or pruning a transcript always does.
        Only a regular file is a transcript. A listing kept since then still
        names the same files, but a transcript that is a symlink can name
        something else by now, its target replaced where it lives: each such
        link is checked again on every call (reviews of 8172685), where a FIFO
        it had come to name had been spread as a transcript.

        `sweep` lists every directory again. `tidy` also removes the stale
        temporaries of revivals from each directory it lists. Only a pass that
        writes asks for that (`_pass`); for every other caller this is a read.
        """
        stems: dict[str, Path] = {}
        base = projects_dir()
        # `_stems_whole` is False when a listing failed for a cause that may
        # pass (EMFILE): what it hid is unknown, not absent. What the user may
        # not read, the app cannot open either (`_permanent`).
        self._stems_whole = True
        try:
            with os.scandir(base) as listing:
                directories = sorted(item.path for item in listing
                                     if item.is_dir(follow_symlinks=False))
        except OSError as exc:
            self._stems_whole = isinstance(exc, FileNotFoundError) or _permanent(exc)
            return stems
        seen = set()
        for directory in directories:
            if current is not None:
                self._checkpoint(current)
            seen.add(directory)
            try:
                mtime = os.stat(directory).st_mtime_ns
            except OSError as exc:
                if not (isinstance(exc, FileNotFoundError) or _permanent(exc)):
                    self._stems_whole = False
                continue
            cached = self._stem_dirs.get(directory)
            if sweep or cached is None or cached[0] != mtime:
                found, links, leftovers = {}, set(), []
                try:
                    with os.scandir(directory) as listing:
                        for item in listing:
                            if item.name.endswith(".jsonl") and item.is_file():
                                found[item.name[:-6]] = Path(item.path)
                                if item.is_symlink():
                                    links.add(item.name[:-6])
                            elif tidy and item.name.endswith(".tmp-revive"):
                                leftovers.append(item.path)
                except OSError as exc:
                    if not (isinstance(exc, FileNotFoundError) or _permanent(exc)):
                        # Not kept as an empty listing: the directory's mtime
                        # has not moved, so a kept one would hide its
                        # transcripts until the next sweep.
                        self._stems_whole = False
                        self._stem_dirs.pop(directory, None)
                        continue
                    found, links = {}, set()
                _remove_leftovers(leftovers)
                self._stem_dirs[directory] = (mtime, found, frozenset(links))
                stems.update(found)
                continue
            listed, links = cached[1], cached[2]
            stems.update(listed if not links else
                         {stem: path for stem, path in listed.items() if stem not in links or path.is_file()})
        for directory in list(self._stem_dirs):
            if directory not in seen:
                del self._stem_dirs[directory]
        return stems

    @staticmethod
    def transcript_title(path: Path, window: int = 262_144) -> str | None:
        """The newest `custom-title` record in the transcript tail.

        The app appends one on every (re)title — UI, backend, and auto alike — so
        the last record is the newest intended title regardless of which
        account's index caught it. `None` means no record inside the window:
        no signal, never a change. A FIFO, device or directory at `path` is no
        signal either, at once (see `_load`).
        """
        try:
            with transcripts.open_regular(path) as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - window))
                tail = stream.read().decode("utf-8", "ignore")
        except OSError:
            return None
        for line in reversed(tail.splitlines()):
            if '"custom-title"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("type") == "custom-title":
                title = entry.get("customTitle")
                if isinstance(title, str) and title:
                    return title
        return None

    # --- the inventory -------------------------------------------------------

    def _sweep_due(self) -> bool:
        return (self._last_sweep is None
                or time.monotonic() - self._last_sweep >= SWEEP_INTERVAL_S)

    def _scan(self, path: Path, current: Pass, *, sweep: bool,
              tidy: bool = False) -> tuple[dict[str, dict] | None, list[str]]:
        """Refresh one folder: `(its entries, or None if unchanged; fresh names)`.

        A fresh name is one whose content this process had not seen at that
        path: a new file, or a file whose bytes changed. `sweep` reads every
        entry again. `tidy` also removes the mirror's stale temporaries from a
        folder it lists, and only a pass that writes asks for that (`_pass`).
        """
        try:
            info = os.stat(path)
            signature: tuple[int, ...] | None = (info.st_dev, info.st_ino, info.st_mtime_ns)
        except OSError:
            signature = None
        state = self._folders.get(path)
        if (not sweep and state is not None and state.complete and signature is not None
                and state.signature == signature and path not in self._dirty):
            current.entries_scanned += len(state.names)
            return None, []
        current.folders_scanned += 1
        if state is not None:
            # An interrupted refresh must not make the old listing reusable.
            # Replace the snapshot, never mutate one shared with the hot worker.
            self._folders[path] = _Folder(state.signature, state.names, state.ids, False)
        # Clear only the old invalidation. An embedded hot write during this
        # listing or its reads must leave a fresh invalidation for the next scan.
        self._dirty.discard(path)
        try:
            with os.scandir(path) as listing:
                # The same ~1.8k names recur in every folder; interned, the
                # folders' listings share one string per name.
                found, leftovers = [], []
                for index, item in enumerate(listing):
                    if index % LISTING_CHECKPOINT_ENTRIES == 0:
                        self._checkpoint(current)
                    if item.name.startswith("local_") and item.name.endswith(".json"):
                        found.append((sys.intern(item.name), item))
                    elif tidy and item.name.endswith(TEMPORARY_SUFFIX):
                        leftovers.append(item.path)
                found.sort(key=lambda row: row[0])
        except OSError as exc:
            # Unknown is not empty: an empty view would hide every session the
            # folder holds and invite copies over them. List it again next pass.
            self._dirty.add(path)
            transient = not (isinstance(exc, FileNotFoundError) or _permanent(exc))
            raise _Unlisted(str(path), transient=transient) from exc
        _remove_leftovers(leftovers)
        previous = state.names if state is not None else frozenset()
        files: dict[str, dict] = {}
        ids: dict[str, list[str]] = {}
        fresh: list[str] = []
        complete = signature is not None
        base = os.fspath(path)
        previous_owner: dict[str, str] | None = None
        for name, item in found:
            self._checkpoint(current)
            key = os.path.join(base, name)
            cached = self._entries.get(key)
            if (not sweep and cached is not None and name in previous
                    and cached[0][1] == item.inode()):
                data = self._payloads[cached[1]].value
            else:
                before = cached[1] if cached is not None else None
                data = self._entry(path / name, listing=item)
                after = self._entries.get(key)
                if after is None:
                    complete = False
                if data and (after is None or after[1] != before):
                    fresh.append(name)
            current.entries_scanned += 1
            files[name] = data
            identity = data.get("cliSessionId") or ""
            if not identity and key in self._unread:
                # Unreadable now: its session is whatever the last listing's
                # read found, and the listing keeps saying so until it reads.
                if previous_owner is None:
                    previous_owner = {held: owner for owner, names in
                                      (state.ids.items() if state is not None else ())
                                      for held in names}
                identity = self._unread[key] or previous_owner.get(name, "")
                self._unread[key] = identity
            if identity:
                ids.setdefault(identity, []).append(name)
        names = frozenset(name for name, _item in found)
        for name in previous - names:
            self._forget(path / name)
        self._folders[path] = _Folder(signature, names, ids, complete)
        return files, fresh

    def _files(self, path: Path) -> dict[str, dict]:
        """An unchanged folder's entries, from the cache."""
        state = self._folders[path]
        return {name: self._file(path, name) for name in sorted(state.names)}

    def _drop_folders(self, folders: list[tuple[str, str, Path]]) -> None:
        """Forget folders that vanished or are now excluded (after a full listing)."""
        live = {path for _account, _org, path in folders}
        for path in list(self._folders):
            if path not in live:
                for name in self._folders.pop(path).names:
                    self._forget(path / name)
        self._dirty &= live

    def _recent(self, path: Path) -> bool:
        """The file was written within `UNRESOLVED_RETRY_S` (the wall clock, as mtimes are)."""
        cached = self._entries.get(os.fspath(path))
        try:
            mtime_ns = cached[0][2] if cached is not None else os.stat(path).st_mtime_ns
        except OSError:
            return False
        return time.time() - mtime_ns / 1e9 <= UNRESOLVED_RETRY_S

    def _openable(self, data: dict) -> bool:
        """The hot pass's resolvability check: a transcript exists for the session."""
        identity = data.get("cliSessionId") or ""
        if not identity:
            return False
        known = self._stems.get(identity)
        if known is not None and known.is_file():
            return True
        for cwd in (data.get("originCwd"), data.get("cwd")):
            if isinstance(cwd, str) and cwd:
                candidate = projects_dir() / slug(cwd) / f"{identity}.jsonl"
                if candidate.is_file():
                    self._stems[identity] = candidate
                    return True
        return False

    # --- writes into the store -----------------------------------------------

    def _journal_write(self, destination: Path, inode: int | None, identity: str,
                       data: dict, kind: str, *, flags: bool = True) -> None:
        """Journal a write, unless the app replaced the file before we looked.

        `flags` is False for an update that changed only the date. It leaves
        every field a flag decision reads as it was, so it does not invalidate
        the full pass's refresh: if it did, an app re-saving one old date could
        hold every session's flags pass after pass (review of #167).
        """
        moved = kind == "updated" and flags
        try:
            info = os.stat(destination)
        except OSError:
            # A failed observation cannot prove the write no longer stands.
            if moved:
                self._flags_moved = True
            return
        if inode is not None and info.st_ino != inode:
            return
        if moved:
            # Includes a write left standing when its put-back failed; an app
            # replacement rejected above is not one of our surviving writes.
            self._flags_moved = True
        folder = f"{destination.parent.parent.name}/{destination.parent.name}"
        self.journal.add(_Write(folder, destination.name, identity,
                                str(data.get("title") or ""), kind,
                                self.now().timestamp(), info.st_ctime_ns))

    def _journal_restamp(self, destination: Path, was: tuple[int, ...], inode: int) -> None:
        """The mirror put back the file it had found as `was`: carry its journal row over.

        A rollback gives the file a new ctime. If the file it restored was the
        mirror's own last write (its journal row's ctime is `was`'s), the report
        must still read it as that write, not as the app rewriting it.
        """
        folder = f"{destination.parent.parent.name}/{destination.parent.name}"
        rows = sorted((row for row in self.journal.rows()
                       if row.folder == folder and row.name == destination.name),
                      key=lambda row: row.at)
        if not rows or rows[-1].ctime_ns != was[4]:
            return                            # it restored the app's own save
        try:
            info = os.stat(destination)
        except OSError:
            return
        if info.st_ino != inode:
            return
        last = rows[-1]
        self.journal.add(_Write(last.folder, last.name, last.identity, last.title, last.kind,
                                last.at, info.st_ctime_ns))

    def _place(self, source: Path, destination: Path, identity: str, data: dict,
               kind: str, current: Pass, *, expect: tuple[int, ...] | None = None,
               exclusive: bool = False) -> bool:
        self._invalidate_folder(destination.parent)
        try:
            inode = _copy_entry(source, destination, expect=expect, exclusive=exclusive)
        except OSError:
            current.skipped += 1              # the source moved; a later pass retries
            return False
        if inode is None:
            return False                      # the destination moved; decide again next pass
        self._forget(destination)
        self._journal_write(destination, inode, identity, data, kind)
        return True

    def _spread(self, identity: str, data: dict, name: str, source: Path,
                folders: list[tuple[str, str, Path]], *,
                has: Callable[[Path, str], bool],
                existing: Callable[[Path, str], dict | None],
                note: Callable[[Path, str, dict, str], None],
                options: Options, current: Pass) -> None:
        """Put one openable session into every folder that lacks it."""
        for _account, _org, path in folders:
            self._checkpoint(current)
            if has(path, identity):
                continue                                    # this account has it
            present = existing(path, name)
            if present is None:                             # the name is free
                if not options.dry_run:
                    # Create-only: a name someone took since the listing is left
                    # alone, and the next pass sees who and decides again.
                    if not self._place(source, path / name, identity, data, "added",
                                       current, exclusive=True):
                        continue
                    note(path, name, data, identity)
                current.added += 1
            elif not (present.get("cliSessionId") or ""):   # stale empty
                # The app empties the id itself when it moves the record off
                # this session: /clear first records the id it leaves in
                # `priorCliSessionIds`, and a cwd or worktree move changes `cwd`
                # on a record newer than the copy being spread. That record is
                # the app's newest, not a frozen copy.
                if identity in (present.get("priorCliSessionIds") or ()) or (
                        present.get("cwd") and data.get("cwd")
                        and present.get("cwd") != data.get("cwd")
                        and _rank(present) > _rank(data)):
                    continue
                if not options.dry_run:
                    # Replace only the stale record the pass classified: its
                    # signature from the listing, or (past the cache limit) one
                    # read now from a file that is still empty.
                    cached = self._entries.get(os.path.join(path, name))
                    if cached is not None:
                        expect = cached[0]
                    else:
                        # Uncached: past the cache limit, or a record that did
                        # not parse (a torn or corrupt file, which the repair
                        # replaces as v1 did). Re-check what is there now.
                        try:
                            expect = _signature_of(path / name)
                        except OSError:
                            continue
                        try:
                            still = _load(path / name, strict=True).get("cliSessionId")
                        except ValueError:
                            still = None            # torn or corrupt: repair it
                        except OSError:
                            continue                # unreadable is unknown, not empty
                        if still:
                            continue
                    if not self._place(source, path / name, identity, data, "repaired",
                                       current, expect=expect):
                        continue
                    note(path, name, data, identity)
                current.repaired += 1
            elif present.get("cliSessionId") == identity:
                continue                  # the folder holds it; a listing raced a save
            else:
                # `local_<id>` filenames are not unique across accounts, so a
                # collision falls back to a cli-derived name rather than
                # clobbering a different session.
                fallback = f"local_{identity}.json"
                destination = path / fallback
                if existing(path, fallback) is not None or destination.exists():
                    continue
                if not options.dry_run:
                    self._invalidate_folder(path)
                    try:
                        body = _load(source, strict=True)
                        body["sessionId"] = f"local_{identity}"   # keep it self-consistent
                        try:                       # preserve sidebar ordering
                            stamp: float | None = source.stat().st_mtime
                        except OSError:
                            stamp = None
                        inode = _write_json(destination, body, mtime=stamp, exclusive=True,
                                            sync=True)
                    except (OSError, ValueError):
                        current.skipped += 1
                        continue
                    if inode is None:
                        continue                    # the name was taken meanwhile
                    self._forget(destination)
                    projected = _project(body)
                    self._journal_write(destination, inode, identity, projected, "added")
                    note(path, fallback, projected, identity)
                current.added += 1

    # --- the steps -----------------------------------------------------------

    def restore_dead(self, folder_files: dict[Path, dict[str, dict]],
                     stems: dict[str, Path], pattern: str, dry_run: bool,
                     current: Pass | None = None) -> int:
        """Revive dead sessions whose transcript survives in an archive.

        Creation-only: never overwrites, never deletes, safe every pass. Mutates
        `stems` so the copy step treats a revived session as openable. The
        archive is walked again only when a new dead session appears or
        `ARCHIVE_RESCAN_S` after the last walk.
        """
        dead: dict[str, dict] = {}
        for files in folder_files.values():
            for data in files.values():
                if current is not None:
                    self._checkpoint(current)
                identity = data.get("cliSessionId") or ""
                if not identity or identity in stems:
                    continue
                # Worktree sessions live under the ORIGIN project dir, so prefer
                # the entry that knows its originCwd.
                if identity not in dead or (data.get("originCwd")
                                            and not dead[identity].get("originCwd")):
                    dead[identity] = data
        if not dead or not pattern:
            return 0
        cache = self._archive
        wanted = frozenset(dead)
        if (cache is not None and cache[0] == pattern
                and time.monotonic() - cache[1] < ARCHIVE_RESCAN_S and wanted <= cache[3]):
            archive = cache[2]
        else:
            # Largest file wins on duplicate stems: an archive can hold several
            # snapshots of one session, and the largest is the longest conversation.
            # Only the dead sessions' files are stat'ed and kept; holding a Path
            # for each of 56k archive files cost ~120 MiB (2026-09-24).
            archive = {}
            for name in globbing.iglob(os.path.expanduser(pattern), recursive=True):
                if current is not None:
                    self._checkpoint(current)
                stem = os.path.splitext(os.path.basename(name))[0]
                if stem not in wanted:
                    continue
                try:
                    size = os.stat(name).st_size
                except OSError:            # an archive sync may unlink mid-walk
                    continue
                previous = archive.get(stem)
                if previous is None or size > previous[0]:
                    archive[stem] = (size, Path(name))
            self._archive = (pattern, time.monotonic(), archive, wanted)
        revived = 0
        for identity, data in sorted(dead.items()):
            if current is not None:
                self._checkpoint(current)
            source = archive.get(identity)
            cwd = data.get("originCwd") or data.get("cwd")
            if not source or not cwd:
                continue
            destination = projects_dir() / slug(cwd) / f"{identity}.jsonl"
            if not dry_run:
                if destination.exists():
                    # Raced with another writer. Only a regular file is a
                    # transcript, as in `transcript_stems`; a FIFO there is
                    # none, and revival never replaces what it finds.
                    if destination.is_file():
                        stems[identity] = destination
                    continue
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    placed = self._revive(source[1], destination)
                except OSError:
                    continue
                if not placed:                  # a transcript appeared since the check: another writer's
                    if destination.is_file():
                        stems[identity] = destination
                    continue
            stems[identity] = destination
            revived += 1
        return revived

    def _revive(self, source: Path, destination: Path) -> bool:
        """Copy an archived transcript to `destination` if nothing stands there;
        False if something does. The copy is a new file of its own (`_temporary`),
        stamped now so cleanup cannot prune it at once, and put in place create-only
        (a hard link): a transcript written after the pass looked is never
        replaced, where `os.replace` had overwritten it."""
        handle, temporary = _temporary(destination, ".tmp-revive")
        try:
            with open(handle, "wb") as out:
                _copy_regular(source, out)
                out.flush()
                stamp = self.now().timestamp()
                os.utime(out.fileno(), (stamp, stamp))
                os.fsync(out.fileno())             # whole before it is anyone's transcript
                inode = os.fstat(out.fileno()).st_ino
            _same_file(temporary, inode)
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError:
                return False
            except OSError as exc:
                if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK):
                    raise
                # A filesystem without hard links: renamed after a fresh look, which
                # leaves the one race `os.replace` had, never replacing what is there.
                if os.path.lexists(destination):
                    return False
                os.rename(temporary, destination)
            return True
        finally:
            temporary.unlink(missing_ok=True)

    def sync_flags(self, folder_files: dict[Path, dict[str, dict]],
                   stems: dict[str, Path], options: Options,
                   current: Pass, *, unread: dict[str, str] | None = None,
                   blind: bool = False, complete: bool = True,
                   retry: set[str] | None = None) -> set[tuple[Path, str]]:
        """Propagate `isArchived`, `isStarred` and the title across every copy.

        The merge base in `mirror-flags.json` holds each session's last synced
        values: a copy that differs from the base is a user action, so the CHANGE
        propagates and archive and un-archive both work. On first divergence with
        no base — the historical backlog — archived-anywhere and starred-anywhere
        win, and a divergent title prefers a manual rename, then the most recently
        active copy.

        A write re-reads the whole file and patches only the synced fields, so
        the rest of the record is the app's newest. Each session's writes are
        all or nothing: if any copy's synced fields moved since this pass read
        it, none is written and the session's merge base is not advanced, so the
        next pass decides again on what is there. A save that lands after
        that check fails the write of its copy; the copies already written
        are then put back (any rewritten since are left alone), and the base
        is held. What no check can catch is an app rename in the instant
        between the last signature check and the mirror's own rename, which
        rename(2) cannot compare first: that save is overwritten. The protocol
        is specified in docs/formal/MirrorFlags.tla, with an executable twin
        in tests/mirror_flags_model.py.

        A session is decided from every copy or not at all. `unread` names the
        copies that exist but could not be read this pass (path -> the session
        id their last good read held, or ""); each one holds its session. One
        whose session is unknown, or `blind` (a folder or account whose
        contents are unknown), holds every session. A held session writes
        nothing and keeps its base. Bases of sessions this pass did not see
        are kept unless the inventory was `complete`. A directory or file the
        user may not read is no one's sidebar and holds nothing.
        `retry` collects only identified held sessions; blind holds wait for
        the full pass instead of enrolling the whole store in hot retries.

        Known limit: the app saves a record from memory, so a folder it holds
        (the loaded one, or one where an earlier account's session still runs)
        can write back a value the mirror changed there, and the merge base
        reads that re-save as a user's change. See the 2026-09-24 report.

        The same publish raises `lastActivityAt` on a copy that trails its
        session's newest by more than `options.activity_lag_s`
        (`activity_targets`), unless the session's flag decision is archived.
        The raises are in the session's batch, so they are written or put
        back with it. Two things differ from a flag. The date is not in the
        merge base. And the pre-check does not hold a session whose date
        moved: the write keeps the later of what the copy holds and what the
        pass decided, and a copy that needs nothing after that is not
        written. A sync raises at most `ACTIVITY_SESSIONS_PER_PASS` sessions,
        those chosen and not published the fewest times first. A date
        more than `ACTIVITY_FUTURE_S` past this pass's clock, or not above
        zero, is no voice: it is not raised and it is never the newest. A
        write that changed only a date does not invalidate the full pass's
        refresh (`_journal_write`).
        The date's protocol is docs/formal/MirrorActivity.tla, with its twin
        in tests/mirror_activity_model.py.
        """
        base_all = _load(self.flags_path)
        groups: dict[str, list[tuple[Path, str, dict]]] = {}
        for path, files in folder_files.items():
            for name, data in files.items():
                identity = data.get("cliSessionId") or ""
                if identity:
                    groups.setdefault(identity, []).append((path, name, data))
        waiting: set[str] = set()
        for _failed, known in (unread or {}).items():
            # Never guessed from a same-named file elsewhere: names are not
            # unique across accounts (review round 6).
            if known:
                waiting.add(known)
            else:
                blind = True                    # whose copy it is, nobody can say
        if blind:
            waiting = set(groups)
        elif retry is not None:
            retry.update(waiting.intersection(groups))
        # Unseen sessions keep their base unless every copy was read.
        fresh: dict[str, dict] = {} if complete else dict(base_all)
        dirty: set[tuple[Path, str]] = set()
        originals: dict[tuple[Path, str], dict] = {}
        owners: dict[tuple[Path, str], str] = {}
        lag_ms = options.activity_lag_s * 1000.0 if options.activity_lag_s > 0 else 0.0
        horizon_ms = (self.now().timestamp() + ACTIVITY_FUTURE_S) * 1000.0
        #: `(how far its stalest copy trails, session, [(folder, name, date)])`.
        lagging: list[tuple[Any, str, list[tuple[Path, str, Any]]]] = []

        def writable(path: Path, name: str) -> dict:
            # Cached/interned snapshots are shared across accounts and passes.
            # Copy only the entry being changed. Nested settings are copied
            # separately below.
            if (path, name) not in dirty:
                originals[(path, name)] = folder_files[path][name]
                folder_files[path][name] = dict(folder_files[path][name])
                dirty.add((path, name))
            return folder_files[path][name]

        def title_of(data: dict) -> str:
            return data.get("title") or ""

        def source_of(data: dict) -> str:
            return data.get("titleSource") or "auto"

        def active_of(data: dict) -> Any:
            # `max` compares these, so they are ranked as `_rank` ranks.
            return _rank(data, ("lastActivityAt", "createdAt"))

        for identity, copies in groups.items():
            self._checkpoint(current)
            if identity in waiting:
                current.flags_held += 1
                if identity in base_all:
                    fresh[identity] = base_all[identity]
                continue
            for path, name, _data in copies:
                owners[(path, name)] = identity
            base = base_all.get(identity) or {}
            base = dict(base)
            for flag, bootstrap in (("isArchived", True), ("isStarred", True)):
                values = {bool(data.get(flag)) for _p, _n, data in copies}
                if len(values) == 1:
                    resolved = values.pop()
                else:
                    recorded = base.get(flag)
                    resolved = (not recorded) if isinstance(recorded, bool) else bootstrap
                if any(bool(data.get(flag)) != resolved for _p, _n, data in copies):
                    for path, name, data in copies:
                        if bool(data.get(flag)) != resolved:
                            writable(path, name)[flag] = resolved
                    current.flag_synced += 1
                base[flag] = resolved

            if options.ultracode_default:
                # The app's spawn path never passes sessionSettings, so a spawned
                # session silently misses the ultracode ruling. An entry whose
                # settings lack the KEY gets `true`; an explicit value — including
                # a deliberate false — is respected and synced nowhere.
                for path, name, data in copies:
                    settings = data.get("sessionSettings")
                    if not isinstance(settings, dict):
                        writable(path, name)["sessionSettings"] = {"ultracode": True}
                    elif "ultracode" not in settings:
                        writable(path, name)["sessionSettings"] = {**settings, "ultracode": True}

            title: str | None = None
            voices = copies
            variants = {(title_of(data), source_of(data)) for _p, _n, data in voices}
            decided: tuple[str, str] | None = None
            if len(variants) > 1:
                recorded = base_all.get(identity, {}).get("title")
                candidates = [data for _p, _n, data in voices
                              if recorded is None or title_of(data) != recorded]
                if candidates:
                    manual = [data for data in candidates if source_of(data) == "manual"]
                    winner = max(manual or candidates, key=active_of)
                    decided = (title_of(winner), source_of(winner))
            elif variants:
                decided = next(iter(variants))
            if decided is not None:
                title = decided[0]
                if any((title_of(data), source_of(data)) != decided for _p, _n, data in copies):
                    for path, name, data in copies:
                        if (title_of(data), source_of(data)) != decided:
                            target = writable(path, name)
                            target["title"], target["titleSource"] = decided
                    current.retitled += 1

            # A title resolution above may have replaced shared snapshots with
            # writable copies. The transcript anchor must compare those new
            # values, as it did before entries were interned.
            copies = [(path, name, folder_files[path][name]) for path, name, _data in copies]

            # The transcript anchor: append-only and account-agnostic, so it
            # survives an index write the app skipped. Only a CHANGE since the
            # last sync propagates; a bootstrap merely records the base.
            transcript = stems.get(identity)
            recorded_title = base_all.get(identity, {}).get("ttitle")
            anchor, stamp = recorded_title, base_all.get(identity, {}).get("tmt")
            if transcript is not None:
                try:
                    mtime = transcript.stat().st_mtime
                except OSError:
                    mtime = None
                if mtime is not None and mtime != stamp:
                    stamp = mtime
                    read = self.transcript_title(transcript)
                    if read is not None:
                        anchor = read
            if (anchor is not None and recorded_title is not None
                    and anchor != recorded_title and anchor != (title or "")):
                winner = max((data for _p, _n, data in copies), key=active_of)
                source = source_of(winner)
                for path, name, data in copies:
                    if (title_of(data), source_of(data)) != (anchor, source):
                        target = writable(path, name)
                        target["title"], target["titleSource"] = anchor, source
                title = anchor
                current.transcript_retitled += 1

            # The date each row shows. A session archived everywhere is in no
            # sidebar list, so its copies' dates are left as they are.
            if lag_ms and not base["isArchived"]:
                # A date from the future is no voice (`ACTIVITY_FUTURE_S`), nor
                # is zero or less, which the app takes for no date.
                dates = [value if _instant_ms(value) and 0 < value <= horizon_ms else None
                         for value in (data.get(ACTIVITY_FIELD) for _p, _n, data in copies)]
                raises = activity_targets(dates, lag_ms)
                if raises:
                    newest = max(value for value in dates if _instant_ms(value))
                    lagging.append((newest - min(dates[index] for index in raises), identity,
                                    [(copies[index][0], copies[index][1], value)
                                     for index, value in raises.items()]))

            record = {"isArchived": base["isArchived"], "isStarred": base["isStarred"]}
            if title is not None:
                record["title"] = title
            if anchor is not None:
                record["ttitle"] = anchor
            if stamp is not None:
                record["tmt"] = stamp
            fresh[identity] = record

        # The dates join the same publish, a bounded number of sessions a pass
        # (`ACTIVITY_SESSIONS_PER_PASS`): first those chosen and not published
        # the fewest times, and among them the furthest behind. A session that
        # needs no raise any more, or is gone, leaves the ledger.
        tries = self._activity_tries
        behind = {identity for _behind, identity, _targets in lagging}
        for identity in [key for key in tries if key not in behind
                         and (key in groups or complete)]:
            del tries[identity]
        lagging.sort(key=lambda item: (tries.get(item[1], 0), -item[0], item[1]))
        chosen: set[str] = set()
        for _behind, identity, targets in lagging[:ACTIVITY_SESSIONS_PER_PASS]:
            for path, name, value in targets:
                writable(path, name)[ACTIVITY_FIELD] = value
            chosen.add(identity)
            current.activity_synced += 1
        current.activity_waiting += len(lagging[ACTIVITY_SESSIONS_PER_PASS:])

        if not options.dry_run:
            # Once writes start, finish the matching merge base. Cancellation
            # inside this batch could mistake our partial writes for user edits.
            self._checkpoint(current, "publishing flags")
            held: set[str] = set()
            batches: dict[str, list[tuple[Path, str]]] = {}
            for path, name in sorted(dirty, key=lambda item: (str(item[0]), item[1])):
                batches.setdefault(owners.get((path, name), ""), []).append((path, name))
            for identity, batch in batches.items():
                # Check every copy first: a session is written whole or not at all.
                ready: list[tuple[Path, dict, dict, dict, tuple[int, ...]]] = []
                for path, name in batch:
                    resolved = folder_files[path].get(name)
                    original = originals.get((path, name))
                    if resolved is None or original is None:
                        continue
                    target = path / name
                    self._invalidate_folder(path)
                    try:
                        expect = _signature_of(target)
                        body = _load(target, strict=True)
                        if (_signature_of(target) != expect
                                or any(body.get(key) != original.get(key) for key in FLAG_FIELDS)):
                            break                   # moved under us; decide next pass
                    except (OSError, ValueError):
                        break
                    before = dict(body)
                    for key in FLAG_WRITES:
                        if key in resolved:
                            body[key] = resolved[key]
                    raised = resolved.get(ACTIVITY_FIELD)
                    if raised != original.get(ACTIVITY_FIELD):
                        # Never lowered: the app may have saved a later date
                        # here since the pass read this copy, and that date is
                        # not among the fields whose change holds the session.
                        # And never over what is no date now, as at the decision.
                        held_now = body.get(ACTIVITY_FIELD)
                        if _instant_ms(held_now) and 0 < held_now < raised:
                            body[ACTIVITY_FIELD] = raised
                    if body == before:
                        continue                # the app's own save already carries it
                    flagged = any(body.get(key) != before.get(key) for key in FLAG_WRITES)
                    ready.append((target, body, before, flagged, expect))
                else:
                    written: list[tuple[Path, dict, int, tuple[int, ...], tuple[int, ...],
                                        bool]] = []
                    for target, body, before, flagged, expect in ready:
                        try:
                            self._forget(target)
                            inode = _write_json(target, body, keep_mtime=True, expect=expect,
                                                sync=True)
                        except (OSError, ValueError):
                            inode = None
                        if inode is None:
                            # The app saved this copy in the instant after the
                            # check. Put back the copies already written, so the
                            # held merge base matches every file again (a copy
                            # changed since the mirror's write is left alone).
                            for (done, old, done_inode, done_expect, was,
                                 done_flagged) in reversed(written):
                                try:
                                    self._forget(done)
                                    back = _write_json(done, old, keep_mtime=True,
                                                       expect=done_expect, sync=True)
                                except (OSError, ValueError):
                                    # The mirror's write stands: journal it.
                                    self._journal_write(done, done_inode, identity,
                                                        folder_files[done.parent][done.name],
                                                        "updated", flags=done_flagged)
                                    continue
                                if back is not None:
                                    self._journal_restamp(done, was, back)
                            held.add(identity)
                            break
                        try:
                            now_signature = _signature_of(target)
                        except OSError:
                            now_signature = None
                            # This successful write cannot enter the rollback
                            # list without its signature. It may survive a later
                            # failed copy, even though no journal call sees it.
                            if flagged:
                                self._flags_moved = True
                        if now_signature is not None and now_signature[1] == inode:
                            written.append((target, before, inode, now_signature, expect,
                                            flagged))
                    else:
                        for target, _before, inode, _signature, _was, was_flagged in written:
                            self._journal_write(target, inode, identity,
                                                folder_files[target.parent][target.name],
                                                "updated", flags=was_flagged)
                    continue
                held.add(identity)
            for identity in held:
                current.flags_held += 1
                self._why.setdefault(f"session {identity}", "a copy changed while the pass published")
                if identity in base_all:
                    fresh[identity] = base_all[identity]
                else:
                    fresh.pop(identity, None)
            if retry is not None:
                retry.update(held)
            for identity in chosen:
                if identity in held:
                    tries[identity] = tries.get(identity, 0) + 1
                else:
                    tries.pop(identity, None)
            if fresh != base_all:
                self.dir.mkdir(parents=True, mode=0o700, exist_ok=True)
                # Synced like the records it describes: a base lost to a crash
                # would hand every divergent session to the bootstrap rule.
                published = _write_json(self.flags_path, fresh, sync=True)
                def decision(row):
                    return (None if row is None else
                            (row.get("isArchived"), row.get("isStarred"), row.get("title")))
                if published is not None and any(
                    decision(fresh.get(identity)) != decision(base_all.get(identity))
                    for identity in fresh.keys() | base_all.keys()
                ):
                    self._flags_moved = True
        return dirty

    # --- one pass ------------------------------------------------------------

    def run_once(self, options: Options | None = None) -> Pass:
        """One full mirroring pass, recorded in the sidecar as it goes (C-23.28).

        The lock is taken FIRST and the sidecar is written second, and the order
        matters more than it looks. The sidecar holds one pass, and a 60 s timer
        over a pass that is still running fires a second one constantly. If the
        loser wrote the sidecar, it would overwrite the running pass's
        `started_at` with its own and then stamp it finished — so a pass hung for
        an hour would read `healthy`, which is exactly the reading C-23.28
        exists to prevent. The loser now touches nothing.

        Within the lock, the sidecar is written BEFORE the work starts: a pass
        that hangs is then visible as in flight rather than as silence, and
        `health` tolerates it for thirty minutes instead of guessing from a
        process listing.
        """
        options = options or Options()
        current = Pass(started_at=_iso(self.now()), dry_run=options.dry_run)
        lock = None
        kept = None
        try:
            lock = self._lock()
            if lock is None:
                current.state = "ok"
                current.finished_at = current.started_at
                current.error = "another pass holds the lock"
                return current           # deliberately without touching the sidecar
            if options.dry_run:
                kept = self._borrow()
            self.journal.refresh()
            self._splits = None                 # a pass's that never recorded
            self._record(current)
            self._pass_payloads = {}
            self._progress_due = time.monotonic() + PROGRESS_INTERVAL_S
            interval = float(self.policy.get("sessions", {}).get("mirror_hot_interval_s", 2))
            self._hot_services = 0
            self._hot_epoch = 0
            if self._inventoried and interval > 0 and options.flag_sync:
                self._hot_worker = self._fork_hot()
                self._hot_options = options
                self._hot_due = time.monotonic() + interval
            self._pass(current, options)
            self._checkpoint(current, "complete")
            current.state = "ok"
            current.finished_at = _iso(self.now())
            self._record(current, last_ok=current.finished_at, load_gap=self._settle(options))
        except (_Cancelled, OSError) as exc:
            current.state = "cancelled" if isinstance(exc, _Cancelled) else "error"
            current.error = f"{type(exc).__name__}: {exc}"
            current.finished_at = _iso(self.now())
            self._record(current, load_gap=self._settle(options))
        finally:
            if lock is not None:
                # Only the pass that holds the lock owns the per-pass payloads.
                self._pass_payloads = {}
                self._hot_worker = None
                self._hot_options = None
                if kept is not None:
                    self._give_back(kept)
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                finally:
                    lock.close()
        return current

    def run_hot(self, options: Options | None = None) -> Pass:
        """Spread new sessions within seconds of their first write (C-23.28).

        The app reads a folder only when it loads it, so a session must be in
        every folder before the next account switch. This pass lists only the
        folders whose directory changed, puts each new or changed record whose
        transcript exists into every folder that lacks the session, and fills
        stale empty copies, and syncs changed sessions' flags, titles and
        settings. Revival and pruning stay with the full pass. The first pass in a process is a full
        one, because spreading needs to know what every folder already holds.
        It shares the full pass's lock and never touches the full pass's
        record in the sidecar, so C-23.28's heartbeat still means a full pass.
        """
        options = options or Options()
        if not self._inventoried:
            return self.run_once(options)
        current = Pass(started_at=_iso(self.now()), dry_run=options.dry_run, kind="hot")
        lock = None
        kept = None
        try:
            lock = self._lock()
            if lock is None:
                current.state = "ok"
                current.finished_at = current.started_at
                current.error = "another pass holds the lock"
                return current
            if options.dry_run:
                kept = self._borrow()
            return self._run_hot_locked(options)
        finally:
            if lock is not None:
                if kept is not None:
                    self._give_back(kept)
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                finally:
                    lock.close()
        return current

    def _run_hot_locked(self, options: Options, *, spread: bool = True) -> Pass:
        """A hot pass owned by the caller's flock, including its own sidecar.

        Called by run_hot, or synchronously by the full lock holder. It never
        acquires or releases a lock and never marks a full pass successful.
        """
        current = Pass(started_at=_iso(self.now()), dry_run=options.dry_run, kind="hot")
        self._flags_moved = False
        try:
            self.journal.refresh()
            self._pass_payloads = {}
            # Sample starts so a slow hot read can be visible without rewriting
            # the sidecar twice per idle tick. A recorded start always finishes.
            self._record_hot(current, None)
            self._hot(current, options, spread=spread)
            current.stage = "complete"
            current.state = "ok"
        except (_Cancelled, OSError) as exc:
            current.state = "cancelled" if isinstance(exc, _Cancelled) else "error"
            current.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._pass_payloads = {}
            current.finished_at = _iso(self.now())
            try:
                self._record_hot(current, self._settle(options))
            except OSError:
                pass
        return current

    def _borrow(self) -> dict[str, Any]:
        """Before a dry run: put copies in place of everything a pass changes
        on this instance, and return the originals for `_give_back`.

        A dry run decides as a real pass does, so it reads the store into the
        inventory, which makes each change it read no longer new to the next
        hot pass, and it clears the retries it decided on. But it published
        nothing. On an instance that passes again, the next real hot pass then
        found no candidates (second review of #167), and a new instance's
        first hot pass was no longer the full one. With the originals put
        back, the instance is as the dry run found it: its retries, its
        inventory and whether it has one, and the ledger of failed raises.

        Called and undone inside the pass's lock. One level of copies is
        enough: a pass replaces the values these hold (a folder's listing, a
        cached entry) and changes none in place, except a payload's reference
        count, so the payloads are copied too. The journal is shared: a dry
        run adds no row to it and only reads its file again.
        """
        kept = dict(vars(self))
        working = {name: type(value)(value) if type(value) in (dict, set, list) else value
                   for name, value in kept.items()}
        working["_payloads"] = {key: _Payload(row.value, row.size, row.refs)
                                for key, row in kept["_payloads"].items()}
        self.__dict__ = working             # in one step, as `_give_back` undoes it
        return kept

    def _give_back(self, kept: dict[str, Any]) -> None:
        """After a dry run: the instance holds exactly what `_borrow` took."""
        self.__dict__ = kept

    def _lock(self):
        path = self.dir / LOCK_NAME
        stream = None
        try:
            self.dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            # Never waiting in open(): a FIFO at the lock's name had held the pass,
            # and the timers' worker that `Timers.stop()` waits for.
            stream = open(transcripts.lock_fd(path), "a")
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if stream is not None:
                stream.close()
            return None
        return stream

    def _pass(self, current: Pass, options: Options) -> None:
        self._checkpoint(current, "finding accounts")
        folders = self.folders(options.exclude)
        kept, unknown = self._listing_gaps(options)
        failed = set(self._unlisted_accounts)
        # A failed account's known folders are listed directly, like any other;
        # only one that does not list either is read by name or holds.
        folders = folders + kept
        current.accounts = len(folders)
        if not folders and not kept and not failed:
            self._drop_folders(folders)
            self._inventoried = True            # an empty store is a complete inventory
            self._splits = self._split_report({}, set(), {})
            return
        sweep = self._sweep_due()
        current.swept = sweep
        # A sweep reads everything again, and a pass that writes removes the
        # mirror's stale temporaries on the way. A dry run reads as that pass
        # would and removes none: a deletion is a write (C-17.4).
        tidy = sweep and not options.dry_run
        if tidy:                                # the mirror's own state files' leftovers too
            try:
                with os.scandir(self.dir) as listing:
                    _remove_leftovers([item.path for item in listing if item.name.endswith(TEMPORARY_SUFFIX)])
            except OSError:
                pass
        self._checkpoint(current, "finding transcripts")
        stems = self.transcript_stems(current, sweep=sweep, tidy=tidy)

        folder_files: dict[Path, dict[str, dict]] = {}
        folder_ids: dict[Path, set[str]] = {}
        fresh_ids: set[str] = set()
        unlisted: list[Path] = []
        self._unread, self._why = {}, {}
        self._checkpoint(current, "reading entries")
        for _account, _org, path in folders:
            self._checkpoint(current)
            try:
                files, fresh = self._scan(path, current, sweep=sweep, tidy=tidy)
            except _Unlisted as exc:
                if exc.transient:
                    unlisted.append(path)   # neither a source nor a target of copies
                continue
            folder_files[path] = files if files is not None else self._files(path)
            folder_ids[path] = set(self._folders[path].ids)
            for name in fresh:
                identity = folder_files[path][name].get("cliSessionId") or ""
                if identity and self._recent(path / name):
                    fresh_ids.add(identity)

        # Remove deleted files and excluded folders only after a full inventory.
        # A cancelled scan must not evict entries it simply did not reach yet,
        # nor this pass forget folders it could not look into.
        self._drop_folders(folders)
        folders = [item for item in folders if item[2] in folder_files]
        self._inventoried = True
        by_name: dict[str, dict[Path, dict]] = {}
        if options.prune:
            for path, files in folder_files.items():
                for name, data in files.items():
                    by_name.setdefault(name, {})[path] = data

        if options.restore and options.archive:
            self._checkpoint(current, "restoring transcripts")
            current.revived = self.restore_dead(folder_files, stems, options.archive,
                                                options.dry_run, current)

        def resolvable(data: dict) -> bool:
            identity = data.get("cliSessionId") or ""
            return bool(identity) and identity in stems

        canonical: dict[str, tuple[Any, dict, str, Path]] = {}
        #: `<record name> -> {conversation: the folders that hold it under that name}`.
        bound: dict[str, dict[str, int]] = {}
        shown: set[str] = set()                 # conversations with an unarchived copy
        for path, files in folder_files.items():
            for name, data in files.items():
                self._checkpoint(current)
                if not resolvable(data):
                    continue
                identity = data["cliSessionId"]
                holders = bound.setdefault(name, {})
                holders[identity] = holders.get(identity, 0) + 1
                if not data.get("isArchived"):
                    shown.add(identity)
                score = _rank(data)
                if identity not in canonical or score > canonical[identity][0]:
                    canonical[identity] = (score, data, name, path / name)
        current.sessions = len(canonical)
        if self._stems_whole and not (unlisted or unknown or failed or self._unread):
            # Only from every folder, every copy and every project directory: a
            # folder that did not list this pass may hold the other half of a
            # split, and a transcript the pass could not see makes its
            # conversation look dead. A report without them would read as
            # clean. The last whole report stands until then.
            self._splits = self._split_report(bound, shown, canonical)
        # A new record this pass read before its transcript existed is no
        # longer fresh to the hot pass, so hand it over to the hot pass's retry.
        # Only records written within the retry window: to a cold pass every
        # record is fresh, and the 126 long-dead sessions of 2026-09-24 are not
        # waiting for a transcript.
        instant = time.monotonic()
        for identity in fresh_ids:
            if identity in canonical:
                self._retry.pop(identity, None)
            else:
                self._retry.setdefault(identity, instant)

        def note(path: Path, name: str, data: dict, identity: str) -> None:
            folder_files[path][name] = data
            folder_ids[path].add(identity)

        self._checkpoint(current, "copying entries")
        for identity, (_score, data, name, source) in canonical.items():
            self._spread(identity, data, name, source, folders,
                         has=lambda path, key: key in folder_ids[path],
                         existing=lambda path, key: folder_files[path].get(key),
                         note=note, options=options, current=current)

        if options.flag_sync:
            self._checkpoint(current, "resolving flags")
            flag_files = folder_files
            if self._hot_services:
                for _attempt in range(2):
                    # An inline hot pass may have advanced the base while this
                    # pass retained old projections. Decide again from a fresh
                    # inventory. Its scan still services hot checkpoints; if
                    # one changed a decision, refresh again before deciding.
                    epoch = self._hot_epoch
                    flag_files, unlisted, unknown, failed = self._flag_inventory(current, options)
                    if epoch == self._hot_epoch:
                        break
                else:
                    # Continuous edits cannot force an endless full refresh.
                    # Hot sync remains active; this full decision is held and
                    # retried next pass without touching any merge base.
                    identities = {data.get("cliSessionId") for files in flag_files.values()
                                  for data in files.values() if data.get("cliSessionId")}
                    current.flags_held += len(identities)
                    current.held_by = [{"path": str(store_dir()),
                                        "reason": "hot sync advanced during flag inventory; retry next pass"}]
                    flag_files = None
            if flag_files is not None:
                self._flags_active = True
                try:
                    self._sync_inventory_flags(flag_files, stems, options, current,
                                               unlisted, unknown, failed)
                finally:
                    self._flags_active = False

        if options.prune:
            self._checkpoint(current, "pruning entries")
            # Off by default: the Claude app prunes dead copies itself on load.
            home = next((path for _a, org, path in folders if org == options.dead_home), None)
            for name, copies in by_name.items():
                self._checkpoint(current)
                if any(resolvable(data) for data in copies.values()):
                    continue                                # openable somewhere
                keep = home if home in copies else sorted(copies)[0]
                for path in list(copies):
                    if path == keep:
                        continue
                    if not options.dry_run:
                        self._invalidate_folder(path)
                        try:
                            (path / name).unlink()
                            self._forget(path / name)
                        except OSError:
                            pass
                    current.pruned += 1

        self._stems = stems
        if sweep:
            self._last_sweep = time.monotonic()

    def _split_report(self, bound: dict[str, dict[str, int]], shown: set[str],
                      canonical: dict[str, tuple[Any, dict, str, Path]]) -> dict[str, Any]:
        """The session ids that open different conversations under different logins.

        A record's name is the app's id for a session. When the records of one
        name hold one conversation in some folders and another in the rest,
        and both transcripts exist, `_spread` gives each a row in every
        folder, the second under a `local_<conversation>.json` name, with the
        same title. `live` counts the ids of which two or more conversations
        still have an unarchived copy: two rows a person can confuse. Counted
        from this pass's inventory, before its flag sync, only from a whole
        one, and recorded once, by this pass (`_pass`, `_record`). How a name
        comes to hold two conversations is not this report's to say; it lists
        what the store holds.
        """
        rows = []
        for name, holders in bound.items():
            if len(holders) < 2:
                continue
            conversations = []
            for identity, folders in holders.items():
                data = canonical[identity][1]
                date = data.get(ACTIVITY_FIELD)
                conversations.append({
                    "id": identity, "folders": folders,
                    "title": str(data.get("title") or ""),
                    "archived": identity not in shown,
                    "last_activity": (_iso(datetime.fromtimestamp(date / 1000, timezone.utc))
                                      if _instant_ms(date) and 0 <= date < 1e14 else None)})
            conversations.sort(key=lambda item: (item["last_activity"] or "", item["id"]),
                               reverse=True)
            rows.append({"name": name, "conversations": conversations,
                         "live": sum(1 for item in conversations if not item["archived"]) > 1})
        # Live ones first, each group newest first; the rest fill what room is left.
        rows.sort(key=lambda row: (row["conversations"][0]["last_activity"] or "", row["name"]),
                  reverse=True)
        rows.sort(key=lambda row: not row["live"])
        return {"count": len(rows), "live": sum(1 for row in rows if row["live"]),
                "checked_at": _iso(self.now()), "sessions": rows[:SPLIT_REPORT_LIMIT]}

    def splits(self) -> dict[str, Any]:
        """The last full pass's split-id report, from the sidecar; read-only."""
        value = self.sidecar().get("splits")
        if not isinstance(value, dict):
            return {"count": 0, "live": 0, "checked_at": None, "sessions": []}
        return value

    def _flag_inventory(self, current: Pass, options: Options):
        """Refresh a full flag snapshot after embedded hot writes."""
        folders = self.folders(options.exclude)
        kept, unknown = self._listing_gaps(options)
        failed = set(self._unlisted_accounts)
        folders += kept
        files, unlisted = {}, []
        self._unread, self._why = {}, {}
        for _account, _org, path in folders:
            self._checkpoint(current)
            try:
                found, _fresh = self._scan(path, current, sweep=False)
            except _Unlisted as exc:
                if exc.transient:
                    unlisted.append(path)
                continue
            files[path] = found if found is not None else self._files(path)
        return files, unlisted, unknown, failed

    def _sync_inventory_flags(self, folder_files, stems, options, current,
                              unlisted, unknown, failed, *, candidates=None) -> None:
        """The same unknown-copy rules for full and incremental flag sync.

        A failed listing is usable by name only while its directory signature
        still matches. Unknown identities hold every candidate; unseen bases
        survive every partial inventory, including every hot pass.
        """
        flag_files = dict(folder_files)
        blind = bool(unknown)
        for where, reason in unknown:
            self._why[where] = reason
        for path in unlisted:
            state = self._folders.get(path)
            now_signature = _directory_signature(path)
            if state is None or now_signature is None or now_signature != state.signature:
                blind = True
                self._why[os.fspath(path)] = ("folder not listed, never listed" if state is None
                                              else "folder not listed, changed since its listing")
                continue
            owner = {name: identity for identity, names in state.ids.items() for name in names}
            files = {}
            for name in sorted(state.names):
                files[name] = self._entry(path / name)
                key = os.path.join(path, name)
                if key in self._unread and not self._unread[key]:
                    self._unread[key] = owner.get(name, "")
            flag_files[path] = files
        if candidates is not None:
            flag_files = {path: {name: data for name, data in files.items()
                                 if data.get("cliSessionId") in candidates}
                          for path, files in flag_files.items()}
        identities = {data["cliSessionId"] for files in flag_files.values()
                      for data in files.values() if data.get("cliSessionId")}
        retry: set[str] = set()
        self.sync_flags(flag_files, stems, options, current, unread=dict(self._unread),
                        blind=blind, complete=candidates is None and not unlisted
                        and not failed and not self._unread, retry=retry)
        self._update_flag_retry(identities if candidates is None else candidates, retry)
        if current.flags_held:
            current.held_by = [{"path": where, "reason": reason}
                               for where, reason in list(self._why.items())[:HELD_BY_LIMIT]]

    def _update_flag_retry(self, considered: set[str], held: set[str]) -> None:
        """Keep both serial inventories' retries limited to identified holds."""
        for inventory in (self, self._hot_worker, self._hot_parent):
            if inventory is not None:
                inventory._flag_retry.difference_update(considered)
                inventory._flag_retry.update(held)

    def _hot(self, current: Pass, options: Options, *, spread: bool = True) -> None:
        self._checkpoint(current, "reading entries")
        self._unread, self._why = {}, {}
        folders = self.folders(options.exclude)
        kept, unknown = self._listing_gaps(options)
        folders = folders + kept
        failed = set(self._unlisted_accounts)
        current.accounts = len(folders)
        fresh: list[tuple[Path, str]] = []
        unlisted: set[Path] = set()
        unknown_folders: list[Path] = []
        for _account, _org, path in folders:
            self._checkpoint(current)
            try:
                _files, names = self._scan(path, current, sweep=False)
            except _Unlisted as exc:
                unlisted.add(path)
                if exc.transient:
                    unknown_folders.append(path)
                continue
            fresh.extend((path, name) for name in names)
        self._drop_folders(folders)             # never forget what it could not list
        folders = [item for item in folders if item[2] not in unlisted]

        instant = time.monotonic()
        identities: dict[str, float] = {}
        for path, name in fresh:
            identity = self._file(path, name).get("cliSessionId") or ""
            if identity:
                identities.setdefault(identity, instant)
        # Deleted sessions must not live forever in either hot retry queue.
        gone = {identity for identity in self._flag_retry | self._retry.keys()
                if not any(identity in state.ids for state in self._folders.values())}
        self._update_flag_retry(gone, set())
        for identity in gone:
            self._retry.pop(identity, None)
        for identity, since in list(self._retry.items()):
            if instant - since > UNRESOLVED_RETRY_S:
                del self._retry[identity]
            else:
                identities.setdefault(identity, since)
        if options.flag_sync:
            for identity in self._flag_retry:
                identities.setdefault(identity, instant)
        current.sessions = len(identities)
        if not identities:
            return

        written: dict[Path, dict[str, dict]] = {}
        written_ids: dict[Path, set[str]] = {}

        def has(path: Path, identity: str) -> bool:
            state = self._folders.get(path)
            return ((state is not None and identity in state.ids)
                    or identity in written_ids.get(path, ()))

        def existing(path: Path, name: str) -> dict | None:
            if name in written.get(path, {}):
                return written[path][name]
            state = self._folders.get(path)
            if state is None or name not in state.names:
                return None
            return self._file(path, name)

        def note(path: Path, name: str, data: dict, identity: str) -> None:
            written.setdefault(path, {})[name] = data
            written_ids.setdefault(path, set()).add(identity)

        self._checkpoint(current, "copying entries" if spread else "resolving flags")
        for identity, since in (identities.items() if spread else ()):
            # Openability is a property of the session, not of a copy: any copy
            # may know the cwd its transcript lives under.
            best: tuple[Any, dict, str, Path] | None = None
            openable = False
            for _account, _org, path in folders:
                state = self._folders.get(path)
                for name in (state.ids.get(identity, ()) if state is not None else ()):
                    data = self._file(path, name)
                    openable = openable or self._openable(data)
                    score = _rank(data)
                    if best is None or score > best[0]:
                        best = (score, data, name, path / name)
            if best is None or not openable:
                self._retry[identity] = since   # the transcript may follow the record
                continue
            self._retry.pop(identity, None)
            _score, data, name, source = best
            self._spread(identity, data, name, source, folders, has=has,
                         existing=existing, note=note, options=options, current=current)

        if options.flag_sync:
            flag_files = {}
            for _account, _org, path in folders:
                state = self._folders.get(path)
                names = (name for identity in identities for name in
                         (state.ids.get(identity, ()) if state is not None else ()))
                flag_files[path] = {name: self._file(path, name) for name in names}
                # Include copies just placed by this hot pass as well.
                flag_files[path].update(written.get(path, {}))
            self._checkpoint(current, "resolving flags")
            self._sync_inventory_flags(flag_files, self._stems, options, current,
                                       unknown_folders, unknown, failed,
                                       candidates=set(identities))

    # --- the load gap --------------------------------------------------------

    def _desktop_state(self) -> AppState:
        if self._desktop is None:
            self._desktop = DesktopLog()
        return self._desktop.poll()

    def _settle(self, options: Options) -> dict[str, Any] | None:
        """Save the journal, pruned to what can still matter, and measure the gap."""
        if options.dry_run:
            return None
        try:
            state = self._desktop_state()
            load = state.load
            horizon = self.now().timestamp() - JOURNAL_WINDOW_S
            since = load.fresh_started_at.timestamp() if load is not None else None

            unsure = self.now().timestamp() - JOURNAL_UNSURE_S

            def keep(row: _Write) -> bool:
                if row.at >= horizon:
                    return True
                if load is None:
                    # This process cannot see which folder is loaded (no log,
                    # or no load in it); another may, so prune only by age.
                    return row.at >= unsure
                return row.folder == load.folder and since is not None and row.at >= since

            self.journal.save(keep)
            return self.load_gap(state)
        except OSError:
            return None

    def load_gap(self, state: AppState | None = None) -> dict[str, Any]:
        """The sessions the running app cannot list, because they arrived after its load.

        `pending` counts the mirror's copies into the folder the app last loaded
        that postdate that load and that the running app cannot list:

        * a copy that filled a new name, until the app rewrites the file (it
          writes only records it holds) or loads the folder again;
        * a copy that replaced a stale empty record the app already held, until
          a load that starts from an empty list. The app holds the empty record,
          so its re-save puts the empty id back; such a file still counts, and
          the next full pass repairs it again.

        `stale` counts flag, title and setting writes into that folder since its
        last fresh load that the app has not rewritten, which the running app
        does not see either (writes into a folder where an earlier account's
        session still runs are not counted). A copy made in the same second as
        the load is counted, because the log cannot order the two.
        """
        if state is None:
            state = self._desktop_state()
        log = str(self._desktop.path) if self._desktop is not None else "the app's log"
        load = state.load
        base = {"status": "unknown", "account": None, "org": None, "loaded_at": None,
                "logged_out_at": None, "pending": 0, "archived": 0, "stale": 0,
                "sessions": [], "log": log}
        if load is None:
            base["detail"] = (state.error or
                              f"{log} records no session-folder load, so the mirror "
                              "cannot tell what the running app lists")
            return base
        folder = store_dir() / load.account / load.org
        started = load.started_at.timestamp()
        fresh_started = load.fresh_started_at.timestamp()
        rows = sorted((row for row in self.journal.rows() if row.folder == load.folder),
                      key=lambda row: row.at)
        latest: dict[str, _Write] = {}
        copied: dict[str, _Write] = {}
        for row in rows:
            latest[row.name] = row
            threshold = started if row.kind == "added" else fresh_started
            if row.kind in ("added", "repaired") and row.at >= threshold:
                copied[row.name] = row
        pending, archived, stale = [], 0, 0
        reads: dict[tuple[str, int, int], dict] = {}
        for name, row in latest.items():
            try:
                info = os.stat(folder / name)
            except OSError:
                continue                     # gone: nothing left to list
            rewritten = info.st_ctime_ns != row.ctime_ns
            if name in copied:
                # Read around the inventory's cache (a report must not change
                # what the next hot pass finds new), and only when the file
                # changed since the report last read it.
                key = (os.fspath(folder / name), info.st_ino, info.st_ctime_ns)
                now_holds = self._gap_reads.get(key)
                if now_holds is None:
                    now_holds = _project(_load(folder / name))
                reads[key] = now_holds
                if rewritten and (copied[name].kind == "added" or now_holds.get("cliSessionId")):
                    continue                 # the app rewrote it, so it holds it
                if now_holds.get("isArchived"):
                    archived += 1
                else:
                    pending.append(copied[name])
            elif row.kind == "updated" and row.at >= fresh_started and not rewritten:
                stale += 1
        self._gap_reads = reads                 # only what this report looked at
        pending.sort(key=lambda row: row.at, reverse=True)
        short = _short(load.account, load.org)
        base.update(account=load.account, org=load.org,
                    loaded_at=_iso(load.started_at), pending=len(pending),
                    archived=archived, stale=stale,
                    sessions=[{"name": row.name, "title": row.title, "kind": row.kind,
                               "copied_at": _iso(datetime.fromtimestamp(row.at, timezone.utc))}
                              for row in pending[:10]])
        when = _clock(load.started_at)
        if state.logged_out_at is not None:
            base["status"] = "logged-out"
            base["logged_out_at"] = _iso(state.logged_out_at)
            base["detail"] = (f"the app logged out at {_clock(state.logged_out_at)}; its "
                              "next login lists a session folder again")
            return base
        if pending:
            count = len(pending)
            base["status"] = "relaunch"
            base["detail"] = (f"{count} session{'s' if count != 1 else ''} copied into "
                              f"{short} after the app loaded it at {when}; the running "
                              "app lists a folder only when it loads it")
        else:
            base["status"] = "ok"
            base["detail"] = (f"the app loaded {short} at {when}"
                              + ("; nothing copied into it since is missing"
                                 if not load.missing else
                                 "; the folder did not exist then"))
        return base

    # --- health (C-23.28) ----------------------------------------------------

    def health(self, *, now: datetime | None = None) -> dict[str, Any]:
        """The full pass's heartbeat, with the hot pass's separate evidence.

        A hot pass cannot make an overdue full pass healthy. Its holds and
        failures must still be visible to `sessions mirror --status`, including
        while the full pass is running; that command renders this detail and
        includes the separate `hot` record in its JSON reply.
        """
        data = self.sidecar()
        result = self._full_health(data, now=now)
        hot = data.get("hot")
        if not isinstance(hot, dict):
            return result
        result["hot"] = hot
        details = []
        if hot.get("state") == "running" and hot.get("finished_at") is None:
            details.append(f"hot pass in flight since {hot.get('started_at') or 'unknown'}"
                           + (f" ({hot['stage']})" if hot.get("stage") else ""))
        elif hot.get("state") in ("error", "cancelled") or hot.get("error"):
            details.append(f"hot pass {hot.get('state') or 'failed'}: {hot.get('error') or 'unknown cause'}")
        held = int(hot.get("flags_held") or 0)
        if held:
            causes = hot.get("held_by") or []
            why = "; ".join(f"{item.get('path')}: {item.get('reason')}" for item in causes[:2])
            details.append(f"hot flags held for {held} session{'s' if held != 1 else ''}"
                           + (f" ({why})" if why else ""))
        if details:
            result["detail"] += "; " + "; ".join(details)
        return result

    def _full_health(self, data: dict[str, Any], *,
                     now: datetime | None = None) -> dict[str, Any]:
        """Mirror health, judged from the sidecar and nothing else (C-23.28).

        `absent` when no pass was ever recorded, `running` while a recorded pass
        is in flight and younger than `mirror_hang_min`, `healthy` when the last
        pass finished inside `mirror_stall_min`, `stalled` otherwise. Log
        recency is never consulted: `--quiet` keeps a no-op pass silent, and
        reading the log as a heartbeat produced a false "stalled" on 2026-08-19.
        """
        instant = now or self.now()
        settings = self.policy.get("sessions", {})
        stall = float(settings.get("mirror_stall_min", DEFAULT_STALL_MIN))
        hang = float(settings.get("mirror_hang_min", DEFAULT_HANG_MIN))
        record = data.get("pass") or {}
        if not record:
            return {"status": "absent", "sidecar": str(self.sidecar_path),
                    "age_min": None, "run_min": None, "detail":
                    "no pass recorded; the mirror has not run against this state root"}
        started = _instant(record.get("started_at"))
        finished = _instant(record.get("finished_at"))
        if record.get("state") == "running" and finished is None:
            run_min = ((instant - started).total_seconds() / 60) if started else None
            if run_min is not None and run_min <= hang:
                return {"status": "running", "sidecar": str(self.sidecar_path),
                        "age_min": None, "run_min": round(run_min, 1),
                        "detail": f"a pass has been in flight for {run_min:.1f} min"}
            return {"status": "stalled", "sidecar": str(self.sidecar_path),
                    "age_min": None,
                    "run_min": round(run_min, 1) if run_min is not None else None,
                    "detail": (f"run hung for {run_min:.1f} min" if run_min is not None
                               else "a pass is in flight with no recorded start")}
        reference = _instant(data.get("updated_at")) or finished
        age_min = ((instant - reference).total_seconds() / 60) if reference else None
        if record.get("state") in ("error", "cancelled"):
            return {"status": "stalled", "sidecar": str(self.sidecar_path),
                    "age_min": round(age_min, 1) if age_min is not None else None,
                    "run_min": None,
                    "detail": f"last pass failed: {record.get('error')}"}
        if age_min is not None and age_min <= stall:
            held = int(record.get("flags_held") or 0)
            causes = record.get("held_by") or []
            why = "; ".join(f"{item.get('path')}: {item.get('reason')}" for item in causes[:2])
            return {"status": "healthy", "sidecar": str(self.sidecar_path),
                    "age_min": round(age_min, 1), "run_min": None, "flags_held": held,
                    "held_by": causes,
                    "detail": f"last pass {age_min:.1f} min ago: "
                              f"{record.get('added', 0)} added, "
                              f"{record.get('repaired', 0)} repaired"
                              + (f"; flags held for {held} session{'s' if held != 1 else ''}"
                                 + (f" ({why})" if why else "") if held else "")}
        return {"status": "stalled", "sidecar": str(self.sidecar_path),
                "age_min": round(age_min, 1) if age_min is not None else None,
                "run_min": None,
                "detail": (f"sidecar idle {age_min:.1f} min, no pass in flight"
                           if age_min is not None else "sidecar records no pass time")}


def health(root: str | Path, policy: dict[str, Any] | None = None, *,
           now: datetime | None = None) -> dict[str, Any]:
    """`doctor`'s entry point (C-23.28)."""
    return Mirror(root, policy).health(now=now)


def load_gap(root: str | Path, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """What the running app cannot list yet; read-only (`doctor`, `--status`)."""
    return Mirror(root, policy).load_gap()


def splits(root: str | Path, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """The session ids that open two conversations; read-only (`doctor`, `--status`)."""
    return Mirror(root, policy).splits()


def run_once(root: str | Path, policy: dict[str, Any] | None = None,
             options: Options | None = None, *, now=None) -> Pass:
    return Mirror(root, policy, now=now).run_once(options)


__all__ = ["ACTIVITY_FIELD", "ACTIVITY_FUTURE_S", "ACTIVITY_SESSIONS_PER_PASS",
           "DEFAULT_ACTIVITY_LAG_S", "SAFE_MS",
           "DEFAULT_HANG_MIN", "DEFAULT_STALL_MIN", "Mirror", "Options", "Pass",
           "activity_targets", "health", "load_gap", "options_from", "run_once", "slug",
           "splits", "store_dir"]
