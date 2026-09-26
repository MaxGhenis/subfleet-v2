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
  saving. The hot pass shares the full pass's worker, so it waits behind a
  full pass in flight: 1-2 s warm, ~9 s for the ten-minute sweep, ~45 s for
  the first pass after a daemon restart. Title, flag and setting changes
  spread with the 60 s full pass, which also repairs, revives and prunes.
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
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

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
#: A hot pass rewrites its sidecar block at most this often unless it changed
#: something.
HOT_RECORD_S = 60.0

#: The settings a session keeps in every copy, unit by unit. Each unit's
#: fields always come from one copy, so a record never mixes two moves: the
#: app writes `cwd`, `worktreePath`, `worktreeName`, `branch`, `sourceBranch`
#: and `gitAnchors` together, and `effortInherited` decides whether `effort`
#: applies (bundle 2.9939.2, read 2026-09-26). The app runs a resumed session
#: on the record's `model` and `effort` and in its `worktreePath` or `cwd`.
#: Grants, worktree retention and the permission mode stay per account.
SETTING_UNITS: dict[str, tuple[str, ...]] = {
    "model": ("model",),
    "effort": ("effort", "effortInherited"),
    "place": ("cwd", "originCwd", "worktreePath", "worktreeName", "worktreeLazy",
              "branch", "sourceBranch", "gitAnchors", "gitAnchorsLookupOnly"),
}
#: Units a user changes with a pick (the model picker, `set_session_model`,
#: `set_session_effort`), which never raises `lastActivityAt`. A place moves
#: in or right after a turn; only a worktree detach moves it without one.
PICKED_UNITS = frozenset({"model", "effort"})
SETTING_FIELDS = tuple(field for fields in SETTING_UNITS.values() for field in fields)
#: The fields `_rank` reads.
RANK_FIELDS = ("lastActivityAt", "lastFocusedAt", "createdAt")
#: A copy the app wrote this long after the session's last activity was
#: written after that activity settled: the app saves a record within 1-3 s
#: of the frame that raised `lastActivityAt` (its save debounce).
SETTLE_MS = 60_000
#: How many values a session's settings base remembers per unit.
SEEN_LIMIT = 64

#: The fields a pass reads from an index entry; the cache keeps nothing else.
#: Records average 12 KB, three quarters of it MCP configuration the mirror
#: never looks at (measured 2026-09-24).
PROJECTED = tuple(dict.fromkeys((
    "sessionId", "cliSessionId", "isArchived", "isStarred", "title", "titleSource",
    *RANK_FIELDS, "sessionSettings", "priorCliSessionIds", *SETTING_FIELDS)))
#: What flag sync decides on. A copy whose on-disk values of these moved since
#: the pass read it is left for the next pass instead of being overwritten.
FLAG_FIELDS = ("cliSessionId", "isArchived", "isStarred", "title", "titleSource",
               "sessionSettings")
FLAG_WRITES = ("isArchived", "isStarred", "title", "titleSource", "sessionSettings")
#: What a publish re-checks on every copy it writes. A write patches the flag
#: fields and every setting field back from what the pass decided, so each of
#: them must still hold what the pass read.
CHECKED_FIELDS = FLAG_FIELDS + SETTING_FIELDS
#: Stands for a field the record does not have, which the app treats
#: differently from `null`.
_ABSENT = object()

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
    re-save does: the known limit, not a hold that could never end."""
    return exc.errno in NO_ONES_SIDEBAR


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
    from . import transcripts
    return transcripts.projects_dir()


def slug(cwd: str) -> str:
    """The `~/.claude/projects` folder name for a session cwd.

    v1 verified this rule empirically against 400 live transcripts on 2026-07-04.
    """
    return re.sub(r"[^A-Za-z0-9-]", "-", cwd)


def _load(path: Path, *, strict: bool = False) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        if strict:
            raise
        return {}
    return value if isinstance(value, dict) else {}


def _read_entry(path: Path) -> bytes:
    """An index entry's bytes: the one read the inventory makes per changed file."""
    return path.read_bytes()


def _temporary(path: Path) -> Path:
    # Never `*.json` or `*.json.tmp`: the app lists the first and promotes the
    # second on load.
    return path.with_name(path.name + ".tmp-subfleet")


def _signature_of(path: str | Path) -> tuple[int, ...]:
    info = os.stat(path)
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size, info.st_ctime_ns)


def _install(temporary: Path, destination: Path, *, expect: tuple[int, ...] | None,
             exclusive: bool) -> bool:
    """Put a finished temporary file in place; False if the destination moved.

    `exclusive` is create-only: a hard link fails if the name was taken since
    the pass looked, so a file the app just created is never replaced.
    `expect` is the signature the pass decided on; the destination is re-read
    right before the rename, which narrows the window in which an app save can
    be lost to the rename itself.
    """
    if exclusive:
        try:
            os.link(temporary, destination)
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
    temporary = _temporary(path)
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, separators=(",", ":"), ensure_ascii=False))
            if sync:                           # a record the app loads: as it does
                stream.flush()
                os.fsync(stream.fileno())
        if stamp is not None:
            os.utime(temporary, (stamp, stamp))
        inode = os.stat(temporary).st_ino
        if not _install(temporary, path, expect=expect, exclusive=exclusive):
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
    temporary = _temporary(destination)
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    try:
        shutil.copy2(source, temporary)
        os.chmod(temporary, 0o600)
        handle = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(handle)                   # as the app does before its rename
        finally:
            os.close(handle)
        inode = os.stat(temporary).st_ino
        if not _install(temporary, destination, expect=expect, exclusive=exclusive):
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
    #: Sessions whose model, effort or place this pass brought every copy to.
    settings_synced: int = 0
    #: Session files (by `sessionId`) whose copies hold different conversation
    #: ids: a `/clear`, a rewind or an undone clear in one account that the
    #: others never saw. Reported, never rewritten (the report of 2026-09-26).
    ids_diverged: int = 0
    #: The first few: `{"session", "ids": {id: copies}, "newest"}`.
    diverged: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in (
            "started_at", "finished_at", "state", "added", "repaired", "revived",
            "pruned", "flag_synced", "retitled", "transcript_retitled",
            "accounts", "sessions", "error", "dry_run", "stage", "entries_scanned",
            "kind", "folders_scanned", "swept", "skipped", "flags_held", "held_by",
            "settings_synced", "ids_diverged", "diverged")}

    @property
    def changed(self) -> bool:
        return any((self.added, self.repaired, self.revived, self.pruned,
                    self.flag_synced, self.retitled, self.transcript_retitled,
                    self.settings_synced))

    @property
    def summary(self) -> str:
        return (f"added {self.added}, repaired {self.repaired}, revived {self.revived}, "
                f"pruned {self.pruned}, flag-synced {self.flag_synced}, "
                f"retitled {self.retitled}, t-retitled {self.transcript_retitled}, "
                f"settings-synced {self.settings_synced}"
                + (f", flags held {self.flags_held}" if self.flags_held else "")
                + (f", ids diverged {self.ids_diverged}" if self.ids_diverged else ""))


@dataclass
class Options:
    """Everything a pass may be told to do or not do."""

    dry_run: bool = False
    prune: bool = False
    dead_home: str = ""
    exclude: tuple[str, ...] = ()
    flag_sync: bool = True
    #: Bring every copy's model, effort and place to one value (runs with
    #: flag sync, in the same all-or-nothing publish).
    settings_sync: bool = True
    restore: bool = True
    archive: str = ""
    ultracode_default: bool = True


def load_config(path: Path | None = None) -> dict[str, Any]:
    """v1's `~/.claude/cc-mirror.json`, read and never written.

    Without it the daemon's timer — which passes no flags — would silently lose
    the archive-restore and the dead-session home, because both are per-user
    facts v1 kept here rather than in code.
    """
    return _load(path or (transcripts_dir() / CONFIG_NAME))


def transcripts_dir() -> Path:
    from . import transcripts
    return transcripts.claude_dir()


def options_from(policy: dict[str, Any], **overrides: Any) -> Options:
    """Policy, then v1's saved defaults and caller flags; exclusions accumulate."""
    settings = policy.get("sessions", {})
    config = load_config()
    values: dict[str, Any] = {
        "ultracode_default": bool(settings.get("mirror_ultracode_default", True)),
        "settings_sync": bool(settings.get("mirror_settings_sync", True))}
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


def _rank(data: dict) -> Any:
    return (data.get("lastActivityAt") or data.get("lastFocusedAt")
            or data.get("createdAt") or 0)


def _rank_ms(data: dict) -> int:
    """`_rank` as a number: epoch milliseconds, 0 when it is not one."""
    value = _rank(data)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _unit_value(data: dict, fields: tuple[str, ...]) -> dict[str, Any]:
    """A unit's fields as the record holds them: absent fields stay absent."""
    return {key: data[key] for key in fields if key in data}


def _unit_digest(value: dict[str, Any]) -> str:
    """A unit value's identity in the settings base."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class SettingCopy:
    """One copy's value of one settings unit, as the decision reads it."""

    digest: str
    rank: int          # `_rank`, epoch ms
    mtime_ms: int      # the file's mtime: the app's last write (the mirror keeps it)


def decide_setting(copies: list[SettingCopy], base: dict[str, Any] | None, *,
                   picked: bool) -> tuple[str, str]:
    """The digest every copy of one unit should hold, and which rule chose it.

    `copies` are in path order. `base` is the unit's settings base: `v` (the
    value last decided, if any), `seen` (the digests of every value a
    deciding pass read or displaced) and the session's `rank` (the greatest
    rank that pass read). In order:

    * `agree`: every copy holds the same value;
    * `first`: no value decided yet. The most active copy wins (greatest
      rank, then latest write, then path order), except that for a picked
      unit a value only copies written after the session's last activity
      settled hold was picked after it and wins (latest write first);
    * `new`: a value no pass has seen can only be a change the app made
      since, a pick or a move; the most active copy holding one wins. The
      app's stale memory can only hold a value some pass read;
    * `activity`: a copy with activity since the base was decided wins, the
      most active first;
    * `base`: otherwise the base stands. A copy that differs without new
      activity holds a value the app re-saved from memory older than the
      mirror's write (or a pick of a value the session held before, which
      spreads with the session's next activity).
    """
    if not copies:
        raise ValueError("a unit is decided from at least one copy")

    def best(indices: list[int], key) -> str:
        top = indices[0]
        for index in indices[1:]:
            if key(copies[index]) > key(copies[top]):
                top = index
        return copies[top].digest

    def activity(item: SettingCopy) -> tuple[int, int]:
        return (item.rank, item.mtime_ms)

    digests = {item.digest for item in copies}
    if len(digests) == 1:
        return copies[0].digest, "agree"
    everyone = list(range(len(copies)))
    decided = base.get("v") if base else None
    if decided is None:
        if picked:
            settled = max(item.rank for item in copies) + SETTLE_MS
            holders: dict[str, list[int]] = {}
            for index, item in enumerate(copies):
                holders.setdefault(item.digest, []).append(index)
            later = [index for index, item in enumerate(copies)
                     if all(copies[other].mtime_ms > settled for other in holders[item.digest])]
            if later:
                return best(later, lambda item: item.mtime_ms), "first"
        return best(everyone, activity), "first"
    seen = set(base.get("seen") or ())
    new = [index for index, item in enumerate(copies) if item.digest not in seen]
    if new:
        return best(new, activity), "new"
    since = int(base.get("rank") or 0)
    active = [index for index, item in enumerate(copies) if item.rank > since]
    if active:
        return best(active, activity), "activity"
    return str(decided), "base"


def _seen_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _remember(seen: list[str], digests: Iterable[str], keep: str | None) -> list[str]:
    """`seen` with `digests` appended (oldest first), at most SEEN_LIMIT long;
    `keep` (the decided value) is never the one dropped."""
    merged = list(seen)
    for digest in digests:
        if digest not in merged:
            merged.append(digest)
    while len(merged) > SEEN_LIMIT:
        drop = next(index for index, digest in enumerate(merged) if digest != keep)
        del merged[drop]
    return merged


def _seen_ahead(settings: Any, displaced: dict[str, set[str]]) -> dict[str, Any]:
    """A settings base with `displaced` added to each unit's `seen`, and
    nothing else changed (the write-ahead before a publish)."""
    record = dict(settings) if isinstance(settings, dict) else {}
    units = dict(record.get("units")) if isinstance(record.get("units"), dict) else {}
    for unit, digests in displaced.items():
        entry = dict(units.get(unit)) if isinstance(units.get(unit), dict) else {}
        entry["seen"] = _remember(_seen_list(entry.get("seen")), sorted(digests),
                                  entry.get("v") if isinstance(entry.get("v"), str) else None)
        units[unit] = entry
    record["units"] = units
    return record


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
        self._stem_dirs: dict[str, tuple[int, dict[str, Path]]] = {}
        #: The last full pass's `<session id> -> transcript`.
        self._stems: dict[str, Path] = {}
        self._archive: tuple[str, float, dict[str, tuple[int, Path]], frozenset[str]] | None = None
        self._last_sweep: float | None = None
        #: True once a full inventory finished in this process: the hot pass
        #: needs one to know what every folder already holds.
        self._inventoried = False
        #: New records the hot pass could not spread yet (no transcript).
        self._retry: dict[str, float] = {}
        self._hot_recorded: tuple[float, Any] | None = None
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

    @staticmethod
    def _signature(path: Path) -> tuple[int, ...]:
        return _signature_of(path)

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

    def _entry(self, path: Path) -> dict:
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
            signature = self._signature(path)
            if cached is not None and cached[0] == signature:
                return self._payloads[cached[1]].value
            self._forget(path)
            raw = _read_entry(path)
        except FileNotFoundError:
            self._forget(path)                  # gone: no longer anyone's copy
            return {}
        except OSError as exc:
            self._forget(path)
            if not _permanent(exc):
                # Unknown, not absent (EMFILE on 2026-09-25): flag sync holds
                # the session it belongs to.
                self._unread[key] = str(known)
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
        value = {key: previous[key] for key in ("hot", "load_gap") if key in previous}
        value.update({
            "pass": current.to_dict(),
            "last_ok_at": last_ok or previous.get("last_ok_at"),
            "interval_s": self.policy.get("sessions", {}).get("mirror_interval_s", 60),
            "updated_at": _iso(self.now()),
        })
        if load_gap is not None:
            value["load_gap"] = load_gap
        _write_json(self.sidecar_path, value)

    def _record_hot(self, current: Pass, load_gap: dict[str, Any] | None) -> None:
        """The hot pass's own block; never the heartbeat C-23.28 judges by."""
        if current.dry_run:
            return
        instant = time.monotonic()
        seen = (load_gap or {}).get("status"), (load_gap or {}).get("pending")
        last = self._hot_recorded
        if (not current.changed and current.state == "ok" and last is not None
                and last[1] == seen and instant - last[0] < HOT_RECORD_S):
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
                         sweep: bool = True) -> dict[str, Path]:
        """`<session id> -> transcript`; a session is openable iff it is a key.

        A session transcript is `projects/<slug>/<id>.jsonl`, one level down;
        the deeper `.jsonl` files are subagent logs (34k of them on 2026-09-24,
        none named like a session). A project directory is re-listed only when
        its mtime moved, which creating or pruning a transcript always does.
        """
        stems: dict[str, Path] = {}
        base = projects_dir()
        try:
            with os.scandir(base) as listing:
                directories = sorted(item.path for item in listing
                                     if item.is_dir(follow_symlinks=False))
        except OSError:
            return stems
        seen = set()
        for directory in directories:
            if current is not None:
                self._checkpoint(current)
            seen.add(directory)
            try:
                mtime = os.stat(directory).st_mtime_ns
            except OSError:
                continue
            cached = self._stem_dirs.get(directory)
            if sweep or cached is None or cached[0] != mtime:
                try:
                    with os.scandir(directory) as listing:
                        found = {item.name[:-6]: Path(item.path) for item in listing
                                 if item.name.endswith(".jsonl") and item.is_file()}
                except OSError:
                    found = {}
                cached = self._stem_dirs[directory] = (mtime, found)
            stems.update(cached[1])
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
        no signal, never a change.
        """
        try:
            with path.open("rb") as stream:
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

    def _scan(self, path: Path, current: Pass, *,
              sweep: bool) -> tuple[dict[str, dict] | None, list[str]]:
        """Refresh one folder: `(its entries, or None if unchanged; fresh names)`.

        A fresh name is one whose content this process had not seen at that
        path: a new file, or a file whose bytes changed.
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
        try:
            with os.scandir(path) as listing:
                # The same ~1.8k names recur in every folder; interned, the
                # folders' listings share one string per name.
                found = sorted((sys.intern(item.name), item.inode()) for item in listing
                               if item.name.startswith("local_") and item.name.endswith(".json"))
        except OSError as exc:
            # Unknown is not empty: an empty view would hide every session the
            # folder holds and invite copies over them. List it again next pass.
            self._dirty.add(path)
            transient = not (isinstance(exc, FileNotFoundError) or _permanent(exc))
            raise _Unlisted(str(path), transient=transient) from exc
        self._dirty.discard(path)
        previous = state.names if state is not None else frozenset()
        files: dict[str, dict] = {}
        ids: dict[str, list[str]] = {}
        fresh: list[str] = []
        complete = signature is not None
        base = os.fspath(path)
        previous_owner: dict[str, str] | None = None
        for name, inode in found:
            self._checkpoint(current)
            key = os.path.join(base, name)
            cached = self._entries.get(key)
            if (not sweep and cached is not None and name in previous
                    and cached[0][1] == inode):
                data = self._payloads[cached[1]].value
            else:
                before = cached[1] if cached is not None else None
                data = self._entry(path / name)
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
        names = frozenset(name for name, _inode in found)
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
                       data: dict, kind: str) -> None:
        """Journal a write, unless the app replaced the file before we looked."""
        try:
            info = os.stat(destination)
        except OSError:
            return
        if inode is not None and info.st_ino != inode:
            return
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
        self._dirty.add(destination.parent)
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
                    self._dirty.add(path)
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
                if destination.exists():                # raced with another writer
                    stems[identity] = destination
                    continue
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    temporary = destination.with_name(destination.name + ".tmp-revive")
                    shutil.copyfile(source[1], temporary)
                    os.replace(temporary, destination)
                    stamp = self.now().timestamp()
                    os.utime(destination, (stamp, stamp))   # cleanup cannot insta-prune
                except OSError:
                    continue
            stems[identity] = destination
            revived += 1
        return revived

    def _mtime_ms(self, path: Path, name: str) -> int:
        """A copy's mtime in ms: the app's last write (every mirror write keeps it)."""
        cached = self._entries.get(os.path.join(path, name))
        try:
            mtime_ns = cached[0][2] if cached is not None else os.stat(path / name).st_mtime_ns
        except OSError:
            return 0
        return mtime_ns // 1_000_000

    def _settle_settings(self, copies: list[tuple[Path, str, dict]], previous: Any,
                         writable: Callable[[Path, str], dict],
                         setting_dirty: set[tuple[Path, str]]
                         ) -> tuple[dict[str, Any], dict[str, set[str]], bool]:
        """Decide one session's model, effort and place from every copy.

        Marks each copy that differs for a write through `writable` and
        `setting_dirty`, and returns the session's new settings base, the
        values each unit's publish displaces (for the write-ahead), and
        whether any copy is written. The base holds the session's `rank` (the
        greatest this pass read) and, per unit, the decided value (`v`, its
        digest, and `value`) and `seen`: every value a deciding pass read.
        """
        ordered = sorted(copies, key=lambda item: (os.fspath(item[0]), item[1]))
        old = previous if isinstance(previous, dict) else {}
        old_units = old.get("units") if isinstance(old.get("units"), dict) else {}
        ranks = [_rank_ms(data) for _path, _name, data in ordered]
        stamps = [self._mtime_ms(path, name) for path, name, _data in ordered]
        units: dict[str, Any] = {}
        displaced: dict[str, set[str]] = {}
        wrote = False
        for unit, fields in SETTING_UNITS.items():
            values = [_unit_value(data, fields) for _path, _name, data in ordered]
            digests = [_unit_digest(value) for value in values]
            prior = old_units.get(unit) if isinstance(old_units.get(unit), dict) else {}
            seen = _seen_list(prior.get("seen"))
            known = prior.get("v") if isinstance(prior.get("v"), str) else None
            stored = prior.get("value") if isinstance(prior.get("value"), dict) else None
            if known is not None and (stored is None or _unit_digest(stored) != known):
                known = None                    # a base it cannot write back: start over
            decided, _rule = decide_setting(
                [SettingCopy(digest, rank, stamp)
                 for digest, rank, stamp in zip(digests, ranks, stamps)],
                {"v": known, "seen": seen, "rank": old.get("rank")} if known else None,
                picked=unit in PICKED_UNITS)
            value = values[digests.index(decided)] if decided in digests else stored
            assert value is not None and _unit_digest(value) == decided
            for (path, name, _data), digest in zip(ordered, digests):
                if digest == decided:
                    continue
                target = writable(path, name)
                for key in fields:
                    if key in value:
                        target[key] = json.loads(json.dumps(value[key]))
                    else:
                        target.pop(key, None)
                setting_dirty.add((path, name))
                wrote = True
            displaced[unit] = {digest for digest in digests if digest != decided}
            units[unit] = {"v": decided, "value": value,
                           "seen": _remember(seen, [*digests, decided], decided)}
        return {"rank": max(ranks, default=0), "units": units}, displaced, wrote

    def sync_flags(self, folder_files: dict[Path, dict[str, dict]],
                   stems: dict[str, Path], options: Options,
                   current: Pass, *, unread: dict[str, str] | None = None,
                   blind: bool = False, complete: bool = True) -> set[tuple[Path, str]]:
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

        Known limit: the app saves a record from memory, so a folder it holds
        (the loaded one, or one where an earlier account's session still runs)
        can write back a value the mirror changed there, and the merge base
        reads that re-save as a user's change. See the 2026-09-24 report.

        With `options.settings_sync`, the same pass brings every copy's model,
        effort and place to one value (`_settle_settings`, `decide_setting`),
        in the same publish: a copy it writes for a setting must also still
        rank as the pass read it, so a copy that ran a turn since is never
        overwritten with an older copy's values. A setting re-saved from
        stale memory spreads only with new activity, never by itself. See
        the 2026-09-26 report.
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
        # Unseen sessions keep their base unless every copy was read.
        fresh: dict[str, dict] = {} if complete else dict(base_all)
        dirty: set[tuple[Path, str]] = set()
        originals: dict[tuple[Path, str], dict] = {}
        owners: dict[tuple[Path, str], str] = {}
        #: Copies this pass writes a setting into: their rank is re-checked too.
        setting_dirty: set[tuple[Path, str]] = set()
        #: Per session and unit, the values its publish will displace. They
        #: join the settings base's `seen` before any copy is written, so a
        #: crash between the writes and the base cannot make a displaced value
        #: read as new when the app's memory saves it back.
        ahead: dict[str, dict[str, set[str]]] = {}

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
            return data.get("lastActivityAt") or data.get("createdAt") or 0

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

            record = {"isArchived": base["isArchived"], "isStarred": base["isStarred"]}
            if title is not None:
                record["title"] = title
            if anchor is not None:
                record["ttitle"] = anchor
            if stamp is not None:
                record["tmt"] = stamp
            previous = base_all.get(identity, {}).get("settings")
            if options.settings_sync:
                settled, displaced, wrote = self._settle_settings(
                    copies, previous, writable, setting_dirty)
                record["settings"] = settled
                if any(displaced.values()):
                    ahead[identity] = displaced
                if wrote:
                    current.settings_synced += 1
            elif isinstance(previous, dict):
                record["settings"] = previous           # switched off: kept, not dropped
            fresh[identity] = record

        if not options.dry_run:
            # Once writes start, finish the matching merge base. Cancellation
            # inside this batch could mistake our partial writes for user edits.
            self._checkpoint(current, "publishing flags")
            if ahead:
                # Write-ahead: what the writes displace is marked seen first.
                # Only `seen` grows; every decided value and flag keeps its base.
                # A session held below keeps this base, `seen` included.
                for identity, displaced in ahead.items():
                    known = dict(base_all.get(identity) or {})
                    known["settings"] = _seen_ahead(known.get("settings"), displaced)
                    base_all[identity] = known
                self.dir.mkdir(parents=True, mode=0o700, exist_ok=True)
                _write_json(self.flags_path, base_all, sync=True)
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
                    self._dirty.add(path)
                    try:
                        expect = _signature_of(target)
                        body = _load(target, strict=True)
                        if (_signature_of(target) != expect
                                or any(body.get(key) != original.get(key) for key in FLAG_FIELDS)
                                or any(body.get(key, _ABSENT) != original.get(key, _ABSENT)
                                       for key in SETTING_FIELDS)
                                # A copy that ran a turn since the read may now
                                # be the newest: never write older values over it.
                                or ((path, name) in setting_dirty
                                    and _rank(body) != _rank(original))):
                            break                   # moved under us; decide next pass
                    except (OSError, ValueError):
                        break
                    before = dict(body)
                    for key in FLAG_WRITES:
                        if key in resolved:
                            body[key] = resolved[key]
                    for key in SETTING_FIELDS:
                        # Unchanged fields write back what the check just saw.
                        if key in resolved:
                            body[key] = resolved[key]
                        else:
                            body.pop(key, None)
                    ready.append((target, body, before, resolved, expect))
                else:
                    written: list[tuple[Path, dict, int, tuple[int, ...], tuple[int, ...]]] = []
                    for target, body, before, resolved, expect in ready:
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
                            for done, old, done_inode, done_expect, was in reversed(written):
                                try:
                                    self._forget(done)
                                    back = _write_json(done, old, keep_mtime=True,
                                                       expect=done_expect, sync=True)
                                except (OSError, ValueError):
                                    # The mirror's write stands: journal it.
                                    self._journal_write(done, done_inode, identity,
                                                        folder_files[done.parent][done.name],
                                                        "updated")
                                    continue
                                if back is not None:
                                    self._journal_restamp(done, was, back)
                            held.add(identity)
                            break
                        try:
                            now_signature = _signature_of(target)
                        except OSError:
                            now_signature = None
                        if now_signature is not None and now_signature[1] == inode:
                            written.append((target, before, inode, now_signature, expect))
                    else:
                        for target, _before, inode, _signature, _was in written:
                            self._journal_write(target, inode, identity,
                                                folder_files[target.parent][target.name], "updated")
                    continue
                held.add(identity)
            for identity in held:
                current.flags_held += 1
                self._why.setdefault(f"session {identity}", "a copy changed while the pass published")
                if identity in base_all:
                    fresh[identity] = base_all[identity]
                else:
                    fresh.pop(identity, None)
            self.dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            # Synced like the records it describes: a base lost to a crash would
            # hand every divergent session to the bootstrap rule. A base that
            # cannot be written fails the pass rather than pass for synced.
            _write_json(self.flags_path, fresh, sync=True)
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
        try:
            lock = self._lock()
            if lock is None:
                current.state = "ok"
                current.finished_at = current.started_at
                current.error = "another pass holds the lock"
                return current           # deliberately without touching the sidecar
            self.journal.refresh()
            self._record(current)
            self._pass_payloads = {}
            self._progress_due = time.monotonic() + PROGRESS_INTERVAL_S
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
        stale empty copies. Title, flag and setting changes, revival and
        pruning stay with the full pass. The first pass in a process is a full
        one, because spreading needs to know what every folder already holds.
        It shares the full pass's lock and never touches the full pass's
        record in the sidecar, so C-23.28's heartbeat still means a full pass.
        """
        options = options or Options()
        if not self._inventoried:
            return self.run_once(options)
        current = Pass(started_at=_iso(self.now()), dry_run=options.dry_run, kind="hot")
        lock = None
        try:
            lock = self._lock()
            if lock is None:
                current.state = "ok"
                current.finished_at = current.started_at
                current.error = "another pass holds the lock"
                return current
            self.journal.refresh()
            self._pass_payloads = {}
            self._hot(current, options)
            current.state = "ok"
            current.finished_at = _iso(self.now())
        except (_Cancelled, OSError) as exc:
            current.state = "cancelled" if isinstance(exc, _Cancelled) else "error"
            current.error = f"{type(exc).__name__}: {exc}"
            current.finished_at = _iso(self.now())
        finally:
            if lock is not None:
                self._pass_payloads = {}
                try:
                    try:
                        self._record_hot(current, self._settle(options))
                    except OSError:
                        pass
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                finally:
                    lock.close()
        return current

    def _lock(self):
        path = self.dir / LOCK_NAME
        stream = None
        try:
            self.dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            stream = path.open("a")
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
            return
        sweep = self._sweep_due()
        current.swept = sweep
        self._checkpoint(current, "finding transcripts")
        stems = self.transcript_stems(current, sweep=sweep)

        folder_files: dict[Path, dict[str, dict]] = {}
        folder_ids: dict[Path, set[str]] = {}
        fresh_ids: set[str] = set()
        unlisted: list[Path] = []
        self._unread, self._why = {}, {}
        self._checkpoint(current, "reading entries")
        for _account, _org, path in folders:
            self._checkpoint(current)
            try:
                files, fresh = self._scan(path, current, sweep=sweep)
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
        self._report_diverged_ids(folder_files, current)
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
        for path, files in folder_files.items():
            for name, data in files.items():
                self._checkpoint(current)
                if not resolvable(data):
                    continue
                identity = data["cliSessionId"]
                score = _rank(data)
                if identity not in canonical or score > canonical[identity][0]:
                    canonical[identity] = (score, data, name, path / name)
        current.sessions = len(canonical)
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
            # Flag sync decides a session from every copy or not at all: a copy
            # it skipped would read as a user's change next pass and undo the
            # change it spread (review round 5). An unlisted folder's copies
            # are read by name from its last listing; a folder never listed
            # hides which sessions it holds, so every session waits.
            flag_files = dict(folder_files)
            blind = bool(unknown)
            for where, reason in unknown:
                self._why[where] = reason
            for path in unlisted:
                state = self._folders.get(path)
                try:
                    info = os.stat(path)
                    now_signature: tuple[int, ...] | None = (info.st_dev, info.st_ino,
                                                             info.st_mtime_ns)
                except OSError:
                    now_signature = None
                if state is None or now_signature is None or now_signature != state.signature:
                    # Never listed, or changed since (the mirror's own copies
                    # change it too): what it holds now is unknown.
                    blind = True
                    self._why[os.fspath(path)] = ("folder not listed, never listed" if state is None
                                                  else "folder not listed, changed since its listing")
                    continue
                owner = {name: identity for identity, names in state.ids.items()
                         for name in names}
                files = {}
                for name in sorted(state.names):
                    files[name] = self._entry(path / name)
                    key = os.path.join(path, name)
                    if key in self._unread and not self._unread[key]:
                        self._unread[key] = owner.get(name, "")
                flag_files[path] = files
            self.sync_flags(flag_files, stems, options, current, unread=dict(self._unread),
                            blind=blind, complete=not unlisted and not failed and not self._unread)
            if current.flags_held:
                current.held_by = [{"path": where, "reason": reason}
                                   for where, reason in list(self._why.items())[:HELD_BY_LIMIT]]

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
                        self._dirty.add(path)
                        try:
                            (path / name).unlink()
                            self._forget(path / name)
                        except OSError:
                            pass
                    current.pruned += 1

        self._stems = stems
        if sweep:
            self._last_sweep = time.monotonic()

    @staticmethod
    def _report_diverged_ids(folder_files: dict[Path, dict[str, dict]], current: Pass) -> None:
        """Count session files whose copies hold different conversation ids.

        The app changes `cliSessionId` in the account where a `/clear`, a
        rewind or an undone clear happens, and nowhere else; opening another
        account's copy resumes the conversation from before. The mirror groups
        copies by that id, so it never converges them. It reports them rather
        than rewrite the id: `priorCliSessionIds` does not order them (an
        undone clear puts the newer id into it, and a resume that finds no
        conversation drops the old id without recording it; bundle 2.9939.2).
        """
        by_session: dict[str, dict[str, list[tuple[int, bool]]]] = {}
        for _path, files in folder_files.items():
            for name, data in files.items():
                identity = data.get("cliSessionId") or ""
                if not identity:
                    continue
                session = str(data.get("sessionId") or name.removesuffix(".json"))
                by_session.setdefault(session, {}).setdefault(identity, []).append(
                    (_rank_ms(data), bool(data.get("isArchived"))))
        diverged = []
        for session, ids in by_session.items():
            if len(ids) < 2:
                continue
            newest = max(ids, key=lambda identity: (max(rank for rank, _a in ids[identity]),
                                                    identity))
            archived = all(flag for rank, flag in ids[newest]
                           if rank == max(r for r, _a in ids[newest]))
            diverged.append({"session": session, "newest": newest, "archived": archived,
                             "ids": {identity: len(held) for identity, held in sorted(ids.items())}})
        current.ids_diverged = len(diverged)
        diverged.sort(key=lambda item: (item["archived"], item["session"]))
        current.diverged = diverged[:HELD_BY_LIMIT] or None

    def _hot(self, current: Pass, options: Options) -> None:
        self._checkpoint(current, "reading entries")
        folders = self.folders(options.exclude)
        try:
            kept, _unknown = self._listing_gaps(options)
        except OSError as exc:
            # Nothing to spread from a store it cannot list, and nothing to
            # forget. The full pass records the failure; a hot pass every 2 s
            # would only repeat it.
            current.error = f"store not listed: {exc}"
            return
        folders = folders + kept
        current.accounts = len(folders)
        fresh: list[tuple[Path, str]] = []
        unlisted: set[Path] = set()
        for _account, _org, path in folders:
            self._checkpoint(current)
            try:
                _files, names = self._scan(path, current, sweep=False)
            except _Unlisted:
                unlisted.add(path)
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
        for identity, since in list(self._retry.items()):
            if instant - since > UNRESOLVED_RETRY_S:
                del self._retry[identity]
            else:
                identities.setdefault(identity, since)
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

        self._checkpoint(current, "copying entries")
        for identity, since in identities.items():
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
        data = self.sidecar()
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
                    "ids_diverged": int(record.get("ids_diverged") or 0),
                    "diverged": record.get("diverged") or [],
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


def run_once(root: str | Path, policy: dict[str, Any] | None = None,
             options: Options | None = None, *, now=None) -> Pass:
    return Mirror(root, policy, now=now).run_once(options)


__all__ = ["DEFAULT_HANG_MIN", "DEFAULT_STALL_MIN", "Mirror", "Options", "Pass",
           "health", "load_gap", "options_from", "run_once", "slug", "store_dir"]
