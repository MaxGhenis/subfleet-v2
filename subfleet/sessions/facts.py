"""The sessions kit's durable facts, read one way by the daemon and by `doctor`.

The daemon answers `sessions state` from its store (C-23.31, C-23.33, C-23.35,
C-26.13). `doctor`'s offline table reads files only (C-17.5), so it builds the
same answer from `state.sqlite3` and `conversations.sqlite3`, opened read-only.
The queries live here so the two answers cannot drift. Each function takes a
`query(sql, params) -> rows` callable whose rows index by column name: the
daemon's `Store.query`, or `query_of` over a read-only connection.
"""

from __future__ import annotations

import errno
import json
import sqlite3
import stat
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

#: The sessions kit's durable facts, as `events` kinds (C-23.33, C-23.35). They
#: are events rather than a table because each is an append-only record of one
#: operator or worker decision, and the latest row for a session is the answer.
NUDGE_EVENT = "session.nudged"
REVIVE_EVENT = "session.revived"
RETIRE_EVENT = "session.retired"
UNRETIRE_EVENT = "session.unretired"
SESSION_KINDS = (NUDGE_EVENT, REVIVE_EVENT, RETIRE_EVENT, UNRETIRE_EVENT)

Query = Callable[[str, Sequence[Any]], Iterable[Mapping[str, Any]]]


def revive_lease_key(session_id: str) -> str:
    """C-23.55: one live revive per session; see `daemon.revive_lease_key`."""
    return f"session:{session_id}:revive"


def session_events(query: Query, kinds: tuple[str, ...],
                   session_ids: set[str] | None) -> dict[str, dict]:
    """The newest event of each kind per session id, keyed `<kind>:<id>`.

    `event_id` rides along because the store stamps `ts` to the second, and
    retiring and unretiring a session inside one second is a thing an operator
    does; the row order is the only tiebreak that is always right.
    """
    marks = ",".join("?" for _ in kinds)
    latest: dict[str, dict] = {}
    # C-3.7: SQLite picks the rows. For named sessions, only theirs; for all,
    # only the newest per kind and session. The loop below still applies every
    # rule. A payload json_valid refuses (a NaN or an Infinity, which json.dumps
    # writes and json.loads reads, or no JSON at all) cannot be filtered in SQL,
    # so every such row of these kinds rides along (`events_not_json`, normally
    # none) and the loop decides it. CASE, not a WHERE term, keeps json_extract
    # off a payload json_valid refuses: SQLite does not promise to test WHERE
    # terms in written order.
    session = "CASE WHEN json_valid(data_json) THEN json_extract(data_json,'$.session_id') END"
    unread = (f"UNION ALL SELECT event_id,kind,ts,data_json FROM events WHERE kind IN ({marks}) "
              "AND NOT json_valid(data_json) ")
    if session_ids is not None:
        if not session_ids:
            return latest
        wanted = sorted(session_ids)
        sql = (f"SELECT event_id,kind,ts,data_json FROM events WHERE kind IN ({marks}) "
               f"AND json_valid(data_json) AND {session} IN ({','.join('?' for _ in wanted)}) "
               + unread + "ORDER BY event_id DESC")
        params: tuple[Any, ...] = (*kinds, *wanted, *kinds)
    else:
        sql = (f"SELECT event_id,kind,ts,data_json FROM events WHERE event_id IN "
               f"(SELECT max(event_id) FROM events WHERE kind IN ({marks}) AND json_valid(data_json) "
               f"GROUP BY kind,{session}) " + unread + "ORDER BY event_id DESC")
        params = (*kinds, *kinds)
    for row in query(sql, params):
        try:
            data = json.loads(row["data_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        sid = data.get("session_id")
        if not isinstance(sid, str) or (session_ids is not None and sid not in session_ids):
            continue
        latest.setdefault(f"{row['kind']}:{sid}",
                          {**data, "at": row["ts"], "event_id": row["event_id"]})
    return latest


def lane_session_ids(query: Query) -> list[str]:
    """Every session id subfleet itself CREATED as a headless lane (C-23.31).

    A revive's attempt records the session it continued, not one it created, so
    counting it would retire a revived session from the fleet for good. A turn
    is left out too: a conversation opened on an existing session runs its turns
    with `--resume <that session>`, and a turn's session is a conversation's,
    reported apart as `conversation_sessions` (C-26.13). Every other kind
    launches under a `--session-id` the daemon minted.
    """
    return sorted({row["native_session_id"] for row in query(
        "SELECT DISTINCT a.native_session_id FROM attempts a "
        "JOIN jobs j USING(job_id) "
        "WHERE a.native_session_id IS NOT NULL AND j.kind NOT IN ('revive','turn')", ())
        if row["native_session_id"]})


def conversation_session_ids(query: Query, bound: Iterable[str]) -> list[str]:
    """C-26.13: every session a conversation binds or a turn job ran.

    Both halves are needed. A conversation records its session only when its
    first turn settles, so until then the turn attempt is the only record of
    it; and a conversation opened on an existing session binds it before any
    turn has run. Nothing deletes a conversation row, so a bound session stays
    the conversation's. Each UUID is also listed in lower case, the spelling
    Claude Code names its transcript with (review L1).
    """
    from ..conversations.store import canonical_native
    ids = {row["native_session_id"] for row in query(
        "SELECT DISTINCT a.native_session_id FROM attempts a "
        "JOIN jobs j USING(job_id) "
        "WHERE a.native_session_id IS NOT NULL AND j.kind='turn'", ())}
    ids |= {item for item in bound if isinstance(item, str)}
    ids |= {canonical_native(item) for item in ids if item}
    return sorted(item for item in ids if item)


def bound_sessions(query: Query) -> set[str]:
    """C-26.13: every native session id a conversation binds, archived or not."""
    return {row["native_session_id"] for row in query(
        "SELECT native_session_id FROM conversations WHERE native_session_id IS NOT NULL", ())
        if row["native_session_id"]}


def state(query: Query, session_ids: set[str] | None, *, bound: Iterable[str]) -> dict[str, Any]:
    """The `sessions state` answer (C-23.33, C-23.35, C-23.55, C-26.13)."""
    latest = session_events(query, SESSION_KINDS, session_ids)
    leases = {row["lease_key"]: row["holder"] for row in
              query("SELECT lease_key,holder FROM leases WHERE lease_key LIKE 'session:%:revive'", ())}
    answer: dict[str, dict] = {}
    for session in sorted(session_ids or {key.split(":", 1)[1] for key in latest}):
        retired = latest.get(f"{RETIRE_EVENT}:{session}")
        cleared = latest.get(f"{UNRETIRE_EVENT}:{session}")
        # Retirement is durable until the operator clears it, and both halves
        # are append-only, so the later ROW wins (C-23.35), by event_id.
        if retired and cleared and cleared["event_id"] > retired["event_id"]:
            retired = None
        answer[session] = {
            "retired": retired,
            "last_nudge": latest.get(f"{NUDGE_EVENT}:{session}"),
            "last_revive": latest.get(f"{REVIVE_EVENT}:{session}"),
            "revive_holder": leases.get(revive_lease_key(session)),
        }
    # C-26.3, D-17: listed, never nudged, revived or cold-swept.
    return {"sessions": answer, "lane_sessions": lane_session_ids(query),
            "conversation_sessions": conversation_session_ids(query, bound)}


def query_of(connection: sqlite3.Connection) -> Query:
    """A `Query` over one connection whose rows are `sqlite3.Row`."""
    def run(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return connection.execute(sql, params).fetchall()
    return run


def offline_state(root: str | Path, *, timeout_s: float = 5.0) -> dict[str, Any]:
    """The `sessions state` answer from the stores on disk, read-only (C-17.5).

    Raises `offline.OfflineUnavailable` when there is no readable main store,
    and `OSError` or `sqlite3.Error` when the conversation store exists and
    cannot be read: an unreadable list of conversations is not an empty one,
    because a conversation's session read as free would be a second writer
    (C-26.13). No conversation store at all is a fresh install with none.
    """
    from ..offline import Offline
    base = Path(root).expanduser()
    path = base / "conversations.sqlite3"
    bound: set[str] = set()
    try:
        info = path.stat()
    except FileNotFoundError:
        info = None
    if info is not None:
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=timeout_s,
                             isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            bound = bound_sessions(query_of(db))
        finally:
            db.close()
    with Offline(base).reading() as connection:
        return state(query_of(connection), None, bound=bound)


__all__ = ["NUDGE_EVENT", "REVIVE_EVENT", "RETIRE_EVENT", "UNRETIRE_EVENT", "SESSION_KINDS",
           "bound_sessions", "conversation_session_ids", "lane_session_ids", "offline_state",
           "query_of", "revive_lease_key", "session_events", "state"]
