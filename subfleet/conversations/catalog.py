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
files for `conversation.open`.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from ..sessions import transcripts
from .redact import scrub

WALL_S = 20.0
TAIL = 256 * 1024
HEAD = 64 * 1024
PROMPT_CHARS = 160
PERMISSION_MAP = {"bypassPermissions": "bypass", "acceptEdits": "accept-edits", "default": "ask",
                  "manual": "ask", "plan": "read-only", "dontAsk": "read-only"}


def map_permission(mode: str | None) -> str:
    """Design D-9's total mapping of a recorded Claude mode; anything else is `ask`."""
    return PERMISSION_MAP.get(mode or "", "ask")


def _claude_record(path: Path) -> dict:
    title, first, cwd, model = None, None, None, None
    try:
        size = path.stat().st_size
        with open(path, "rb") as stream:
            head = stream.read(HEAD)
            stream.seek(max(0, size - TAIL))
            tail = stream.read(TAIL)
    except OSError:
        return {}
    for raw in reversed(tail.splitlines()):
        if b'"custom-title"' in raw or b'"customTitle"' in raw:
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if row.get("type") == "custom-title" and row.get("customTitle"):
                title = row["customTitle"]
                break
    for raw in head.splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        cwd = cwd or row.get("cwd")
        if row.get("type") == "user" and not row.get("isMeta") and not first:
            content = (row.get("message") or {}).get("content")
            text = content if isinstance(content, str) else transcripts.text_of(transcripts.blocks(row.get("message")))
            if text and not text.startswith("<"):
                first = scrub(text.strip())[:PROMPT_CHARS]
    for raw in reversed(tail.splitlines()):
        if b'"assistant"' not in raw:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        m = (row.get("message") or {}).get("model")
        if row.get("type") == "assistant" and m and m != "<synthetic>":
            model = m
            break
    mode = transcripts.last_permission_mode(path)
    return {"title": scrub(title)[:200] if title else None, "first_prompt": first, "cwd": cwd, "model": model,
            "permission_mode": mode, "headless": bool(transcripts.headless_transcript(path))}


def _codex_record(path: Path) -> dict:
    try:
        with open(path, "rb") as stream:
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
          wall_s: float = WALL_S, clock=time.monotonic) -> dict:
    """One capped indexing run; returns the catalog it wrote."""
    root = Path(root)
    cache_path = root / "catalog-cache.json"
    try:
        cache = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        cache = {}
    started, complete, fresh = clock(), True, {}
    items: list[dict] = []
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
    for path in paths:
        record = _cached(cache, fresh, path, _claude_record, started, wall_s, clock)
        if record is None:
            complete = False
            continue
        sid = path.stem
        if record.get("headless") or sid in known_attempts or not record:
            continue
        cwd = record.get("cwd") or ""
        blocker = "tmp-workspace" if cwd.startswith(("/tmp/", "/private/tmp/")) else None
        items.append({"provider": "claude", "native_session_id": sid, "path": str(path), "home": None,
                      "title": record.get("title"), "first_prompt": record.get("first_prompt"), "cwd": cwd,
                      "model": record.get("model"), "permission_mode": record.get("permission_mode"),
                      "mtime": fresh[str(path)]["mtime"], "continuable": blocker is None,
                      "continue_blocker": blocker, "archived": False})
    # Codex
    names = _codex_names(codex_app_home or Path.home() / ".codex")
    homes = [(Path(row["home"]), row["lane_id"]) for row in lanes if row.get("provider") == "codex" and row.get("home")]
    homes.append((codex_app_home or Path.home() / ".codex", None))
    for home, lane_id in homes:
        for sub, archived in (("sessions", False), ("archived_sessions", True)):
            base = home / sub
            if not base.is_dir():
                continue
            for path in base.rglob("rollout-*.jsonl"):
                record = _cached(cache, fresh, path, _codex_record, started, wall_s, clock)
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
        item["live_elsewhere"] = item["provider"] == "claude" and item["native_session_id"] in live
    items.sort(key=lambda i: i["mtime"], reverse=True)
    # Every live session, listed or not: a conversation born in Subfleet and
    # resumed in a terminal is no catalog item but is still held (C-26.3).
    catalog = {"generated_at": _utc(), "complete": complete, "items": items, "live_claude": sorted(live)}
    _atomic(root / "catalog.json", catalog)
    _atomic(cache_path, fresh if complete else {**cache, **fresh})
    return catalog


def _cached(cache: dict, fresh: dict, path: Path, reader, started: float, wall_s: float, clock):
    try:
        st = path.stat()
    except OSError:
        return {}
    key = str(path)
    hit = cache.get(key)
    if hit and hit.get("size") == st.st_size and hit.get("mtime") == st.st_mtime:
        fresh[key] = hit
        return hit["record"]
    if clock() - started > wall_s:
        return None
    record = reader(path)
    fresh[key] = {"size": st.st_size, "mtime": st.st_mtime, "record": record}
    return record


def _codex_names(home: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    try:
        for raw in (home / "session_index.jsonl").read_text().splitlines():
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
    """Session ids a live Claude process outside Subfleet holds (IR-16)."""
    from ..sessions import registry
    return {row.session_id for row in registry.rows() if row.alive and _outside_claude(row.pid)}


def external_writers(session_id: str) -> list[int]:
    """Pids of live Claude processes outside Subfleet that hold this session: its
    turns wait for them (C-26.3, design D-17). Read now, not from the catalog."""
    from ..sessions import registry
    return sorted(row.pid for row in registry.rows()
                  if row.session_id == session_id and row.alive and row.pid and _outside_claude(row.pid))


def _outside_claude(pid: int | None) -> bool:
    """A registry row's pid is a running Claude executable that no Subfleet attempt
    started. A registry file can outlive its process and the pid be reused by an
    unrelated one, which must not hold a session."""
    if not pid:
        return False
    try:
        comm = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    if "claude" not in os.path.basename(comm).lower():
        return False
    return not _subfleet_owned(pid)


def _subfleet_owned(pid: int | None) -> bool:
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
    from ..guardian import atomic_publish
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_publish(path, (json.dumps(value, separators=(",", ":")) + "\n").encode())


# --- readers the daemon uses ----------------------------------------------------


def read_catalog(root: Path, *, query: str | None = None, exclude: set | None = None, limit: int = 200,
                 before: str | None = None, include_archived: bool = False, stale_after_s: float | None = None,
                 now: datetime | None = None) -> dict:
    """A page of `catalog.json`, and what it is worth (C-30.1, design D-23).

    `state` says whether the file is `absent` (no run has finished yet),
    `unreadable`, `stale` (older than `stale_after_s`) or `fresh`, with its age;
    a missing, damaged or old catalog never fails `conversation.list`.
    """
    try:
        catalog = json.loads((Path(root) / "catalog.json").read_text())
        state = "fresh"
    except FileNotFoundError:
        catalog, state = {}, "absent"
    except (OSError, ValueError):
        catalog, state = {}, "unreadable"
    if not isinstance(catalog, dict):
        catalog, state = {}, "unreadable"
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
    exclude = exclude or set()
    needle = (query or "").lower().strip()
    out = []
    items = catalog.get("items") if isinstance(catalog.get("items"), list) else []
    # Every session a live process outside Subfleet held at the last run, bound
    # to a conversation or not. An old run says nothing about now.
    live: list[str] = []
    if state == "fresh":
        recorded = catalog.get("live_claude")
        live = sorted({str(x) for x in recorded if isinstance(x, str) and x}) if isinstance(recorded, list) else sorted(
            {str(item["native_session_id"]) for item in items
             if isinstance(item, dict) and item.get("live_elsewhere") and item.get("native_session_id")})
    for item in items:
        if not isinstance(item, dict) or item.get("provider") not in ("claude", "codex") \
                or not item.get("native_session_id"):
            continue
        if (item["provider"], item["native_session_id"]) in exclude:
            continue
        if item.get("archived") and not include_archived:
            continue
        if before is not None and str(item.get("mtime")) >= str(before):
            continue
        if needle and not any(needle in str(item.get(k) or "").lower() for k in ("title", "cwd", "first_prompt")):
            continue
        out.append(item)
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


def spawn_refresh(root: Path) -> subprocess.Popen | None:
    """Start one catalog run unless one is running (the lock decides), and return
    the process without waiting for it. Its owner reaps it (`Popen.poll`)."""
    if refresh_running(root) is not False:
        return None
    package_root = str(Path(__file__).resolve().parent.parent.parent)
    env = {**os.environ, "PYTHONPATH": package_root}
    return subprocess.Popen([sys.executable, "-m", "subfleet.conversations.catalog", "--state-root", str(root)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True, env=env, cwd=package_root)


def request_refresh(root: Path) -> dict:
    """Start one catalog run unless one is running (a lock file decides)."""
    process = spawn_refresh(root)
    return {"requested": process is not None, "running": process is not None or bool(refresh_running(root))}


def native_session(provider: str, session_id: str, *, home: str | None, root: Path, lanes: list[dict]) -> dict | None:
    """One session's facts for `conversation.open` (reads that session's files only)."""
    if provider == "claude":
        path = transcripts.transcript_path(session_id)
        if path is None:
            return None
        record = _claude_record(path)
        if record.get("headless"):
            return {"continuable": False, "continue_blocker": "a Subfleet lane run"}
        cwd = record.get("cwd") or transcripts.last_cwd(path)
        if not cwd or not os.path.isdir(cwd):
            return {"continuable": False, "continue_blocker": "its working directory no longer exists"}
        if cwd.startswith(("/tmp/", "/private/tmp/")):
            return {"continuable": False, "continue_blocker": "tmp-workspace"}
        model = record.get("model") or ""
        return {"cwd": cwd, "title": record.get("title") or record.get("first_prompt"),
                "model_value": _claude_value(model), "permission": map_permission(record.get("permission_mode")),
                "permission_source": record.get("permission_mode"), "continuable": True, "lane_id": None}
    homes = [(Path(r["home"]), r["lane_id"]) for r in lanes if r.get("provider") == "codex" and r.get("home")]
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


def _claude_value(model_id: str) -> str:
    """A served Claude model id as the value a conversation's settings carry."""
    return model_id or "opus"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="subfleet-catalog")
    parser.add_argument("--state-root", type=Path, required=True)
    args = parser.parse_args(argv)
    lock = args.state_root / "catalog.lock"
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0
    lanes = []
    try:
        import sqlite3
        db = sqlite3.connect(f"file:{args.state_root / 'state.sqlite3'}?mode=ro", uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        lanes = [dict(r) for r in db.execute("SELECT lane_id, provider, home FROM lanes")]
        db.close()
    except Exception:
        pass
    build(args.state_root, lanes=lanes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
