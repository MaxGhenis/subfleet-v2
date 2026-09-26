"""Claude Code's session registry: which row speaks, and what is not a session.

Every interactive Claude Code session registers `~/.claude/sessions/<pid>.json`
holding `sessionId`, `pid`, `name`, `cwd`, `startedAt`, and
`messagingSocketPath`. The file is keyed by **pid**, not by session id, so a
session that the desktop app restarted after an account switch leaves its old
row behind for a while and two rows name one session.

C-23.30 says which of them speaks: the row whose recorded pid is live, then the
one whose socket is present, then the newest by start time. This module never
kills or deregisters the loser — killing a process it did not launch is a far
larger blast radius than a missed notice (`docs/reports/D-surface.md` §4) — but
it does report both, because a *duplicate live instance* of one session id is
the 2026-09-04 amend war and the operator has to see it.

C-23.31 says what is not a session at all: a headless lane run. It is never a
`ping` or notice target, never appears in a listing unless lanes are explicitly
included, and is never revived or continued; a request naming one is refused
with the reason. Two signals identify one, and either is enough:

* the **recorded lane marker** — the daemon's own `attempts.native_session_id`,
  handed in by the caller as `lane_ids`, which is authoritative for every lane
  run subfleet launched; and
* the **transcript shape** (`transcripts.headless_transcript`), which catches a
  `claude -p` run subfleet did not launch and lane runs whose attempt row the
  ledger no longer holds.

`find_session` in `subfleet/notify_push.py` already ranks duplicates this way
for the delivery layer. This module is the listing and refusal side of the same
rule; both read the same files and neither writes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import transcripts


def sessions_dir() -> Path:
    return transcripts.claude_dir() / "sessions"


def _pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                 # someone else's process, but a process
    except OSError:
        return False
    return True


def _is_socket(path: str | None) -> bool:
    if not path:
        return False
    try:
        return Path(path).is_socket()
    except OSError:
        return False


@dataclass(frozen=True)
class SessionRow:
    """One `~/.claude/sessions/<pid>.json`, as read."""

    session_id: str
    pid: int | None
    socket: str | None
    name: str | None
    cwd: str | None
    started_at: float | None
    alive: bool
    socket_present: bool
    registry_path: str

    @property
    def rank(self) -> tuple[bool, bool, float]:
        """C-23.30's order: live pid, then present socket, then newest start."""
        return (self.alive, self.socket_present, self.started_at or 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "pid": self.pid,
                "socket": self.socket, "name": self.name, "cwd": self.cwd,
                "started_at": self.started_at, "alive": self.alive,
                "socket_present": self.socket_present,
                "registry_path": self.registry_path}


def _row(path: Path) -> SessionRow | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("sessionId"), str):
        return None
    if not data["sessionId"]:
        return None
    pid = data.get("pid") if isinstance(data.get("pid"), int) else None
    sock = data.get("messagingSocketPath")
    sock = sock if isinstance(sock, str) else None
    started = data.get("startedAt")
    return SessionRow(
        session_id=data["sessionId"],
        pid=pid,
        socket=sock,
        name=data.get("name") if isinstance(data.get("name"), str) else None,
        cwd=data.get("cwd") if isinstance(data.get("cwd"), str) else None,
        started_at=float(started) if isinstance(started, (int, float)) else None,
        alive=_pid_alive(pid),
        socket_present=_is_socket(sock),
        registry_path=str(path),
    )


def rows(directory: Path | None = None) -> list[SessionRow]:
    """Every readable registry row, oldest file first for a stable listing.

    `directory` names another registry (the legacy import's `--claude-dir`); by
    default it is `sessions_dir()`.
    """
    try:
        paths = sorted((directory if directory is not None else sessions_dir()).glob("*.json"))
    except OSError:
        return []
    found = [_row(path) for path in paths]
    return [row for row in found if row is not None]


def grouped(all_rows: Iterable[SessionRow] | None = None) -> dict[str, list[SessionRow]]:
    """Rows by session id, each list ranked best-first (C-23.30)."""
    groups: dict[str, list[SessionRow]] = {}
    for row in (rows() if all_rows is None else all_rows):
        groups.setdefault(row.session_id, []).append(row)
    for value in groups.values():
        value.sort(key=lambda row: row.rank, reverse=True)
    return groups


def speaker(candidates: Iterable[SessionRow]) -> SessionRow | None:
    """The row that speaks for a session id (C-23.30)."""
    ranked = sorted(candidates, key=lambda row: row.rank, reverse=True)
    return ranked[0] if ranked else None


@dataclass(frozen=True)
class Session:
    """A session as the kit sees it: one speaking row plus everything it shadows."""

    session_id: str
    row: SessionRow
    others: tuple[SessionRow, ...] = ()
    lane: bool = False

    @property
    def pid(self) -> int | None:
        return self.row.pid

    @property
    def alive(self) -> bool:
        return self.row.alive

    @property
    def live_pids(self) -> tuple[int, ...]:
        """Every live pid registered under this session id, best-ranked first."""
        return tuple(row.pid for row in (self.row, *self.others)
                     if row.alive and row.pid is not None)

    @property
    def duplicate(self) -> bool:
        """C-23.30 and the 2026-09-04 amend war: two live instances of one id."""
        return len(self.live_pids) > 1

    def to_dict(self) -> dict[str, Any]:
        return {**self.row.to_dict(), "lane": self.lane,
                "duplicate": self.duplicate, "live_pids": list(self.live_pids),
                "shadowed": [row.to_dict() for row in self.others]}


def is_lane_run(session_id: str, *, lane_ids: Iterable[str] = (),
                transcript: str | Path | None = None) -> bool:
    """C-23.31: a headless lane run is not a session.

    The recorded marker wins outright; the transcript shape is the fallback for
    a `claude -p` run subfleet did not launch, or one whose attempt row the
    ledger has already reaped.
    """
    if session_id and session_id in set(lane_ids):
        return True
    if transcript is None:
        transcript = transcripts.transcript_path(session_id)
    return transcripts.headless_transcript(transcript)


def sessions(*, lane_ids: Iterable[str] = (),
             include_lanes: bool = False,
             live_only: bool = True) -> list[Session]:
    """The session listing (C-23.30, C-23.31).

    `live_only` keeps the rows whose pid is still running, which is what a nudge
    or a `ping` can reach. Lane runs are excluded unless `include_lanes`, and
    when they are included they are marked rather than silently mixed in.
    """
    marker = set(lane_ids)
    listing: list[Session] = []
    for session_id, candidates in grouped().items():
        best = speaker(candidates)
        if best is None:
            continue
        if live_only and not any(row.alive for row in candidates):
            continue
        lane = is_lane_run(session_id, lane_ids=marker)
        if lane and not include_lanes:
            continue
        others = tuple(row for row in candidates if row is not best)
        listing.append(Session(session_id=session_id, row=best, others=others, lane=lane))
    listing.sort(key=lambda item: (not item.row.alive,
                                   -(item.row.started_at or 0.0), item.session_id))
    return listing


def find(session_id: str, *, lane_ids: Iterable[str] = ()) -> Session | None:
    """One session by id, including a session whose every row is dead."""
    candidates = grouped().get(session_id)
    if not candidates:
        return None
    best = speaker(candidates)
    if best is None:
        return None
    return Session(session_id=session_id, row=best,
                   others=tuple(row for row in candidates if row is not best),
                   lane=is_lane_run(session_id, lane_ids=lane_ids))


def duplicate_report(listing: Iterable[Session]) -> list[str]:
    """One line per session id with more than one live instance, naming both pids.

    The plan does not claim a notice stops the second instance's writes, which is
    why the daemon also refuses a second writable job for a session id already
    running from another instance (C-6.5). This is the operator-visible half.
    """
    lines = []
    for item in listing:
        if item.duplicate:
            pids = ", ".join(str(pid) for pid in item.live_pids)
            lines.append(f"{item.session_id}: {len(item.live_pids)} live instances "
                         f"(pids {pids}) — one nudge was sent, to pid {item.pid}")
    return lines
