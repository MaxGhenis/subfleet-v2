"""The native session catalog (C-30.1, C-30.2; design D-23, §10).

`python -m subfleet.conversations.catalog --state-root <root>` indexes every
Claude transcript at depth one under the projects directory and every Codex
rollout under the enrolled lane homes and `~/.codex`, writing
`<root>/catalog.json` atomically. A (path, size, mtime) cache means a run
re-reads only what changed; a run stops after 20 s and says `complete:false`
until one run has visited everything. Exclusions (lane runs, Codex exec and
subagent threads) come before any limit. Nothing here writes a native store.

The daemon starts the process on a timer and on request and never scans these
trees on a request or control thread; `native_session` reads one session's
files for `conversation.open`. A run writes only for the service that started
it and only into the root it locked (`Owner`); it never creates a state root.
One that stops publishing for either reason says so in its exit status
(`OWNER_GONE`, `FENCE_BROKEN`), which the service logs while it tracks the run.
"""

from __future__ import annotations

import argparse
import fcntl
import functools
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from ..sessions import transcripts
from .redact import scrub
from .store import canonical_native

WALL_S = 20.0
#: A run's exit status when it stopped publishing because its owner is gone (`Owner`):
#: the service that started it closed, or the state root it was started for was
#: removed or replaced. Not 0, so a service that still tracks the run, and so is open,
#: logs it; after close() nothing tracks the run and the decline it expects is quiet.
OWNER_GONE = 3
#: A run's exit status when its `--fence-fd` is not a pipe: not the fence its owner
#: made (a descriptor never passed, or one a standard-stream redirect replaced), so it
#: cannot tell whether that owner is open, and writes nothing.
FENCE_BROKEN = 4
#: Why a run stopped publishing, by its exit status, for its owner's log.
DECLINED = {OWNER_GONE: "it found its owner gone (the fence closed, or the state root removed or replaced)",
            FENCE_BROKEN: "its fence descriptor was not the pipe it was given"}
TAIL = 256 * 1024
HEAD = 64 * 1024
PROMPT_CHARS = 160
PERMISSION_MAP = {"bypassPermissions": "bypass", "acceptEdits": "accept-edits", "default": "ask",
                  "manual": "ask", "plan": "read-only", "dontAsk": "read-only"}


def map_permission(mode: str | None) -> str:
    """Design D-9's total mapping of a recorded Claude mode; anything else is `ask`."""
    return PERMISSION_MAP.get(mode or "", "ask")


#: The shape of a Claude record in `catalog-cache.json`; a cached record of another
#: version is read again. 2: the record carries `workspace` (`_workspace`), which
#: discovery and opening share (review of 3c1a34e, finding 7).
CLAUDE_RECORD_VERSION = 2


def _claude_record(path: Path, opener=transcripts.open_regular) -> dict:
    """What discovery and opening read of one Claude transcript copy, its
    `workspace` included: the cwd it continues from (`_workspace`, C-30.2), not
    only its first `cwd`, so the catalog shows and judges a moved session as
    `conversation.open` will open it."""
    title, first, cwd, model = None, None, None, None
    try:
        size = path.stat().st_size
        with opener(path, "rb") as stream:
            head = stream.read(HEAD)
            stream.seek(max(0, size - TAIL))
            tail = stream.read(TAIL)
    except OSError:
        return {}
    for raw in reversed(tail.splitlines()):
        if b'"custom-title"' in raw or b'"customTitle"' in raw:
            row = _object(raw)
            if row.get("type") == "custom-title" and row.get("customTitle") and isinstance(row["customTitle"], str):
                title = row["customTitle"]
                break
    for raw in head.splitlines():
        row = _object(raw)
        cwd = cwd or (row["cwd"] if isinstance(row.get("cwd"), str) else None)
        if row.get("type") == "user" and not row.get("isMeta") and not first:
            content = _object(row.get("message")).get("content")
            text = content if isinstance(content, str) else transcripts.text_of(transcripts.blocks(row.get("message")))
            if text and not text.startswith("<"):
                first = scrub(text.strip())[:PROMPT_CHARS]
    for raw in reversed(tail.splitlines()):
        if b'"assistant"' not in raw:
            continue
        row = _object(raw)
        m = _object(row.get("message")).get("model")
        if row.get("type") == "assistant" and m and isinstance(m, str) and m != "<synthetic>":
            model = m
            break
    mode = transcripts.last_permission_mode(path)
    headless = bool(transcripts.headless_transcript(path))
    return {"title": scrub(title)[:200] if title else None, "first_prompt": first, "cwd": cwd, "model": model,
            "permission_mode": mode, "headless": headless,
            "workspace": None if headless else _workspace(Path(path), cwd)}


def _object(value: Any) -> dict:
    """A transcript line (bytes) or a field of one as an object; anything else, a
    valid JSON line that is not an object included, as an empty one (review L2)."""
    if isinstance(value, bytes):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _codex_record(path: Path, opener=transcripts.open_regular) -> dict:
    try:
        with opener(path, "rb") as stream:
            head = stream.read(HEAD)
    except OSError:
        return {}
    meta, first, model = {}, None, None
    for raw in head.splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        payload = row.get("payload") or {}
        if row.get("type") == "session_meta" and not meta:
            meta = payload
        if row.get("type") == "turn_context" and payload.get("model"):
            model = payload["model"]
        if (row.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user"
                and not first):
            text = " ".join(c.get("text", "") for c in payload.get("content", []) if isinstance(c, dict)).strip()
            if text and not text.startswith("<"):
                first = scrub(text)[:PROMPT_CHARS]
    source = meta.get("source")
    excluded = source == "exec" or (isinstance(source, dict) and "subagent" in source) or \
        meta.get("originator") == "codex_exec"
    return {"id": meta.get("id"), "cwd": meta.get("cwd"), "first_prompt": first, "model": model,
            "excluded": bool(excluded), "originator": meta.get("originator")}


def build(root: Path, *, lanes: list[dict], claude_projects: Path | None = None, codex_app_home: Path | None = None,
          wall_s: float = WALL_S, clock=time.monotonic, may_write: Callable[[], bool] | None = None) -> dict:
    """One capped indexing run; returns the catalog it built. `may_write` is asked
    before each file is published, and once it says no nothing more is (`Owner`)."""
    root = Path(root)
    cache_path = root / "catalog-cache.json"
    try:
        cache = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        cache = {}
    started, complete, fresh = clock(), True, {}
    items: list[dict] = []
    # The run is its own process, capped at `wall_s` and stopped by its service after
    # 60 s (C-30.1), so it opens sessions plainly: a FIFO among them holds this run,
    # never the daemon. Readers in the daemon default to `transcripts.open_regular`.
    claude_record = functools.partial(_claude_record, opener=open)
    codex_record = functools.partial(_codex_record, opener=open)
    projects = claude_projects or transcripts.projects_dir()
    known_attempts = set(_attempt_native_ids(root))
    # Claude
    paths: list[Path] = []
    try:
        for d in os.scandir(projects):
            if d.is_dir():
                try:
                    paths.extend(Path(f.path) for f in os.scandir(d.path) if f.name.endswith(".jsonl"))
                except OSError:
                    continue
    except OSError:
        pass
    # One item per session: one that moved leaves a copy under each project
    # directory, and `conversation.open` continues the newest
    # (`transcripts.transcript_path`), so that is the copy shown (C-30.2).
    newest: dict[str, tuple[float, Path]] = {}
    for path in paths:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if path.stem not in newest or mtime > newest[path.stem][0]:
            newest[path.stem] = (mtime, path)
    for path in (path for _, path in newest.values()):
        record = _cached(cache, fresh, path, claude_record, started, wall_s, clock, version=CLAUDE_RECORD_VERSION)
        if record is None:
            complete = False
            continue
        sid = path.stem
        if record.get("headless") or sid in known_attempts or not record:
            continue
        cwd = record.get("workspace") or ""
        blocker = "tmp-workspace" if _temporary(cwd) else None
        items.append({"provider": "claude", "native_session_id": sid, "path": str(path), "home": None,
                      "title": record.get("title"), "first_prompt": record.get("first_prompt"), "cwd": cwd,
                      "model": record.get("model"), "permission_mode": record.get("permission_mode"),
                      "mtime": fresh[str(path)]["mtime"], "continuable": blocker is None,
                      "continue_blocker": blocker, "archived": False})
    # Codex
    names = _codex_names(codex_app_home or Path.home() / ".codex", opener=open)
    homes = [(Path(row["home"]), row["lane_id"]) for row in lanes if row.get("provider") == "codex" and row.get("home")]
    homes.append((codex_app_home or Path.home() / ".codex", None))
    for home, lane_id in homes:
        for sub, archived in (("sessions", False), ("archived_sessions", True)):
            base = home / sub
            if not base.is_dir():
                continue
            for path in base.rglob("rollout-*.jsonl"):
                record = _cached(cache, fresh, path, codex_record, started, wall_s, clock)
                if record is None:
                    complete = False
                    continue
                if not record or record.get("excluded") or not record.get("id") or record["id"] in known_attempts:
                    continue
                items.append({"provider": "codex", "native_session_id": record["id"], "path": str(path),
                              "home": str(home), "lane_id": lane_id,
                              "title": names.get(record["id"]), "first_prompt": record.get("first_prompt"),
                              "cwd": record.get("cwd"), "model": record.get("model"), "permission_mode": None,
                              "mtime": fresh[str(path)]["mtime"], "continuable": lane_id is not None,
                              "continue_blocker": None if lane_id else "codex-app thread: continue by handoff",
                              "archived": archived})
    live = _live_claude_sessions()
    for item in items:
        item["live_elsewhere"] = (item["provider"] == "claude"
                                  and canonical_native(item["native_session_id"]) in live)
    items.sort(key=lambda i: i["mtime"], reverse=True)
    # Every live session, listed or not: a conversation born in Subfleet and
    # resumed in a terminal is no catalog item but is still held (C-26.3).
    catalog = {"generated_at": _utc(), "complete": complete, "items": items, "live_claude": sorted(live)}
    for path, value in ((root / "catalog.json", catalog), (cache_path, fresh if complete else {**cache, **fresh})):
        if may_write is not None and not may_write():
            break
        _atomic(path, value)
    return catalog


def _cached(cache: dict, fresh: dict, path: Path, reader, started: float, wall_s: float, clock,
            version: int | None = None):
    """`reader(path)`, or its cached record while the file's size and mtime, and
    the record's `version`, are unchanged."""
    try:
        st = path.stat()
    except OSError:
        return {}
    key = str(path)
    hit = cache.get(key)
    if hit and hit.get("size") == st.st_size and hit.get("mtime") == st.st_mtime and hit.get("version") == version:
        fresh[key] = hit
        return hit["record"]
    if clock() - started > wall_s:
        return None
    record = reader(path)
    fresh[key] = {"size": st.st_size, "mtime": st.st_mtime, "record": record,
                  **({"version": version} if version is not None else {})}
    return record


def _codex_names(home: Path, opener=transcripts.open_regular) -> dict[str, str]:
    names: dict[str, str] = {}
    try:
        with opener(home / "session_index.jsonl", "r") as stream:
            index = stream.read()
        for raw in index.splitlines():
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if row.get("id") and row.get("thread_name"):
                names[row["id"]] = scrub(row["thread_name"])[:200]
    except OSError:
        pass
    return names


def _live_claude_sessions() -> set[str]:
    """Session ids a live Claude process outside Subfleet holds (IR-16), a UUID in
    lower case (`store.canonical_native`)."""
    from ..sessions import registry
    return {canonical_native(row.session_id) for row in registry.rows() if row.alive and _outside_claude(row)}


def external_writers(session_id: str) -> list[int]:
    """Pids of live Claude processes outside Subfleet that hold this session: its
    turns wait for them (C-26.3, design D-17). Read now, not from the catalog.

    Both ids are compared in one spelling (`store.canonical_native`): a store
    written before bindings were canonical keeps an upper-case UUID, while Claude
    Code registers the lower-case one, and either may be asked about."""
    from ..sessions import registry
    wanted = canonical_native(session_id)
    return sorted(row.pid for row in registry.rows()
                  if canonical_native(row.session_id) == wanted and row.alive and row.pid and _outside_claude(row))


def _outside_claude(row) -> bool:
    """A registry row's process is the one that wrote the row, and no Subfleet
    attempt started it (C-26.3). A row can outlive its process and the pid be
    reused: the row's `procStart` must equal the process's start (2.1.280 writes
    it as `TZ=UTC ps -o lstart=` prints it); a row without one needs a Claude
    executable. When `ps` cannot answer, the row holds its session: a second
    writer is worse than a wait."""
    if not row.pid:
        return False
    try:
        out = subprocess.run(["/bin/ps", "-p", str(row.pid), "-o", "lstart=,comm="], capture_output=True, text=True,
                             timeout=5, env={**os.environ, "TZ": "UTC"})
    except (OSError, subprocess.SubprocessError):
        return True
    line = out.stdout.strip()
    if not line:
        return out.returncode not in (0, 1)         # 1: no such process; else ps failed to answer
    started, comm = line[:24], line[24:].strip()
    if row.proc_start:
        if started != row.proc_start:
            return False                                                       # a reused pid
    elif "claude" not in os.path.basename(comm).lower():
        return False
    return not _subfleet_owned(row.pid)


def _subfleet_owned(pid: int | None) -> bool:
    """The process carries a Subfleet attempt's markers. Unknown (ps failed) is not owned."""
    if not pid:
        return False
    try:
        out = subprocess.run(["/bin/ps", "-Ewwp", str(pid), "-o", "command="], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "SUBFLEET_ATTEMPT=" in out


def _attempt_native_ids(root: Path) -> Iterable[str]:
    import sqlite3
    path = Path(root) / "state.sqlite3"
    if not path.exists():
        return []
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            return [r[0] for r in db.execute(
                "SELECT DISTINCT a.native_session_id FROM attempts a JOIN jobs j USING(job_id) "
                "WHERE a.native_session_id IS NOT NULL AND j.kind NOT IN ('revive','turn')")]
        finally:
            db.close()
    except sqlite3.Error:
        return []


def _utc() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _atomic(path: Path, value: Any) -> None:
    """Publish into the state root as it stands. Never create it: a run that outlived
    its owner recreated a root the owner had just removed (2026-09-25); now the
    temporary file cannot be made and the run fails instead."""
    from ..guardian import atomic_publish
    atomic_publish(path, (json.dumps(value, separators=(",", ":")) + "\n").encode())


class Owner:
    """Whether one run may still write (C-30.1). The lock it holds must still be
    `<root>/catalog.lock`, so the root was neither removed nor replaced since the
    run took it. The service that started the run must still be open: it holds the
    only write end of a pipe whose read end the run inherits as `fence_fd`, and
    closes it in `close()`; the kernel closes it if the daemon dies. End-of-file
    there means the owner is gone. A run started by hand has no fence."""

    def __init__(self, root: Path, lock_fd: int, fence_fd: int | None = None):
        self.lock_path, self.lock_fd, self.fence_fd = Path(root) / "catalog.lock", lock_fd, fence_fd
        self.gone = False                   # it said no: nothing more is published (OWNER_GONE)

    def __call__(self) -> bool:
        if not self.gone and not self._here():
            self.gone = True
        return not self.gone

    def _here(self) -> bool:
        try:
            here, held = os.stat(self.lock_path), os.fstat(self.lock_fd)
        except OSError:
            return False
        return (here.st_dev, here.st_ino) == (held.st_dev, held.st_ino) and fence_open(self.fence_fd)


def fence_open(fd: int | None) -> bool:
    """Whether the write end of the fence pipe `fd` reads from is still open. A
    non-blocking read: nobody ever writes, so it blocks while the end is open and
    returns end-of-file once it is closed. No fence: nothing to wait on."""
    if fd is None:
        return True
    try:
        os.set_blocking(fd, False)
        return os.read(fd, 1) != b""
    except BlockingIOError:
        return True
    except OSError:
        return False


def fence_is_pipe(fd: int) -> bool:
    """Whether `fd` is a pipe, as the owner's fence is (`FENCE_BROKEN` otherwise)."""
    try:
        return stat.S_ISFIFO(os.fstat(fd).st_mode)
    except OSError:
        return False


def fence_pipe() -> tuple[int, int]:
    """A new fence pipe for `Owner`, (read, write), both above 2 and close-on-exec.

    `os.pipe()` returns the lowest free descriptors. In an owner with 0, 1 or 2
    closed, a read end there is replaced in the run by its /dev/null standard streams
    (`spawn_refresh`), and the run reads end-of-file as if its owner had closed; a
    write end there would take whatever the owner writes to that stream."""
    raw = os.pipe()
    moved: list[int] = []
    try:
        for fd in raw:
            moved.append(fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 3))
    except OSError:
        for fd in moved:
            os.close(fd)
        raise
    finally:
        for fd in raw:
            os.close(fd)
    return moved[0], moved[1]


# --- readers the daemon uses ----------------------------------------------------


#: The last parse of each catalog file, by path: (file identity, catalog, state).
#: The app lists every 30 s and opens call here too; one parse per catalog run
#: keeps ~70k objects per call out of the daemon's garbage collector, whose
#: passes held the GIL for minutes while the machine was swapping (2026-09-25).
#: Callers treat the catalog and its items as read-only.
_PARSED: dict[str, tuple[tuple, dict, str]] = {}


def _load(path: Path) -> tuple[dict, str]:
    try:
        st = path.stat()
    except FileNotFoundError:
        return {}, "absent"
    except OSError:
        return {}, "unreadable"
    identity = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    hit = _PARSED.get(str(path))
    if hit and hit[0] == identity:
        return hit[1], hit[2]
    try:
        catalog, state = json.loads(path.read_text()), "fresh"
    except FileNotFoundError:
        return {}, "absent"
    except (OSError, ValueError):
        catalog, state = {}, "unreadable"
    if not isinstance(catalog, dict):
        catalog, state = {}, "unreadable"
    _PARSED[str(path)] = (identity, catalog, state)
    return catalog, state


def read_catalog(root: Path, *, query: str | None = None, exclude: set | None = None, limit: int = 200,
                 before: str | None = None, include_archived: bool = False, stale_after_s: float | None = None,
                 now: datetime | None = None) -> dict:
    """A page of `catalog.json`, and what it is worth (C-30.1, design D-23).

    `state` says whether the file is `absent` (no run has finished yet),
    `unreadable`, `stale` (older than `stale_after_s`) or `fresh`, with its age;
    a missing, damaged or old catalog never fails `conversation.list`.
    """
    catalog, state = _load(Path(root) / "catalog.json")
    generated_at = catalog.get("generated_at") if isinstance(catalog.get("generated_at"), str) else None
    age_s = None
    if generated_at:
        try:
            age_s = max(0.0, ((now or datetime.now(UTC)) - datetime.fromisoformat(
                generated_at.replace("Z", "+00:00"))).total_seconds())
        except ValueError:
            age_s = None
    if state == "fresh" and (age_s is None or (stale_after_s is not None and age_s > stale_after_s)):
        state = "stale"
    # A binding and a live registry row may spell a UUID in either case; the
    # catalog's own ids are transcript names, in lower case (C-26.3).
    exclude = {(provider, canonical_native(native)) for provider, native in (exclude or set())}
    needle = (query or "").lower().strip()
    out = []
    items = catalog.get("items") if isinstance(catalog.get("items"), list) else []
    # Every session a live process outside Subfleet held at the last run, bound
    # to a conversation or not. An old run says nothing about now.
    live: list[str] = []
    if state == "fresh":
        recorded = catalog.get("live_claude")
        live = sorted({canonical_native(x) for x in recorded if isinstance(x, str) and x}) \
            if isinstance(recorded, list) else sorted(
                {canonical_native(str(item["native_session_id"])) for item in items
                 if isinstance(item, dict) and item.get("live_elsewhere") and item.get("native_session_id")})
    for item in items:
        if not isinstance(item, dict) or item.get("provider") not in ("claude", "codex") \
                or not item.get("native_session_id"):
            continue
        if (item["provider"], canonical_native(item["native_session_id"])) in exclude:
            continue
        if item.get("archived") and not include_archived:
            continue
        if before is not None and str(item.get("mtime")) >= str(before):
            continue
        if needle and not any(needle in str(item.get(k) or "").lower() for k in ("title", "cwd", "first_prompt")):
            continue
        # An old run says nothing about which processes hold a session now.
        out.append(item if state == "fresh" else {**item, "live_elsewhere": False})
        if len(out) >= max(1, min(limit, 500)):
            break
    return {"generated_at": generated_at, "complete": bool(catalog.get("complete", False)), "items": out,
            "next": out[-1].get("mtime") if out and len(out) >= limit else None, "state": state,
            "age_s": None if age_s is None else round(age_s, 1), "stale_after_s": stale_after_s,
            "live_elsewhere": live}


def refresh_running(root: Path) -> bool | None:
    """Whether a catalog run holds the lock now; None when the lock cannot be read.
    A non-blocking probe: it never waits for a run."""
    lock = Path(root) / "catalog.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    except OSError:
        return None
    finally:
        os.close(fd)


def spawn_refresh(root: Path, *, fence_fd: int | None = None) -> subprocess.Popen | None:
    """Start one catalog run unless one is running (the lock decides), and return
    the process without waiting for it. Its owner reaps it (`Popen.poll`) and stops
    it on close. `fence_fd` is the read end of the owner's fence pipe, the one
    descriptor the run inherits (`Owner`), above 2 (`fence_pipe`)."""
    if fence_fd is not None and fence_fd <= 2:
        raise ValueError(f"fence descriptor {fence_fd} would be replaced by the run's standard streams")
    if refresh_running(root) is not False:
        return None
    package_root = str(Path(__file__).resolve().parent.parent.parent)
    env = {**os.environ, "PYTHONPATH": package_root}
    argv = [sys.executable, "-m", "subfleet.conversations.catalog", "--state-root", str(root)]
    if fence_fd is not None:
        argv += ["--fence-fd", str(fence_fd)]
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True, env=env, cwd=package_root,
                            pass_fds=() if fence_fd is None else (fence_fd,))


def native_session(provider: str, session_id: str, *, home: str | None, root: Path, lanes: list[dict]) -> dict | None:
    """One session's facts for `conversation.open` (reads that session's files only)."""
    if provider == "claude":
        path = transcripts.transcript_path(session_id)
        if path is None:
            return None
        return claude_session(path)
    homes =[(Path(r["home"]), r["lane_id"]) for r in lanes if r.get("provider") == "codex" and r.get("home")]
    for base, lane_id in homes + [(Path(home) if home else Path.home() / ".codex", None)]:
        matches = list((base / "sessions").rglob(f"rollout-*{session_id}.jsonl")) if (base / "sessions").is_dir() else []
        if len(matches) != 1:
            continue
        record = _codex_record(matches[0])
        if lane_id is None:
            return {"continuable": False, "continue_blocker": "codex-app thread: continue by handoff"}
        return {"cwd": record.get("cwd"), "title": _codex_names(Path.home() / ".codex").get(session_id)
                or record.get("first_prompt"), "model_value": record.get("model") or "",
                "permission": "read-only", "continuable": True, "lane_id": lane_id}
    return None


def claude_session(path: Path) -> dict:
    """One Claude transcript's facts for a conversation row: its cwd (`_workspace`),
    title, model value and D-9 permission, and whether it continues here (C-30.2,
    IR-15).

    `conversation.open` of a native session and the legacy cockpit import
    (C-30.4) create a conversation from exactly these facts.
    """
    record = _claude_record(path)
    if record.get("headless"):
        return {"continuable": False, "continue_blocker": "a Subfleet lane run"}
    cwd = record.get("workspace")
    if not cwd or not os.path.isdir(cwd):
        return {"continuable": False, "continue_blocker": "its working directory no longer exists"}
    if _temporary(cwd):
        return {"continuable": False, "continue_blocker": "tmp-workspace"}
    model = record.get("model") or ""
    return {"cwd": cwd, "title": record.get("title") or record.get("first_prompt"),
            "model_value": _claude_value(model), "permission": map_permission(record.get("permission_mode")),
            "permission_source": record.get("permission_mode"), "continuable": True, "lane_id": None}


def _workspace(path: Path, first: str | None) -> str | None:
    """The working directory a turn continues this transcript copy from.

    A session that moved to another worktree leaves a copy under each project
    directory, and `transcripts.transcript_path` picks the newest; that copy's
    first rows keep the old cwd and its last rows the new one (review L7). The
    directory the file is in names the cwd it belongs to, so the workspace is
    the latest cwd in the copy that names it: a session that moved and then
    `cd`s within its new project keeps the new project, one that `cd`s within
    its only project keeps that. When none names the directory (a copy directly
    under `projects/`), the first cwd, else the last, as before.
    """
    for line in transcripts.lines_reversed(path, chunk=TAIL):
        cwd = _object(line.encode()).get("cwd")
        if isinstance(cwd, str) and cwd and path.parent.name in _project_names(cwd):
            return cwd                    # the latest cwd that names this copy's directory
    if first and path.parent.name in _project_names(first):
        return first
    return first or transcripts.last_cwd(path)


def _temporary(cwd: str) -> bool:
    """A workspace under /tmp does not continue in place (C-30.2, IR-15): discovery
    and opening ask the same question of the same `workspace`."""
    return cwd.startswith(("/tmp/", "/private/tmp/"))


def _project_names(cwd: str) -> set[str]:
    """The names Claude Code may give `cwd`'s project directory: the Claude
    adapter's encoding (`adapters.claude.encode_project_dir`: `/`, `.`, `_` to
    `-`), and every other character but letters and digits to `-` as well."""
    return {re.sub(r"[/._]", "-", cwd), re.sub(r"[^A-Za-z0-9]", "-", cwd)}


def _claude_value(model_id: str) -> str:
    """A served Claude model id as the value a conversation's settings carry."""
    return model_id or "opus"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="subfleet-catalog")
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--fence-fd", type=int, help="the read end of the starting service's fence pipe (Owner)")
    args = parser.parse_args(argv)
    if args.fence_fd is not None and not fence_is_pipe(args.fence_fd):
        return FENCE_BROKEN                 # not its owner's fence: it cannot tell whether the owner is open
    if not fence_open(args.fence_fd):
        return OWNER_GONE                   # the service closed before the run began: touch nothing
    lock = args.state_root / "catalog.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    except FileNotFoundError:
        return OWNER_GONE                   # no state root, and a run never makes one
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0                            # another run holds the lock, and writes the catalog
    lanes = []
    try:
        import sqlite3
        db = sqlite3.connect(f"file:{args.state_root / 'state.sqlite3'}?mode=ro", uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        lanes = [dict(r) for r in db.execute("SELECT lane_id, provider, home FROM lanes")]
        db.close()
    except Exception:
        pass
    owner = Owner(args.state_root, fd, args.fence_fd)
    build(args.state_root, lanes=lanes, may_write=owner)
    return OWNER_GONE if owner.gone else 0


def _terminated(signum, frame):
    """A stop unwinds, so `atomic_publish` removes its temporary file on the way out
    rather than leaving it in the state root."""
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _terminated)
    raise SystemExit(main())
