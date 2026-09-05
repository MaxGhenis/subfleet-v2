"""The daemon's durable SQLite store (C-3).

Transactions are deliberately synchronous and short. Callers perform process,
network, and filesystem work before entering a transaction, then atomically
record the resulting state. The shared connection is serialized across workers.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import Closure, Credential, Decision, IdentityStatus, Lane, LaneOwner, Reading

SCHEMA_VERSION = 2
Row = dict[str, Any]

#: C-3.1: migrations are additive and numbered. Each entry is the statements that
#: carry a database from `n - 1` to `n`; `store_schema.sql` always describes the
#: newest version, so a fresh database never runs one.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    # C-10.6, C-1.4: bind a Claude lane to the identity its own credential
    # reports, with the email kept beside it as a label and never as a key.
    2: (
        "ALTER TABLE lanes ADD COLUMN identity TEXT",
        "ALTER TABLE lanes ADD COLUMN label TEXT",
        "ALTER TABLE lanes ADD COLUMN identity_status TEXT CHECK (identity_status IS NULL "
        "OR identity_status IN ('verified','enrolled','mismatch','unverified'))",
    ),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class SchemaVersionError(RuntimeError):
    code = 1


class Store:
    def __init__(self, path: str | Path, read_only: bool = False, *, readonly: bool | None = None):
        self.path = Path(path)
        self.read_only = read_only if readonly is None else readonly
        self._lock = threading.RLock()
        self._depth = 0
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
                # Columns first, then the schema file: `store_schema.sql` always
                # describes the newest version, and its indexes cannot be created
                # over an older table until that table has caught up (C-3.1).
                if version and version < SCHEMA_VERSION:
                    self.connection.execute("BEGIN IMMEDIATE")
                    self._migrate(version)
                    self.connection.commit()
                schema = Path(__file__).with_name("store_schema.sql").read_text()
                self.connection.executescript("BEGIN IMMEDIATE;\n" + schema)
                if not version:
                    self.connection.execute("INSERT INTO schema_version VALUES (?,?)", (SCHEMA_VERSION, utc_now()))
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
        """C-3.2: serialize a mutation and its audit event; nested calls use savepoints."""
        if self.read_only:
            raise sqlite3.OperationalError("store is read-only")
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
        with self._lock:
            return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def one(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        with self._lock:
            row = self.connection.execute(sql, params).fetchone()
            return dict(row) if row is not None else None

    def close(self) -> None:
        with self._lock:
            self.connection.close()

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

    def list_readings(self, lane_id: str | None = None) -> list[Row]:
        return self.query("SELECT * FROM readings" + (" WHERE lane_id=?" if lane_id else "") + " ORDER BY observed_at DESC,reading_id DESC", (lane_id,) if lane_id else ())

    def put_closure(self, closure: Closure) -> int:
        with self.transaction("closure.recorded", lane_id=closure.lane_id):
            existing = self.one("SELECT * FROM closures WHERE lane_id=? AND scope=? AND released_at IS NULL ORDER BY until_at DESC LIMIT 1", (closure.lane_id, closure.scope))
            if existing:
                if closure.until_at > existing["until_at"]:
                    self._update("closures", "closure_id", existing["closure_id"], asdict(closure))
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
