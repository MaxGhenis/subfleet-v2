"""The desktop sidebar mirror: one Claude Code sidebar across every account.

The Claude desktop app stores one small JSON index file per Code session at
`~/Library/Application Support/Claude/claude-code-sessions/<account>/<org>/local_<id>.json`,
and the sidebar shows only the folder of the account that is currently logged
in — so every session vanishes when Max switches accounts, which he does daily.
The transcript itself lives in `~/.claude/projects/<slug(cwd)>/<cli>.jsonl` and is
account-agnostic: only the folder an index file sits in decides which account
"owns" a session. Copying the index into every folder unifies the sidebars. That
is the whole mechanism; there is no provider call anywhere in this module (plan
decision 8, C-23.28).

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
no-op pass.
"""

from __future__ import annotations

import fcntl
import glob as globbing
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

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

#: A pass the sidecar records as in flight is healthy until this long after its
#: recorded start (C-23.28). An 8.5-minute pass was observed on 2026-08-18
#: during app-churn re-seeding; a 45-minute one is hung.
DEFAULT_HANG_MIN = 30.0
#: How long after a finished pass the mirror is still considered fresh.
DEFAULT_STALL_MIN = 10.0


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


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: Any, *, keep_mtime: bool = False) -> None:
    """Atomic replace; optionally restoring the previous mtime.

    Per-account sidebar ordering is the file's mtime, so a flag-sync write that
    changed one boolean must not reorder the sidebar.
    """
    before = None
    if keep_mtime and path.exists():
        try:
            before = path.stat().st_mtime
        except OSError:
            before = None
    temporary = path.with_name(path.name + ".tmp-subfleet")
    temporary.write_text(json.dumps(value, separators=(",", ":"), ensure_ascii=False),
                         encoding="utf-8")
    os.replace(temporary, path)
    if before is not None:
        try:
            os.utime(path, (before, before))
        except OSError:
            pass


# --- the pass ----------------------------------------------------------------

@dataclass
class Pass:
    """One mirroring pass, as the sidecar records it (C-23.28)."""

    started_at: str
    finished_at: str | None = None
    state: str = "running"                  # running | ok | error
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

    def to_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in (
            "started_at", "finished_at", "state", "added", "repaired", "revived",
            "pruned", "flag_synced", "retitled", "transcript_retitled",
            "accounts", "sessions", "error", "dry_run")}

    @property
    def summary(self) -> str:
        return (f"added {self.added}, repaired {self.repaired}, revived {self.revived}, "
                f"pruned {self.pruned}, flag-synced {self.flag_synced}, "
                f"retitled {self.retitled}, t-retitled {self.transcript_retitled}")


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
        "ultracode_default": bool(settings.get("mirror_ultracode_default", True))}
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


class Mirror:
    """A mirroring pass against one state root and one desktop session store."""

    def __init__(self, root: str | Path, policy: dict[str, Any] | None = None, *,
                 now=None):
        self.root = Path(root)
        self.policy = policy or {}
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.dir = self.root / "sessions"

    # --- state files ---------------------------------------------------------

    @property
    def sidecar_path(self) -> Path:
        return self.dir / SIDECAR_NAME

    @property
    def flags_path(self) -> Path:
        return self.dir / FLAGS_NAME

    def sidecar(self) -> dict[str, Any]:
        return _load(self.sidecar_path)

    def _record(self, current: Pass, *, last_ok: str | None = None) -> None:
        if current.dry_run:
            return
        self.dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        previous = self.sidecar()
        value = {
            "pass": current.to_dict(),
            "last_ok_at": last_ok or previous.get("last_ok_at"),
            "interval_s": self.policy.get("sessions", {}).get("mirror_interval_s", 60),
            "updated_at": _iso(self.now()),
        }
        _write_json(self.sidecar_path, value)

    # --- the store -----------------------------------------------------------

    def folders(self, exclude: Iterable[str]) -> list[tuple[str, str, Path]]:
        """Every `<account>/<org>` directory the desktop store holds."""
        excluded = [value for value in exclude if value]
        found: list[tuple[str, str, Path]] = []
        base = store_dir()
        try:
            accounts = sorted(item for item in base.iterdir() if item.is_dir())
        except OSError:
            return found
        for account in accounts:
            try:
                orgs = sorted(item for item in account.iterdir() if item.is_dir())
            except OSError:
                continue
            for org in orgs:
                if any(value in account.name or value in org.name for value in excluded):
                    continue
                found.append((account.name, org.name, org))
        return found

    @staticmethod
    def transcript_stems() -> dict[str, Path]:
        """`<session id> -> transcript`; a session is openable iff it is a key."""
        stems: dict[str, Path] = {}
        try:
            for path in projects_dir().glob("**/*.jsonl"):
                stems[path.stem] = path
        except OSError:
            return stems
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

    # --- the steps -----------------------------------------------------------

    def restore_dead(self, folder_files: dict[Path, dict[str, dict]],
                     stems: dict[str, Path], pattern: str, dry_run: bool) -> int:
        """Revive dead sessions whose transcript survives in an archive.

        Creation-only: never overwrites, never deletes, safe every pass. Mutates
        `stems` so the copy step treats a revived session as openable.
        """
        dead: dict[str, dict] = {}
        for files in folder_files.values():
            for data in files.values():
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
        # Largest file wins on duplicate stems: an archive can hold several
        # snapshots of one session, and the largest is the longest conversation.
        archive: dict[str, tuple[int, Path]] = {}
        for name in globbing.glob(os.path.expanduser(pattern), recursive=True):
            path = Path(name)
            try:
                size = path.stat().st_size
            except OSError:            # an archive sync may unlink mid-walk
                continue
            previous = archive.get(path.stem)
            if previous is None or size > previous[0]:
                archive[path.stem] = (size, path)
        revived = 0
        for identity, data in sorted(dead.items()):
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

    def sync_flags(self, folder_files: dict[Path, dict[str, dict]],
                   stems: dict[str, Path], options: Options,
                   current: Pass) -> set[tuple[Path, str]]:
        """Propagate `isArchived`, `isStarred` and the title across every copy.

        The merge base in `mirror-flags.json` holds each session's last synced
        values: a copy that differs from the base is a user action, so the CHANGE
        propagates and archive and un-archive both work. On first divergence with
        no base — the historical backlog — archived-anywhere and starred-anywhere
        win, and a divergent title prefers a manual rename, then the most recently
        active copy.
        """
        base_all = _load(self.flags_path)
        groups: dict[str, list[tuple[Path, str, dict]]] = {}
        for path, files in folder_files.items():
            for name, data in files.items():
                identity = data.get("cliSessionId") or ""
                if identity:
                    groups.setdefault(identity, []).append((path, name, data))
        fresh: dict[str, dict] = {}
        dirty: set[tuple[Path, str]] = set()

        def title_of(data: dict) -> str:
            return data.get("title") or ""

        def source_of(data: dict) -> str:
            return data.get("titleSource") or "auto"

        def active_of(data: dict) -> Any:
            return data.get("lastActivityAt") or data.get("createdAt") or 0

        for identity, copies in groups.items():
            base = base_all.get(identity) or {}
            for flag, bootstrap in (("isArchived", True), ("isStarred", True)):
                values = {bool(data.get(flag)) for _p, _n, data in copies}
                if len(values) == 1:
                    resolved = values.pop()
                else:
                    recorded = base.get(flag)
                    resolved = (not recorded) if isinstance(recorded, bool) else bootstrap
                    for path, name, data in copies:
                        if bool(data.get(flag)) != resolved:
                            data[flag] = resolved
                            dirty.add((path, name))
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
                        data["sessionSettings"] = {"ultracode": True}
                        dirty.add((path, name))
                    elif "ultracode" not in settings:
                        settings["ultracode"] = True
                        dirty.add((path, name))

            title: str | None = None
            variants = {(title_of(data), source_of(data)) for _p, _n, data in copies}
            if len(variants) > 1:
                recorded = base_all.get(identity, {}).get("title")
                candidates = [data for _p, _n, data in copies
                              if recorded is None or title_of(data) != recorded]
                if candidates:
                    manual = [data for data in candidates if source_of(data) == "manual"]
                    winner = max(manual or candidates, key=active_of)
                    title, source = title_of(winner), source_of(winner)
                    for path, name, data in copies:
                        if (title_of(data), source_of(data)) != (title, source):
                            data["title"], data["titleSource"] = title, source
                            dirty.add((path, name))
                    current.retitled += 1
            elif variants:
                title = next(iter(variants))[0]

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
                        data["title"], data["titleSource"] = anchor, source
                        dirty.add((path, name))
                title = anchor
                current.transcript_retitled += 1

            record = {"isArchived": base["isArchived"], "isStarred": base["isStarred"]}
            if title is not None:
                record["title"] = title
            if anchor is not None:
                record["ttitle"] = anchor
            if stamp is not None:
                record["tmt"] = stamp
            fresh[identity] = record

        if not options.dry_run:
            for path, name in sorted(dirty, key=lambda item: (str(item[0]), item[1])):
                data = folder_files[path].get(name)
                if data is None:
                    continue
                try:
                    _write_json(path / name, data, keep_mtime=True)
                except OSError:
                    continue
            self.dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            try:
                _write_json(self.flags_path, fresh)
            except OSError:
                pass
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
            self._record(current)
            self._pass(current, options)
            current.state = "ok"
            current.finished_at = _iso(self.now())
            self._record(current, last_ok=current.finished_at)
        except OSError as exc:
            current.state = "error"
            current.error = f"{type(exc).__name__}: {exc}"
            current.finished_at = _iso(self.now())
            self._record(current)
        finally:
            if lock is not None:
                try:
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
        folders = self.folders(options.exclude)
        current.accounts = len(folders)
        if not folders:
            return
        stems = self.transcript_stems()

        folder_files: dict[Path, dict[str, dict]] = {}
        folder_ids: dict[Path, set[str]] = {}
        by_name: dict[str, dict[Path, dict]] = {}
        for _account, _org, path in folders:
            files, identities = {}, set()
            try:
                entries = sorted(path.glob("local_*.json"))
            except OSError:
                entries = []
            for entry in entries:
                data = _load(entry)
                files[entry.name] = data
                by_name.setdefault(entry.name, {})[path] = data
                identity = data.get("cliSessionId") or ""
                if identity:
                    identities.add(identity)
            folder_files[path] = files
            folder_ids[path] = identities

        if options.restore and options.archive:
            current.revived = self.restore_dead(folder_files, stems, options.archive,
                                                options.dry_run)

        def resolvable(data: dict) -> bool:
            identity = data.get("cliSessionId") or ""
            return bool(identity) and identity in stems

        def rank(data: dict) -> Any:
            return (data.get("lastActivityAt") or data.get("lastFocusedAt")
                    or data.get("createdAt") or 0)

        canonical: dict[str, tuple[Any, dict, str, Path]] = {}
        for path, files in folder_files.items():
            for name, data in files.items():
                if not resolvable(data):
                    continue
                identity = data["cliSessionId"]
                score = rank(data)
                if identity not in canonical or score > canonical[identity][0]:
                    canonical[identity] = (score, data, name, path / name)
        current.sessions = len(canonical)

        for identity, (_score, data, name, source) in canonical.items():
            for _account, _org, path in folders:
                if identity in folder_ids[path]:
                    continue                                # this account has it
                existing = folder_files[path].get(name)
                if existing is None:                        # the name is free
                    if not options.dry_run:
                        shutil.copy2(source, path / name)
                        folder_files[path][name] = dict(data)
                        folder_ids[path].add(identity)
                    current.added += 1
                elif not (existing.get("cliSessionId") or ""):   # stale empty
                    if not options.dry_run:
                        shutil.copy2(source, path / name)
                        folder_files[path][name] = dict(data)
                        folder_ids[path].add(identity)
                    current.repaired += 1
                else:
                    # `local_<id>` filenames are not unique across accounts, so a
                    # collision falls back to a cli-derived name rather than
                    # clobbering a different session.
                    fallback = f"local_{identity}.json"
                    destination = path / fallback
                    if destination.exists():
                        continue
                    if not options.dry_run:
                        body = dict(data)
                        body["sessionId"] = f"local_{identity}"   # keep it self-consistent
                        _write_json(destination, body)
                        try:                       # preserve sidebar ordering
                            stamp = source.stat().st_mtime
                            os.utime(destination, (stamp, stamp))
                        except OSError:
                            pass
                        folder_files[path][fallback] = body
                        folder_ids[path].add(identity)
                    current.added += 1

        if options.flag_sync:
            self.sync_flags(folder_files, stems, options, current)

        if options.prune:
            # Off by default: the Claude app prunes dead copies itself on load.
            home = next((path for _a, org, path in folders if org == options.dead_home), None)
            for name, copies in by_name.items():
                if any(resolvable(data) for data in copies.values()):
                    continue                                # openable somewhere
                keep = home if home in copies else sorted(copies)[0]
                for path in list(copies):
                    if path == keep:
                        continue
                    if not options.dry_run:
                        try:
                            (path / name).unlink()
                        except OSError:
                            pass
                    current.pruned += 1

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
        if record.get("state") == "error":
            return {"status": "stalled", "sidecar": str(self.sidecar_path),
                    "age_min": round(age_min, 1) if age_min is not None else None,
                    "run_min": None,
                    "detail": f"last pass failed: {record.get('error')}"}
        if age_min is not None and age_min <= stall:
            return {"status": "healthy", "sidecar": str(self.sidecar_path),
                    "age_min": round(age_min, 1), "run_min": None,
                    "detail": f"last pass {age_min:.1f} min ago: "
                              f"{record.get('added', 0)} added, "
                              f"{record.get('repaired', 0)} repaired"}
        return {"status": "stalled", "sidecar": str(self.sidecar_path),
                "age_min": round(age_min, 1) if age_min is not None else None,
                "run_min": None,
                "detail": (f"sidecar idle {age_min:.1f} min, no pass in flight"
                           if age_min is not None else "sidecar records no pass time")}


def health(root: str | Path, policy: dict[str, Any] | None = None, *,
           now: datetime | None = None) -> dict[str, Any]:
    """`doctor`'s entry point (C-23.28)."""
    return Mirror(root, policy).health(now=now)


def run_once(root: str | Path, policy: dict[str, Any] | None = None,
             options: Options | None = None, *, now=None) -> Pass:
    return Mirror(root, policy, now=now).run_once(options)


__all__ = ["DEFAULT_HANG_MIN", "DEFAULT_STALL_MIN", "Mirror", "Options", "Pass",
           "health", "options_from", "run_once", "slug", "store_dir"]
