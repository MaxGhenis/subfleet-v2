"""Read-only view of the store for when the daemon is down (C-17.5).

`runs`, `runs show`, `status`, and `kill` keep working without a daemon. Reads
go through a `mode=ro` URI so this process can never write the store (C-3.4);
offline `kill` is the one side effect, and it signals only a recorded process
group whose identity it has verified (C-5.3, C-5.4). Anything it cannot verify
it refuses with the reason rather than signalling a pid that may have been
recycled.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .client import same_process
from .contracts import Exit, JobState

STORE_NAME = "state.sqlite3"
KNOWN_SCHEMA_VERSION = 1          # C-3.5
LIVE_JOB_STATES = ("queued", "running", "waiting")
LIVE_ATTEMPT_STATES = ("reserved", "starting", "running", "finalizing")


class SchemaTooNew(Exception):
    """The store was written by a newer subfleet than this one (C-3.5)."""

    code = Exit.OPERATIONAL

    def __init__(self, found: int, known: int = KNOWN_SCHEMA_VERSION):
        super().__init__(f"the store is at schema version {found}; this CLI knows "
                         f"version {known}")
        self.found, self.known = found, known
        self.fix = "upgrade subfleet, or let the daemon that wrote it act"


class OfflineUnavailable(Exception):
    """No readable store, so offline mode has nothing to answer from."""

    code = Exit.DAEMON_UNAVAILABLE

    def __init__(self, message: str, fix: str = "subfleet daemon start"):
        super().__init__(message)
        self.fix = fix


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _duration_s(started: Any, finished: Any) -> float | None:
    begin, end = _parse_ts(started), _parse_ts(finished)
    if begin is None:
        return None
    end = end or datetime.now(timezone.utc)
    return max(0.0, (end - begin).total_seconds())


class Offline:
    """Read-only access to `<state root>/state.sqlite3` (C-3.4, C-17.5)."""

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser()

    @property
    def store_path(self) -> Path:
        return self.root / STORE_NAME

    @contextlib.contextmanager
    def reading(self):
        """A read-only connection that is always closed, with clean failures."""
        conn = self.connect()
        try:
            yield conn
        except sqlite3.Error as exc:
            raise OfflineUnavailable(
                f"cannot read {self.store_path}: {exc}") from exc
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        if not self.store_path.exists():
            raise OfflineUnavailable(
                f"no daemon and no store at {self.store_path}")
        uri = f"file:{self.store_path.as_uri().removeprefix('file:')}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        except sqlite3.Error as exc:
            raise OfflineUnavailable(f"cannot read {self.store_path}: {exc}") from exc
        conn.row_factory = sqlite3.Row
        return conn

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def _tables(conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {row["name"] for row in rows}

    def schema_version(self, conn: sqlite3.Connection) -> int | None:
        if "schema_version" not in self._tables(conn):
            return None
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return None if row is None else row["v"]

    # --- runs (C-17.1, C-17.5) ----------------------------------------------

    _JOB_SELECT = """
        SELECT j.*,
               a.attempt_id      AS attempt_id,
               a.seq             AS attempt_seq,
               a.lane_id         AS lane_id,
               a.model_requested AS model_requested,
               a.model_served    AS model_served,
               a.state           AS attempt_state,
               a.rc              AS attempt_rc,
               a.outcome_class   AS outcome_class,
               a.guardian_pid    AS guardian_pid,
               a.child_pid       AS child_pid,
               a.pgid            AS pgid,
               a.boot_id         AS boot_id,
               a.proc_start      AS proc_start,
               a.attestation     AS attestation,
               l.provider        AS provider,
               l.account_key     AS account_key,
               (SELECT SUM(bytes) FROM artifacts
                  WHERE artifacts.attempt_id = a.attempt_id
                    AND artifacts.role = 'deliverable') AS out_bytes
          FROM jobs j
          LEFT JOIN attempts a
                 ON a.attempt_id = (SELECT attempt_id FROM attempts
                                     WHERE attempts.job_id = j.job_id
                                     ORDER BY seq DESC LIMIT 1)
          LEFT JOIN lanes l ON l.lane_id = a.lane_id
    """

    def list_jobs(self, *, session: str | None = None, running: bool = False,
                  last: int = 20) -> list[dict[str, Any]]:
        where, params = [], []
        if session:
            where.append("j.caller_session = ?")
            params.append(session)
        if running:
            placeholders = ",".join("?" for _ in LIVE_JOB_STATES)
            where.append(f"j.state IN ({placeholders})")
            params.extend(LIVE_JOB_STATES)
        sql = self._JOB_SELECT
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY j.created_at DESC, j.job_id DESC"
        if last and last > 0:
            sql += " LIMIT ?"
            params.append(int(last))
        with self.reading() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._job_dict(row) for row in rows]

    def show_job(self, job_id: str) -> dict[str, Any]:
        with self.reading() as conn:
            row = conn.execute(self._JOB_SELECT + " WHERE j.job_id = ?",
                               (job_id,)).fetchone()
            if row is None:
                raise LookupError(f"no job {job_id!r} in {self.store_path}")
            job = self._job_dict(row)
            job["attempts"] = [dict(item) for item in conn.execute(
                "SELECT * FROM attempts WHERE job_id = ? ORDER BY seq", (job_id,))]
            ids = [attempt["attempt_id"] for attempt in job["attempts"]]
            job["artifacts"] = []
            if ids:
                marks = ",".join("?" for _ in ids)
                job["artifacts"] = [dict(item) for item in conn.execute(
                    f"SELECT * FROM artifacts WHERE attempt_id IN ({marks})"
                    " ORDER BY artifact_id", ids)]
            job["notices"] = [dict(item) for item in conn.execute(
                "SELECT * FROM notices WHERE job_id = ? ORDER BY notice_id", (job_id,))]
            decisions = conn.execute(
                "SELECT decision_json FROM decisions WHERE job_id = ?"
                " ORDER BY evaluated_at DESC, decision_id DESC LIMIT 1",
                (job_id,)).fetchone()
        job["decision"] = None
        if decisions is not None:
            try:
                job["decision"] = json.loads(decisions["decision_json"])
            except (json.JSONDecodeError, TypeError):
                job["decision"] = None
        job["offline"] = True
        return job

    @staticmethod
    def _job_dict(row: sqlite3.Row) -> dict[str, Any]:
        job = {key: row[key] for key in row.keys()}
        job["duration_s"] = _duration_s(job.get("started_at"), job.get("finished_at"))
        job["model"] = job.get("model_served") or job.get("model_requested")
        job["family"] = job.get("provider")
        job["lane"] = job.get("lane_id")
        job["id"] = job.get("job_id")
        try:
            job["exclusions"] = json.loads(job.get("exclusions") or "[]")
        except (json.JSONDecodeError, TypeError):
            job["exclusions"] = []
        return job

    # --- status (C-17.1) -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self.reading() as conn:
            tables = self._tables(conn)
            lanes = [dict(row) for row in conn.execute(
                "SELECT * FROM lanes ORDER BY lane_id")] if "lanes" in tables else []
            readings = [dict(row) for row in conn.execute(
                """SELECT r.* FROM readings r
                     JOIN (SELECT lane_id, scope, window, MAX(observed_at) AS newest
                             FROM readings GROUP BY lane_id, scope, window) latest
                       ON latest.lane_id = r.lane_id AND latest.scope = r.scope
                      AND latest.window = r.window AND latest.newest = r.observed_at
                    ORDER BY r.lane_id, r.window""")] if "readings" in tables else []
            closures = [dict(row) for row in conn.execute(
                "SELECT * FROM closures WHERE released_at IS NULL"
                " ORDER BY lane_id, until_at")] if "closures" in tables else []
            in_flight: dict[str, int] = {}
            if "attempts" in tables:
                marks = ",".join("?" for _ in LIVE_ATTEMPT_STATES)
                for row in conn.execute(
                        f"SELECT lane_id, COUNT(*) AS n FROM attempts"
                        f" WHERE state IN ({marks}) GROUP BY lane_id",
                        LIVE_ATTEMPT_STATES):
                    in_flight[row["lane_id"]] = row["n"]
            version = self.schema_version(conn)
        for lane in lanes:
            lane["in_flight"] = in_flight.get(lane.get("lane_id"), 0)
        return {
            "offline": True,
            "state_root": str(self.root),
            "schema_version": version,
            "lanes": lanes,
            "readings": readings,
            "closures": closures,
            "running": self.list_jobs(running=True, last=50),
        }

    # --- kill (C-17.5, C-5.3, C-5.4) ----------------------------------------

    def kill(self, job_id: str, *, sig: int = signal.SIGTERM) -> dict[str, Any]:
        """Signal the recorded pgid, but only on a verified identity.

        Returns a record of what happened. `signalled` is the only outcome that
        touched a process; everything else is a refusal that names its reason,
        because the store is read-only here and a recycled pid cannot be undone.
        """
        with self.reading() as conn:
            version = self.schema_version(conn)
        if isinstance(version, int) and version > KNOWN_SCHEMA_VERSION:
            raise SchemaTooNew(version)
        job = self.show_job(job_id)
        state = job.get("state")
        if state in {s.value for s in JobState if s.terminal}:
            return {"job_id": job_id, "action": "already-finished", "state": state,
                    "reason": f"job is {state}"}
        live = [a for a in job.get("attempts", [])
                if a.get("state") in LIVE_ATTEMPT_STATES]
        if not live:
            return {"job_id": job_id, "action": "refused", "state": state,
                    "reason": "no live attempt is recorded for this job"}
        attempt = live[-1]
        pgid, pid = attempt.get("pgid"), attempt.get("guardian_pid")
        if not pgid or not pid:
            return {"job_id": job_id, "action": "refused", "state": state,
                    "attempt_id": attempt.get("attempt_id"),
                    "reason": "the attempt has no recorded pgid and guardian pid yet"}
        identity = same_process(pid, attempt.get("boot_id"), attempt.get("proc_start"))
        if identity is False:
            return {"job_id": job_id, "action": "already-dead", "state": state,
                    "attempt_id": attempt.get("attempt_id"), "pid": pid, "pgid": pgid,
                    "reason": f"guardian pid {pid} is gone; the daemon will finalize it"}
        if identity is None:
            return {"job_id": job_id, "action": "refused", "state": state,
                    "attempt_id": attempt.get("attempt_id"), "pid": pid, "pgid": pgid,
                    "reason": (f"cannot verify that pid {pid} is still the recorded "
                               "guardian (C-5.3); refusing to signal a pid that may "
                               "have been recycled")}
        try:
            os.killpg(int(pgid), sig)
        except (OSError, OverflowError, ValueError) as exc:
            return {"job_id": job_id, "action": "failed", "state": state,
                    "attempt_id": attempt.get("attempt_id"), "pid": pid, "pgid": pgid,
                    "reason": f"killpg({pgid}) failed: {exc}"}
        return {"job_id": job_id, "action": "signalled", "state": state,
                "attempt_id": attempt.get("attempt_id"), "pid": pid, "pgid": pgid,
                "signal": int(sig),
                "reason": ("signalled the recorded process group; the row stays as it "
                           "is until a daemon reconciles it")}
