"""The daemon's durable SQLite store (C-3).

Transactions are deliberately synchronous and short. Callers perform process,
network, and filesystem work before entering a transaction, then atomically
record the resulting state. The one writing connection is serialized across
workers by the store lock.

C-3.7: a store opened with `readers` also keeps that many read-only
connections. A read made outside a transaction (`query`, `one`) takes one of
them instead of the store lock, so no read waits for a writer and readers run
side by side (SQLite releases the GIL while a statement steps). A read made
by a thread that holds the store lock goes to the writing connection, so a
transaction still sees its own uncommitted rows. `snapshot()` gives a block of
reads one committed state without the store lock.

The read connections are a pool with two rules (review of 5841d8b, finding 2):
snapshots hold at most all but `STATEMENT_RESERVE` of them, so a one-statement
read (a hook, `ping`, the wait hub, the control loop) always has a connection
no snapshot can take; and no read waits for one longer than `read_wait_s`:
after that it opens a connection of its own for that read alone, and every
wait is reported (rate-limited) through the store lock's `LockWatch`.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .contracts import Closure, Credential, Decision, IdentityStatus, Lane, LaneOwner, Reading
from .lockwatch import WatchedLock, thread_name

SCHEMA_VERSION = 6
Row = dict[str, Any]

# Canonical observations use the partial time index; offset/fractional clocks
# use the parsed-time expression index. Both UNION branches search time bounds.
WEEKLY_HISTORY_SQL = (
    "SELECT * FROM readings WHERE window='seven_day' AND label IN ('provider','stale-provider') "
    "AND observed_at BETWEEN ? AND ? AND observed_at GLOB ? "
    "UNION ALL SELECT * FROM readings WHERE window='seven_day' "
    "AND label IN ('provider','stale-provider') AND NOT observed_at GLOB ? "
    "AND julianday(observed_at) BETWEEN julianday(?) AND julianday(?)"
)


def weekly_history_params(now: str | datetime | None = None) -> tuple[str, ...]:
    from .quota_projection import instant
    at = instant(now) if now is not None else datetime.now(timezone.utc)
    end = at.isoformat(timespec="seconds").replace("+00:00", "Z")
    start = (at - timedelta(hours=24)).isoformat(timespec="seconds").replace("+00:00", "Z")
    return start, end, Store.CANONICAL_TS, Store.CANONICAL_TS, start, at.isoformat()

#: C-3.7: read connections no snapshot may hold, kept for one-statement reads.
STATEMENT_RESERVE = 2
#: C-3.7: the longest a read waits for a pooled connection before it opens one
#: of its own for that read alone (closed after it).
READ_WAIT_S = 1.0
#: C-3.7: how long `close` waits for reads in progress before it closes the idle
#: connections; a read still going then closes its own when it ends.
CLOSE_WAIT_S = 5.0
#: C-3.7: the write-ahead log file is cut back to this size each time SQLite
#: restarts the log, so a backlog one long reader built up does not stay on
#: disk. SQLite's automatic checkpoints are PASSIVE: they never wait for a
#: reader, only stop short of the oldest snapshot still open.
WAL_SIZE_LIMIT = 64 * 1024 * 1024

#: C-3.1: migrations are additive and numbered. Each entry is the statements that
#: carry a database from `n - 1` to `n`; `store_schema.sql` always describes the
#: newest version, so a fresh database never runs one.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    # C-10.6, C-1.4: bind a Claude lane to the identity its own credential
    # reports, with the email kept beside it as a label and never as a key.
    4: (
        "ALTER TABLE lanes ADD COLUMN identity TEXT",
        "ALTER TABLE lanes ADD COLUMN label TEXT",
        "ALTER TABLE lanes ADD COLUMN identity_status TEXT CHECK (identity_status IS NULL "
        "OR identity_status IN ('verified','enrolled','mismatch','unverified'))",
    ),
    # A per-job operator authorization; old jobs retain no authorization.
    5: ("ALTER TABLE jobs ADD COLUMN unmeasured_reserve_reason TEXT",),
    # C-12.9, d714: the MCP servers a job named; an older job named none.
    6: ("ALTER TABLE jobs ADD COLUMN mcp_servers TEXT NOT NULL DEFAULT '[]'",),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _pin_notice_key(data_json: str | None) -> tuple | None:
    """C-11.8: (id, session, creation time) of the service notice a `job.pin_noticed` event names."""
    try:
        data = json.loads(data_json or "{}")
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("service_notice_id"), int):
        return None
    return data["service_notice_id"], data.get("session_id"), data.get("created_at")


#: C-11.8: the expression `events_pin_notice` indexes, character for character.
PIN_NOTICE_ID = "(CASE WHEN json_valid(data_json) THEN json_extract(data_json,'$.service_notice_id') END)"


def pin_notice_jobs(query: Callable[[str, Sequence[Any]], Iterable[Any]],
                    rows: Iterable[Mapping[str, Any]]) -> dict[int, str]:
    """C-11.8, C-15.2: service notice id -> the job a pin's notice is about, from its
    `job.pin_noticed` event. `rows` are service notice rows with their stored
    (positive) ids. A row matches its event by id, session and creation time
    together: a service notice's id is reused once the row with the highest id is
    deleted (it has no AUTOINCREMENT), and a ping, a nudge or an alert that gets an
    old pin notice's id names none."""
    wanted = {(row["notice_id"], row["session_id"], row["created_at"]): row["notice_id"] for row in rows}
    found: dict[int, str] = {}
    ids = sorted({key[0] for key in wanted})
    # Exactly the events that name one of these notices, newest first, with no
    # window (`events_pin_notice`): events naming no notice (a pin with no one to
    # tell, C-15.8, and every event's empty audit row) once crowded a deliverable
    # notice's event out of the newest 1,000 and left that notice unnamed. The
    # CASE keeps json_extract off a payload json_valid refuses, since SQLite does
    # not promise to test WHERE terms in written order. Such a row names no notice:
    # a pin record holds strings, ints and lists of strings, never a float, so it
    # is never a NaN or Infinity payload that json.loads would read and json_valid
    # refuse. No ORDER BY: it would steer the planner to `events_kind`, which
    # walks every pin event; the few matches are ordered here, newest first.
    events = []
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        events += query("SELECT event_id,job_id,data_json FROM events WHERE kind='job.pin_noticed' AND "
                        f"{PIN_NOTICE_ID} IN ({','.join('?' * len(chunk))})", chunk)
    for event in sorted(events, key=lambda event: event["event_id"], reverse=True):
        key = _pin_notice_key(event["data_json"])
        if key in wanted and event["job_id"]:
            found.setdefault(wanted[key], event["job_id"])
    return found


def notice_fingerprint(row: Mapping[str, Any]) -> str:
    """C-15.8: what a listed notice is beyond its id: its creation time and a digest
    of its text. A notice's id is reused once the newest row is deleted (neither
    notice table has AUTOINCREMENT), so `--ack` and `--withdraw` act on a row only
    while it still has the fingerprint the listing showed. Two rows alike in id,
    session, creation second and text are one notice to the session that reads it."""
    digest = hashlib.sha256(str(row["text"]).encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return f"{row['created_at']} {digest}"


#: C-15.8: the columns `notices` lists, common to job and service notices.
NOTICE_COLUMNS = "notice_id, session_id, text, state, transport, created_at, offered_at, acknowledged_at"


def notice_rows(query: Callable[[str, Sequence[Any]], Iterable[Any]], session_id: str | None = None,
                *, resolved: bool = False) -> list[dict[str, Any]]:
    """C-15.8: a session's notices (every session's when None), as `notices` lists them.

    Unresolved (`pending` or `offered`) only, unless `resolved`: then also the
    `surfaced` and `acknowledged` rows retention still keeps (C-23.26). A
    service notice carries its id negated, and the job a pin's notice is about
    (else none), as `notice.pending` returns it, so one id names one row across
    both tables. Ordered by session
    (a job notice with no caller session sorts first, as ""), then creation.
    """
    where, params = [], []
    if session_id is not None:
        where.append("session_id=?")
        params.append(session_id)
    if not resolved:
        where.append("state IN ('pending','offered')")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = [dict(row) for row in query(f"SELECT {NOTICE_COLUMNS}, job_id FROM notices{clause}", params)]
    service = [dict(row) for row in query(f"SELECT {NOTICE_COLUMNS} FROM service_notices{clause}", params)]
    about = pin_notice_jobs(query, service)
    rows += [{**row, "notice_id": -row["notice_id"], "job_id": about.get(row["notice_id"])} for row in service]
    rows.sort(key=lambda row: (row["session_id"] or "", str(row["created_at"]), abs(row["notice_id"])))
    for row in rows:
        row["fingerprint"] = notice_fingerprint(row)            # what `--ack`/`--withdraw` send back
    return rows


class SchemaVersionError(RuntimeError):
    code = 1


class SnapshotWriteError(RuntimeError):
    """C-3.7: a transaction was begun inside a read snapshot on the same thread.

    Its reads would go to the snapshot, which cannot see the transaction's own
    writes, so a check-then-insert inside it (`add_artifact`, `put_closure`)
    writes a duplicate (review of 5841d8b, finding 4). It is refused before
    anything is written."""


class Store:
    def __init__(self, path: str | Path, read_only: bool = False, *, readonly: bool | None = None,
                 readers: int = 0, read_wait_s: float = READ_WAIT_S):
        self.path = Path(path)
        self.read_only = read_only if readonly is None else readonly
        # C-3.7: read connections, opened on first use, at most `readers` of them.
        self._max_readers = 0 if self.read_only else max(0, int(readers))
        self._idle: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue()
        self._readers: list[sqlite3.Connection] = []
        self._readers_lock = threading.Lock()
        # Signalled whenever a read gives its connection back (`close` waits on it).
        self._returned = threading.Condition(self._readers_lock)
        # id(connection) -> (thread ident, monotonic start, "snapshot" | "statement")
        # for every read connection in use, the pool's and a read's own.
        self._in_use: dict[int, tuple[int, float, str]] = {}
        # C-3.7: snapshots take a slot before a pooled connection, so they never
        # hold the last STATEMENT_RESERVE of them (a pool of one is shared).
        reserve = min(STATEMENT_RESERVE, max(0, self._max_readers - 1))
        self.snapshot_share = self._max_readers - reserve
        self._snapshot_slots = threading.BoundedSemaphore(max(1, self.snapshot_share))
        self.read_wait_s = read_wait_s
        #: Reads that found the pool out of connections, those that opened their
        #: own after `read_wait_s`, and the longest such wait (`daemon.status`).
        self.pool_waits = {"waits": 0, "own_connections": 0, "longest_wait_s": 0.0}
        self._local = threading.local()
        self._closed = False
        # C-3.6: an RLock that remembers its holder, so the daemon can say who
        # held it, for how long, and what they were doing.
        self._lock = WatchedLock("store")
        self._depth = 0
        # Bumped by every committed top-level transaction that changed a row.
        # A reader compares it without taking `_lock` (an int read is atomic)
        # to learn whether anything it derived from the store can have changed.
        self.generation = 0
        if not self.read_only:
            self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        uri = self.path.resolve().as_uri() + ("?mode=ro" if self.read_only else "?mode=rwc")
        self.connection = sqlite3.connect(uri, uri=True, isolation_level=None,
                                          check_same_thread=False, timeout=5)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA busy_timeout=5000")
            self.connection.execute("PRAGMA foreign_keys=ON")
            exists = self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
            version = self.connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] if exists else 0
            if version and version > SCHEMA_VERSION:
                raise SchemaVersionError(f"store schema {version} is newer than supported schema {SCHEMA_VERSION}")
            if self.read_only:
                if not version:
                    raise SchemaVersionError(f"store schema {version or 0} is uninitialized; supported schema {SCHEMA_VERSION}")
                self.connection.execute("PRAGMA query_only=ON")
            else:
                self.connection.execute("PRAGMA journal_mode=WAL")
                self.connection.execute("PRAGMA synchronous=FULL")
                self.connection.execute(f"PRAGMA journal_size_limit={int(WAL_SIZE_LIMIT)}")
                # Columns first, then the schema file: `store_schema.sql` always
                # describes the newest version, and its indexes cannot be created
                # over an older table until that table has caught up (C-3.1).
                migrated_from = None
                if version and version < SCHEMA_VERSION:
                    self.connection.execute("BEGIN IMMEDIATE")
                    self._migrate(version)          # records one schema_version row per step
                    self.connection.commit()
                    migrated_from, version = version, SCHEMA_VERSION
                schema = Path(__file__).with_name("store_schema.sql").read_text()
                self.connection.executescript("BEGIN IMMEDIATE;\n" + schema)
                if (migrated_from is not None and migrated_from < 3) or (version and version < 3):
                    columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(jobs)")}
                    for name, declaration in (("isolated_review", "INTEGER NOT NULL DEFAULT 0"),
                                              ("review_root", "TEXT"), ("round_lease", "TEXT")):
                        if name not in columns:
                            self.connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
                if not version:
                    self.connection.execute("INSERT INTO schema_version VALUES (?,?)", (SCHEMA_VERSION, utc_now()))
                if not version or migrated_from is not None:
                    # A fresh store records its version once; a migrated store already
                    # recorded each step and records only the event here (C-3.1).
                    self.connection.execute("INSERT INTO events(ts,kind,data_json) VALUES (?,?,?)",
                                            (utc_now(), "schema.applied", _json({"version": SCHEMA_VERSION})))
                self.connection.commit()
                os.chmod(self.path, 0o600)
        except BaseException:
            self.connection.close()
            raise
        self._columns = {
            row["name"]: {col["name"] for col in self.connection.execute(f'PRAGMA table_info("{row["name"]}")')}
            for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    def _migrate(self, version: int) -> None:
        """C-3.1: carry an older database forward, one numbered step at a time.

        Runs inside the caller's open transaction, so a database is either fully
        migrated or untouched, and before `store_schema.sql` is applied, so the
        newest schema's indexes find their columns. Each `ADD COLUMN` is skipped
        when the column is already there, so an interrupted upgrade re-runs safely.
        """
        for step in range(version + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS.get(step, ()):
                if statement.startswith("ALTER TABLE "):
                    _alter, _table_kw, table, _add, _column_kw, column, *_rest = statement.split()
                    present = {row[1] for row in
                               self.connection.execute(f'PRAGMA table_info("{table}")')}
                    if column in present:
                        continue
                self.connection.execute(statement)
            self.connection.execute("INSERT INTO schema_version VALUES (?,?)", (step, utc_now()))
            self.connection.execute("INSERT INTO events(ts,kind,data_json) VALUES (?,?,?)",
                                    (utc_now(), "schema.migrated", _json({"version": step})))

    @property
    def conn(self) -> sqlite3.Connection:
        return self.connection

    @contextmanager
    def transaction(self, kind: str = "state.changed", *, job_id: str | None = None,
                    attempt_id: str | None = None, lane_id: str | None = None,
                    data: Mapping[str, Any] | None = None) -> Iterator[sqlite3.Connection]:
        """C-3.2: serialize a mutation and its audit event; nested calls use savepoints.

        C-3.7: never inside a `snapshot()` on the same thread (`SnapshotWriteError`);
        another thread's transaction is how a commit reaches a snapshot's lifetime."""
        if self.read_only:
            raise sqlite3.OperationalError("store is read-only")
        if getattr(self._local, "in_snapshot", False):
            raise SnapshotWriteError(
                "a transaction cannot begin inside a read snapshot on the same thread: its reads "
                "would see the snapshot, not its own writes (C-3.7); write before or after the "
                "snapshot")
        with self._lock:
            depth = self._depth
            savepoint = f"store_{depth}"
            self.connection.execute("BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT {savepoint}")
            self._depth += 1
            before = self.connection.total_changes
            try:
                yield self.connection
                if self.connection.total_changes != before:
                    self.connection.execute(
                        "INSERT INTO events(ts,kind,job_id,attempt_id,lane_id,data_json) VALUES (?,?,?,?,?,?)",
                        (utc_now(), kind, job_id, attempt_id, lane_id, _json(data or {})))
                self.connection.execute("COMMIT" if depth == 0 else f"RELEASE SAVEPOINT {savepoint}")
                if depth == 0 and self.connection.total_changes != before:
                    self.generation += 1
            except BaseException:
                if depth == 0:
                    self.connection.rollback()
                else:
                    self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            finally:
                self._depth -= 1

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        if self._reads_elsewhere():
            with self._reading() as conn:
                return [dict(row) for row in conn.execute(sql, params).fetchall()]
        with self._lock:
            return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def one(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        if self._reads_elsewhere():
            with self._reading() as conn:
                cursor = conn.execute(sql, params)
                try:
                    row = cursor.fetchone()
                finally:
                    cursor.close()          # ends the statement, so no snapshot is kept open
                return dict(row) if row is not None else None
        with self._lock:
            row = self.connection.execute(sql, params).fetchone()
            return dict(row) if row is not None else None

    # --- C-3.7: reads off the store lock --------------------------------------

    def _holds_writer(self) -> bool:
        held = self._lock.held
        return held is not None and held[0] == threading.get_ident()

    def _reads_elsewhere(self) -> bool:
        """Whether this thread's next read goes to a read connection."""
        if getattr(self._local, "snapshot", None) is not None:
            return True
        return bool(self._max_readers) and not self._closed and not self._holds_writer()

    def _open_reader(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, isolation_level=None,
                               check_same_thread=False, timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA query_only=ON")
        except BaseException:
            conn.close()
            raise
        return conn

    def _checkout(self, kind: str) -> tuple[sqlite3.Connection, bool]:
        """A read connection for one statement or one snapshot (`kind`), and
        whether it is the pool's (False: opened for this read alone).

        A snapshot first takes one of `snapshot_share` slots, so snapshots never
        hold the connections kept for statements. Neither waits longer than
        `read_wait_s` in all: then it opens a connection of its own, and the
        wait is reported either way (C-3.7)."""
        started = time.monotonic()
        deadline = started + self.read_wait_s
        slot = kind != "snapshot" or self._snapshot_slots.acquire(False)
        waited = not slot
        if not slot:
            slot = self._snapshot_slots.acquire(timeout=self.read_wait_s)
        conn, pooled = None, False
        try:
            if slot:
                conn, blocked = self._pooled(deadline)
                waited, pooled = waited or blocked, conn is not None
            if conn is None:
                with self._readers_lock:
                    if self._closed:
                        raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
                conn = self._open_reader()
        except BaseException:
            if slot and kind == "snapshot":
                self._snapshot_slots.release()
            raise
        if kind == "snapshot" and slot and not pooled:
            self._snapshot_slots.release()      # it holds no pooled connection after all
        with self._readers_lock:
            self._in_use[id(conn)] = (threading.get_ident(), time.monotonic(), kind)
        if waited:
            self._pool_waited(time.monotonic() - started, kind, pooled)
        return conn, pooled

    def _pooled(self, deadline: float) -> tuple[sqlite3.Connection | None, bool]:
        """An idle pooled connection, a new one while the pool is below its size,
        or one given back before `deadline`; and whether it had to wait."""
        try:
            return self._idle.get_nowait(), False
        except queue.Empty:
            pass
        with self._readers_lock:
            if self._closed:
                raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
            grow = len(self._readers) < self._max_readers
            if grow:
                self._readers.append(None)          # the place, filled below
        if grow:
            try:
                conn = self._open_reader()
            except BaseException:
                with self._readers_lock:
                    if None in self._readers:           # `close` may have emptied the list meanwhile
                        self._readers.remove(None)
                raise
            with self._readers_lock:
                # The store closed while this connection opened: `close` found no
                # read in use and dropped the pool, this place with it.
                closed = self._closed or None not in self._readers
                if not closed:
                    self._readers[self._readers.index(None)] = conn
            if closed:
                conn.close()
                raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
            return conn, False
        try:
            return self._idle.get(timeout=max(0.0, deadline - time.monotonic())), True
        except queue.Empty:
            return None, True

    def _checkin(self, conn: sqlite3.Connection, pooled: bool, kind: str) -> None:
        with self._readers_lock:
            self._in_use.pop(id(conn), None)
            keep = pooled and not self._closed
            if keep:
                self._idle.put(conn)
            self._returned.notify_all()
        if pooled and kind == "snapshot":
            self._snapshot_slots.release()
        if not keep:
            conn.close()                            # a read's own connection, or the store closed

    def _pool_waited(self, seconds: float, kind: str, pooled: bool) -> None:
        """Count a wait for a read connection, and report it through the store
        lock's watch (C-3.6's rate limit: one line a minute, the rest counted)."""
        with self._readers_lock:
            self.pool_waits["waits"] += 1
            if not pooled:
                self.pool_waits["own_connections"] += 1
            self.pool_waits["longest_wait_s"] = max(self.pool_waits["longest_wait_s"], round(seconds, 3))
            holds = sorted(self._in_use.values(), key=lambda hold: hold[1])
            size = len(self._readers)
        watch = self._lock.watch
        if watch is None:
            return
        me, now = threading.get_ident(), time.monotonic()

        def render() -> str:
            snapshots = sum(1 for _, _, held_for in holds if held_for == "snapshot")
            listed = "; ".join(f"{thread_name(ident)} for a {held_for}, {now - since:.1f} s"
                               for ident, since, held_for in holds if ident != me)
            return (f"{thread_name(me)} waited {seconds:.2f} s for a read connection for a {kind}"
                    + ("" if pooled else f" and, none free after {self.read_wait_s:g} s, opened one of its own")
                    + f"; {len(holds)} in use, {snapshots} by snapshots (at most {self.snapshot_share} of the "
                      f"{size} pooled): {listed or 'none'}")
        try:
            watch.note(self._lock.name, "read-pool", seconds, render)
        except Exception:                           # noqa: BLE001 - a report never fails a read
            pass

    def read_holds(self) -> list[tuple[int, float, str]]:
        """(thread ident, monotonic start, kind) of every read connection in use."""
        with self._readers_lock:
            return list(self._in_use.values())

    def read_pool(self) -> dict[str, Any]:
        """The read pool at this moment, for `daemon.status` (C-3.7)."""
        with self._readers_lock:
            holds = list(self._in_use.values())
            return {"size": self._max_readers, "open": len(self._readers),
                    "snapshot_share": self.snapshot_share, "in_use": len(holds),
                    "snapshots": sum(1 for _, _, kind in holds if kind == "snapshot"),
                    **self.pool_waits}

    @contextmanager
    def _reading(self) -> Iterator[sqlite3.Connection]:
        pinned = getattr(self._local, "snapshot", None)
        if pinned is not None:
            yield pinned
            return
        conn, pooled = self._checkout("statement")
        try:
            yield conn
        finally:
            self._checkin(conn, pooled, "statement")

    @contextmanager
    def snapshot(self) -> Iterator[None]:
        """C-3.7: every read this thread makes in the block sees one committed
        state, and none of them takes the store lock.

        Inside a transaction, or nested in another snapshot, the block reads what
        the enclosing one reads. A store without read connections holds the
        store lock for the block instead, which gives the same one state.

        No transaction may begin in the block on this thread: it raises
        `SnapshotWriteError`, with or without read connections, so code that
        passes on a store without them does not write duplicates on one with.

        The block holds a read connection throughout, so it should read and
        leave: build what the rows are for after it (review of 5841d8b).
        """
        if getattr(self._local, "snapshot", None) is not None or self._holds_writer():
            yield
            return
        if not self._reads_elsewhere():
            with self._lock:
                self._local.in_snapshot = True
                try:
                    yield
                finally:
                    self._local.in_snapshot = False
            return
        conn, pooled = self._checkout("snapshot")
        try:
            conn.execute("BEGIN")                   # deferred: the snapshot is taken at the first read
            self._local.snapshot = conn
            self._local.in_snapshot = True
            try:
                yield
            finally:
                self._local.snapshot = None
                self._local.in_snapshot = False
                conn.execute("ROLLBACK")            # a read transaction has nothing to keep
        finally:
            self._checkin(conn, pooled, "snapshot")

    def close(self) -> None:
        """Close the writer, then each read connection once no read is using it.

        C-3.7: a read already in progress when the store closes finishes on its
        connection; one still going after `CLOSE_WAIT_S` closes that connection
        itself when it ends. A read that starts after `close` is refused."""
        with self._lock:
            self._closed = True
            self.connection.close()
        with self._readers_lock:
            self._returned.wait_for(lambda: not self._in_use, timeout=CLOSE_WAIT_S)
            idle = []
            while True:
                try:
                    idle.append(self._idle.get_nowait())
                except queue.Empty:
                    break
            self._readers = []
        for conn in idle:
            conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _insert(self, table: str, values: Mapping[str, Any], *, kind: str | None = None) -> int:
        if table not in self._columns or not values or set(values) - self._columns[table]:
            raise ValueError(f"invalid fields for {table}")
        fields = ",".join(f'"{field}"' for field in values)
        marks = ",".join("?" for _ in values)
        with self.transaction(kind or f"{table}.insert", **{key: values[key] for key in ("job_id", "attempt_id", "lane_id") if key in values}) as conn:
            cursor = conn.execute(f'INSERT INTO "{table}" ({fields}) VALUES ({marks})', tuple(values.values()))
            return int(cursor.lastrowid)

    def _update(self, table: str, key: str, identity: Any, values: Mapping[str, Any]) -> None:
        if table not in self._columns or not values or set(values) - self._columns[table] or key not in self._columns[table]:
            raise ValueError(f"invalid fields for {table}")
        fields = ",".join(f'"{field}"=?' for field in values)
        with self.transaction(f"{table}.update", **({key: identity} if key in ("job_id", "attempt_id", "lane_id") else {})) as conn:
            conn.execute(f'UPDATE "{table}" SET {fields} WHERE "{key}"=?', (*values.values(), identity))

    @staticmethod
    def lane_from_row(row: Mapping[str, Any]) -> Lane:
        # `.get` for the version-2 columns: a read-only handle on a database this
        # process may not migrate still yields a usable Lane (C-3.1).
        return Lane(row["lane_id"], row["provider"], row["account_key"],
                    Credential(row["provider"], row["credential_ref"], row["credential_kind"], row["credential_epoch"]),
                    row["home"], LaneOwner(row["owner"]), bool(row["desktop"]), bool(row["enabled"]),
                    row.get("identity"), row.get("label"))

    def put_lane(self, lane: Lane, *, plan: str | None = None,
                 identity_status: str | None = None) -> None:
        if identity_status is not None:
            # C-10.6: four statuses and no fifth. Named here as well as in the
            # schema so a hand-edited roster fails with words, not a constraint.
            try:
                IdentityStatus(str(identity_status))
            except ValueError:
                allowed = ", ".join(status.value for status in IdentityStatus)
                raise ValueError(
                    f"identity_status {identity_status!r} is not one of {allowed}") from None
        values = {"lane_id": lane.lane_id, "provider": lane.provider, "account_key": lane.account_key,
                  "credential_ref": lane.credential.ref, "credential_kind": lane.credential.kind,
                  "credential_epoch": lane.credential.epoch, "home": lane.home, "owner": lane.owner,
                  "desktop": int(lane.desktop), "enabled": int(lane.enabled), "plan": plan,
                  "identity": lane.identity, "label": lane.label,
                  "identity_status": str(identity_status) if identity_status else None,
                  "created_at": utc_now(), "updated_at": utc_now()}
        with self.transaction("lane.enrolled", lane_id=lane.lane_id):
            existing = self.one("SELECT * FROM lanes WHERE lane_id=?", (lane.lane_id,))
            if existing:
                immutable = ("provider", "account_key", "credential_ref", "credential_kind", "credential_epoch", "home")
                if any(existing[key] != values[key] for key in immutable):
                    raise ValueError("lane binding is immutable; disable it and create a new lane id")
                # C-10.6, C-1.3: an unbound lane may learn its identity once. A lane
                # already bound to a different account is a new binding and needs a
                # new lane id, so an operator re-enrols rather than silently rebinds.
                for key in ("identity", "label"):
                    if existing.get(key) and values[key] and existing[key] != values[key]:
                        raise ValueError(
                            f"lane {key} is immutable once recorded; re-enrol under a new lane id")
                learned = {key: values[key] for key in ("identity", "label")
                           if values[key] and not existing.get(key)}
                self.update_lane(lane.lane_id, owner=lane.owner, desktop=int(lane.desktop),
                                 enabled=int(lane.enabled), plan=plan, clear_mismatch=True,
                                 **learned,
                                 **({"identity_status": values["identity_status"]}
                                    if values["identity_status"] else {}))
            else:
                self._insert("lanes", values)

    add_lane = put_lane

    def update_lane(self, lane_id: str, *, clear_mismatch: bool = False, **values: Any) -> None:
        # `identity_status` is the one identity column that moves: every probe
        # cycle re-asks the profile endpoint (C-10.6). `identity` and `label` are
        # part of the binding and are only ever learned once.
        if set(values) - {"owner", "desktop", "enabled", "plan",
                          "identity", "label", "identity_status"}:
            raise ValueError("lane binding is immutable")
        if values.get("identity_status") and not clear_mismatch:
            current = self.one("SELECT identity_status FROM lanes WHERE lane_id=?", (lane_id,))
            if (current and current["identity_status"] == "mismatch"
                    and values["identity_status"] != "mismatch"):
                # C-10.6: only an operator re-enrolling the lane releases it.
                raise ValueError("a mismatched lane is released only by re-enrolment")
        self._update("lanes", "lane_id", lane_id, {**values, "updated_at": utc_now()})

    def get_lane(self, lane_id: str) -> Lane | None:
        row = self.one("SELECT * FROM lanes WHERE lane_id=?", (lane_id,))
        return self.lane_from_row(row) if row else None

    def lane_rows(self) -> list[Row]:
        return self.query("SELECT * FROM lanes ORDER BY lane_id")

    def list_lanes(self) -> list[Lane]:
        return [self.lane_from_row(row) for row in self.lane_rows()]

    def add_job(self, values: Mapping[str, Any] | None = None, **fields: Any) -> str:
        data = {"state": "queued", "created_at": utc_now(), **(values or {}), **fields}
        self._insert("jobs", data)
        return data["job_id"]

    def get_job(self, job_id: str) -> Row | None:
        return self.one("SELECT * FROM jobs WHERE job_id=?", (job_id,))

    def get_job_by_request(self, request_id: str) -> Row | None:
        return self.one("SELECT * FROM jobs WHERE request_id=?", (request_id,))

    def list_jobs(self, *, state: str | None = None, session_id: str | None = None, limit: int | None = None) -> list[Row]:
        clauses, values = [], []
        for column, value in (("state", state), ("caller_session", session_id)):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value)
        sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created_at DESC,job_id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            values.append(limit)
        return self.query(sql, values)

    def update_job(self, job_id: str, **values: Any) -> None:
        self._update("jobs", "job_id", job_id, values)

    def add_attempt(self, values: Mapping[str, Any] | None = None, **fields: Any) -> str:
        data = {"state": "reserved", "reserved_at": utc_now(), **(values or {}), **fields}
        self._insert("attempts", data)
        return data["attempt_id"]

    def get_attempt(self, attempt_id: str) -> Row | None:
        return self.one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))

    def list_attempts(self, job_id: str | None = None) -> list[Row]:
        return self.query("SELECT * FROM attempts" + (" WHERE job_id=?" if job_id else "") + " ORDER BY reserved_at,seq", (job_id,) if job_id else ())

    def update_attempt(self, attempt_id: str, **values: Any) -> None:
        if set(values) & {"attempt_id", "job_id", "seq", "lane_id", "model_requested"}:
            raise ValueError("attempt identity, lane, and model are immutable")
        self._update("attempts", "attempt_id", attempt_id, values)

    def add_artifact(self, attempt_id: str, role: str, path: str, sha256: str, bytes: int) -> int:
        with self.transaction("artifact.recorded", attempt_id=attempt_id):
            existing = self.one("SELECT * FROM artifacts WHERE attempt_id=? AND role=? AND path=?", (attempt_id, role, path))
            if existing:
                if existing["sha256"] != sha256 or existing["bytes"] != bytes:
                    raise ValueError("artifact already recorded with a different digest or size")
                return existing["artifact_id"]
            return self._insert("artifacts", {"attempt_id": attempt_id, "role": role, "path": path, "sha256": sha256, "bytes": bytes, "created_at": utc_now()})

    def list_artifacts(self, attempt_id: str) -> list[Row]:
        return self.query("SELECT * FROM artifacts WHERE attempt_id=? ORDER BY artifact_id", (attempt_id,))

    def add_notice(self, job_id: str, text: str, session_id: str | None = None, *, state: str = "pending", transport: str | None = None) -> int:
        return self._insert("notices", {"job_id": job_id, "session_id": session_id, "text": text, "state": state, "transport": transport, "created_at": utc_now()})

    def list_notices(self, session_id: str | None = None, *, pending: bool = False) -> list[Row]:
        clauses, values = [], []
        if session_id is not None:
            clauses.append("session_id=?")
            values.append(session_id)
        if pending:
            clauses.append("state IN ('pending','offered')")
        return self.query("SELECT * FROM notices" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY notice_id", values)

    def update_notice(self, notice_id: int, **values: Any) -> None:
        self._update("notices", "notice_id", notice_id, values)

    def acknowledge_notices(self, session_id: str, notice_ids: Sequence[int]) -> int:
        if not notice_ids:
            return 0
        with self.transaction("notice.acknowledged", data={"session_id": session_id, "notice_ids": list(notice_ids)}) as conn:
            marks = ",".join("?" for _ in notice_ids)
            result = conn.execute(f"UPDATE notices SET state='acknowledged',acknowledged_at=? WHERE session_id=? AND notice_id IN ({marks}) AND state IN ('pending','offered')", (utc_now(), session_id, *notice_ids))
            return result.rowcount

    def add_decision(self, job_id: str, decision: Decision | Mapping[str, Any], attempt_id: str | None = None) -> int:
        value = asdict(decision) if isinstance(decision, Decision) else dict(decision)
        return self._insert("decisions", {"job_id": job_id, "attempt_id": attempt_id, "evaluated_at": utc_now(), "policy_hash": value["policy_hash"], "decision_json": _json(value)})

    def list_decisions(self, job_id: str) -> list[Row]:
        return self.query("SELECT * FROM decisions WHERE job_id=? ORDER BY decision_id", (job_id,))

    def acquire_lease(self, lease_key: str, holder: str, expires_at: str | None = None) -> bool:
        with self.transaction("lease.acquired", data={"lease_key": lease_key, "holder": holder}) as conn:
            existing = conn.execute("SELECT holder FROM leases WHERE lease_key=?", (lease_key,)).fetchone()
            if existing:
                return existing["holder"] == holder
            conn.execute("INSERT INTO leases VALUES (?,?,?,?)", (lease_key, holder, utc_now(), expires_at))
            return True

    def release_leases(self, holder: str, *, prefix: str | None = None) -> int:
        with self.transaction("lease.released", data={"holder": holder, "prefix": prefix}) as conn:
            if prefix is None:
                return conn.execute("DELETE FROM leases WHERE holder=?", (holder,)).rowcount
            return conn.execute("DELETE FROM leases WHERE holder=? AND substr(lease_key,1,?)=?", (holder, len(prefix), prefix)).rowcount

    def list_leases(self, holder: str | None = None) -> list[Row]:
        return self.query("SELECT * FROM leases" + (" WHERE holder=?" if holder else "") + " ORDER BY lease_key", (holder,) if holder else ())

    def add_reading(self, reading: Reading) -> int:
        return self._insert("readings", asdict(reading))

    #: A timestamp `utc_now` writes. Strings of this one fixed-width shape sort
    #: as the instants they name, so SQL's max over them is the newest.
    CANONICAL_TS = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z"

    def latest_reading_candidates(self) -> list[Row]:
        """C-3.7: every reading that can be the newest of its lane, scope and window.

        `capacity.latest_readings` keeps only the newest reading of each key,
        by parsed instant and then reading id; a view used to read and parse
        every reading ever recorded (34k rows and growing, the view's largest
        cost). This returns, per key, the readings at the greatest timestamp of
        the canonical shape, where string order is time order, plus every
        reading whose timestamp has any other shape, for Python to compare. The
        newest reading of every key is always among them, so
        `latest_readings` gives the same answer over these as over all.
        """
        return self.query(
            "WITH top AS (SELECT lane_id,scope,window,max(observed_at) AS at FROM readings "
            "WHERE observed_at GLOB ? GROUP BY lane_id,scope,window) "
            "SELECT r.* FROM readings r JOIN top ON r.lane_id=top.lane_id AND r.scope=top.scope "
            "AND r.window=top.window AND r.observed_at=top.at "
            "UNION ALL SELECT * FROM readings WHERE NOT observed_at GLOB ? "
            "ORDER BY observed_at DESC,reading_id DESC", (self.CANONICAL_TS, self.CANONICAL_TS))

    def list_readings(self, lane_id: str | None = None) -> list[Row]:
        return self.query("SELECT * FROM readings" + (" WHERE lane_id=?" if lane_id else "") + " ORDER BY observed_at DESC,reading_id DESC", (lane_id,) if lane_id else ())

    def weekly_projection_samples(self, *, now: str | datetime | None = None) -> list[Row]:
        """Read only 24 hours of weekly provider history, apart from newest evidence."""
        return self.query(WEEKLY_HISTORY_SQL, weekly_history_params(now))

    def put_closure(self, closure: Closure) -> int:
        """One open closure per lane and scope: a later end extends the row in place.

        Every call leaves a `closure.recorded` event for the lane, including a
        limit reported again that ends no later than the open row's and so
        changes nothing in it (C-18.3). That event is the only trace of such a
        report, and a busy lane's usage read looks for it before it releases
        the closure. The row is rewritten as it is so that the transaction has a
        change to record.
        """
        with self.transaction("closure.recorded", lane_id=closure.lane_id) as conn:
            existing = self.one("SELECT * FROM closures WHERE lane_id=? AND scope=? AND released_at IS NULL ORDER BY until_at DESC LIMIT 1", (closure.lane_id, closure.scope))
            if existing:
                if closure.until_at > existing["until_at"]:
                    self._update("closures", "closure_id", existing["closure_id"], asdict(closure))
                else:
                    conn.execute("UPDATE closures SET until_at=until_at WHERE closure_id=?", (existing["closure_id"],))
                return existing["closure_id"]
            return self._insert("closures", {**asdict(closure), "created_at": utc_now()})

    add_closure = put_closure

    def list_closures(self, lane_id: str | None = None, *, active_at: str | None = None) -> list[Row]:
        clauses, values = [], []
        if lane_id is not None:
            clauses.append("lane_id=?")
            values.append(lane_id)
        if active_at is not None:
            clauses.append("released_at IS NULL AND until_at>?")
            values.append(active_at)
        return self.query("SELECT * FROM closures" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY closure_id", values)

    def add_event(self, kind: str, *, job_id: str | None = None, attempt_id: str | None = None,
                  lane_id: str | None = None, data: Mapping[str, Any] | None = None) -> int:
        return self._insert("events", {"ts": utc_now(), "kind": kind, "job_id": job_id, "attempt_id": attempt_id,
                                       "lane_id": lane_id, "data_json": _json(data or {})}, kind=kind)

    def list_events(self, job_id: str | None = None) -> list[Row]:
        return self.query("SELECT * FROM events" + (" WHERE job_id=?" if job_id else "") + " ORDER BY event_id", (job_id,) if job_id else ())

    def add_action(self, values: Mapping[str, Any] | None = None, **fields: Any) -> str:
        data = {"state": "pending", "created_at": utc_now(), "updated_at": utc_now(), **(values or {}), **fields}
        self._insert("actions", data)
        return data["action_id"]

    def get_action(self, action_id: str) -> Row | None:
        return self.one("SELECT * FROM actions WHERE action_id=?", (action_id,))

    def update_action(self, action_id: str, **values: Any) -> None:
        self._update("actions", "action_id", action_id, {**values, "updated_at": utc_now()})
