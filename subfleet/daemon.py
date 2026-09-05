"""Durable job ownership, asynchronous execution, and the local socket API.

The scheduler reserves rows before launch. Guardians own provider waits;
bounded workers do process inspection and filesystem publication. SQLite
transactions contain only SQL (C-3.3, C-16.4).
"""
from __future__ import annotations

import argparse
import dataclasses
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import __version__
from . import ids, procs, protocol
from .adapters.base import AdapterError
from .adapters.registry import get_adapter
from .contracts import (
    HEADLESS_MARKER, START_GRACE_S, TERM_GRACE_S, WAIT_POLL_MAX_S,
    Attestation, Credential, ExitInfo, JobSpec, Lane, LaneOwner, Launch,
    Outcome, OutcomeClass, Sandbox, attempt_dir,
)
from .credentials import resolve_credential
from .guardian import atomic_publish
from .policy import load_policy, policy_hash, pick
from .salvage import git_head, salvage, validate_writable_workdir
from .store import Store

TERMINAL = ("succeeded", "failed", "cancelled", "lost")
LIVE = ("reserved", "starting", "running", "finalizing")
WRITE_PREAMBLE = (
    "<!-- subfleet:write -->\n"
    "Work only in the assigned workspace. Preserve existing changes. "
    "Commit each coherent step on the assigned branch; never rewrite history "
    "or push to main or master. Leave a concise report of changes and tests.\n\n"
)
HEADLESS_PREAMBLE = (
    HEADLESS_MARKER + "\nThis is a delegated, headless job. Complete the task "
    "autonomously, preserve the caller's work, and return a final deliverable.\n\n"
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def age(timestamp: str | None) -> float:
    if timestamp is None:
        return 0.0
    return (datetime.now(timezone.utc) - datetime.fromisoformat(timestamp.replace("Z", "+00:00"))).total_seconds()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n").encode()


class DaemonUnavailable(RuntimeError):
    code = 69


class Daemon:
    def __init__(self, state_root: str | Path, *, tick_s: float = .05,
                 start_grace_s: float = START_GRACE_S, term_grace_s: float = TERM_GRACE_S,
                 guardian_start_delay_s: float = 0,
                 crash_hook: Callable[[str, str, str | None], None] | None = None,
                 publish_hook: Callable[[str, Path], None] | None = None):
        self.root = Path(state_root).expanduser().resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.tick_s, self.start_grace_s, self.term_grace_s = tick_s, start_grace_s, term_grace_s
        self.guardian_start_delay_s = guardian_start_delay_s
        self.crash_hook, self.publish_hook = crash_hook, publish_hook
        self.stopping = threading.Event()
        self.changed = threading.Condition()
        self._submit_lock = threading.Lock()
        self._busy_lock = threading.Lock()
        self._busy: set[str] = set()
        self._launches: dict[str, Launch] = {}
        self._children: dict[str, subprocess.Popen] = {}
        self._starting_deadlines: dict[str, float] = {}
        self._connections: set[socket.socket] = set()
        self._connection_lock = threading.Lock()
        self._closed = False
        self._socket: socket.socket | None = None
        self._lock_fd = os.open(self.root / "daemon.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._lock_fd)
            raise DaemonUnavailable("another daemon holds daemon.lock") from None
        ident = {"pid": os.getpid(), "boot_id": procs.boot_id(),
                 "proc_start": procs.proc_start(os.getpid()), "version": __version__}
        os.ftruncate(self._lock_fd, 0)
        os.write(self._lock_fd, json_bytes(ident))
        os.fsync(self._lock_fd)
        self.log = logging.getLogger(f"subfleet.daemon.{id(self)}")
        log_fd = os.open(self.root / "daemon.log", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        self._log_handler = logging.StreamHandler(os.fdopen(log_fd, "a"))
        self.log.addHandler(self._log_handler)
        self.log.setLevel(logging.INFO)
        for directory in ("jobs", "lanes", "worktrees"):
            (self.root / directory).mkdir(mode=0o700, exist_ok=True)
        policy_path = self.root / "policy.json"
        if not policy_path.exists():
            atomic_publish(policy_path, Path(__file__).with_name("default_policy.json").read_bytes())
        self.policy = load_policy(policy_path)
        self.policy_digest = policy_hash(policy_path)
        self.store = Store(self.root / "state.sqlite3")
        self._seed_lanes()
        self.workers = ThreadPoolExecutor(max_workers=12, thread_name_prefix="subfleet-io")
        self.requests = ThreadPoolExecutor(max_workers=16, thread_name_prefix="subfleet-api")
        self.readers = ThreadPoolExecutor(max_workers=32, thread_name_prefix="subfleet-socket")
        self.waiters = ThreadPoolExecutor(max_workers=16, thread_name_prefix="subfleet-wait")
        self._control_thread: threading.Thread | None = None

    def _seed_lanes(self) -> None:
        path = self.root / "lanes.json"
        if not path.exists():
            atomic_publish(path, b"[]\n")
        roster = json.loads(path.read_bytes())
        if isinstance(roster, dict):
            roster = roster.get("lanes", [])
        for row in roster:
            if self.store.get_lane(row["lane_id"]):
                continue
            credential = row.get("credential") or {
                "provider": row["provider"], "ref": row["credential_ref"],
                "kind": row["credential_kind"], "epoch": row.get("credential_epoch", 1),
            }
            self.store.put_lane(Lane(
                row["lane_id"], row["provider"], row["account_key"], Credential(**credential),
                row.get("home"), LaneOwner(row.get("owner", "v2")),
                bool(row.get("desktop", False)), bool(row.get("enabled", True)),
            ))

    def _boundary(self, name: str, job_id: str, attempt_id: str | None = None) -> None:
        if self.crash_hook:
            self.crash_hook(name, job_id, attempt_id)

    def _publish(self, role: str, path: Path, contents: bytes) -> None:
        if self.publish_hook:
            self.publish_hook(role, path)
        atomic_publish(path, contents)

    def _notify(self) -> None:
        with self.changed:
            self.changed.notify_all()

    def _job(self, job_id: str) -> dict:
        row = self.store.get_job(job_id)
        if row is None:
            raise protocol.ProtocolError(f"unknown job {job_id}")
        return row

    def _spec(self, job: dict, **overrides: Any) -> JobSpec:
        names = {f.name for f in dataclasses.fields(JobSpec)}
        values = {k: v for k, v in job.items() if k in names}
        values.update(sandbox=Sandbox(job["sandbox"]), exclusions=tuple(json.loads(job["exclusions"])))
        values.update(overrides)
        return JobSpec(**values)

    def _pick(self, job: dict, *, extra_exclusions: tuple[str, ...] = ()):
        live = self.store.query("SELECT lane_id, count(*) AS n FROM attempts WHERE state IN "
                                "('reserved','starting','running','finalizing') GROUP BY lane_id")
        return pick(self.policy, self.store.list_lanes(), pinned_model=job.get("pinned_model"),
                    pinned_lane=job.get("pinned_lane"), task=job.get("task"), tier=job.get("tier"),
                    exclusions=tuple(json.loads(job.get("exclusions", "[]"))) + extra_exclusions,
                    allow_desktop=bool(job.get("allow_desktop")),
                    closures=self.store.query("SELECT * FROM closures WHERE released_at IS NULL"),
                    readings=self.store.query("SELECT * FROM readings"),
                    in_flight={r["lane_id"]: r["n"] for r in live}, policy_digest=self.policy_digest)

    def submit(self, args: protocol.SubmitArgs) -> dict:
        # Called on a filesystem worker, never on the socket reader pool.
        with self._submit_lock:
            try:
                ids.request_id(args.request_id)
                sandbox = Sandbox(args.sandbox)
                workdir = Path(args.workdir).expanduser().resolve(strict=True)
                if not workdir.is_dir():
                    raise ValueError("workdir must be a directory")
                if any(workdir == p or p in workdir.parents for p in (Path("/tmp"), Path("/private/tmp"))) and not args.allow_tmp:
                    raise AdapterError("workdir is under /tmp", fix="pass --allow-tmp or use a durable workdir")
                prompt = Path(args.prompt_path).expanduser().read_bytes()
                out = str(Path(args.out_path).expanduser().resolve()) if args.out_path else None
                if out and not Path(out).parent.is_dir():
                    raise ValueError("output directory must exist")
                if sandbox == Sandbox.WORKSPACE_WRITE:
                    validate_writable_workdir(workdir)
                head = git_head(workdir)
                if sandbox == Sandbox.WORKSPACE_WRITE and head is None:
                    raise AdapterError("writable jobs require a committed git repository", fix="initialize a feature branch and commit a baseline")
                model = args.pinned_model
                if model:
                    model = self.policy["retired"].get(model, model)
                    if model not in self.policy["models"]:
                        matches = [k for k, v in self.policy["models"].items() if v["id"] == model]
                        if not matches:
                            raise ValueError(f"unknown model {model}")
                        model = matches[0]
                if args.task and args.task not in self.policy["chains"]:
                    raise ValueError(f"unknown task {args.task}")
                if args.tier and args.tier not in self.policy["tiers"]:
                    raise ValueError(f"unknown tier {args.tier}")
                if not model and not args.task:
                    raise ValueError("submit requires pinned_model or task")
                lane = self.store.get_lane(args.pinned_lane) if args.pinned_lane else None
                if args.pinned_lane and not lane:
                    raise ValueError(f"unknown lane {args.pinned_lane}")
                task_model = model or self.policy["chains"][args.task][self.policy["tiers"].index(args.tier or "standard")]
                provider = self.policy["models"][task_model]["provider"]
                if lane and lane.provider != provider:
                    raise ValueError("pinned lane and model providers disagree")
                if provider == "claude" and HEADLESS_MARKER.encode() not in prompt.splitlines():
                    prompt = HEADLESS_PREAMBLE.encode() + prompt
                if sandbox == Sandbox.WORKSPACE_WRITE and not args.no_preamble:
                    prompt = WRITE_PREAMBLE.encode() + prompt
                caps = self.policy["caps"]
                max_attempts = args.max_attempts if args.max_attempts is not None else caps["max_attempts"]
                max_wall_s = args.max_wall_s if args.max_wall_s is not None else caps["max_wall_s"]
                if not isinstance(max_attempts, int) or not 1 <= max_attempts <= caps["max_attempts"]:
                    raise ValueError("max_attempts must be positive and within policy caps")
                if not isinstance(max_wall_s, (int, float)) or not 0 < max_wall_s <= caps["max_wall_s"]:
                    raise ValueError("max_wall_s must be positive and within policy caps")
                digest = ids.payload_digest(prompt, workdir=str(workdir), workdir_head=head,
                    task=args.task, tier=args.tier, pinned_model=model, pinned_lane=args.pinned_lane,
                    sandbox=sandbox.value, exclusions=args.exclusions, out_path=out,
                    allow_desktop=args.allow_desktop, policy_hash=self.policy_digest)
            except (OSError, ValueError, TypeError) as exc:
                raise protocol.ProtocolError(str(exc)) from exc
            existing = self.store.one("SELECT * FROM jobs WHERE request_id=?", (args.request_id,))
            if existing:
                if existing["payload_digest"] != digest:
                    raise protocol.ProtocolError("request id already used with a different payload")
                return {"job_id": existing["job_id"], "request_id": args.request_id, "created": False}
            job_id = ids.job_id(args.name or args.task or model,
                                existing=[r["job_id"] for r in self.store.query("SELECT job_id FROM jobs")])
            jobdir = self.root / "jobs" / job_id
            values = dataclasses.asdict(args)
            for k in ("allow_tmp", "no_preamble", "dry_run"):
                values.pop(k)
            values.update(job_id=job_id, state="queued", payload_digest=digest,
                          workdir=str(workdir), workdir_head=head, out_path=out,
                          pinned_model=model, prompt_path=str(jobdir / "prompt.md"),
                          exclusions=json.dumps(sorted(args.exclusions)), policy_hash=self.policy_digest,
                          max_attempts=max_attempts, max_wall_s=max_wall_s, created_at=utcnow())
            if args.dry_run:
                return {"dry_run": True, "decision": dataclasses.asdict(self._pick(values))}
            self._validate_conflicts(values)
            jobdir.mkdir(mode=0o700)
            self._publish("prompt", jobdir / "prompt.md", prompt)
            self._publish("manifest", jobdir / "manifest.json", json_bytes({"job": values}))
            with self.store.transaction("job.submitted", job_id=job_id) as tx:
                # Recheck the parent in the same transaction as insertion so a
                # concurrent parent cancellation cannot leave an uncancelled child.
                self._validate_conflicts(values)
                columns = ",".join(values)
                tx.execute(f"INSERT INTO jobs ({columns}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
            self._notify()
            return {"job_id": job_id, "request_id": args.request_id, "created": True}

    def _validate_conflicts(self, job: dict) -> None:
        if job.get("parent_job_id"):
            parent = self._job(job["parent_job_id"])
            if parent["cancel_requested_at"] and not job.get("independent"):
                raise AdapterError("parent is cancelled", fix="submit an independent job")
            count = self.store.one("SELECT count(*) AS n FROM jobs WHERE parent_job_id=?", (parent["job_id"],))["n"]
            if count >= self.policy["caps"]["max_child_jobs"]:
                raise AdapterError("parent child budget exhausted", fix="use a new parent job")
        conflicts: list[tuple[str, Any, str]] = []
        if job.get("out_path"):
            conflicts.append(("out_path", job["out_path"], "use a different -o path or wait for its owner"))
            lease = self.store.one("SELECT * FROM leases WHERE lease_key=?", (f"out:{job['out_path']}",))
            if lease:
                raise AdapterError("output path is held by another job", fix="use a different -o path or resolve quarantine")
        if job["sandbox"] == "workspace-write":
            if job.get("in_place"):
                conflicts.append(("workdir", job["workdir"], "wait for the current writer or choose another worktree"))
                if self.store.one("SELECT * FROM leases WHERE lease_key=?", (f"worktree:{job['workdir']}",)):
                    raise AdapterError("worktree has a lease", fix="resolve its owner before reusing the workspace")
            if job.get("caller_session"):
                conflicts.append(("caller_session", job["caller_session"], "wait for this session's writable job"))
        for column, value, fix in conflicts:
            writable = " AND sandbox='workspace-write'" if column != "out_path" else ""
            if self.store.one(f"SELECT job_id FROM jobs WHERE {column}=? AND state NOT IN "
                              f"('succeeded','failed','cancelled','lost'){writable}", (value,)):
                raise AdapterError(f"{column} is held by a live job", fix=fix)

    def dispatch(self, op: str, args: dict) -> dict:
        if op == "submit":
            return self.submit(protocol.coerce_args(protocol.SubmitArgs, args))
        if op == "list":
            a = protocol.coerce_args(protocol.ListArgs, args)
            sql, params = "SELECT * FROM jobs WHERE 1", []
            if a.mine is not None:
                sql += " AND caller_session=?"; params.append(a.mine)
            if a.running:
                sql += " AND state NOT IN ('succeeded','failed','cancelled','lost')"
            sql += " ORDER BY created_at DESC, rowid DESC"
            if a.last is not None:
                if not isinstance(a.last, int) or a.last < 0:
                    raise protocol.ProtocolError("last must be a nonnegative integer")
                sql += " LIMIT ?"; params.append(a.last)
            return {"jobs": self.store.query(sql, params)}
        if op == "show":
            a = protocol.coerce_args(protocol.ShowArgs, args)
            job = self._job(a.job_id)
            with self.store.transaction("notice.acknowledged", job_id=a.job_id) as tx:
                tx.execute("UPDATE notices SET state='acknowledged',acknowledged_at=? WHERE job_id=? "
                           "AND state!='acknowledged'", (utcnow(), a.job_id))
            return {"job": job, "attempts": self.store.query("SELECT * FROM attempts WHERE job_id=? ORDER BY seq", (a.job_id,)),
                    "artifacts": self.store.query("SELECT artifacts.* FROM artifacts JOIN attempts USING(attempt_id) WHERE job_id=?", (a.job_id,)),
                    "notices": self.store.query("SELECT * FROM notices WHERE job_id=?", (a.job_id,))}
        if op == "wait":
            return self.wait(protocol.coerce_args(protocol.WaitArgs, args))
        if op == "kill":
            return self.kill(protocol.coerce_args(protocol.KillArgs, args))
        if op == "lanes":
            return {"lanes": self.store.query("SELECT * FROM lanes ORDER BY lane_id"),
                    "leases": self.store.query("SELECT * FROM leases WHERE lease_key LIKE 'lane:%'")}
        if op == "readings":
            return {"readings": self.store.query("SELECT * FROM readings ORDER BY observed_at DESC"),
                    "closures": self.store.query("SELECT * FROM closures WHERE released_at IS NULL AND until_at>?", (utcnow(),))}
        if op == "why":
            a = protocol.coerce_args(protocol.WhyArgs, args)
            if a.job_id:
                self._job(a.job_id)
                row = self.store.one("SELECT decision_json FROM decisions WHERE job_id=? ORDER BY decision_id DESC LIMIT 1", (a.job_id,))
                return {"decision": json.loads(row["decision_json"]) if row else None}
            return {"decision": dataclasses.asdict(self._pick({**dataclasses.asdict(a), "exclusions": json.dumps(a.exclusions)}))}
        if op.startswith("notice."):
            a = protocol.coerce_args(protocol.NoticeArgs, args)
            if op == "notice.ack":
                with self.store.transaction("notice.acknowledged") as tx:
                    for notice_id in a.notice_ids:
                        tx.execute("UPDATE notices SET state='acknowledged',acknowledged_at=? WHERE notice_id=? AND session_id=? AND state!='acknowledged'", (utcnow(), notice_id, a.session_id))
            return {"notices": self.store.query("SELECT * FROM notices WHERE session_id=? AND state IN ('pending','offered') ORDER BY notice_id", (a.session_id,))}
        if op == "ping":
            return {"pong": True, "version": __version__, "session_id": args.get("session_id"), "text": args.get("text", "")}
        if op == "daemon.status":
            return {"pid": os.getpid(), "version": __version__, "state_root": str(self.root),
                    "active_attempts": self.store.one("SELECT count(*) n FROM attempts WHERE state IN ('reserved','starting','running','finalizing')")["n"]}
        raise protocol.ProtocolError(f"unknown op {op}")

    def wait(self, args: protocol.WaitArgs) -> dict:
        try:
            deadline = time.monotonic() + max(0, min(float(args.deadline_s), WAIT_POLL_MAX_S))
        except (TypeError, ValueError):
            raise protocol.ProtocolError("deadline_s must be a number") from None
        job_ids = args.job_ids
        if not job_ids:
            job_ids = [j["job_id"] for j in self.dispatch("list", {"mine": args.mine, "last": 1 if args.last else None})["jobs"]]
        while True:
            jobs = [self._job(j) for j in job_ids]
            pending_exports = any(self.store.one("SELECT 1 FROM leases WHERE holder=? AND lease_key LIKE 'out:%'", (j["job_id"],)) for j in jobs if j["state"] == "succeeded")
            if all(j["state"] in TERMINAL for j in jobs) and not pending_exports:
                return {"jobs": jobs, "timeout": False}
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self.stopping.is_set():
                return {"timeout": True}
            with self.changed:
                self.changed.wait(min(remaining, .25))

    def kill(self, args: protocol.KillArgs) -> dict:
        job = self._job(args.job_id)
        quarantine = self.store.one("SELECT * FROM attempts WHERE job_id=? AND state='quarantined' ORDER BY seq DESC LIMIT 1", (args.job_id,))
        if args.confirm_dead or args.force_release:
            if not quarantine:
                return {"job_id": args.job_id, "status": "already finished" if job["state"] in TERMINAL else "not quarantined"}
            self._schedule("resolve:" + args.job_id, self._resolve_quarantine, quarantine, args)
            return {"job_id": args.job_id, "status": "resolution requested"}
        with self.store.transaction("job.cancel_requested", job_id=args.job_id) as tx:
            job = self._job(args.job_id)
            if job["state"] in TERMINAL:
                return {"job_id": args.job_id, "status": "already finished"}
            rows = tx.execute("WITH RECURSIVE family(job_id) AS (SELECT ? UNION ALL SELECT j.job_id FROM jobs j JOIN family f ON j.parent_job_id=f.job_id WHERE j.independent=0) SELECT j.* FROM jobs j JOIN family f USING(job_id)", (args.job_id,)).fetchall()
            for raw in rows:
                row = dict(raw)
                if row["state"] in TERMINAL:
                    continue
                tx.execute("UPDATE jobs SET cancel_requested_at=COALESCE(cancel_requested_at,?) WHERE job_id=?", (utcnow(), row["job_id"]))
                active = tx.execute("SELECT 1 FROM attempts WHERE job_id=? AND state IN ('reserved','starting','running','finalizing','quarantined')", (row["job_id"],)).fetchone()
                if not active:
                    tx.execute("UPDATE jobs SET state='cancelled',rc=130,finished_at=?,wait_reason=NULL,next_check_at=NULL WHERE job_id=?", (utcnow(), row["job_id"]))
                    tx.execute("DELETE FROM leases WHERE holder=?", (row["job_id"],))
                    self._notice(tx, row, "unknown", 130, None, "cancelled before launch")
        self._notify()
        return {"job_id": args.job_id, "status": "cancel requested"}

    @staticmethod
    def _notice(tx, job: dict, cls: str, rc: int | None, deliverable: str | None, summary: str) -> None:
        if tx.execute("SELECT 1 FROM notices WHERE job_id=?", (job["job_id"],)).fetchone():
            return
        text = f"{job['job_id']}: {cls}; rc={rc}; deliverable={deliverable or '-'}; out={job.get('out_path') or '-'}\n{summary}"
        tx.execute("INSERT INTO notices(job_id,session_id,text,state,created_at) VALUES(?,?,?,'pending',?)", (job["job_id"], job.get("caller_session"), text, utcnow()))

    def _schedule(self, key: str, fn: Callable, *args) -> None:
        with self._busy_lock:
            if key in self._busy or self.stopping.is_set():
                return
            self._busy.add(key)
        future = self.workers.submit(fn, *args)
        def done(f):
            try:
                f.result()
            except Exception as exc:
                # Provider/keychain errors can contain secrets; log the error
                # type only. Safe details belong in structured outcome rows.
                self.log.error("worker %s failed: %s", key, type(exc).__name__)
            finally:
                with self._busy_lock:
                    self._busy.discard(key)
                self._notify()
        future.add_done_callback(done)

    def _respond(self, conn: socket.socket, write_lock: threading.Lock, req: protocol.Request) -> None:
        try:
            response = protocol.ok(req.id, self.dispatch(req.op, req.args))
        except (protocol.ProtocolError, AdapterError) as exc:
            response = protocol.fail(req.id, exc.code, str(exc), exc.fix)
        except (ValueError, TypeError, KeyError) as exc:
            response = protocol.fail(req.id, 2, f"invalid arguments: {exc}")
        except Exception as exc:
            self.log.error("request %s failed: %s", req.op, type(exc).__name__)
            response = protocol.fail(req.id, 1, "operation failed; inspect daemon status")
        try:
            with write_lock:
                conn.sendall(protocol.encode(response))
        except OSError:
            pass  # A client disconnect cannot cancel its durable job.

    def _connection(self, conn: socket.socket) -> None:
        write_lock = threading.Lock()
        pending = []
        try:
            with conn.makefile("rb") as reader:
                while not self.stopping.is_set():
                    line = reader.readline(1024 * 1024 + 1)
                    if not line:
                        break
                    try:
                        if len(line) > 1024 * 1024:
                            raise protocol.ProtocolError("request exceeds 1 MiB")
                        req = protocol.decode_request(line)
                    except (protocol.ProtocolError, UnicodeDecodeError) as exc:
                        with write_lock:
                            conn.sendall(protocol.encode(protocol.fail("", 2, str(exc))))
                        continue
                    # Submission filesystem work and long polls have separate
                    # pools; ordinary read/cancel operations stay responsive.
                    pool = self.workers if req.op == "submit" else self.waiters if req.op == "wait" else self.requests
                    pending = [f for f in pending if not f.done()]
                    pending.append(pool.submit(self._respond, conn, write_lock, req))
        except OSError:
            pass
        finally:
            # No process waits here. Running callbacks own their response socket
            # until they finish, including after the caller closes its write half.
            def finish(_=None):
                if all(f.done() for f in pending):
                    conn.close()
                    with self._connection_lock:
                        self._connections.discard(conn)
            for f in pending:
                f.add_done_callback(finish)
            finish()

    def serve_forever(self) -> None:
        sock_path = self.root / "daemon.sock"
        sock_path.unlink(missing_ok=True)  # Protected by the lifetime flock.
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        self._socket.listen(64)
        self._socket.settimeout(.2)
        self._control_thread = threading.Thread(target=self._control, name="subfleet-control", daemon=True)
        self._control_thread.start()
        try:
            while not self.stopping.is_set():
                try:
                    conn, _ = self._socket.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.stopping.is_set():
                        break
                    raise
                with self._connection_lock:
                    self._connections.add(conn)
                self.readers.submit(self._connection, conn)
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stopping.set()
        self._notify()
        if self._socket:
            self._socket.close()
        if self._control_thread and threading.current_thread() != self._control_thread:
            self._control_thread.join(timeout=2)
        with self._connection_lock:
            for conn in self._connections:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        for pool in (self.readers, self.requests, self.waiters, self.workers):
            pool.shutdown(wait=True, cancel_futures=True)
        self.store.close()
        (self.root / "daemon.sock").unlink(missing_ok=True)
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        os.close(self._lock_fd)
        self.log.removeHandler(self._log_handler)
        self._log_handler.stream.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="subfleet supervised daemon")
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--state-root", default=os.environ.get("SUBFLEET_HOME", "~/.subfleet"))
    args = parser.parse_args(argv)
    try:
        daemon = Daemon(args.state_root)
    except DaemonUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 69
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: daemon.stopping.set())
    daemon.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
